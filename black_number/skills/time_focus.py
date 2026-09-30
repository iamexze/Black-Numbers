"""Time skills — timers, reminders, focus sessions, stopwatch.

These are the skills that made assistants useful in the first place: "remind me
in ten minutes" is the request everything else is judged against. They all run on
the one background scheduler (core/scheduler), so there is a single clock to
reason about and a single place a pending job can be listed or cancelled.

Setting a timer is REVERSIBLE, not read-only: it schedules something that will
speak to you later, which is a change to the world, and it can be cancelled.
Cancelling someone's reminder is MUTATING, because the information is gone.
"""

from __future__ import annotations

import re
import time

from ..core.scheduler import human_delta
from .base import Context, Result, Risk, Skill

# The stopwatch is the one piece of genuinely session-local state in the system:
# a running clock means nothing after a restart, so it is deliberately not
# persisted. Keyed by name so several can run at once.
_STOPWATCH: dict[str, float] = {}

_UNITS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
}

# Spoken quantities. Articles count as one ("a minute"), and fractions are real
# values, because "half an hour" is how people actually say 1800 seconds.
_WORD_NUM = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "ninety": 90, "half": 0.5, "quarter": 0.25,
}

# Words that carry no quantity and are skipped rather than guessed at.
_FILLER = {"of", "and", "for", "in", "the", "about", "another", "next", "plus"}

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_TOKEN = re.compile(r"\d+(?:\.\d+)?|[a-z]+")
# "an hour and a half" is one quantity plus half of the same unit, so the unit
# is duplicated before tokenising rather than special-cased in the scan.
_AND_A_HALF = re.compile(
    r"\b(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\s+and\s+(?:a\s+)?half\b"
)


def parse_duration(text: str) -> float | None:
    """'90', '10 min', '1h30m', 'half an hour', 'an hour and a half' → seconds.

    A bare number means minutes, which is what someone setting a timer means by
    "set a timer for twenty". Returns None when nothing quantitative is found,
    so a caller can say so instead of inventing a duration.
    """
    t = text.strip().lower()
    if not t:
        return None
    t = _AND_A_HALF.sub(lambda m: f"{m.group(1)} 0.5 {m.group(1)}", t)
    tokens = _TOKEN.findall(t)

    total = 0.0
    found = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _NUMBER.fullmatch(tok):
            value = float(tok)
        elif tok in _WORD_NUM:
            value = float(_WORD_NUM[tok])
        elif tok in _UNITS:
            total += _UNITS[tok]          # "a minute" said as just "minute"
            found = True
            i += 1
            continue
        else:
            i += 1                        # filler or noise: ignore, never guess
            continue
        # Look past filler and articles for the unit this value belongs to.
        j = i + 1
        while j < len(tokens) and (tokens[j] in _FILLER or tokens[j] in ("a", "an")):
            j += 1
        if j < len(tokens) and tokens[j] in _UNITS:
            total += value * _UNITS[tokens[j]]
            i = j + 1
        else:
            total += value * 60           # bare number: minutes
            i += 1
        found = True
    return total if found and total > 0 else None


def parse_clock_time(text: str, now: float | None = None) -> float | None:
    """'7:30pm', '19:30', '8am' → the next epoch time that reads like that.

    Always resolves forward: asking at 9pm for '8am' means tomorrow morning, not
    a time that has already gone.
    """
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text.strip().lower())
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if minute > 59:
        return None
    if ampm:
        if hour < 1 or hour > 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif hour > 23:
        return None
    base = time.localtime(now if now is not None else time.time())
    target = list(base)
    target[3], target[4], target[5] = hour, minute, 0
    target[8] = -1                      # let mktime resolve DST for the target date
    when = time.mktime(tuple(target))   # type: ignore[arg-type]
    if when <= (now if now is not None else time.time()):
        when += 86400                   # already past today, so tomorrow
    return when


