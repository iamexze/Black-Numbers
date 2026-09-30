"""Media and machine state — playback, appearance, display, focus.

The AppleScript here targets whichever player is actually running rather than
assuming one. Media keys are deliberately not used: synthesising NX media keys
needs Accessibility permission and fails silently when it is missing, whereas
`tell application "Music" to playpause` either works or returns an error we can
report honestly.

Do Not Disturb has no supported scripting interface on modern macOS. Rather than
poke at private defaults that break on each release, this module drives a
user-created Shortcut through the `shortcuts` CLI and, when that Shortcut does
not exist, says exactly how to create it. A capability the user can turn on in
thirty seconds beats one that silently does nothing.
"""

from __future__ import annotations

import shutil
import subprocess

from .base import Context, Result, Risk, Skill

PLAYERS = ("Music", "Spotify")
DND_SHORTCUT = "Toggle Do Not Disturb"


def _sh(cmd: list[str], timeout: int = 10) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except FileNotFoundError:
        return False, f"{cmd[0]} isn't available"
    except subprocess.TimeoutExpired:
        return False, "timed out — a permission dialog may be waiting"
    except Exception as e:
        return False, str(e)


def _osa(script: str, timeout: int = 10) -> tuple[bool, str]:
    return _sh(["osascript", "-e", script], timeout=timeout)


def _running(app: str) -> bool:
    ok, out = _osa(f'application "{app}" is running', timeout=6)
    return ok and out.strip() == "true"


def _active_player() -> str:
    """The player that is playing, else the one that is merely open, else ''."""
    open_players = [p for p in PLAYERS if _running(p)]
    for p in open_players:
        ok, state = _osa(f'tell application "{p}" to player state as string', timeout=6)
        if ok and state.strip().lower() == "playing":
            return p
    return open_players[0] if open_players else ""


# ── playback ────────────────────────────────────────────────────────────────
_ACTIONS = {
    "play_pause": "playpause",
    "play": "play",
    "pause": "pause",
    "next": "next track",
    "previous": "previous track",
}


def _media_control(a: dict, _c) -> Result:
    action = str(a.get("action") or "play_pause").strip().lower().replace("-", "_")
    if action in ("toggle", "playpause"):
        action = "play_pause"
    verb = _ACTIONS.get(action)
    if verb is None:
        return Result.fail(f"I don't know the playback action '{action}'.")
    player = (a.get("player") or "").strip() or _active_player()
    if not player:
        return Result.fail("Nothing is playing — Music and Spotify are both closed.")
    ok, out = _osa(f'tell application "{player}" to {verb}')
    if not ok:
        return Result.fail(f"{player} wouldn't respond.", detail=out)
    label = action.replace("_", "/")
    return Result.say(f"{label.capitalize()} on {player}.")


def _now_playing(_a, _c) -> Result:
    player = _active_player()
    if not player:
        return Result.say("Nothing is playing.")
    ok, state = _osa(f'tell application "{player}" to player state as string', timeout=6)
    if ok and state.strip().lower() != "playing":
        return Result.say(f"{player} is open but {state.strip()}.")
    ok, out = _osa(
        f'tell application "{player}" to get (name of current track) '
        '& " — " & (artist of current track)'
    )
    if not ok or not out.strip():
        return Result.fail(f"{player} is playing but wouldn't tell me what.", detail=out)
    return Result.say(f"{out.strip()}, on {player}.", data={"player": player, "track": out.strip()})


# ── appearance and display ──────────────────────────────────────────────────
def _dark_mode(a: dict, _c) -> Result:
    want = str(a.get("state") or "toggle").strip().lower()
    if want == "status":
        ok, out = _osa('tell application "System Events" to tell appearance '
                       "preferences to get dark mode")
        if not ok:
            return Result.fail("I need Automation permission to read the appearance.", detail=out)
        return Result.say(f"Dark mode is {'on' if out.strip() == 'true' else 'off'}.")
    target = {"on": "true", "off": "false", "toggle": "not dark mode"}.get(want)
    if target is None:
        return Result.fail("Say on, off, toggle or status.")
    ok, out = _osa('tell application "System Events" to tell appearance '
                   f"preferences to set dark mode to {target}")
    if not ok:
        return Result.fail(
            "I need Automation permission for System Events to change the appearance.",
            detail="Grant it in System Settings > Privacy & Security > Automation.",
        )
    return Result.say("Dark mode toggled." if want == "toggle" else f"Dark mode {want}.")


def _lock_screen(_a, _c) -> Result:
    # The documented lock is the Control-Command-Q chord; there is no cleaner
    # scripting entry point, so this needs Accessibility permission.
    ok, out = _osa('tell application "System Events" to keystroke "q" '
                   "using {command down, control down}")
    if not ok:
        return Result.fail("I need Accessibility permission to lock the screen.", detail=out)
    return Result.say("Locking.")


