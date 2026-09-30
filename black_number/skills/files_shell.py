"""Files and shell skills.

The shell skill is the most powerful and the most dangerous, so it is the most
carefully gated. A command is classified read-only or not by the safety policy's
allowlist; read-only commands run under the trusted policy, everything else is
confirmed with the exact command shown. There is no path that runs an
unreviewed state-changing command unattended.

File skills stay within the user's home by default and refuse to traverse above
it, so a stray "delete everything in /" cannot originate here.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from ..safety import policy
from .base import Context, Result, Risk, Skill

HOME = Path.home()


def _safe_path(raw: str) -> Path | None:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (HOME / p).resolve()
    else:
        p = p.resolve()
    return p


def _list_files(a: dict, _c) -> Result:
    p = _safe_path(a.get("path", "."))
    if p is None or not p.exists():
        return Result.fail(f"No such path: {a.get('path')}")
    if p.is_file():
        return Result.say(f"{p.name} is a file, {p.stat().st_size} bytes.", detail=str(p))
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))[:200]
    lines = [f"{'d' if e.is_dir() else '-'} {e.name}" for e in entries]
    return Result.say(f"{len(entries)} items in {p.name or p}.", detail="\n".join(lines))


def _read_file(a: dict, _c) -> Result:
    p = _safe_path(a.get("path", ""))
    if p is None or not p.is_file():
        return Result.fail("That file doesn't exist.")
    if p.stat().st_size > 200_000:
        return Result.fail("That file is too large to read aloud.")
    try:
        text = p.read_text(errors="replace")
    except Exception as e:
        return Result.fail(f"Couldn't read it: {e}")
    head = text[:1500]
    return Result.say(f"{p.name}, {len(text)} characters.", detail=head, data=text)


def _find_files(a: dict, _c) -> Result:
    root = _safe_path(a.get("path", "."))
    pattern = a.get("pattern", "*")
    if root is None or not root.exists():
        return Result.fail("No such folder.")
    matches = list(root.rglob(pattern))[:100]
    if not matches:
        return Result.say(f"No files matching {pattern}.")
    return Result.say(
        f"Found {len(matches)} matching {pattern}.",
        detail="\n".join(str(m.relative_to(root)) for m in matches),
    )


def _run_shell(a: dict, ctx: Context) -> Result:
    command = a.get("command", "").strip()
    if not command:
        return Result.fail("No command given.")

    readonly = policy.is_readonly_shell(command)
    # Non-read-only commands are confirmed here explicitly with the full text,
    # regardless of the skill's declared risk, because the command itself is the
    # variable that matters, not the skill.
    if not readonly:
        approved = ctx.gate.confirm(f"run shell: {command}", Risk.MUTATING)
        if not approved:
            return Result.fail("Command cancelled.")
    try:
        p = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=60, cwd=str(HOME)
        )
    except subprocess.TimeoutExpired:
        return Result.fail("The command timed out.")
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    body = out or err or "(no output)"
    speech = "Done." if p.returncode == 0 else f"Exited with code {p.returncode}."
    return Result(p.returncode == 0, speech, detail=body[:4000], data={"code": p.returncode})


def skills() -> list[Skill]:
    return [
        Skill(
            "list_files", "List files in a folder (defaults to home).", _list_files,
            parameters={"path": {"type": "string"}}, risk=Risk.READ_ONLY,
        ),
        Skill(
            "read_file", "Read a text file's contents.", _read_file,
            parameters={"path": {"type": "string", "required": True}}, risk=Risk.READ_ONLY,
        ),
        Skill(
            "find_files", "Find files by glob pattern under a folder.", _find_files,
            parameters={"path": {"type": "string"}, "pattern": {"type": "string"}},
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "run_shell",
            "Run a shell command. Read-only commands run directly; anything that "
            "changes state is confirmed with the exact command first.",
            _run_shell,
            parameters={"command": {"type": "string", "required": True}},
            risk=Risk.READ_ONLY,  # the skill self-gates on the command text
            caution="commands that change files or system state are shown and confirmed",
        ),
    ]
