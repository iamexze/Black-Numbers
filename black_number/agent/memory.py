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

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [f for f in self.facts if f.get("kind") == kind]

    def recall_block(self) -> str:
        """Long-term memory, formatted for the system prompt. '' if there is none.

        Lessons are separated from facts and stated as standing instructions,
        because they are different in kind. A fact ("the user lives in Kochi") is
        context to draw on; a lesson ("don't read long output aloud") is a
        directive about behaviour. Mixing them into one list buries the
        directives, and a correction that gets ignored is not a correction — so
        the lessons come last, where an instruction carries most weight, and are
        labelled as binding.
        """
        if not self.facts:
            return ""
        lessons = self.of_kind("lesson")
        others = [f for f in self.facts if f.get("kind") != "lesson"]
        blocks: list[str] = []
        if others:
            blocks.append(
                "What you know about the user and prior sessions:\n"
                + "\n".join(f"- {f['text']}" for f in others[-40:])
            )
        if lessons:
            blocks.append(
                "Corrections you have been given. These are standing instructions and "
                "they override your defaults:\n"
                + "\n".join(f"- {f['text']}" for f in lessons[-20:])
            )
        return "\n\n".join(blocks)