def _sleep_display(_a, _c) -> Result:
    ok, out = _sh(["pmset", "displaysleepnow"], timeout=6)
    return Result.say("Display off.") if ok else Result.fail("Couldn't sleep the display.", detail=out)


def _keep_awake(a: dict, ctx: Context) -> Result:
    """Hold the machine awake for a while, via caffeinate.

    Launched detached and deliberately not waited on: the point is that it
    outlives the request. The pid is returned so it can be stopped.
    """
    minutes = a.get("minutes")
    if minutes in (None, ""):
        minutes = 60
    try:
        minutes = max(1, min(int(minutes), 24 * 60))
    except (TypeError, ValueError):
        return Result.fail("Keep it awake for how many minutes?")
    if not shutil.which("caffeinate"):
        return Result.fail("caffeinate isn't available on this system.")
    if str(a.get("state") or "").strip().lower() == "off":
        ok, out = _sh(["pkill", "-f", "caffeinate -dimsu"], timeout=6)
        return Result.say("Sleep re-enabled." if ok else "Nothing was holding it awake.")
    try:
        proc = subprocess.Popen(
            ["caffeinate", "-dimsu", "-t", str(minutes * 60)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception as e:
        return Result.fail(f"Couldn't hold it awake: {e}")
    return Result.say(
        f"Staying awake for {minutes} {'minute' if minutes == 1 else 'minutes'}.",
        detail=f"caffeinate pid {proc.pid}", data={"pid": proc.pid, "minutes": minutes},
    )


def _do_not_disturb(a: dict, _c) -> Result:
    if not shutil.which("shortcuts"):
        return Result.fail("The `shortcuts` command isn't on this system, so I can't reach Focus.")
    name = a.get("shortcut") or DND_SHORTCUT
    ok, out = _sh(["shortcuts", "run", name], timeout=20)
    if ok:
        return Result.say("Focus toggled.")
    return Result.fail(
        f"I need a Shortcut named '{name}' to control Focus.",
        detail=(
            "macOS gives no scriptable Do Not Disturb switch. Create it once:\n"
            "  Shortcuts app > new shortcut > add the 'Set Focus' action >\n"
            f"  set it to toggle Do Not Disturb > name it exactly '{name}'.\n"
            f"Then this works. (shortcuts said: {out[:200]})"
        ),
    )


def _set_wallpaper(a: dict, _c) -> Result:
    from pathlib import Path

    raw = (a.get("path") or "").strip()
    if not raw:
        return Result.fail("Which image?")
    p = Path(raw).expanduser().resolve()
    if not p.is_file():
        return Result.fail(f"There's no file at {p}.")
    if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".heic", ".tiff", ".gif"):
        return Result.fail("That doesn't look like an image.")
    ok, out = _osa(f'tell application "System Events" to tell every desktop to set '
                   f'picture to "{p}"')
    if not ok:
        return Result.fail("I need Automation permission to change the wallpaper.", detail=out)
    return Result.say(f"Wallpaper set to {p.name}.")


def skills() -> list[Skill]:
    return [
        Skill(
            "media_control",
            "Control playback in Music or Spotify: play_pause, play, pause, next, previous.",
            _media_control,
            parameters={
                "action": {"type": "string",
                           "enum": ["play_pause", "play", "pause", "next", "previous"]},
                "player": {"type": "string", "enum": list(PLAYERS)},
            },
            risk=Risk.REVERSIBLE,
        ),
        Skill("now_playing", "Say what is currently playing.", _now_playing, risk=Risk.READ_ONLY),
        Skill(
            "dark_mode", "Turn dark mode on, off, toggle it, or report it.", _dark_mode,
            parameters={"state": {"type": "string", "enum": ["on", "off", "toggle", "status"]}},
            risk=Risk.REVERSIBLE,
        ),
        Skill("lock_screen", "Lock the screen immediately.", _lock_screen, risk=Risk.REVERSIBLE),
        Skill("sleep_display", "Turn the display off without locking.", _sleep_display,
              risk=Risk.REVERSIBLE),
        Skill(
            "keep_awake", "Stop the Mac sleeping for a number of minutes, or turn that off.",
            _keep_awake,
            parameters={"minutes": {"type": "integer"},
                        "state": {"type": "string", "enum": ["on", "off"]}},
            risk=Risk.REVERSIBLE,
        ),
        Skill(
            "do_not_disturb", "Toggle Focus / Do Not Disturb via a named Shortcut.",
            _do_not_disturb,
            parameters={"shortcut": {"type": "string"}},
            risk=Risk.REVERSIBLE,
        ),
        Skill(
            "set_wallpaper", "Set the desktop picture to an image file.", _set_wallpaper,
            parameters={"path": {"type": "string", "required": True}},
            risk=Risk.REVERSIBLE,
        ),
    ]
