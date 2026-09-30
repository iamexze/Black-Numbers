"""The agent loop — perceive, think, act, speak.

This is the spine. For each utterance it:
  1. adds it to memory,
  2. asks the brain what to do given the tools,
  3. runs any tool calls through the registry (which enforces safety),
  4. feeds results back to the brain until it has a final answer,
  5. speaks and prints that answer.

The tool cycle is bounded so a confused brain cannot loop forever. Everything is
logged, so the whole run is replayable from the transcript — which is what a
later, self-improving version reads to critique itself.
"""

from __future__ import annotations

import time

from ..brain.base import Brain
from ..skills.base import Context, Result
from ..skills.registry import Registry

# Raised from 6: a protocol step can itself fan out, and a request like
# "check the weather then set a timer" legitimately needs several rounds.
MAX_TOOL_STEPS = 8

SYSTEM_TEMPLATE = """You are Black Number, a voice-activated assistant running \
on the user's own Mac. You are concise, direct and calm — you speak the way a \
competent chief of staff would, not the way a chatbot does. Prefer doing to \
explaining.{address}

It is {now}. Trust that over any date you think you remember.

You control this machine through tools. When a request maps to a tool, call it. \
When a fact is obtainable with a tool, obtain it rather than guessing — in \
particular, use `calculate` for arithmetic and `convert_units` for units instead \
of working them out yourself, because they are exact and you are not.

Related work can be saved as a protocol and run by name; `protocol_list` shows \
what already exists, and `protocol_define` teaches a new one. If the user \
corrects you on how you should behave, record it with `self_lesson` so it \
survives this session.

Keep spoken replies to one or two sentences, because they are read aloud. Put \
longer detail in text, not speech. Never claim to have done something you did \
not do: if a tool fails or a confirmation is declined, say so plainly. You do \
not present investment, legal or medical advice as fact; you attribute it.

{memory}"""


class Agent:
    def __init__(self, brain: Brain, registry: Registry, ctx: Context):
        self.brain = brain
        self.registry = registry
        self.ctx = ctx

    def _system(self) -> str:
        """Build the system prompt for this turn.

        The clock is injected every turn rather than once per session, so a long
        session does not drift into believing it is still morning. Long-term
        memory is appended last, where standing instructions carry most weight.
        """
        address = getattr(self.ctx.cfg, "address", "") or ""
        return SYSTEM_TEMPLATE.format(
            address=f" You address the user as {address}." if address else "",
            now=time.strftime("%A, %-d %B %Y, %-I:%M %p"),
            memory=self.ctx.memory.recall_block(),
        )

    def handle(self, utterance: str) -> Result:
        """Process one utterance end to end. Returns the final Result."""
        mem = self.ctx.memory
        mem.add("user", utterance)
        tools = self.registry.tool_schemas()

        last: Result = Result.say("")
        for _ in range(MAX_TOOL_STEPS):
            # A provider is a network dependency. Its failures are handled inside
            # each brain, but an unforeseen one must still not take down a running
            # assistant, so the call is wrapped here as the final backstop.
            try:
                turn = self.brain.think(mem.transcript(), tools, self._system())
            except Exception as e:
                self.ctx.log.event("brain_error", brain=self.brain.name, error=str(e))
                return Result.fail(f"My reasoning backend failed: {e}")

            if not turn.tool_calls:
                text = turn.text or "Done."
                mem.add("assistant", text)
                return Result.say(text)

            # Record the assistant's tool-use turn, then run each call.
            # Prefer the provider's own blocks: rebuilding the turn from text and
            # tool calls would drop anything else it sent, and a reasoning block
            # dropped here is a block the model expected to see replayed.
            if turn.raw_blocks is not None:
                assistant_blocks: list = turn.raw_blocks
            else:
                assistant_blocks = []
                if turn.text:
                    assistant_blocks.append({"type": "text", "text": turn.text})
                for call in turn.tool_calls:
                    assistant_blocks.append(
                        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.args}
                    )
            mem.add("assistant", assistant_blocks)

            result_blocks = []
            for call in turn.tool_calls:
                if turn.text:
                    self.ctx.speak(turn.text)  # narrate intent before a slow tool
                last = self.registry.execute(call.name, call.args, self.ctx)
                result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": last.detail or last.speech,
                        "is_error": not last.ok,
                    }
                )
            mem.add("user", result_blocks)

            # The offline brain returns done=False with a single call and no
            # follow-up reasoning; surface its result directly.
            if self.brain.name == "offline":
                return last

        return last or Result.say("I've stopped after several steps to avoid looping.")
