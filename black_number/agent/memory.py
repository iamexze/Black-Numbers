"""Memory — short-term conversation plus a durable fact store.

Two tiers, mirroring how the assistant should think:

  working memory  the last N turns, kept in RAM, sent to the brain each step.
                  Trimmed so the context never grows without bound.

  long-term       facts worth keeping across sessions — your name, preferences,
                  standing instructions — appended to a JSONL file and reloaded
                  at startup. This is the seed of the "self-improving" property:
                  the assistant accumulates what it learns about you and its own
                  corrections, and a later version can read the whole record.

Kept deliberately small and file-based for now. It swaps for a vector store or
SQLite behind the same three methods when that earns its place.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class Memory:
    def __init__(self, memory_dir: Path, working_limit: int = 24):
        self.dir = memory_dir
        self.facts_path = memory_dir / "facts.jsonl"
        self.working: list[dict[str, Any]] = []
        self.working_limit = working_limit
        self.facts: list[dict[str, Any]] = self._load_facts()

    # ── working memory ──────────────────────────────────────────────────────
    def add(self, role: str, content: Any) -> None:
        self.working.append({"role": role, "content": content})
        # Trim in whole turns from the front, but never orphan a tool_result
        # (which must follow its tool_use), so we drop from the oldest user turn.
        while len(self.working) > self.working_limit:
            self.working.pop(0)

    def transcript(self) -> list[dict[str, Any]]:
        return list(self.working)

    def reset(self) -> None:
        self.working.clear()

    # ── long-term facts ─────────────────────────────────────────────────────
    def _load_facts(self) -> list[dict[str, Any]]:
        if not self.facts_path.exists():
            return []
        out = []
        for line in self.facts_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out

    def remember(self, text: str, kind: str = "fact") -> None:
        row = {"ts": round(time.time(), 3), "kind": kind, "text": text}
        self.facts.append(row)
        with self.facts_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def forget_all(self) -> int:
        n = len(self.facts)
        self.facts.clear()
        if self.facts_path.exists():
            self.facts_path.unlink()
        return n

    def recall_block(self) -> str:
        """The facts, formatted for the system prompt. Empty string if none."""
        if not self.facts:
            return ""
        lines = [f"- {f['text']}" for f in self.facts[-40:]]
        return "What you know about the user and prior sessions:\n" + "\n".join(lines)
