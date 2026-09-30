"""Clipboard and notes — catching things before they are lost.

The clipboard is the shortest path between the assistant and whatever you are
actually doing: "read my clipboard and summarise it" needs no file, no path and
no permission dialog. Notes are the other half — a timestamped append-only
scratchpad, kept as plain Markdown under var/notes so it stays yours and stays
greppable by anything else on the machine.

Reading the clipboard is READ_ONLY. Writing to it is REVERSIBLE, and it is
genuinely a change: it destroys whatever the user had copied, which is why it
passes the gate rather than happening silently.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from .base import Context, Result, Risk, Skill

MAX_NOTE_BYTES = 2_000_000


def _pbpaste() -> tuple[bool, str]:
    try:
        p = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=6)
        return p.returncode == 0, p.stdout
    except FileNotFoundError:
        return False, "pbpaste isn't available on this system."
    except subprocess.TimeoutExpired:
        return False, "the clipboard didn't respond"
    except Exception as e:
        return False, str(e)


def _pbcopy(text: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True, timeout=6)
        return p.returncode == 0, p.stderr.strip()
    except FileNotFoundError:
        return False, "pbcopy isn't available on this system."
    except Exception as e:
        return False, str(e)


# ── clipboard ───────────────────────────────────────────────────────────────
def _clipboard_read(_a, _c) -> Result:
    ok, text = _pbpaste()
    if not ok:
        return Result.fail(f"I couldn't read the clipboard: {text}")
    if not text.strip():
        return Result.say("The clipboard is empty.")
    words = len(text.split())
    first = text.strip().splitlines()[0][:120]
    return Result.say(
        f"{words} {'word' if words == 1 else 'words'} on the clipboard, starting '{first[:60]}'.",
        detail=text[:6000],
        data=text,
    )


def _clipboard_write(a: dict, _c) -> Result:
    text = a.get("text")
    if text is None or text == "":
        return Result.fail("Copy what?")
    ok, err = _pbcopy(str(text))
    if not ok:
        return Result.fail(f"I couldn't write to the clipboard: {err}")
    n = len(str(text))
    return Result.say(f"Copied, {n} {'character' if n == 1 else 'characters'}.")


# ── notes ───────────────────────────────────────────────────────────────────
def _notebook(ctx: Context, name: str = "scratch") -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "scratch"
    if not safe.endswith(".md"):
        safe += ".md"
    ctx.cfg.notes_dir.mkdir(parents=True, exist_ok=True)
    return ctx.cfg.notes_dir / safe


def _note_add(a: dict, ctx: Context) -> Result:
    text = (a.get("text") or "").strip()
    if not text:
        return Result.fail("Note what down?")
    book = _notebook(ctx, a.get("notebook") or "scratch")
    stamp = time.strftime("%Y-%m-%d %H:%M")
    fresh = not book.exists()
    with book.open("a", encoding="utf-8") as f:
        if fresh:
            f.write(f"# {book.stem}\n\n")
        f.write(f"- **{stamp}** — {text}\n")
    return Result.say("Noted.", detail=f"{book}\n- {stamp} — {text}")


def _note_read(a: dict, ctx: Context) -> Result:
    book = _notebook(ctx, a.get("notebook") or "scratch")
    if not book.exists():
        return Result.say(f"The {book.stem} notebook is empty.")
    if book.stat().st_size > MAX_NOTE_BYTES:
        return Result.fail("That notebook is too large to read back.")
    text = book.read_text(errors="replace")
    entries = [ln for ln in text.splitlines() if ln.startswith("- ")]
    recent = entries[-int(a.get("limit") or 15):]
    return Result.say(
        f"{len(entries)} {'note' if len(entries) == 1 else 'notes'} in {book.stem}.",
        detail="\n".join(recent) or text[:2000],
        data=entries,
    )


def _note_search(a: dict, ctx: Context) -> Result:
    query = (a.get("query") or "").strip()
    if not query:
        return Result.fail("Search the notes for what?")
    ctx.cfg.notes_dir.mkdir(parents=True, exist_ok=True)
    rx = re.compile(re.escape(query), re.I)
    hits: list[str] = []
    for book in sorted(ctx.cfg.notes_dir.glob("*.md")):
        if book.stat().st_size > MAX_NOTE_BYTES:
            continue
        for line in book.read_text(errors="replace").splitlines():
            if rx.search(line):
                hits.append(f"{book.stem}: {line.lstrip('- ')}")
            if len(hits) >= 60:
                break
    if not hits:
        return Result.say(f"Nothing in my notes about {query}.")
    return Result.say(
        f"{len(hits)} {'match' if len(hits) == 1 else 'matches'} for {query}.",
        detail="\n".join(hits),
        data=hits,
    )


def _note_books(_a, ctx: Context) -> Result:
    ctx.cfg.notes_dir.mkdir(parents=True, exist_ok=True)
    books = sorted(ctx.cfg.notes_dir.glob("*.md"))
    if not books:
        return Result.say("I have no notebooks yet.")
    rows = []
    for b in books:
        count = sum(1 for ln in b.read_text(errors="replace").splitlines() if ln.startswith("- "))
        rows.append(f"{b.stem:24} {count} notes  {time.strftime('%Y-%m-%d', time.localtime(b.stat().st_mtime))}")
    return Result.say(f"{len(books)} {'notebook' if len(books) == 1 else 'notebooks'}.",
                      detail="\n".join(rows))


def _note_to_clipboard(a: dict, ctx: Context) -> Result:
    """Capture whatever is on the clipboard straight into a notebook — the move
    you want after copying something you will otherwise lose."""
    ok, text = _pbpaste()
    if not ok or not text.strip():
        return Result.fail("There's nothing on the clipboard to keep.")
    label = (a.get("label") or "").strip()
    body = f"{label}: {text.strip()}" if label else text.strip()
    return _note_add({"text": body.replace("\n", " ")[:2000],
                      "notebook": a.get("notebook") or "clippings"}, ctx)


def skills() -> list[Skill]:
    return [
        Skill("clipboard_read", "Read the current clipboard contents.",
              _clipboard_read, risk=Risk.READ_ONLY),
        Skill(
            "clipboard_write", "Replace the clipboard with the given text.",
            _clipboard_write,
            parameters={"text": {"type": "string", "required": True}},
            risk=Risk.REVERSIBLE,
            caution="this replaces whatever is currently copied",
        ),
        Skill(
            "note_add", "Append a timestamped note to a local Markdown notebook.",
            _note_add,
            parameters={"text": {"type": "string", "required": True},
                        "notebook": {"type": "string"}},
            risk=Risk.REVERSIBLE,
        ),
        Skill(
            "note_read", "Read back recent notes from a notebook.", _note_read,
            parameters={"notebook": {"type": "string"}, "limit": {"type": "integer"}},
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "note_search", "Search every notebook for a phrase.", _note_search,
            parameters={"query": {"type": "string", "required": True}},
            risk=Risk.READ_ONLY,
        ),
        Skill("note_books", "List the notebooks and how much is in each.",
              _note_books, risk=Risk.READ_ONLY),
        Skill(
            "note_clipboard", "Save what's on the clipboard into a notebook.",
            _note_to_clipboard,
            parameters={"label": {"type": "string"}, "notebook": {"type": "string"}},
            risk=Risk.REVERSIBLE,
        ),
    ]
