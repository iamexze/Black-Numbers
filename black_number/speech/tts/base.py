"""Text-to-speech interface. speak() must never block the main loop for long
and must never raise — a broken voice degrades to silence, not a crash."""
from __future__ import annotations


class TTS:
    name = "base"

    def speak(self, text: str) -> None: ...
    def stop(self) -> None: ...
    def available(self) -> bool:
        return True
