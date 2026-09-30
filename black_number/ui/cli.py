"""The CLI runtime — assembles the parts and runs the interaction loop.

This is the composition root: it builds config, logging, memory, the safety
gate, the brain, every skill, and the agent, then drives them from whatever
speech source is configured. The same assembly serves typed and voice input;
only the STT source differs.
"""

from __future__ import annotations

import sys

from ..agent.loop import Agent
from ..agent.memory import Memory
from ..brain.router import build_brain
from ..core import config
from ..core.log import open_log
from ..safety.gate import Gate
from ..skills import files_shell, meta, security, system_mac, web_research
from ..skills.base import Context
from ..skills.registry import Registry
from ..speech.factory import build_stt, build_tts

BANNER = r"""
  ██████╗ ██╗      █████╗  ██████╗██╗  ██╗    ███╗   ██╗██╗   ██╗███╗   ███╗
  ██╔══██╗██║     ██╔══██╗██╔════╝██║ ██╔╝    ████╗  ██║██║   ██║████╗ ████║
  ██████╔╝██║     ███████║██║     █████╔╝     ██╔██╗ ██║██║   ██║██╔████╔██║
  ██╔══██╗██║     ██╔══██║██║     ██╔═██╗     ██║╚██╗██║██║   ██║██║╚██╔╝██║
  ██████╔╝███████╗██║  ██║╚██████╗██║  ██╗    ██║ ╚████║╚██████╔╝██║ ╚═╝ ██║
  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝    ╚═╝  ╚═══╝ ╚═════╝ ╚═╝     ╚═╝
"""

# Meta-commands handled by the runtime itself, not sent to the brain.
CONTROL = {"quit", "exit", "stop", "goodbye", "shut down"}


def _ask_yesno(prompt: str) -> bool:
    try:
        ans = input(f"  ? {prompt} — proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes", "yeah", "yep", "do it", "go", "ok")


def build_registry() -> Registry:
    reg = Registry()
    reg.add_all(system_mac.skills())
    reg.add_all(files_shell.skills())
    reg.add_all(web_research.skills())
    reg.add_all(security.skills())
    reg.add_all(meta.skills())
    return reg


def run(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cfg = config.load()
    log = open_log(cfg.log_dir)

    if sys.stdout.isatty():
        print("\033[38;5;179m" + BANNER + "\033[0m")

    tts = build_tts(cfg, log)
    speak = tts.speak

    gate = Gate(cfg.confirm, _ask_yesno, log, speak=speak)
    memory = Memory(cfg.memory_dir)
    registry = build_registry()
    brain = build_brain(cfg, log)
    ctx = Context(cfg=cfg, log=log, gate=gate, memory=memory, speak=speak, registry=registry)
    agent = Agent(brain, registry, ctx)

    # Startup report — what's actually live.
    log.system(f"Black Number online — brain: {brain.name}, {len(registry.all())} skills.")
    for k, v in cfg.describe():
        log.system(f"{k}: {v}")
    greeting = "Black Number online. What do you need?"
    log.bn(greeting)
    speak(greeting)

    stt = build_stt(cfg, log)

    # One-shot mode: `bn "do this"` runs a single request and exits.
    if argv:
        one = " ".join(argv)
        log.you(one)
        result = agent.handle(one)
        log.bn(result.speech)
        if result.detail and result.detail != result.speech:
            print(result.detail)
        speak(result.speech)
        _drain(tts)
        log.close()
        return 0 if result.ok else 1

    # Interactive loop.
    try:
        for utterance in stt.listen():
            u = utterance.strip()
            if u.lower() in CONTROL:
                break
            if stt.name != "text":
                log.you(u)  # text source already echoes via input()
            result = agent.handle(u)
            log.bn(result.speech)
            if result.detail and result.detail.strip() and result.detail != result.speech:
                print("     " + result.detail.replace("\n", "\n     "))
            speak(result.speech)
    except KeyboardInterrupt:
        pass

    bye = "Shutting down. Goodbye."
    log.bn(bye)
    speak(bye)
    _drain(tts)
    log.close()
    return 0


def _drain(tts) -> None:
    """Let a final spoken line finish before the process exits."""
    proc = getattr(tts, "_proc", None)
    if proc is not None:
        try:
            proc.wait(timeout=15)
        except Exception:
            pass
