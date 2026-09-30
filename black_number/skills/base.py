"""The skill contract — the unit the whole assistant is built from.

A skill is one capability: adjusting volume, searching the web, running an
audited command. Every skill is the same shape, so the agent can reason about
all of them uniformly and new ones can be added without touching the core.

A skill declares:
  - `name` and `description`: what the brain sees when choosing a tool
  - `parameters`: a JSON-schema-ish dict of arguments
  - `risk`: how much trust running it requires (see safety.policy)
  - `run(args, ctx)`: the actual work, returning a Result

Skills never speak or print directly. They return a Result and let the agent
decide how to present it. That keeps a skill usable from voice, from text, and
from another skill calling it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable


class Risk(IntEnum):
    """How dangerous an action is, which sets whether it needs confirmation.

    READ_ONLY   observes without changing anything (list files, get volume)
    REVERSIBLE  changes state but easily undone (set volume, open an app)
    MUTATING    changes state with real consequences (move files, quit apps)
    DESTRUCTIVE hard or impossible to undo (delete, overwrite, network attack)
    """

    READ_ONLY = 0
    REVERSIBLE = 1
    MUTATING = 2
    DESTRUCTIVE = 3


@dataclass
class Result:
    """What a skill returns. `speech` is what the assistant says out loud;
    `detail` is the fuller text shown on screen; `data` is machine-readable."""

    ok: bool
    speech: str
    detail: str = ""
    data: Any = None

    @classmethod
    def say(cls, speech: str, detail: str = "", data: Any = None) -> "Result":
        return cls(True, speech, detail or speech, data)

    @classmethod
    def fail(cls, speech: str, detail: str = "") -> "Result":
        return cls(False, speech, detail or speech)


@dataclass
class Skill:
    name: str
    description: str
    run: Callable[[dict[str, Any], "Context"], Result]
    parameters: dict[str, Any] = field(default_factory=dict)
    risk: Risk = Risk.READ_ONLY
    # Free-text preconditions for the human-readable confirmation prompt, e.g.
    # "requires an authorized target". Shown before a risky action runs.
    caution: str = ""

    def tool_schema(self) -> dict[str, Any]:
        """Anthropic tool-use shape. The provider adapts this per API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.parameters,
                "required": [k for k, v in self.parameters.items() if v.get("required")],
            },
        }


@dataclass
class Context:
    """Everything a skill is allowed to reach: config, logging, the safety gate,
    memory, and a handle to call sibling skills. Passed to every `run`."""

    cfg: Any
    log: Any
    gate: Any  # safety.gate.Gate
    memory: Any  # agent.memory.Memory
    speak: Callable[[str], None]  # so a long-running skill can narrate progress
    registry: Any = None  # skills.registry.Registry, for skills that compose others
    scheduler: Any = None  # core.scheduler.Scheduler, for anything that waits
