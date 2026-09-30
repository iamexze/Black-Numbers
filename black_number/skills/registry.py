"""The skill registry — discovery and dispatch.

Skills register here at startup. The registry hands the brain the tool schemas
and executes tool calls by name, applying the safety gate before anything that
changes state runs. New capability = register a Skill; nothing else changes.
"""

from __future__ import annotations

from typing import Any

from ..safety.gate import Gate
from .base import Context, Result, Risk, Skill


class Registry:
    def __init__(self):
        self._skills: dict[str, Skill] = {}

    def add(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def add_all(self, skills: list[Skill]) -> None:
        for s in skills:
            self.add(s)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [s.tool_schema() for s in self._skills.values()]

    def execute(self, name: str, args: dict[str, Any], ctx: Context) -> Result:
        skill = self._skills.get(name)
        if skill is None:
            return Result.fail(f"I don't have a skill called {name}.")

        # The one checkpoint: risky actions pass the gate before running.
        gate: Gate = ctx.gate
        if skill.risk >= Risk.REVERSIBLE:
            human = _describe_call(skill, args)
            if not gate.confirm(human, skill.risk, skill.caution):
                return Result.fail("Okay, I won't do that.", detail="cancelled by user")

        ctx.log.event("skill_run", name=name, args=args, risk=skill.risk.name)
        try:
            result = skill.run(args, ctx)
        except Exception as e:  # a broken skill must not crash the assistant
            ctx.log.event("skill_error", name=name, error=str(e))
            return Result.fail(f"That failed: {e}")
        ctx.log.event("skill_result", name=name, ok=result.ok, speech=result.speech)
        return result


def _describe_call(skill: Skill, args: dict[str, Any]) -> str:
    """A short, human sentence for the confirmation prompt."""
    if not args:
        return skill.name.replace("_", " ")
    shown = ", ".join(f"{k}={v}" for k, v in args.items() if v not in (None, ""))
    return f"{skill.name.replace('_', ' ')} ({shown})"
