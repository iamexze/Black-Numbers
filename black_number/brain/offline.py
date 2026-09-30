"""The offline brain — deterministic intent matching, no model required.

This is what runs on a fresh clone with no API key, and the permanent fallback
when the network or the model is unavailable. It cannot hold a conversation, but
it maps plain phrasings onto the right tool reliably, so every skill is usable
from the first minute. Think of it as the reflexes; the language model is the
cortex.

It stays a transparent table of patterns rather than becoming a parser. Two
properties keep that table honest as it grows:

  A rule may decline. A builder that returns None means "this looked like my
  pattern but the content says otherwise", and matching continues with the next
  rule. That is what lets `what's 15% of 200` reach the calculator while
  `what's the time` reaches the clock, without either rule needing to know about
  the other.

  Order is meaning. The first match wins, so specific rules precede general
  ones — `start a focus session` is listed above `start <app>`, because
  otherwise it would open an application called "a focus session".
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from .base import Brain, ToolCall, Turn

# ── shared fragments ────────────────────────────────────────────────────────
# Commands trusted enough that a bare `git status` is read as a shell request.
SHELL_COMMANDS = {
    "git", "ls", "cat", "grep", "find", "npm", "npx", "pnpm", "yarn", "pip", "pip3",
    "python", "python3", "pytest", "brew", "docker", "kubectl", "make", "curl",
    "node", "deno", "cargo", "go", "ruff", "df", "du", "ps", "top", "uname",
    "whoami", "uptime", "which", "head", "tail", "wc", "awk", "sed", "sw_vers",
}
# A command word followed by one of these is English, not a shell invocation:
# "find my keys" and "make me a coffee" are not commands.
NOT_COMMAND_NEXT = {"me", "my", "a", "an", "the", "us", "him", "her", "them", "it", "out", "sure"}

_NUMWORD = r"\d+(?:\.\d+)?|a|an|half|quarter|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|ninety"
_UNITWORD = r"second|sec|minute|min|hour|hr|day|week"
_AT_RX = re.compile(r"\bat\s+(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)\b", re.I)
_IN_RX = re.compile(rf"\bin\s+((?:{_NUMWORD})[\w\s./-]*?(?:{_UNITWORD})s?)\b", re.I)
_FOR_RX = re.compile(rf"\b(?:for|of)\s+((?:{_NUMWORD})[\w\s./-]*?(?:{_UNITWORD})s?)\b", re.I)


def _strip_lead(text: str) -> str:
    return re.sub(r"^\s*(?:to|that|about|me)\s+", "", text.strip(), flags=re.I).strip(" ,.?!")


# ── builders that may decline by returning None ─────────────────────────────
def _shell_if_command(m) -> dict | None:
    """Accept `run git status`, decline `run your tests`."""
    body = m.group(1).strip()
    tokens = body.split()
    if not tokens or tokens[0].lower() not in SHELL_COMMANDS:
        return None
    if len(tokens) > 1 and tokens[1].lower() in NOT_COMMAND_NEXT:
        return None
    return {"command": body}


def _bare_command(m) -> dict | None:
    head, rest = m.group(1).lower(), (m.group(2) or "").strip()
    if head not in SHELL_COMMANDS:
        return None
    first = rest.split()[0].lower() if rest else ""
    if first in NOT_COMMAND_NEXT:
        return None
    return {"command": f"{head} {rest}".strip()}


def _timed(text: str) -> dict:
    """Pull a time out of a phrase: 'in 20 minutes' or 'at 7pm'."""
    args: dict[str, Any] = {}
    rest = text
    if (dur := _IN_RX.search(rest)) is not None:
        args["duration"] = dur.group(1).strip()
        rest = rest[:dur.start()] + " " + rest[dur.end():]
    elif (at := _AT_RX.search(rest)) is not None:
        args["at"] = at.group(1).strip()
        rest = rest[:at.start()] + " " + rest[at.end():]
    elif (dur2 := _FOR_RX.search(rest)) is not None:
        args["duration"] = dur2.group(1).strip()
        rest = rest[:dur2.start()] + " " + rest[dur2.end():]
    args["_text"] = _strip_lead(rest)
    return args


def _reminder(m) -> dict | None:
    parsed = _timed((m.group(1) or "").strip())
    text = parsed.pop("_text")
    if not text:
        return None
    # No time given is fine: the skill asks rather than inventing one.
    return {"text": text, **parsed}


def _apple_reminder(m) -> dict | None:
    got = _reminder(m)
    return got if got else None


def _timer(m) -> dict:
    parsed = _timed((m.group(1) or "").strip())
    label = parsed.pop("_text")
    args = {k: v for k, v in parsed.items() if k in ("duration", "at")}
    if label and len(label) < 40:
        args["label"] = label
    return args


def _timer_prefix(m) -> dict | None:
    """'set a 10 minute timer' — the duration sits before the word timer.

    Declines when what precedes "timer" is not a duration, so "set a timer for
    ten minutes" falls through to the rule that reads the trailing phrase
    instead of parsing the article "a" as one minute.
    """
    from ..skills.time_focus import parse_duration

    raw = (m.group(1) or "").strip()
    if not raw or raw.lower() in ("a", "an", "the", "new", "another"):
        return None
    return {"duration": raw} if parse_duration(raw) else None


def _focus(m) -> dict:
    text = m.string          # the whole utterance: "pomodoro for 50 minutes"
    args: dict[str, Any] = {}
    if (mins := re.search(rf"\b(\d+)\s*(?:{_UNITWORD})s?\b", text, re.I)) is not None:
        args["work_minutes"] = int(mins.group(1))
    if (rounds := re.search(r"\b(\d+)\s*(?:rounds?|cycles?|blocks?)\b", text, re.I)) is not None:
        args["rounds"] = int(rounds.group(1))
    return args


def _calc(m) -> dict | None:
    """Only claim an utterance that is actually arithmetic."""
    expr = (m.group(1) or "").strip().rstrip("?").strip()
    if not expr or not re.search(r"\d", expr):
        return None
    arithmetic = re.search(
        r"[+\-*/%^]|\b(?:plus|minus|times|divided|over|percent|sqrt|log|squared|cubed)\b"
        r"|\d\s*x\s*\d|\d\s*%", expr, re.I)
    if not arithmetic:
        return None
    if re.search(r"\b(?:time|date|day|weather|temperature)\b", expr, re.I):
        return None
    return {"expression": expr}


def _convert(m) -> dict | None:
    """Accept '12 miles to km' only when both units are ones we actually know."""
    from ..skills.world import TEMPS, UNITS, normalise_unit

    value, src, dst = m.group(1), m.group(2), m.group(3)
    a, b = normalise_unit(src), normalise_unit(dst)
    known = set(UNITS) | TEMPS
    if a not in known or b not in known:
        return None
    return {"value": float(value), "from": src, "to": dst}


def _define(m) -> dict | None:
    word = (m.group(1) or "").strip().rstrip("?").strip()
    if not word or not re.fullmatch(r"[A-Za-z][A-Za-z'\- ]{1,40}", word):
        return None
    if len(word.split()) > 3:
        return None
    return {"word": word}


def _weather(m) -> dict:
    where = re.search(r"\b(?:in|for|at)\s+([A-Za-z][A-Za-z\s.'-]{1,40})$",
                      m.string.strip().rstrip("?"), re.I)
    return {"location": where.group(1).strip()} if where else {}


def _news(m) -> dict:
    topic = re.search(r"\b(?:about|on|regarding)\s+(.+)$", m.string.strip().rstrip("?"), re.I)
    return {"topic": topic.group(1).strip()} if topic else {}


def _protocol(m) -> dict:
    return {"name": m.group(1).strip()}


def _wifi(m) -> dict:
    return {"state": m.group(1).lower()}


def _dark(m) -> dict:
    state = (m.group(1) or "").lower()
    return {"state": state if state in ("on", "off") else "toggle"}


def _port(m) -> dict:
    return {"port": int(m.group(1))}


def _media(action: str):
    return lambda m: {"action": action}


def _note_add(m) -> dict | None:
    text = _strip_lead(m.group(1) or "")
    return {"text": text} if text else None


class OfflineBrain(Brain):
    name = "offline"

    # (regex, tool_name, arg_builder). First match whose builder returns a dict
    # wins; a builder returning None declines and matching continues.
    RULES: list[tuple[str, str, Any]] = [
        # ── shell, first so an explicit command beats any keyword inside it ──
        (r"^\s*(?:run|shell|exec)\s*:\s*(.+)", "run_shell", lambda m: {"command": m.group(1).strip()}),
        (r"^\s*(?:run|shell|exec)\s+(.+)", "run_shell", _shell_if_command),

        # ── the assistant reasoning about itself ─────────────────────────────
        (r"\b(?:run\s+)?your\s+tests\b|\bself.?test\b|\bare you (?:ok|working|healthy)\b",
         "self_test", lambda m: {}),
        (r"\bdiagnose\b|\bself.?diagnos\w*\b|\bwhat(?:'s| is) going wrong\b|"
         r"\bwhat(?:'s| is) been failing\b", "self_diagnose", lambda m: {}),
        (r"\b(?:your )?metrics\b|\bhow (?:much|often) (?:have|do) you\b|\byour usage\b",
         "self_metrics", lambda m: {}),
        (r"\bwhat have you learned\b|\byour lessons\b|\bwhat lessons\b",
         "self_lessons", lambda m: {}),
        (r"\b(?:learn|lesson)\s*:\s*(.+)", "self_lesson", lambda m: {"lesson": m.group(1).strip()}),
        (r"\b(?:remember to always|from now on)\s+(.+)", "self_lesson",
         lambda m: {"lesson": m.group(1).strip()}),
        (r"\b(?:your )?inventory\b|\bwhat skills\b|\bskills have you never\b|\bcapability gap\b",
         "self_inventory", lambda m: {}),

        # ── protocols, before the generic open/start rules ──────────────────
        (r"\b(?:define|teach|create|save|learn)\s+(?:a\s+)?(?:new\s+)?protocol\s+"
         # Longest alternative first: with "with|steps" ordered the other way,
         # "with steps: clock" leaves the word "steps:" inside the captured steps.
         r"(?:called\s+|named\s+)?([a-z_][\w]*)\s+(?:as\s+steps?|with\s+steps?|as|with|steps?)"
         r"[:\s]+(.+)",
         "protocol_define", lambda m: {"name": m.group(1), "steps": m.group(2).strip()}),
        (r"\b(?:run|start|execute|initiate|engage|activate|begin)\s+(?:the\s+)?([a-z_][\w]*)\s+protocol\b",
         "protocol_run", _protocol),
        (r"\bprotocol\s+([a-z_][\w]*)\b", "protocol_run", _protocol),
        (r"\b(?:list|show|which|what)\b[^.]*\bprotocols\b", "protocol_list", lambda m: {}),
        (r"\bprotocols\b", "protocol_list", lambda m: {}),

        # ── time: timers, reminders, focus, stopwatch ────────────────────────
        (r"\b(?:start|stop|reset)\s+(?:the\s+|a\s+)?stopwatch\b", "stopwatch",
         lambda m: {"action": m.group(0).strip().split()[0].lower()}),
        (r"\bstopwatch\b", "stopwatch", lambda m: {"action": "read"}),
        (r"\b(?:focus|pomodoro|deep work)\s*(?:session|block|mode|time)?\b", "focus_session", _focus),
        (r"\b(?:set|start|put on|give me)\s+(?:a|an)?\s*([\w\s.-]+?)\s+timer\b",
         "set_timer", _timer_prefix),
        (r"\b(?:set|start|put on|give me)\s+(?:a|an)?\s*timer\b(.*)", "set_timer", _timer),
        (r"\btimer\b(.*)", "set_timer", _timer),
        (r"\badd\s+(?:a\s+)?reminder\b(.*)", "reminder_create", _apple_reminder),
        (r"\bremind me\b(.*)", "set_reminder", _reminder),
        (r"\b(?:list|show|what(?:'s| is) on)\s+(?:my\s+)?(?:pending\s+)?(?:timers?|reminders? list)\b|"
         r"\bwhat(?:'s| is) pending\b|\bpending timers?\b", "list_timers", lambda m: {}),
        (r"\bcancel\s+(?:the\s+)?(?:timer|reminder)s?\s*(\w+)?\b", "cancel_timer",
         lambda m: {"id": (m.group(1) or "all")}),
        (r"\bcancel (?:all|everything)\b", "cancel_timer", lambda m: {"id": "all"}),

        # ── calendar and the Reminders app ──────────────────────────────────
        (r"\bwhat(?:'s| is) (?:on )?(?:my |the )?(?:calendar|schedule|agenda)\b|"
         r"\bmy (?:calendar|schedule|agenda)\b|\btoday(?:'s)? events\b|"
         r"\bwhat(?:'s| is) (?:next|coming up)\b", "calendar_events", lambda m: {}),
        (r"\b(?:my |open )?reminders\b", "reminders_list", lambda m: {}),

        # ── weather and news ────────────────────────────────────────────────
        (r"\bforecast\b|\bweather\s+(?:this week|tomorrow|for the week)\b", "forecast", _weather),
        (r"\bweather\b|\bis it (?:raining|sunny|hot|cold)\b|\btemperature outside\b|"
         r"\bhow (?:warm|cold|hot) is it\b", "weather", _weather),
        (r"\b(?:news|headlines|what(?:'s| is) happening in the world)\b", "news_briefing", _news),

        # ── arithmetic, units, definitions ──────────────────────────────────
        (r"\b(?:convert\s+)?(-?[\d.]+)\s*([A-Za-z°/^0-9]+)\s+(?:to|in|into|as)\s+([A-Za-z°/^0-9]+)\b",
         "convert_units", _convert),
        (r"\b(?:calculate|compute|work out|evaluate)\s+(.+)", "calculate", _calc),
        (r"\b(?:what(?:'s| is)|how much is|how many is)\s+(.+)", "calculate", _calc),
        (r"\b(?:define|definition of)\s+(.+)", "define", _define),
        (r"\bwhat does\s+(.+?)\s+mean\b", "define", _define),

        # ── network ─────────────────────────────────────────────────────────
        (r"\bping\s+([A-Za-z0-9._:-]+)", "ping_host", lambda m: {"host": m.group(1)}),
        (r"\b(?:public|external)\s+ip\b", "public_ip", lambda m: {}),
        (r"\b(?:speed ?test|how fast is my (?:internet|connection)|throughput|bandwidth)\b",
         "throughput_test", lambda m: {}),
        (r"\b(?:what(?:'s| is)\s+)?(?:running|listening)?\s*on port (\d+)\b", "port_check", _port),
        (r"\bport (\d+)\b", "port_check", _port),
        (r"\b(?:turn\s+)?(?:the\s+)?wi-?fi\s+(on|off)\b", "wifi_power", _wifi),
        (r"\b(?:am i|are we)\s+(?:online|connected)\b|\bnetwork status\b|\bmy ip\b|"
         r"\bip address\b|\bwi-?fi status\b|\bis the internet (?:up|working)\b",
         "network_status", lambda m: {}),
        (r"\b(?:resolve|dns(?: lookup)?)\s+([A-Za-z0-9.-]+)", "dns_lookup",
         lambda m: {"host": m.group(1)}),

        # ── clipboard and notes ─────────────────────────────────────────────
        (r"\b(?:what(?:'s| is) (?:on|in) (?:my|the) clipboard|read (?:my|the) clipboard|"
         r"paste)\b", "clipboard_read", lambda m: {}),
        (r"\bsave (?:my |the )?clipboard\b|\bkeep (?:this|that) clipping\b",
         "note_clipboard", lambda m: {}),
        (r"\bcopy\s+(.+?)\s+to (?:the )?clipboard\b", "clipboard_write",
         lambda m: {"text": m.group(1).strip()}),
        (r"\bsearch (?:my )?notes?\s+(?:for\s+)?(.+)", "note_search",
         lambda m: {"query": m.group(1).strip().rstrip("?")}),
        (r"\b(?:read|show|list)\s+(?:my |the )?notes?\b", "note_read", lambda m: {}),
        (r"\bnotebooks?\b", "note_books", lambda m: {}),
        (r"\b(?:note|jot)\s+(?:down\s+|that\s+)?(.+)", "note_add", _note_add),

        # ── media and appearance ───────────────────────────────────────────
        (r"\bwhat(?:'s| is) (?:playing|this song)\b|\bnow playing\b", "now_playing", lambda m: {}),
        (r"\b(?:next|skip)(?: track| song)?\b", "media_control", _media("next")),
        (r"\b(?:previous|last)(?: track| song)\b|\bgo back a track\b", "media_control",
         _media("previous")),
        (r"\b(?:pause|resume|play)\b(?!\s*\w*list)", "media_control", _media("play_pause")),
        (r"\bdark mode\s*(on|off)?\b|\bturn (on|off) dark mode\b", "dark_mode", _dark),
        (r"\block (?:the )?(?:screen|mac|computer)\b|\block up\b", "lock_screen", lambda m: {}),
        (r"\b(?:sleep|turn off) (?:the )?(?:display|screen)\b", "sleep_display", lambda m: {}),
        (r"\b(?:keep|stay) (?:it |the mac )?awake\b|\bdon'?t (?:let it )?sleep\b",
         "keep_awake", lambda m: {}),
        (r"\b(?:do not disturb|dnd)\b", "do_not_disturb", lambda m: {}),
        (r"\bset (?:the )?wallpaper (?:to )?(.+)", "set_wallpaper",
         lambda m: {"path": m.group(1).strip()}),

        # ── machine and repositories ────────────────────────────────────────
        (r"\bgit status\b|\bwhat(?:'s| is) (?:dirty|uncommitted|changed in the repo)\b|"
         r"\brepo status\b", "git_status", lambda m: {}),
        (r"\bgit log\b|\brecent commits\b|\blast commits?\b", "git_log", lambda m: {}),
        (r"\b(?:machine|system) health\b|\bhow(?:'s| is) the (?:machine|mac|system)\b|"
         r"\b(?:cpu|load average|ram usage|memory usage)\b|\bwhat(?:'s| is) using (?:my )?cpu\b",
         "machine_health", lambda m: {}),
        (r"\bscan (?:the )?project\b|\bwhat(?:'s| is) (?:in )?this project\b|\bproject scan\b",
         "project_scan", lambda m: {}),
        (r"\brun (?:the )?tests\b|\btest the project\b", "run_tests", lambda m: {}),
        (r"\b(?:search|grep) (?:the )?code (?:for )?(.+)", "code_search",
         lambda m: {"pattern": m.group(1).strip()}),
        (r"\bdisk (?:usage|space)\b|\bhow (?:big|much space)\b", "disk_usage", lambda m: {}),
        (r"\b(?:open|edit) (.+?) in (?:my )?(?:editor|code|vs ?code)\b", "open_in_editor",
         lambda m: {"path": m.group(1).strip()}),

        # ── original system control ─────────────────────────────────────────
        # Deliberately below every specific "start/open ..." rule above, so that
        # "start a focus session" is a focus session and not an app by that name.
        (r"\b(?:open|launch|start)\s+(.+)", "open_app",
         lambda m: {"name": m.group(1).strip()}),
        (r"\b(?:quit|close)\s+(.+)", "quit_app", lambda m: {"name": m.group(1).strip()}),
        (r"\b(volume|sound)\b.*\b(up|louder|raise|increase)\b", "set_volume",
         lambda m: {"direction": "up"}),
        (r"\b(volume|sound)\b.*\b(down|quieter|lower|decrease)\b", "set_volume",
         lambda m: {"direction": "down"}),
        (r"\b(mute|silence)\b", "set_volume", lambda m: {"direction": "mute"}),
        (r"\bset (?:the )?volume to (\d{1,3})\b", "set_volume",
         lambda m: {"level": int(m.group(1))}),
        (r"\bvolume\b", "get_volume", lambda m: {}),
        (r"\b(brightness)\b.*\b(up|brighter|raise)\b", "set_brightness",
         lambda m: {"direction": "up"}),
        (r"\b(brightness)\b.*\b(down|dimmer|lower)\b", "set_brightness",
         lambda m: {"direction": "down"}),
        (r"\btake a screenshot\b|\bscreenshot\b", "screenshot", lambda m: {}),
        (r"\bwhat(?:'s| is) running\b|\bopen apps\b|\brunning apps\b", "list_apps", lambda m: {}),
        (r"\bwhat(?:'s| is) in focus\b|\bfront ?most\b|\bwhich app\b", "frontmost_app",
         lambda m: {}),
        (r"\bwhat(?:'s| is) the time\b|\btime is it\b|\bwhat time\b", "clock", lambda m: {}),
        (r"\b(what day|what(?:'s| is) the date|today's date)\b", "clock", lambda m: {}),
        (r"\bbattery\b", "battery", lambda m: {}),
        (r"\bbrightness\b", "set_brightness", lambda m: {"direction": "up"}),

        # ── memory ──────────────────────────────────────────────────────────
        (r"\bwhat do you (?:remember|know)\b", "recall", lambda m: {}),
        (r"\bremember (?:that )?(.+)", "remember", lambda m: {"fact": m.group(1).strip()}),
        (r"\bforget everything\b|\bclear your memory\b", "forget", lambda m: {}),

        # ── security ────────────────────────────────────────────────────────
        (r"\b(audit|security check|check security|harden|am i secure)\b",
         "security_audit_local", lambda m: {}),
        (r"\b(check|scan)\s+ports?\b(?:\s+(?:on\s+)?(\S+))?", "security_check_ports",
         lambda m: {"target": (m.group(2) or "localhost")}),

        # ── files ───────────────────────────────────────────────────────────
        (r"\b(search|look up|google|find out about)\s+(.+)", "web_search",
         lambda m: {"query": re.sub(r"^(?:for|about|up)\s+", "", m.group(2).strip(), flags=re.I)}),
        (r"\b(list|show)\s+(?:the )?files\b(?:\s+in\s+(.+))?", "list_files",
         lambda m: {"path": (m.group(2) or ".").strip()}),
        (r"\bread (?:the )?file\s+(.+)", "read_file", lambda m: {"path": m.group(1).strip()}),

        # ── bare shell commands, late so English wins ties ──────────────────
        (r"^\s*([a-z0-9_]+)\s+(.+)$", "run_shell", _bare_command),

        # ── self-description, last ──────────────────────────────────────────
        (r"\breview (?:yourself|your (?:actions|work|history))\b|\bwhat have you done\b",
         "review_self", lambda m: {}),
        (r"\b(status|how are you|health)\b", "status", lambda m: {}),
        (r"\bwho are you\b|\bwhat can you do\b|\bhelp\b", "capabilities", lambda m: {}),
    ]

    UNMATCHED = (
        "I don't have a language model connected, so I only understand direct "
        "commands right now. Try 'what's the weather', 'set a timer for ten minutes', "
        "'run the sitrep protocol', 'am I online', or 'help' for the full list. "
        "Add an API key to .env for full conversation."
    )

    # `skill_name key=value ...` — the escape hatch that makes all 81 skills
    # reachable without a model. Both halves are strict on purpose: the first word
    # must be an exactly-registered skill name, and any arguments must be
    # key=value. So "note_add text='buy milk'" is a direct call, while "define
    # serendipity" and "weather in Paris" are left to the phrase rules below.
    _DIRECT = re.compile(r"^\s*([a-z][a-z0-9_]*)\s*(.*?)\s*$", re.S)
    _KV_ONLY = re.compile(r"""(?:[a-z_][\w]*=(?:"[^"]*"|'[^']*'|\S+)(?:\s+|$))+""", re.I)

    def __init__(self):
        self._compiled = [(re.compile(p, re.I), name, fn) for p, name, fn in self.RULES]

    def _direct_call(self, text: str, tool_names: set[str]) -> ToolCall | None:
        from ..skills.protocols import coerce_value

        m = self._DIRECT.match(text)
        if m is None:
            return None
        name, rest = m.group(1), (m.group(2) or "").strip()
        if name not in tool_names:
            return None
        if rest and not self._KV_ONLY.fullmatch(rest):
            return None
        args: dict[str, Any] = {}
        if rest:
            try:
                tokens = shlex.split(rest)
            except ValueError:
                return None
            for token in tokens:
                key, sep, value = token.partition("=")
                if not sep:
                    return None
                args[key.strip()] = coerce_value(value.strip())
        return ToolCall(id="offline", name=name, args=args)

    def think(self, messages, tools, system) -> Turn:
        text = ""
        for m in reversed(messages):
            if m["role"] == "user":
                text = m["content"] if isinstance(m["content"], str) else ""
                break
        if not text.strip():
            return Turn(text="I'm listening.")

        tool_names = {t["name"] for t in tools}

        # The shell escape stays ahead of everything, including a direct call, so
        # "run: git status" is still a command and not a hunt for a skill.
        if not re.match(r"^\s*(?:run|shell|exec)\s*:", text, re.I):
            direct = self._direct_call(text, tool_names)
            if direct is not None:
                return Turn(tool_calls=[direct], done=False)

        for rx, name, build in self._compiled:
            if name not in tool_names:
                continue
            match = rx.search(text)
            if not match:
                continue
            args = build(match)
            if args is None:
                continue          # the rule declined; keep looking
            return Turn(tool_calls=[ToolCall(id="offline", name=name, args=args)], done=False)

        return Turn(text=self.UNMATCHED)
