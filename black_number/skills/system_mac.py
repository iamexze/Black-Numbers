"""System skills — controlling the Mac itself.

These wrap small, well-understood macOS commands. The read-only ones (volume,
battery, clock, frontmost app) run without confirmation; anything that changes
state (setting volume, opening/quitting apps) is REVERSIBLE and passes the gate.

Some of these use AppleScript against System Events, which macOS guards behind
an Automation permission. The first time one runs, macOS shows a permission
dialog; if it is denied, the skill returns a clear message instead of failing
silently. Nothing here assumes a permission it hasn't got.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from .base import Context, Result, Risk, Skill


def _run(cmd: list[str], timeout: int = 8) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or p.stderr).strip()
        return p.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "timed out (a permission dialog may be waiting)"
    except Exception as e:
        return False, str(e)


def _osa(script: str) -> tuple[bool, str]:
    return _run(["osascript", "-e", script])


# ── volume ───────────────────────────────────────────────────────────────────
def _get_volume(_a, _c) -> Result:
    ok, out = _osa("output volume of (get volume settings)")
    if not ok:
        return Result.fail("I couldn't read the volume.")
    return Result.say(f"Volume is at {out}.")


def _set_volume(a: dict, _c) -> Result:
    if "level" in a:
        lvl = max(0, min(100, int(a["level"])))
        ok, _ = _osa(f"set volume output volume {lvl}")
        return Result.say(f"Volume set to {lvl}.") if ok else Result.fail("Couldn't set volume.")
    ok, cur = _osa("output volume of (get volume settings)")
    cur = int(cur) if ok and cur.isdigit() else 30
    d = a.get("direction", "up")
    if d == "mute":
        _osa("set volume with output muted")
        return Result.say("Muted.")
    nxt = min(100, cur + 12) if d == "up" else max(0, cur - 12)
    _osa("set volume without output muted")
    ok, _ = _osa(f"set volume output volume {nxt}")
    return Result.say(f"Volume {d}, now {nxt}.") if ok else Result.fail("Couldn't change volume.")


# ── brightness ─────────────────────────────────────────────────────────────—
def _set_brightness(a: dict, _c) -> Result:
    # `brightness` CLI isn't installed by default; use the media keys via
    # System Events, which needs Accessibility permission. Degrade clearly.
    d = a.get("direction", "up")
    key = "144" if d == "up" else "145"  # F-key codes for brightness up/down
    ok, out = _osa(f'tell application "System Events" to key code {key}')
    if ok:
        return Result.say(f"Brightness {d}.")
    return Result.fail(
        "I need Accessibility permission to change brightness.",
        detail="Grant it in System Settings > Privacy & Security > Accessibility.",
    )


# ── apps ─────────────────────────────────────────────────────────────────────
def _open_app(a: dict, _c) -> Result:
    name = a.get("name", "").strip()
    if not name:
        return Result.fail("Which app?")
    ok, out = _run(["open", "-a", name])
    if ok:
        return Result.say(f"Opening {name}.")
    # try as a URL or file if it wasn't an app
    if name.startswith(("http://", "https://")) or "." in name:
        ok2, _ = _run(["open", name])
        if ok2:
            return Result.say(f"Opening {name}.")
    return Result.fail(f"I couldn't find an app called {name}.")


def _quit_app(a: dict, _c) -> Result:
    name = a.get("name", "").strip()
    ok, out = _osa(f'tell application "{name}" to quit')
    return Result.say(f"Quitting {name}.") if ok else Result.fail(f"Couldn't quit {name}: {out}")


def _list_apps(_a, _c) -> Result:
    ok, out = _osa(
        'tell application "System Events" to get name of '
        "(every process whose background only is false)"
    )
    if not ok:
        return Result.fail("I need Automation permission to list apps.", detail=out)
    apps = [x.strip() for x in out.split(",") if x.strip()]
    return Result.say(f"{len(apps)} apps are open.", detail=", ".join(apps))


def _frontmost(_a, _c) -> Result:
    ok, out = _osa("tell application \"System Events\" to get name of first process whose frontmost is true")
    return Result.say(f"You're in {out}.") if ok else Result.fail("Couldn't tell.", detail=out)


# ── info ─────────────────────────────────────────────────────────────────────
def _clock(_a, _c) -> Result:
    return Result.say(time.strftime("It's %-I:%M %p on %A, %-d %B %Y."))


def _battery(_a, _c) -> Result:
    ok, out = _run(["pmset", "-g", "batt"])
    if not ok:
        return Result.fail("Couldn't read the battery.")
    pct = "?"
    charging = "on battery"
    for tok in out.split():
        if tok.endswith("%;"):
            pct = tok.rstrip("%;")
        if tok.endswith("%"):
            pct = tok.rstrip("%")
    if "AC Power" in out or "charging" in out.lower():
        charging = "charging" if "discharging" not in out.lower() else "on battery"
    return Result.say(f"Battery is at {pct}%, {charging}.")


def _screenshot(a: dict, ctx: Context) -> Result:
    shots = ctx.cfg.root / "var"
    path = shots / f"shot-{int(time.time())}.png"
    ok, out = _run(["screencapture", "-x", str(path)])
    return Result.say("Screenshot saved.", detail=str(path)) if ok else Result.fail("Couldn't capture.")


def skills() -> list[Skill]:
    return [
        Skill("get_volume", "Report the current output volume.", _get_volume, risk=Risk.READ_ONLY),
        Skill(
            "set_volume", "Change output volume. direction up/down/mute, or an absolute level 0-100.",
            _set_volume,
            parameters={
                "direction": {"type": "string", "enum": ["up", "down", "mute"]},
                "level": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            risk=Risk.REVERSIBLE,
        ),
        Skill(
            "set_brightness", "Change screen brightness up or down.", _set_brightness,
            parameters={"direction": {"type": "string", "enum": ["up", "down"], "required": True}},
            risk=Risk.REVERSIBLE,
        ),
        Skill(
            "open_app", "Open a Mac application, a file, or a URL by name.", _open_app,
            parameters={"name": {"type": "string", "required": True}},
            risk=Risk.REVERSIBLE,
        ),
        Skill(
            "quit_app", "Quit a running application by name.", _quit_app,
            parameters={"name": {"type": "string", "required": True}},
            risk=Risk.MUTATING,
        ),
        Skill("list_apps", "List the currently open applications.", _list_apps, risk=Risk.READ_ONLY),
        Skill("frontmost_app", "Say which app is in focus right now.", _frontmost, risk=Risk.READ_ONLY),
        Skill("clock", "Give the current time and date.", _clock, risk=Risk.READ_ONLY),
        Skill("battery", "Report battery level and charging state.", _battery, risk=Risk.READ_ONLY),
        Skill("screenshot", "Capture the screen to a file.", _screenshot, risk=Risk.REVERSIBLE),
    ]
