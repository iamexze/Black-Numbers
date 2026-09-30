"""The Anthropic brain — the full-reasoning default.

Uses the Messages API with tool use. The agent loop drives the tool cycle; this
class only translates between our neutral Turn/ToolCall shapes and the SDK. If
the `anthropic` package is not installed or no key is set, `available()` returns
False and the router falls back to the offline brain without error.

Four details here are deliberate and easy to get wrong:

  Adaptive thinking. Claude Opus 5 reasons adaptively; `budget_tokens` is
  rejected outright on this generation. Reasoning depth is set with
  `output_config.effort`, which is exposed as BN_EFFORT so a voice assistant can
  be tuned for latency without editing code.

  Thinking blocks are replayed verbatim. The API's contract is that thinking
  blocks are passed back unchanged when the conversation continues on the same
  model. So `Turn` carries the response's raw content blocks and the agent loop
  appends those rather than rebuilding the turn from text and tool calls, which
  would silently drop them.

  A refusal is not an error. `stop_reason == "refusal"` returns HTTP 200 with
  `stop_details`, and `stop_details` is null for every other stop reason — so it
  is read only after that check.

  The model failing must not end the session. A rate limit or a dropped
  connection returns a Turn that says so plainly and leaves the assistant
  running, because an assistant that exits when the network blips is worse than
  one that says "try again".
"""

from __future__ import annotations

from typing import Any

from .base import Brain, ToolCall, Turn

# Non-streaming requests stay comfortably inside the SDK's HTTP timeout at this
# size, and it is large enough that a tool-use turn is never truncated mid-call.
MAX_TOKENS = 16_000


class AnthropicBrain(Brain):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, effort: str = "high"):
        self.api_key = api_key
        self.model = model
        self.effort = effort
        self._client = None
        try:
            import anthropic

            self._anthropic = anthropic
        except Exception:
            self._anthropic = None

    def available(self) -> bool:
        return bool(self.api_key) and self._anthropic is not None

    def _ensure(self):
        if self._client is None:
            self._client = self._anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def think(self, messages, tools, system) -> Turn:
        client = self._ensure()
        a = self._anthropic
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tools,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
            )
        except a.BadRequestError as e:
            return Turn(text=f"The model rejected that request: {getattr(e, 'message', e)}")
        except a.AuthenticationError:
            return Turn(text="My API key was rejected. Check ANTHROPIC_API_KEY in .env.")
        except a.PermissionDeniedError:
            return Turn(text="My API key isn't permitted to use that model.")
        except a.NotFoundError:
            return Turn(text=f"The model '{self.model}' doesn't exist. Check BN_ANTHROPIC_MODEL.")
        except a.RateLimitError as e:
            wait = getattr(getattr(e, "response", None), "headers", {}) or {}
            after = wait.get("retry-after", "a moment")
            return Turn(text=f"I'm rate limited. Try again in {after} seconds.")
        except a.APIConnectionError:
            return Turn(text="I can't reach the model — the network looks down.")
        except a.APIStatusError as e:
            return Turn(text=f"The model returned an error ({e.status_code}). Try again.")

        # A refusal arrives as a successful response, so check before reading
        # content or stop_details — the latter is null for every other reason.
        if resp.stop_reason == "refusal":
            detail = getattr(resp, "stop_details", None)
            why = getattr(detail, "explanation", "") if detail else ""
            return Turn(text="I won't help with that." + (f" {why}" if why else ""))

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                # dict() over the parsed input — never string-matched, because the
                # escaping of tool-input JSON is not guaranteed stable.
                calls.append(ToolCall(id=block.id, name=block.name, args=dict(block.input)))

        if resp.stop_reason == "max_tokens" and not calls:
            text_parts.append("(I ran out of room mid-answer.)")

        return Turn(
            text=" ".join(p for p in text_parts if p).strip(),
            tool_calls=calls,
            done=(resp.stop_reason != "tool_use"),
            # Kept verbatim so the loop can replay thinking blocks unchanged.
            raw_blocks=list(resp.content),
        )
