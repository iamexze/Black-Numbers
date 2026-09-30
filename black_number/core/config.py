"""Configuration — the single place settings are resolved.

Precedence, highest first: real environment variables, then a `.env` file in the
project root, then the defaults below. Nothing here raises if a key is missing;
a missing API key downgrades a capability rather than crashing the assistant.
That property — every layer degrades instead of failing — is the backbone of
the whole design, so it starts here.
"""

from __future__ import annotations

import importlib.util
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
    anthropic_model: str = field(default_factory=lambda: get("BN_ANTHROPIC_MODEL", "claude-opus-5"))
    # Reasoning depth: low | medium | high | xhigh | max. `high` is the API's own
    # default and the right setting for correctness; dropping to medium or low
    # makes a voice assistant noticeably snappier on routine commands, which is a
    # tradeoff the user should make rather than one made for them.
    effort: str = field(default_factory=lambda: get("BN_EFFORT", "high"))
    ollama_host: str = field(default_factory=lambda: get("BN_OLLAMA_HOST", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: get("BN_OLLAMA_MODEL", "llama3.1:8b"))

    # Speech
    stt: str = field(default_factory=lambda: get("BN_STT", "text"))
    tts: str = field(default_factory=lambda: get("BN_TTS", "macos_say"))
    voice: str = field(default_factory=lambda: get("BN_VOICE", "Daniel"))
    wake_word: str = field(default_factory=lambda: get("BN_WAKE_WORD", "black number").lower())

    # Persona — how it addresses you, and where "here" is for local questions.
    # Both are cosmetic on purpose: an empty honorific is the default because
    # most people find one grating, and the weather skill needs a place to mean
    # "outside" before it can answer without being told.
    address: str = field(default_factory=lambda: get("BN_ADDRESS", ""))
    city: str = field(default_factory=lambda: get("BN_CITY", ""))

    # Safety
    confirm: str = field(default_factory=lambda: get("BN_CONFIRM", "trusted"))

    # Paths
    root: Path = ROOT
    log_dir: Path = ROOT / "var" / "logs"
    memory_dir: Path = ROOT / "var" / "memory"
    notes_dir: Path = ROOT / "var" / "notes"
    state_dir: Path = ROOT / "var" / "state"

    def effective_brain(self) -> str:
        """The brain that will actually run, after checking prerequisites.

        Asking for Anthropic without a key silently becomes the offline brain,
        so a fresh clone talks back instead of throwing on the first request. The
        SDK being installed is checked too, not just the key: a key with no
        `anthropic` package is the commonest half-configured state, and reporting
        "anthropic" there would make the startup banner lie about what is running.
        """
        if self.brain == "anthropic":
            if not self.anthropic_key:
                return "offline"
            if importlib.util.find_spec("anthropic") is None:
                return "offline"
        return self.brain

    def describe(self) -> list[tuple[str, str]]:
        """Human-readable status, shown at startup and by the `status` command."""
        b = self.effective_brain()
        brain_note = b
        if b != self.brain:
            why = ("no API key set" if not self.anthropic_key
                   else "the `anthropic` package isn't installed")
            brain_note = f"{b}  (requested {self.brain}, {why})"
        return [
            ("brain", brain_note),
            ("model", self.anthropic_model if b == "anthropic" else "—"),
            ("effort", self.effort if b == "anthropic" else "—"),
            ("speech in", self.stt),
            ("speech out", self.tts if self.tts != "none" else "silent"),
            ("voice", self.voice),
            ("wake word", self.wake_word),
            ("confirm policy", self.confirm),
            ("address as", self.address or "(none)"),
            ("home city", self.city or "(ask each time)"),
        ]


def load() -> Config:
    cfg = Config()
    for d in (cfg.log_dir, cfg.memory_dir, cfg.notes_dir, cfg.state_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg
