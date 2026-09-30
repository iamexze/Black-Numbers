"""Reminders, Calendar and Notes — the user's own data, through AppleScript.

Two decisions here are worth explaining, because both avoid a class of bug that
is easy to ship and hard to notice.

Dates are never passed as strings. Building `date "30/09/2026 19:30"` in
AppleScript depends on the machine's locale, so the same script silently means a
different day on a different Mac. Instead every time is converted to an offset in
seconds from `current date`, computed in Python, and the script does arithmetic
on a real date object. Reading back works the same way: events report their start
as an offset, and Python turns it into a local time. No date is ever parsed from
text.

User text is escaped before it reaches a script. A reminder called `"; do
something else` would otherwise change the meaning of the script that carries it,
which is injection in a different suit. `_as_literal` handles it in one place.

All of this needs Automation permission, which macOS will ask for once per app on
first use. Denial or a waiting dialog surfaces as a clear message naming the
permission, never as a silent failure.
"""

from __future__ import annotations

import re
import subprocess
import time

from .base import Context, Result, Risk, Skill
from .time_focus import parse_clock_time, parse_duration

PERMISSION_HINT = "Grant it in System Settings > Privacy & Security > Automation."


def _as_literal(text: str) -> str:
    """Escape a Python string so it is safe inside an AppleScript double-quoted
    literal. Backslash first, then quotes; newlines become spaces because a raw
    newline would terminate the literal."""
    return (str(text).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", " ").replace("\r", " "))


def _osa(script: str, timeout: int = 30) -> tuple[bool, str]:
    try:
        p = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or p.stderr).strip()
        return p.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "timed out — a permission dialog may be waiting, or the app is slow to start"
    except FileNotFoundError:
        return False, "osascript isn't available — this skill needs macOS"
    except Exception as e:
        return False, str(e)


def _permission_failure(app: str, out: str) -> Result:
    if "not authorized" in out.lower() or "-1743" in out or "1743" in out:
        return Result.fail(f"I need Automation permission for {app}.", detail=PERMISSION_HINT)
    if "timed out" in out:
        return Result.fail(f"{app} didn't respond in time.", detail=out)
    return Result.fail(f"{app} returned an error.", detail=out)


def _midnight_today() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _resolve_when(a: dict) -> tuple[float | None, str]:
    """Absolute epoch for 'at' or a relative duration. (None, '') means unset."""
    if a.get("at"):
        when = parse_clock_time(str(a["at"]))
        return (when, "") if when else (None, f"I couldn't read '{a['at']}' as a time.")
    for key in ("in", "duration"):
        if a.get(key):
            secs = parse_duration(str(a[key]))
            return ((time.time() + secs), "") if secs else (None, f"I couldn't read '{a[key]}'.")
    if a.get("minutes") not in (None, ""):
        try:
            return time.time() + float(a["minutes"]) * 60, ""
        except (TypeError, ValueError):
            return None, "That isn't a number of minutes."
    return None, ""


# ── Reminders ───────────────────────────────────────────────────────────────
def _reminder_create(a: dict, _c) -> Result:
    text = (a.get("text") or "").strip()
    if not text:
        return Result.fail("What should the reminder say?")
    when, why = _resolve_when(a)
    if why:
        return Result.fail(why)
    list_name = (a.get("list") or "").strip()

    props = [f'name:"{_as_literal(text)}"']
    if when is not None:
        offset = int(round(when - time.time()))
        props.append(f"remind me date:((current date) + {offset})")
    if a.get("notes"):
        props.append(f'body:"{_as_literal(a["notes"])}"')
    target = (f'list "{_as_literal(list_name)}"' if list_name else "default list")
    script = (
        'tell application "Reminders"\n'
        f"  make new reminder at end of {target} with properties "
        "{" + ", ".join(props) + "}\n"
        "end tell"
    )
    ok, out = _osa(script)
    if not ok:
        if list_name and "Can’t get list" in out or "can't get list" in out.lower():
            return Result.fail(f"There's no Reminders list called '{list_name}'.")
        return _permission_failure("Reminders", out)
    if when is None:
        return Result.say(f"Added '{text}' to Reminders.")
    at = time.strftime("%-I:%M %p on %A", time.localtime(when))
    return Result.say(f"Added '{text}' to Reminders for {at}.",
                      data={"text": text, "due": when})


