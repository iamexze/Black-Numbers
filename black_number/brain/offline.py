"""The offline brain — deterministic intent matching, no model required.

This is what runs on a fresh clone with no API key, and the permanent fallback
when the network or the model is unavailable. It cannot hold a conversation, but
it reliably maps plain phrasings to the right tool, so every skill is usable
from day one. Think of it as the reflexes; the LLM brain is the cortex.

It is intentionally simple and transparent: a table of patterns to tool calls.
When it cannot match, it says so honestly rather than guessing.
"""

from __future__ import annotations

import re
from typing import Any

from .base import Brain, ToolCall, Turn


class OfflineBrain(Brain):
    name = "offline"

    # (regex, tool_name, arg_builder). First match wins. arg_builder receives
    # the regex match and returns the tool arguments.
    RULES: list[tuple[str, str, Any]] = [
        # The explicit-command escape hatch is checked first, so "run: git status"
        # goes to the shell rather than matching a keyword inside the command.
        (r"^\s*(?:run|shell|exec)[:\s]+(.+)", "run_shell", lambda m: {"command": m.group(1).strip()}),
        (r"\b(volume|sound)\b.*\b(up|louder|raise|increase)\b", "set_volume", lambda m: {"direction": "up"}),
        (r"\b(volume|sound)\b.*\b(down|quieter|lower|decrease)\b", "set_volume", lambda m: {"direction": "down"}),
        (r"\b(mute|silence)\b", "set_volume", lambda m: {"direction": "mute"}),
        (r"\bset (?:the )?volume to (\d{1,3})\b", "set_volume", lambda m: {"level": int(m.group(1))}),
        (r"\bvolume\b", "get_volume", lambda m: {}),
        (r"\b(brightness)\b.*\b(up|brighter|raise)\b", "set_brightness", lambda m: {"direction": "up"}),
        (r"\b(brightness)\b.*\b(down|dimmer|lower)\b", "set_brightness", lambda m: {"direction": "down"}),
        (r"\b(open|launch|start)\s+(.+)", "open_app", lambda m: {"name": m.group(2).strip()}),
        (r"\b(quit|close)\s+(.+)", "quit_app", lambda m: {"name": m.group(2).strip()}),
        (r"\bwhat(?:'s| is) the time\b|\btime is it\b|\bwhat time\b", "clock", lambda m: {}),
        (r"\b(what day|what(?:'s| is) the date|today's date)\b", "clock", lambda m: {}),
        (r"\btake a screenshot\b|\bscreenshot\b", "screenshot", lambda m: {}),
        (r"\bwhat(?:'s| is) running\b|\bopen apps\b|\brunning apps\b", "list_apps", lambda m: {}),
        (r"\b(search|look up|google|find out about)\s+(.+)", "web_search", lambda m: {"query": m.group(2).strip()}),
        (r"\b(list|show)\s+(?:the )?files\b(?:\s+in\s+(.+))?", "list_files", lambda m: {"path": (m.group(2) or ".").strip()}),
        (r"\bbattery\b", "battery", lambda m: {}),
        (r"\bbrightness\b", "set_brightness", lambda m: {"direction": "up"}),
        (r"\b(audit|security check|check security|harden|am i secure)\b", "security_audit_local", lambda m: {}),
        (r"\b(check|scan)\s+ports?\b(?:\s+(?:on\s+)?(\S+))?", "security_check_ports", lambda m: {"target": (m.group(2) or "localhost")}),
        (r"\bremember (?:that )?(.+)", "remember", lambda m: {"fact": m.group(1).strip()}),
        (r"\bwhat do you (?:remember|know)\b", "recall", lambda m: {}),
        (r"\b(status|how are you|health)\b", "status", lambda m: {}),
        (r"\breview (?:yourself|your (?:actions|work|history))\b|\bwhat have you done\b", "review_self", lambda m: {}),
        (r"\bwhat(?:'s| is) in focus\b|\bfront ?most\b|\bwhich app\b", "frontmost_app", lambda m: {}),
        (r"\bread (?:the )?file\s+(.+)", "read_file", lambda m: {"path": m.group(1).strip()}),
        (r"^\s*(?:run|shell|exec)[:\s]+(.+)", "run_shell", lambda m: {"command": m.group(1).strip()}),
        (r"\bwho are you\b|\bwhat can you do\b|\bhelp\b", "capabilities", lambda m: {}),
    ]

    def __init__(self):
        self._compiled = [(re.compile(p, re.I), name, fn) for p, name, fn in self.RULES]

    def think(self, messages, tools, system) -> Turn:
        text = ""
        for m in reversed(messages):
            if m["role"] == "user":
                text = m["content"] if isinstance(m["content"], str) else ""
                break
        if not text:
            return Turn(text="I'm listening.")

        tool_names = {t["name"] for t in tools}
        for rx, name, build in self._compiled:
            match = rx.search(text)
            if match and name in tool_names:
                return Turn(
                    tool_calls=[ToolCall(id="offline", name=name, args=build(match))],
                    done=False,
                )

        return Turn(
            text=(
                "I don't have a language model connected, so I only understand direct "
                "commands right now. Try 'volume up', 'open Safari', 'what's the time', "
                "or 'search <something>'. Add an API key to .env for full conversation."
            )
        )
