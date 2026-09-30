"""The CLI runtime — assembles the parts and runs the interaction loop.

This is the composition root: it builds config, logging, memory, the safety
gate, the scheduler, the brain, every skill and the agent, then drives them from
whatever speech source is configured. The same assembly serves typed and voice
input; only the STT source differs.

Nothing here is required for a skill to work, which is the point — the runtime is
replaceable. A menu-bar app or a web front end would build the same objects and
call `agent.handle`.
"""

from __future__ import annotations

import sys

from ..agent.loop import Agent
from ..agent.memory import Memory
from ..brain.router import build_brain
from ..core import config, theme
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

# Every skill family, in the order they are reported at startup. Adding a
# capability to the assistant is adding a line here and nothing else.
SKILL_MODULES = (
    system_mac, files_shell, web_research, security, meta,
    time_focus, clipboard_notes, network, media_mac, dev_ops,
    world, mac_personal, protocols, self_upgrade,
)

# Handled by the runtime rather than sent to the brain.
CONTROL = {"quit", "exit", "goodbye", "shut down", "shutdown"}
SLASH_HELP = {
    "/help": "this list",
    "/skills": "every registered skill, by risk",
    "/protocols": "the named routines available",
    "/status": "configuration and health",
    "/timers": "what is pending",
    "/quit": "shut down",
}


def _ask_yesno(prompt: str) -> bool:
    try:
        ans = input(f"  ? {prompt} — proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes", "yeah", "yep", "do it", "go", "ok", "okay", "sure", "affirmative")


def build_registry() -> Registry:
    reg = Registry()
    for module in SKILL_MODULES:
        reg.add_all(module.skills())
    return reg


def _boot_checks(cfg, brain, registry, stt, tts) -> list[tuple[str, bool | None, str]]:
    """What is live, what fell back, what is missing — stated rather than implied."""
    wanted_brain = cfg.brain
    brain_state: bool | None = True if brain.name == wanted_brain else None
    brain_note = brain.name if brain_state else f"{brain.name} (wanted {wanted_brain}, no key)"
    return [
        ("reasoning", brain_state, brain_note),
        ("skills", True, f"{len(registry.all())} registered"),
        ("protocols", True, f"{len(protocols.BUILTIN)} built in"),
        ("voice out", True if tts.name != "silent" else None,
         tts.name if tts.name != "silent" else "silent (no `say`)"),
        ("voice in", True if stt.name != "text" else None,
         stt.name if stt.name != "text" else "typed input"),
        ("scheduler", True, "running"),
        ("safety gate", cfg.confirm != "never", f"policy: {cfg.confirm}"),
        ("memory", True, "loaded"),
    ]


def _slash(cmd: str, ctx: Context, registry: Registry) -> bool:
    """Runtime commands. Returns False when the user asked to stop."""
    cmd = cmd.strip().lower()
    if cmd in ("/quit", "/exit"):
        return False
    if cmd == "/help":
        print(theme.panel("runtime commands", list(SLASH_HELP.items())))
        print("  Everything else is a request. Try 'what's the weather', "
              "'set a timer for 10 minutes', 'run the sitrep protocol'.")
    elif cmd == "/skills":
        by_risk: dict[str, list[str]] = {}
        for s in registry.all():
            by_risk.setdefault(s.risk.name, []).append(s.name)
        rows = [(level, ", ".join(sorted(by_risk.get(level, []))))
                for level in ("READ_ONLY", "REVERSIBLE", "MUTATING", "DESTRUCTIVE")
                if by_risk.get(level)]
        print(theme.panel(f"{len(registry.all())} skills", rows))
    elif cmd == "/protocols":
        print(registry.execute("protocol_list", {}, ctx).detail)
    elif cmd == "/status":
        print(theme.panel("status", list(ctx.cfg.describe())))
    elif cmd == "/timers":
        print(registry.execute("list_timers", {}, ctx).detail)
    else:
        print(f"  Unknown command {cmd}. Try /help.")
    return True


def run(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cfg = config.load()
    log = open_log(cfg.log_dir)

    tts = build_tts(cfg, log)
    speak = tts.speak
    gate = Gate(cfg.confirm, _ask_yesno, log, speak=speak)
    memory = Memory(cfg.memory_dir)
    registry = build_registry()
    brain = build_brain(cfg, log)

    # The scheduler speaks through the same channels as everything else, so a
    # reminder that fires mid-session is announced and recorded identically to a
    # direct reply. Lateness is surfaced: a reminder that waited through a
    # restart says so rather than pretending to be on time.
    def on_due(job: Job, late: float) -> None:
        prefix = {"reminder": "Reminder", "timer": "Timer", "focus": "Focus"}.get(job.kind, "")
        line = f"{prefix}: {job.label}" if prefix else job.label
        if late > 90:
            line += f"  (this was due {int(late // 60)} minutes ago)"
        print()
        log.bn(line)
        speak(line)

    scheduler = Scheduler(cfg.state_dir / "timers.json", on_due, log)
    scheduler.start()

    ctx = Context(cfg=cfg, log=log, gate=gate, memory=memory, speak=speak,
                  registry=registry, scheduler=scheduler)
    agent = Agent(brain, registry, ctx)
    stt = build_stt(cfg, log)

    if sys.stdout.isatty():
        print(theme.mark("mark ii  ·  local  ·  yours"))
    theme.boot(_boot_checks(cfg, brain, registry, stt, tts))

    pending = scheduler.pending()
    if pending:
        log.system(f"{len(pending)} scheduled {'item' if len(pending) == 1 else 'items'} "
                   f"carried over — next: {pending[0].describe()}")

    who = f", {cfg.address}" if cfg.address else ""
    greeting = f"Black Number online{who}. What do you need?"
    log.bn(greeting)
    speak(greeting)

    exit_code = 0
    try:
        # One-shot mode: `bn "do this"` runs a single request and exits.
        if argv:
            one = " ".join(argv)
            log.you(one)
            result = agent.handle(one)
            log.bn(result.speech)
            if result.detail and result.detail != result.speech:
                print(result.detail)
            speak(result.speech)
            exit_code = 0 if result.ok else 1
        else:
            if sys.stdout.isatty():
                log.system("/help for runtime commands, or just say what you need.")
            for utterance in stt.listen():
                u = utterance.strip()
                if not u:
                    continue
                if u.lower() in CONTROL:
                    break
                if u.startswith("/"):
                    if not _slash(u, ctx, registry):
                        break
                    continue
                if stt.name != "text":
                    log.you(u)      # the text source already echoes via input()
                result = agent.handle(u)
                log.bn(result.speech)
                if result.detail and result.detail.strip() and result.detail != result.speech:
                    print("     " + result.detail.replace("\n", "\n     "))
                speak(result.speech)
    except KeyboardInterrupt:
        print()
    finally:
        scheduler.stop()

    still = scheduler.pending()
    bye = "Shutting down."
    if still:
        bye += f" {len(still)} scheduled {'item' if len(still) == 1 else 'items'} will be waiting."
    log.bn(bye)
    speak(bye)
    _drain(tts)
    log.close()
    return exit_code


def _drain(tts) -> None:
    """Let a final spoken line finish before the process exits."""
    proc = getattr(tts, "_proc", None)
    if proc is not None:
        try:
            proc.wait(timeout=15)
        except Exception:
            pass
