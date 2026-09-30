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

from ..brain.base import Brain
from ..skills.base import Context, Result
from ..skills.registry import Registry

MAX_TOOL_STEPS = 6

SYSTEM_TEMPLATE = """You are Black Number, a voice-activated personal assistant \
running on the user's own Mac. You are concise, direct and calm — you speak the \
way a competent chief of staff would, not a chatbot. Prefer doing to explaining.

You control the machine through tools. When a request maps to a tool, call it. \
When you need a fact you can look up with a tool, look it up rather than guessing. \
Keep spoken replies short: one or two sentences, because they are read aloud. Put \
any longer detail in text, not speech.

You never pretend to have done something you did not do. If a tool fails or a \
confirmation is declined, say so plainly. You do not give investment, legal or \
medical advice as fact; you attribute it.

{memory}"""


class Agent:
    def __init__(self, brain: Brain, registry: Registry, ctx: Context):
        self.brain = brain
        self.registry = registry
        self.ctx = ctx

    def _system(self) -> str:
        return SYSTEM_TEMPLATE.format(memory=self.ctx.memory.recall_block())

    def handle(self, utterance: str) -> Result:
        """Process one utterance end to end. Returns the final Result."""
        mem = self.ctx.memory
        mem.add("user", utterance)
        tools = self.registry.tool_schemas()

        last: Result = Result.say("")
        for _ in range(MAX_TOOL_STEPS):
            turn = self.brain.think(mem.transcript(), tools, self._system())

            if not turn.tool_calls:
                text = turn.text or "Done."
                mem.add("assistant", text)
                return Result.say(text)

            # Record the assistant's tool-use turn, then run each call.
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
