"""Presentation — the assistant's face: emblem, boot sequence, panels.

Rendering lives here, apart from logic, for the same reason a skill returns a
Result instead of printing: the runtime decides how something looks, and the
same information can be shown in a terminal, spoken aloud, or later drawn in a
window without touching a single skill.

Everything degrades. When stdout is not a terminal — a pipe, a test, CI — the
colour codes and the animation are dropped and the output stays plain, so a
captured run is as readable as a live one.
"""

from __future__ import annotations

import re
import shutil
import sys
import time

_ANSI = re.compile(r"\033\[[0-9;]*m")

# Palette. Brass is the house colour; everything else is used sparingly, because
# a status display that is all colour carries no information.
BRASS = "\033[38;5;179m"
STEEL = "\033[38;5;110m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
AMBER = "\033[38;5;214m"
RESET = "\033[0m"

EMBLEM = r"""
             ▄▄▄▄▄
          ▄█████████▄
        ██████ ▀ ██████
       █████   ◆◆◆   █████
        ██████ ▄ ██████
          ▀█████████▀
             ▀▀▀▀▀
"""

WORDMARK = r"""
  ██████╗ ██╗      █████╗  ██████╗██╗  ██╗    ███╗   ██╗██╗   ██╗███╗   ███╗
  ██╔══██╗██║     ██╔══██╗██╔════╝██║ ██╔╝    ████╗  ██║██║   ██║████╗ ████║
  ██████╔╝██║     ███████║██║     █████╔╝     ██╔██╗ ██║██║   ██║██╔████╔██║
  ██╔══██╗██║     ██╔══██║██║     ██╔═██╗     ██║╚██╗██║██║   ██║██║╚██╔╝██║
  ██████╔╝███████╗██║  ██║╚██████╗██║  ██╗    ██║ ╚████║╚██████╔╝██║ ╚═╝ ██║
  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝    ╚═╝  ╚═══╝ ╚═════╝ ╚═╝     ╚═╝
"""


def is_tty() -> bool:
    return sys.stdout.isatty()


def paint(text: str, *codes: str) -> str:
    """Colour `text`, or return it untouched when colour would be noise."""
    if not codes or not is_tty():
        return text
    return "".join(codes) + text + RESET


def visible_len(text: str) -> int:
    """Length as the eye sees it, with escape sequences discounted."""
    return len(_ANSI.sub("", text))


def term_width(default: int = 80) -> int:
    try:
        return max(48, min(shutil.get_terminal_size((default, 24)).columns, 120))
    except Exception:
        return default


# ── the mark ────────────────────────────────────────────────────────────────
def mark(tagline: str = "") -> str:
    """The emblem beside the wordmark, as one block."""
    if not is_tty():
        return "BLACK NUMBER" + (f" — {tagline}" if tagline else "")
    emblem = [ln for ln in EMBLEM.splitlines() if ln.strip()]
    words = [ln for ln in WORDMARK.splitlines() if ln.strip()]
    out = [paint(ln, BRASS) for ln in words]
    out.insert(0, "")
    body = "\n".join(paint(ln, BRASS, BOLD) for ln in emblem)
    if tagline:
        body += "\n" + paint(f"       {tagline}", DIM)
    return body + "\n" + "\n".join(out)


# ── panels ──────────────────────────────────────────────────────────────────
def panel(title: str, rows, width: int | None = None) -> str:
    """A boxed key/value or plain-line panel.

    `rows` is a list of (label, value) pairs or plain strings. The box is sized
    to its contents, capped to the terminal, and every line is padded by the
    width the eye sees rather than the byte count, so colour never skews it.
    """
    pairs = [r if isinstance(r, tuple) else (r, "") for r in rows]
    label_w = max((visible_len(k) for k, v in pairs), default=0)
    body = [f"{k.ljust(label_w)}  {v}".rstrip() if v else k for k, v in pairs]
    inner = max([visible_len(b) for b in body] + [visible_len(title) + 2])
    inner = min(inner, (width or term_width()) - 4)
    top = f"┌─ {paint(title, BOLD)} " + "─" * max(0, inner - visible_len(title) - 1) + "┐"
    lines = [top]
    for b in body:
        pad = " " * max(0, inner - visible_len(b))
        lines.append(f"│ {b}{pad} │")
    lines.append("└" + "─" * (inner + 2) + "┘")
    return "\n".join(paint(ln, DIM) if ln.startswith(("┌", "└")) else ln for ln in lines)


def rule(label: str = "") -> str:
    w = term_width()
    if not label:
        return paint("─" * w, DIM)
    return paint("── " + label + " " + "─" * max(0, w - len(label) - 4), DIM)


# ── boot sequence ───────────────────────────────────────────────────────────
STATE_GLYPH = {True: ("◆", GREEN), False: ("◇", RED), None: ("◈", AMBER)}


def boot(checks, out=print, animate: bool | None = None) -> None:
    """Report each subsystem as it comes up.

    `checks` is a list of (label, state, note) where state is True (live),
    False (unavailable) or None (degraded to a fallback). The distinction is the
    point: this assistant is designed to run with pieces missing, so the boot
    report says which pieces those are instead of pretending all is well.
    """
    if animate is None:
        animate = is_tty()
    out(rule("subsystems"))
    for label, state, note in checks:
        glyph, colour = STATE_GLYPH.get(state, STATE_GLYPH[None])
        line = f"  {paint(glyph, colour)} {label.ljust(16)} {paint(note, DIM) if note else ''}"
        out(line.rstrip())
        if animate:
            time.sleep(0.035)
    out(rule())
