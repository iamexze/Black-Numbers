"""The Anthropic brain — the full-reasoning default.

Uses the Messages API with tool use. The agent loop drives the tool cycle; this
class only translates between our neutral Turn/ToolCall shapes and the SDK. If
the `anthropic` package is not installed or no key is set, `available()` returns
False and the router falls back to the offline brain without error.
"""

from __future__ import annotations

from typing import Any

from .base import Brain, ToolCall, Turn


class AnthropicBrain(Brain):
    name = "anthropic"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self._client = None
        try:
            import anthropic  # noqa: F401

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
        resp = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            tools=tools,
            messages=messages,
        )
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, args=dict(block.input)))
        return Turn(
            text=" ".join(text_parts).strip(),
            tool_calls=calls,
            done=(resp.stop_reason != "tool_use"),
        )