def _when_from_args(a: dict) -> tuple[float | None, str]:
    """Resolve the several ways a caller can express a time. Returns (epoch, why)."""
    if a.get("at"):
        when = parse_clock_time(str(a["at"]))
        return (when, "" if when else f"I couldn't read '{a['at']}' as a time of day.")
    raw = a.get("duration") or a.get("minutes") or a.get("seconds")
    if raw in (None, ""):
        return (None, "How long?")
    if a.get("seconds") is not None and a.get("duration") is None and a.get("minutes") is None:
        try:
            return (time.time() + float(a["seconds"]), "")
        except (TypeError, ValueError):
            return (None, "That isn't a number of seconds.")
    if a.get("minutes") is not None and a.get("duration") is None:
        try:
            return (time.time() + float(a["minutes"]) * 60, "")
        except (TypeError, ValueError):
            return (None, "That isn't a number of minutes.")
    secs = parse_duration(str(raw))
    return (time.time() + secs, "") if secs else (None, f"I couldn't read '{raw}' as a duration.")


def _require_scheduler(ctx: Context) -> Result | None:
    if getattr(ctx, "scheduler", None) is None:
        return Result.fail("My scheduler isn't running, so I can't hold a timer for you.")
    return None


# ── timers and reminders ────────────────────────────────────────────────────
def _set_timer(a: dict, ctx: Context) -> Result:
    if (bad := _require_scheduler(ctx)) is not None:
        return bad
    when, why = _when_from_args(a)
    if when is None:
        return Result.fail(why)
    label = (a.get("label") or "timer").strip()
    job = ctx.scheduler.add(label, when, kind="timer")
    return Result.say(
        f"Timer set: {label}, {human_delta(when - time.time())}.",
        detail=f"{job.id}  {label}  fires at {time.strftime('%-I:%M:%S %p', time.localtime(when))}",
        data={"id": job.id, "due": when},
    )


def _set_reminder(a: dict, ctx: Context) -> Result:
    if (bad := _require_scheduler(ctx)) is not None:
        return bad
    text = (a.get("text") or a.get("label") or "").strip()
    if not text:
        return Result.fail("Remind you of what?")
    when, why = _when_from_args(a)
    if when is None:
        return Result.fail(why)
    job = ctx.scheduler.add(text, when, kind="reminder")
    at = time.strftime("%-I:%M %p", time.localtime(when))
    return Result.say(
        f"I'll remind you {human_delta(when - time.time())}, at {at}.",
        detail=f"{job.id}  {text}  {at}",
        data={"id": job.id, "due": when},
    )


def _list_timers(_a, ctx: Context) -> Result:
    if (bad := _require_scheduler(ctx)) is not None:
        return bad
    jobs = ctx.scheduler.pending()
    if not jobs:
        return Result.say("Nothing pending.")
    now = time.time()
    detail = "\n".join(f"{j.kind:8} {j.describe(now)}" for j in jobs)
    nxt = jobs[0]
    return Result.say(
        f"{len(jobs)} pending. Next is {nxt.label}, {human_delta(nxt.remaining(now))}.",
        detail=detail,
        data=[{"id": j.id, "label": j.label, "due": j.due, "kind": j.kind} for j in jobs],
    )


def _cancel_timer(a: dict, ctx: Context) -> Result:
    if (bad := _require_scheduler(ctx)) is not None:
        return bad
    which = str(a.get("id", "")).strip()
    if which in ("all", "*"):
        n = ctx.scheduler.cancel_all()
        return Result.say(f"Cleared {n} pending {'item' if n == 1 else 'items'}.")
    if not which:
        return Result.fail("Cancel which one? Give an id, or 'all'.")
    job = ctx.scheduler.cancel(which if which.startswith("t") else f"t{which}")
    if job is None:
        return Result.fail(f"I have nothing pending with id {which}.")
    return Result.say(f"Cancelled {job.label}.")


