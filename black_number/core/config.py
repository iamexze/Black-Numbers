"""Configuration — the single place settings are resolved.

Precedence, highest first: real environment variables, then a `.env` file in the
project root, then the defaults below. Nothing here raises if a key is missing;
a missing API key downgrades a capability rather than crashing the assistant.
That property — every layer degrades instead of failing — is the backbone of
the whole design, so it starts here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> dict[str, str]:
    env: dict[str, str] = {}
    f = ROOT / ".env"
    if not f.exists():
        return env
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


_DOTENV = _load_dotenv()


def get(key: str, default: str = "") -> str:
    """Resolve a single setting. Real env wins over .env wins over default."""
    return os.environ.get(key) or _DOTENV.get(key) or default


@dataclass(frozen=True)
class Config:
    # Brain
    brain: str = field(default_factory=lambda: get("BN_BRAIN", "anthropic"))
    anthropic_key: str = field(default_factory=lambda: get("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(default_factory=lambda: get("BN_ANTHROPIC_MODEL", "claude-opus-4-8"))
    ollama_host: str = field(default_factory=lambda: get("BN_OLLAMA_HOST", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: get("BN_OLLAMA_MODEL", "llama3.1:8b"))

    # Speech
    stt: str = field(default_factory=lambda: get("BN_STT", "text"))
    tts: str = field(default_factory=lambda: get("BN_TTS", "macos_say"))
    voice: str = field(default_factory=lambda: get("BN_VOICE", "Daniel"))
    wake_word: str = field(default_factory=lambda: get("BN_WAKE_WORD", "black number").lower())

    # Safety
    confirm: str = field(default_factory=lambda: get("BN_CONFIRM", "trusted"))

    # Paths
    root: Path = ROOT
    log_dir: Path = ROOT / "var" / "logs"
    memory_dir: Path = ROOT / "var" / "memory"

    def effective_brain(self) -> str:
        """The brain that will actually run, after checking prerequisites.

        Asking for Anthropic without a key silently becomes the offline brain,
        so a fresh clone talks back instead of throwing on the first request.
        """
        if self.brain == "anthropic" and not self.anthropic_key:
            return "offline"
        return self.brain

    def describe(self) -> list[tuple[str, str]]:
        """Human-readable status, shown at startup and by the `status` command."""
        b = self.effective_brain()
        brain_note = b
        if b != self.brain:
            brain_note = f"{b}  (requested {self.brain}, no API key set)"
        return [
            ("brain", brain_note),
            ("speech in", self.stt),
            ("speech out", self.tts if self.tts != "none" else "silent"),
            ("voice", self.voice),
            ("wake word", self.wake_word),
            ("confirm policy", self.confirm),
        ]


def load() -> Config:
    cfg = Config()
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    return cfg
