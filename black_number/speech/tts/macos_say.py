"""macOS `say` — free, on-device, no install. Speaks asynchronously so the
assistant stays responsive, and can be interrupted mid-sentence (barge-in)."""
from __future__ import annotations

import shutil
import subprocess

from .base import TTS


class MacSay(TTS):
    name = "macos_say"

    def __init__(self, voice: str = "Daniel", rate: int = 190):
        self.voice = voice
        self.rate = rate
        self._proc: subprocess.Popen | None = None

    def available(self) -> bool:
        return shutil.which("say") is not None

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        self.stop()
        try:
            self._proc = subprocess.Popen(
                ["say", "-v", self.voice, "-r", str(self.rate), text]
            )
        except Exception:
            self._proc = None

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._proc = None