# ── focus sessions ──────────────────────────────────────────────────────────
def _focus_session(a: dict, ctx: Context) -> Result:
    """A work/break cycle, scheduled in full up front.

    Every boundary is queued immediately rather than each one scheduling the
    next, so the whole session is visible in `list_timers` and cancellable, and a
    missed callback cannot silently end the sequence.
    """
    if (bad := _require_scheduler(ctx)) is not None:
        return bad
    work = int(a.get("work_minutes") or 25)
    brk = int(a.get("break_minutes") or 5)
    rounds = max(1, min(int(a.get("rounds") or 4), 12))
    task = (a.get("task") or "").strip()
    if work < 1 or brk < 0:
        return Result.fail("Give me a work block of at least a minute.")

    t = time.time()
    marks = []
    for i in range(1, rounds + 1):
        t += work * 60
        last = i == rounds
        marks.append(ctx.scheduler.add(
            f"Round {i} of {rounds} done" + (" — session complete" if last else ". Break."),
            t, kind="focus", payload={"round": i, "phase": "work"},
        ))
        if not last and brk:
            t += brk * 60
            marks.append(ctx.scheduler.add(
                f"Break over. Round {i + 1} of {rounds}.", t,
                kind="focus", payload={"round": i + 1, "phase": "break"},
            ))
    total = (t - time.time()) / 60
    head = f"Focus session started: {rounds} × {work} minutes"
    if task:
        head += f" on {task}"
    return Result.say(
        f"{head}. I'll call each boundary. Done in about {round(total)} minutes.",
        detail="\n".join(m.describe() for m in marks),
        data={"ids": [m.id for m in marks]},
    )


# ── stopwatch ───────────────────────────────────────────────────────────────
def _stopwatch(a: dict, _c) -> Result:
    action = str(a.get("action") or "").strip().lower()
    name = (a.get("name") or "default").strip() or "default"
    if action in ("", "start") and name not in _STOPWATCH:
        action = "start"
    elif action == "":
        action = "read"

    if action == "start":
        _STOPWATCH[name] = time.time()
        return Result.say(f"Stopwatch {name} running.")
    if name not in _STOPWATCH:
        return Result.fail(f"No stopwatch called {name} is running.")
    elapsed = time.time() - _STOPWATCH[name]
    pretty = human_delta(-elapsed).replace(" ago", "")
    if action in ("stop", "reset"):
        del _STOPWATCH[name]
        return Result.say(f"Stopped at {pretty}.", data={"seconds": round(elapsed, 2)})
    return Result.say(f"{pretty} so far.", data={"seconds": round(elapsed, 2)})


def skills() -> list[Skill]:
    return [
        Skill(
            "set_timer",
            "Set a timer that speaks when it finishes. Accepts a duration "
            "('10 min', '1h30m'), plain minutes, or a clock time via `at`.",
            _set_timer,
            parameters={
                "duration": {"type": "string", "description": "e.g. '10 min', '90s', '1h30m'"},
                "minutes": {"type": "number"},
                "seconds": {"type": "number"},
                "at": {"type": "string", "description": "clock time, e.g. '7:30pm'"},
                "label": {"type": "string"},
            },
            risk=Risk.REVERSIBLE,
        ),
        Skill(
            "set_reminder",
            "Remind the user of something at a time or after a delay.",
            _set_reminder,
            parameters={
                "text": {"type": "string", "required": True},
                "duration": {"type": "string"},
                "minutes": {"type": "number"},
                "at": {"type": "string"},
            },
            risk=Risk.REVERSIBLE,
        ),
        Skill("list_timers", "List pending timers, reminders and focus blocks.",
              _list_timers, risk=Risk.READ_ONLY),
        Skill(
            "cancel_timer", "Cancel a pending timer or reminder by id, or 'all'.",
            _cancel_timer,
            parameters={"id": {"type": "string", "required": True}},
            risk=Risk.MUTATING,
            caution="a cancelled reminder is gone",
        ),
        Skill(
            "focus_session",
            "Start a work/break focus cycle, announcing each boundary aloud.",
            _focus_session,
            parameters={
                "work_minutes": {"type": "integer"},
                "break_minutes": {"type": "integer"},
                "rounds": {"type": "integer"},
                "task": {"type": "string"},
            },
            risk=Risk.REVERSIBLE,
        ),
        Skill(
            "stopwatch", "Start, read or stop a stopwatch.", _stopwatch,
            parameters={
                "action": {"type": "string", "enum": ["start", "read", "stop", "reset"]},
                "name": {"type": "string"},
            },
            risk=Risk.READ_ONLY,
        ),
    ]