def _reminders_list(a: dict, _c) -> Result:
    list_name = (a.get("list") or "").strip()
    target = f'list "{_as_literal(list_name)}"' if list_name else "default list"
    # A tab-separated single string is returned rather than an AppleScript list,
    # because AppleScript's own list formatting is not stable across versions.
    script = (
        'set out to ""\n'
        'tell application "Reminders"\n'
        f"  repeat with r in (every reminder of {target} whose completed is false)\n"
        '    set out to out & (name of r) & "\\t"\n'
        "  end repeat\n"
        "end tell\n"
        "return out"
    )
    ok, out = _osa(script, timeout=30)
    if not ok:
        return _permission_failure("Reminders", out)
    items = [x.strip() for x in out.split("\t") if x.strip()]
    if not items:
        where = f" in {list_name}" if list_name else ""
        return Result.say(f"Nothing outstanding in Reminders{where}.")
    limit = max(1, min(int(a.get("limit") or 15), 60))
    shown = items[:limit]
    return Result.say(
        f"{len(items)} open {'reminder' if len(items) == 1 else 'reminders'}. "
        f"First: {shown[0]}.",
        detail="\n".join(f"- {x}" for x in shown), data=items,
    )


# ── Calendar ────────────────────────────────────────────────────────────────
def _calendar_events(a: dict, _c) -> Result:
    days = max(1, min(int(a.get("days") or 1), 14))
    # Offsets, not formatted dates: the script returns seconds from midnight and
    # Python does the formatting, so nothing depends on the machine's locale.
    script = (
        "set d1 to (current date)\n"
        "set time of d1 to 0\n"
        f"set d2 to d1 + ({days} * days)\n"
        'set out to ""\n'
        'tell application "Calendar"\n'
        "  repeat with c in calendars\n"
        "    repeat with e in (every event of c whose start date >= d1 and start date < d2)\n"
        '      set out to out & (summary of e) & "\\t" & ((start date of e) - d1) & "\\t" '
        '& ((end date of e) - (start date of e)) & "\\t" & (name of c) & "\\n"\n'
        "    end repeat\n"
        "  end repeat\n"
        "end tell\n"
        "return out"
    )
    ok, out = _osa(script, timeout=90)   # Calendar is genuinely slow to enumerate
    if not ok:
        return _permission_failure("Calendar", out)

    midnight = _midnight_today()
    events = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        title, offset, length, cal = parts[0], parts[1], parts[2], parts[3]
        try:
            start = midnight + float(offset)
            minutes = float(length) / 60
        except ValueError:
            continue
        events.append({"title": title.strip(), "start": start,
                       "minutes": round(minutes), "calendar": cal.strip()})
    events.sort(key=lambda e: e["start"])
    if not events:
        span = "today" if days == 1 else f"the next {days} days"
        return Result.say(f"Nothing on the calendar for {span}.")

    now = time.time()
    lines = []
    for e in events:
        when = time.strftime("%a %-I:%M %p", time.localtime(e["start"]))
        dur = f"{e['minutes']}m" if e["minutes"] < 90 else f"{e['minutes']/60:.1f}h"
        lines.append(f"{when:<16} {dur:>6}  {e['title']}   [{e['calendar']}]")
    upcoming = [e for e in events if e["start"] >= now]
    if upcoming:
        nxt = upcoming[0]
        mins = (nxt["start"] - now) / 60
        soon = (f"in {round(mins)} minutes" if mins < 90
                else time.strftime("at %-I:%M %p", time.localtime(nxt["start"])))
        speech = (f"{len(events)} {'event' if len(events) == 1 else 'events'}. "
                  f"Next is {nxt['title']} {soon}.")
    else:
        speech = f"{len(events)} {'event' if len(events) == 1 else 'events'}, all now past."
    return Result.say(speech, detail="\n".join(lines), data=events)


