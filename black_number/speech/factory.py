"""Choose speech providers, degrading to text/silent if a choice can't run."""
from __future__ import annotations

from .stt.base import STT
from .stt.text import TextInput
from .tts.base import TTS
from .tts.silent import Silent


def build_tts(cfg, log) -> TTS:
    if cfg.tts == "macos_say":
        from .tts.macos_say import MacSay
        t = MacSay(voice=cfg.voice)
        if t.available():
            return t
        log.system("`say` unavailable — output will be silent.")
    return Silent()


def build_stt(cfg, log) -> STT:
    want = cfg.stt
    if want == "macos":
        from .stt.macos_speech import MacSpeech
        s = MacSpeech()
        if s.available():
            log.system("Listening on the microphone (macOS on-device speech).")
            return s
        log.system("macOS speech bridge not installed — falling back to typed input. "
                   "Install with: pip install -e '.[macspeech]'")
    elif want == "whisper":
        log.system("whisper source not built yet — using typed input.")
    return TextInput()
