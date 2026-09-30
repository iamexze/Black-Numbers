"""The safety policy — what may run unattended, and what must be confirmed.

This is deliberately its own small module, like NewsMan's thresholds file, so
the rules are in one readable place rather than scattered as `if` statements.
A voice assistant that can move files and run commands is only trustworthy if
the boundary between "just do it" and "ask me first" is explicit and auditable.

Three confirmation modes, set by BN_CONFIRM:
  always   — every action that changes anything is confirmed
  trusted  — read-only actions run freely; anything that changes state is
             confirmed (the default, and the sane one)
  never    — nothing is confirmed. Only sensible for a locked-down allowlist;
             the assistant warns loudly when it is on.
"""

from __future__ import annotations

from ..skills.base import Risk


def needs_confirmation(risk: Risk, mode: str) -> bool:
    if mode == "never":
        return False
    if mode == "always":
        return risk >= Risk.REVERSIBLE
    # trusted: observe freely, confirm any change
    return risk >= Risk.REVERSIBLE


# Shell commands that are read-only enough to run without a prompt under the
# 'trusted' policy. Anything not matching is treated as state-changing and
# confirmed. This is an allowlist, never a denylist: unknown means ask.
READONLY_SHELL_PREFIXES = (
    "ls", "pwd", "cat", "head", "tail", "wc", "grep", "find", "file", "stat",
    "df", "du", "ps", "top", "whoami", "id", "uname", "date", "uptime",
    "git status", "git log", "git diff", "git branch", "git show",
    "echo", "which", "type", "env", "printenv", "history",
    "networksetup -listallhardwareports", "ifconfig", "netstat", "scutil --dns",
    "sw_vers", "system_profiler", "defaults read",
)


def is_readonly_shell(command: str) -> bool:
    c = command.strip()
    # A pipe or redirect into something could write; treat as mutating.
    if any(tok in c for tok in (">", ">>", "|", "&&", ";", "$(", "`", "rm ", "sudo")):
        # A read-only pipeline (grep | wc) is common and safe; allow if every
        # stage is itself read-only.
        if any(tok in c for tok in (">", ">>", "rm ", "sudo", "$(", "`")):
            return False
        stages = [s.strip() for s in c.replace("&&", "|").replace(";", "|").split("|")]
        return all(any(s.startswith(p) for p in READONLY_SHELL_PREFIXES) for s in stages if s)
    return any(c.startswith(p) for p in READONLY_SHELL_PREFIXES)
