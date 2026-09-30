"""No voice output. The assistant is text-only but fully functional."""
from __future__ import annotations

from .base import TTS


class Silent(TTS):
    name = "none"

    def speak(self, text: str) -> None: ...
    def stop(self) -> None: ...
