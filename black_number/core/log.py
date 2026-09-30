"""Structured logging.

Two sinks: a readable line on the console for the user, and an append-only
JSONL transcript on disk. The transcript is what makes the assistant
inspectable and, later, self-improving: every turn, every tool call, every
confirmation and its outcome is a row a future version can read back and learn
from. Nothing is hidden from it.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

# ANSI, used only when stdout is a terminal.
_TTY = sys.stdout.isatty()
_DIM = "\033[2m" if _TTY else ""
_BOLD = "\033[1m" if _TTY else ""
_RESET = "\033[0m" if _TTY else ""
_BRASS = "\033[38;5;179m" if _TTY else ""
_RED = "\033[31m" if _TTY else ""
_GREEN = "\033[32m" if _TTY else ""


class Log:
    def __init__(self, path: Path):
        self.path = path
        self._fh = path.open("a", encoding="utf-8")

    def event(self, kind: str, **fields: Any) -> None:
        """Append one structured event to the JSONL transcript."""
        row = {"ts": round(time.time(), 3), "kind": kind, **fields}
        try:
            self._fh.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception:
            pass  # logging must never take down the assistant

    # ── Console channels. The user reads these; the transcript keeps them too.
    def system(self, msg: str) -> None:
        print(f"{_DIM}· {msg}{_RESET}")
        self.event("system", msg=msg)

    def you(self, msg: str) -> None:
        print(f"{_BOLD}you{_RESET}  {msg}")
        self.event("user", text=msg)

    def bn(self, msg: str) -> None:
        print(f"{_BRASS}{_BOLD}bn{_RESET}   {msg}")
        self.event("assistant", text=msg)

    def warn(self, msg: str) -> None:
        print(f"{_RED}! {msg}{_RESET}")
        self.event("warn", msg=msg)

    def ok(self, msg: str) -> None:
        print(f"{_GREEN}✓{_RESET} {msg}")
        self.event("ok", msg=msg)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def open_log(log_dir: Path) -> Log:
    day = time.strftime("%Y-%m-%d")
    return Log(log_dir / f"bn-{day}.jsonl")
