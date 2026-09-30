"""Keyboard input as a speech source. Always available, zero dependencies, and
the interface used for tests and for anyone who prefers typing. Voice sources
are strict upgrades of this."""
from __future__ import annotations

import sys
from typing import Iterator

from .base import STT


class TextInput(STT):
    name = "text"

    def __init__(self, prompt: str = "you  "):
        self.prompt = prompt

    def listen(self) -> Iterator[str]:
        while True:
            try:
                line = input(self.prompt)
            except (EOFError, KeyboardInterrupt):
                return
            if line.strip():
                yield line