def _calendar_create(a: dict, _c) -> Result:
    title = (a.get("title") or "").strip()
    if not title:
        return Result.fail("What's the event called?")
    when, why = _resolve_when(a)
    if why:
        return Result.fail(why)
    if when is None:
        return Result.fail("When is it? Give a time like '3pm' or 'in 2 hours'.")
    minutes = a.get("duration_minutes")
    try:
        minutes = max(5, min(int(minutes or 60), 24 * 60))
    except (TypeError, ValueError):
        return Result.fail("That isn't a number of minutes.")
    offset = int(round(when - time.time()))
    cal = (a.get("calendar") or "").strip()
    target = f'calendar "{_as_literal(cal)}"' if cal else "first calendar"
    props = [f'summary:"{_as_literal(title)}"',
             f"start date:((current date) + {offset})",
             f"end date:((current date) + {offset + minutes * 60})"]
    if a.get("location"):
        props.append(f'location:"{_as_literal(a["location"])}"')
    script = ('tell application "Calendar"\n'
              f"  make new event at end of events of {target} with properties "
              "{" + ", ".join(props) + "}\n"
              "end tell")
    ok, out = _osa(script, timeout=45)
    if not ok:
        if cal and "calendar" in out.lower() and "get" in out.lower():
            return Result.fail(f"There's no calendar called '{cal}'.")
        return _permission_failure("Calendar", out)
    at = time.strftime("%-I:%M %p on %A", time.localtime(when))
    return Result.say(f"'{title}' is on the calendar for {at}, {minutes} minutes.",
                      data={"title": title, "start": when, "minutes": minutes})


# ── Notes ───────────────────────────────────────────────────────────────────
def _apple_note(a: dict, _c) -> Result:
    title = (a.get("title") or "").strip()
    body = (a.get("body") or "").strip()
    if not title and not body:
        return Result.fail("What should the note say?")
    if not title:
        title = body.split(".")[0][:60]
    # Notes renders its body as HTML, so the title is repeated as a heading and
    # newlines become breaks — otherwise the note arrives as one run-on line.
    html_body = "<div><b>" + _as_literal(title) + "</b></div>"
    for line in (a.get("body") or "").splitlines():
        html_body += "<div>" + _as_literal(line) + "</div>"
    script = ('tell application "Notes"\n'
              f'  make new note at folder "Notes" with properties '
              f'{{name:"{_as_literal(title)}", body:"{html_body}"}}\n'
              "end tell")
    ok, out = _osa(script, timeout=30)
    if not ok:
        # Some accounts have no folder literally named "Notes"; retry at the root.
        script2 = ('tell application "Notes"\n'
                   f'  make new note with properties '
                   f'{{name:"{_as_literal(title)}", body:"{html_body}"}}\n'
                   "end tell")
        ok2, out2 = _osa(script2, timeout=30)
        if not ok2:
            return _permission_failure("Notes", out or out2)
    return Result.say(f"Note '{title}' saved to Notes.")


def skills() -> list[Skill]:
    return [
        Skill(
            "reminder_create",
            "Add a reminder to the macOS Reminders app, optionally with a due time.",
            _reminder_create,
            parameters={
                "text": {"type": "string", "required": True},
                "at": {"type": "string", "description": "clock time, e.g. '6pm'"},
                "in": {"type": "string", "description": "delay, e.g. '2 hours'"},
                "minutes": {"type": "number"},
                "list": {"type": "string"},
                "notes": {"type": "string"},
            },
            risk=Risk.REVERSIBLE,
        ),
        Skill(
            "reminders_list", "Read outstanding items from the Reminders app.",
            _reminders_list,
            parameters={"list": {"type": "string"}, "limit": {"type": "integer"}},
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "calendar_events", "Read calendar events for today or the next few days.",
            _calendar_events, parameters={"days": {"type": "integer"}}, risk=Risk.READ_ONLY,
        ),
        Skill(
            "calendar_create", "Create a calendar event.", _calendar_create,
            parameters={
                "title": {"type": "string", "required": True},
                "at": {"type": "string"}, "in": {"type": "string"},
                "duration_minutes": {"type": "integer"},
                "calendar": {"type": "string"}, "location": {"type": "string"},
            },
            risk=Risk.REVERSIBLE,
        ),
        Skill(
            "apple_note", "Save a note into the macOS Notes app.", _apple_note,
            parameters={"title": {"type": "string"}, "body": {"type": "string"}},
            risk=Risk.REVERSIBLE,
        ),
    ]
