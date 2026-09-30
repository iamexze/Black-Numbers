"""Speech-to-text interface.

A source yields recognised utterances. The text source reads typed lines; the
macOS and whisper sources listen to the microphone. All three present the same
generator so the agent loop is identical regardless of how words arrive."""
from __future__ import annotations

from typing import Iterator


class STT:
    name = "base"

    def listen(self) -> Iterator[str]:
        """Yield one recognised utterance at a time until the source ends."""
        raise NotImplementedError

    def available(self) -> bool:
        return True
