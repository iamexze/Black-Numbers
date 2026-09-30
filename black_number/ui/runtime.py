"""The composition root — assembling a working assistant, once.

Everything the assistant needs is built here and handed back as one object: the
config, the transcript log, speech in and out, the safety gate, memory, the skill
registry, the brain, the scheduler, the skill context and the agent.

This exists because there is now more than one front end. The CLI and the local
web console must assemble the assistant *identically* — same gate, same registry,
same risk policy — and the way to guarantee that is to have one function that does
it rather than two that look alike. A front end supplies only what is genuinely
specific to it: how to ask the user a yes/no question, and what to do when a
scheduled job fires.

The one thing a front end must get right is `ask`. It is the entire safety
boundary: `Gate` calls it before any state-changing action and treats its return
value as the decision. A front end that cannot ask a question must return False.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..agent.loop import Agent
from ..agent.memory import Memory
from ..brain.router import build_brain
from ..core.log import open_log
from ..core.scheduler import Job, Scheduler
from ..safety.gate import Gate
from ..skills import (
    clipboard_notes,
    dev_ops,
    files_shell,
    mac_personal,
    media_mac,
    meta,
    network,
    protocols,
    security,
    self_upgrade,
    system_mac,
    time_focus,
    web_research,
    world,
)
from ..skills.base import Context
from ..skills.registry import Registry
from ..speech.factory import build_stt, build_tts

# Every skill family. Adding a capability to the assistant is adding a line here
# and nothing else — no core file changes.
SKILL_MODULES = (
    system_mac, files_shell, web_research, security, meta,
    time_focus, clipboard_notes, network, media_mac, dev_ops,
    world, mac_personal, protocols, self_upgrade,
)


def build_registry() -> Registry:
    reg = Registry()
    for module in SKILL_MODULES:
        reg.add_all(module.skills())
    return reg


@dataclass
class Runtime:
    cfg: Any
    log: Any
    tts: Any
    speak: Callable[[str], None]
    gate: Gate
    memory: Memory
    registry: Registry
    brain: Any
    scheduler: Scheduler
    ctx: Context
    agent: Agent
    stt: Any = None

    # ── reporting ───────────────────────────────────────────────────────────
    def boot_checks(self) -> list[tuple[str, bool | None, str]]:
        """What is live, what fell back, what is missing — stated, not implied.

        True means running as configured, None means running on a fallback, False
        means unavailable. The distinction is the point: this assistant is designed
        to run with pieces missing, so the report says which pieces those are.
        """
        wanted = self.cfg.brain
        brain_ok: bool | None = True if self.brain.name == wanted else None
        brain_note = self.brain.name if brain_ok else f"{self.brain.name} (wanted {wanted})"
        rows: list[tuple[str, bool | None, str]] = [
            ("reasoning", brain_ok, brain_note),
            ("skills", True, f"{len(self.registry.all())} registered"),
            ("protocols", True, f"{len(protocols.BUILTIN)} built in"),
            ("voice out", True if self.tts.name != "none" else None,
             self.tts.name if self.tts.name != "none" else "silent (no `say`)"),
        ]
        if self.stt is not None:
            rows.append(("voice in", True if self.stt.name != "text" else None,
                         self.stt.name if self.stt.name != "text" else "typed input"))
        rows += [
            ("scheduler", True, "running"),
            ("safety gate", self.cfg.confirm != "never", f"policy: {self.cfg.confirm}"),
            ("memory", True, f"{len(self.memory.facts)} remembered"),
        ]
        return rows

    def shutdown(self, drain: bool = True) -> None:
        self.scheduler.stop()
        if drain:
            proc = getattr(self.tts, "_proc", None)
            if proc is not None:
                try:
                    proc.wait(timeout=15)
                except Exception:
                    pass
        self.log.close()


def default_due_sink(log, speak) -> Callable[[Job, float], None]:
    """Announce a fired job. Lateness is surfaced rather than hidden: a reminder
    that waited through a restart says so instead of pretending to be on time."""

    def on_due(job: Job, late: float) -> None:
        prefix = {"reminder": "Reminder", "timer": "Timer", "focus": "Focus"}.get(job.kind, "")
        line = f"{prefix}: {job.label}" if prefix else job.label
        if late > 90:
            line += f"  (this was due {int(late // 60)} minutes ago)"
        log.bn(line)
        speak(line)

    return on_due


def build(
    cfg,
    ask: Callable[[str], bool],
    *,
    log=None,
    speak_hook: Callable[[str], None] | None = None,
    on_due: Callable[[Job, float], None] | None = None,
    want_stt: bool = True,
) -> Runtime:
    """Assemble a complete assistant.

    `ask` is the safety boundary and is required — there is no default, because a
    front end silently defaulting to "yes" is exactly the bug this design exists
    to prevent.

    `speak_hook` observes everything spoken, for a front end that needs to display
    it as well as voice it. It cannot suppress speech and its failures are
    swallowed, so a broken display never costs the user the spoken reply.
    """
    log = log or open_log(cfg.log_dir)

    tts = build_tts(cfg, log)

    def speak(text: str) -> None:
        if speak_hook is not None:
            try:
                speak_hook(text)
            except Exception:
                pass          # a display problem must not swallow the voice
        tts.speak(text)

    gate = Gate(cfg.confirm, ask, log, speak=speak)
    memory = Memory(cfg.memory_dir)
    registry = build_registry()
    brain = build_brain(cfg, log)

    scheduler = Scheduler(
        cfg.state_dir / "timers.json",
        on_due or default_due_sink(log, speak),
        log,
    )
    scheduler.start()

    ctx = Context(cfg=cfg, log=log, gate=gate, memory=memory, speak=speak,
                  registry=registry, scheduler=scheduler)
    agent = Agent(brain, registry, ctx)
    stt = build_stt(cfg, log) if want_stt else None

    return Runtime(cfg=cfg, log=log, tts=tts, speak=speak, gate=gate, memory=memory,
                   registry=registry, brain=brain, scheduler=scheduler, ctx=ctx,
                   agent=agent, stt=stt)
