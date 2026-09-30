"""Brain selection — pick the requested provider, fall back if it can't run.

The router is what lets the same assistant behave identically whether a key is
present or not. It tries the configured brain, and if that one is unavailable it
walks down to something that always works (offline), telling the user once what
happened rather than failing.
"""

from __future__ import annotations

from .anthropic_brain import AnthropicBrain
from .base import Brain
from .offline import OfflineBrain
from .ollama_brain import OllamaBrain


def build_brain(cfg, log) -> Brain:
    want = cfg.brain
    chain: list[Brain] = []

    if want == "anthropic":
        chain.append(AnthropicBrain(cfg.anthropic_key, cfg.anthropic_model))
    elif want == "ollama":
        chain.append(OllamaBrain(cfg.ollama_host, cfg.ollama_model))
    # offline is always the floor
    chain.append(OfflineBrain())

    for brain in chain:
        if brain.available():
            if brain.name != want and want != "offline":
                log.system(f"Brain '{want}' unavailable — using '{brain.name}'.")
            return brain

    return OfflineBrain()
