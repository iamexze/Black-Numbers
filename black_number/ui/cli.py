"""The terminal front end.

It owns three things and delegates everything else to `ui.runtime`: how a yes/no
question is asked (a line on stdin), how a turn is displayed, and the `/` runtime
commands. The assistant itself — gate, registry, scheduler, brain — is assembled
by the shared composition root, so the terminal and the web console cannot drift
apart on anything that matters.
"""

from __future__ import annotations

import sys

from ..core import config, theme
from ..core.log import open_log
from ..skills.base import Context
from ..skills.registry import Registry
from . import runtime
from .runtime import SKILL_MODULES, build_registry  # re-exported: imported elsewhere

__all__ = ["run", "build_registry", "SKILL_MODULES"]

# Handled by the runtime rather than sent to the brain.
CONTROL = {"quit", "exit", "goodbye", "shut down", "shutdown"}
YES = {"y", "yes", "yeah", "yep", "do it", "go", "ok", "okay", "sure", "affirmative"}
SLASH_HELP = {
    "/help": "this list",
    "/skills": "every registered skill, by risk",
    "/protocols": "the named routines available",
    "/status": "configuration and health",
    "/timers": "what is pending",
    "/serve": "how to start the local web console",
    "/quit": "shut down",
}


def _ask_yesno(prompt: str) -> bool:
    """The safety boundary for the terminal. Anything that is not an explicit yes
    is a no, including end-of-input."""
    try:
        answer = input(f"  ? {prompt} — proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in YES


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
        for skill in registry.all():
            by_risk.setdefault(skill.risk.name, []).append(skill.name)
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
    elif cmd == "/serve":
        print("  python3 -m black_number serve        # http://127.0.0.1:3002")
    else:
        print(f"  Unknown command {cmd}. Try /help.")
    return True


def _show(result, log) -> None:
    log.bn(result.speech)
    if result.detail and result.detail.strip() and result.detail != result.speech:
        print("     " + result.detail.replace("\n", "\n     "))


def run(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cfg = config.load()
    log = open_log(cfg.log_dir)
    rt = runtime.build(cfg, _ask_yesno, log=log)

    if sys.stdout.isatty():
        print(theme.mark("mark ii  ·  local  ·  yours"))
    theme.boot(rt.boot_checks())

    pending = rt.scheduler.pending()
    if pending:
        log.system(f"{len(pending)} scheduled {'item' if len(pending) == 1 else 'items'} "
                   f"carried over — next: {pending[0].describe()}")

    who = f", {cfg.address}" if cfg.address else ""
    greeting = f"Black Number online{who}. What do you need?"
    log.bn(greeting)
    rt.speak(greeting)

    exit_code = 0
    try:
        # One-shot mode: `bn "do this"` runs a single request and exits.
        if argv:
            one = " ".join(argv)
            log.you(one)
            result = rt.agent.handle(one)
            _show(result, log)
            rt.speak(result.speech)
            exit_code = 0 if result.ok else 1
        else:
            if sys.stdout.isatty():
                log.system("/help for runtime commands, or just say what you need.")
            for utterance in rt.stt.listen():
                text = utterance.strip()
                if not text:
                    continue
                if text.lower() in CONTROL:
                    break
                if text.startswith("/"):
                    if not _slash(text, rt.ctx, rt.registry):
                        break
                    continue
                if rt.stt.name != "text":
                    log.you(text)     # the text source already echoes via input()
                result = rt.agent.handle(text)
                _show(result, log)
                rt.speak(result.speech)
    except KeyboardInterrupt:
        print()
    finally:
        rt.scheduler.stop()

    still = rt.scheduler.pending()
    bye = "Shutting down."
    if still:
        bye += f" {len(still)} scheduled {'item' if len(still) == 1 else 'items'} will be waiting."
    log.bn(bye)
    rt.speak(bye)
    rt.shutdown()
    return exit_code
