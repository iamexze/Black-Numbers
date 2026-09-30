"""Protocols — named routines the assistant can be taught and can run.

A protocol is an ordered list of skill calls under one name: "run diagnostics",
"morning protocol", "wind down". This is the cheapest real extensibility in the
system. New behaviour needs no code, no restart and no model — it is data, saved
to disk, available immediately, and readable by a human who wants to check what
it will do before saying yes.

The safety property that matters, and the one the test suite pins down:

    A protocol is not a way around the gate.

`protocol_run` does not execute anything itself. Every step goes back through
Registry.execute, so each one is classified and confirmed on its own merits. A
protocol containing a destructive step prompts exactly as that step would if it
had been asked for directly. That is why this skill is declared READ_ONLY —
it carries no authority of its own, in the same way `run_shell` self-gates on
the command rather than on the skill.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path
from typing import Any

from .base import Context, Result, Risk, Skill

MAX_STEPS = 24
MAX_DEPTH = 3          # a protocol may call a protocol, but not without end
_DEPTH = {"n": 0}

# Shipped routines. They are merged under anything the user defines with the
# same name, so these are defaults rather than fixtures. Steps naming a skill
# that isn't registered are reported and skipped, which lets a built-in mention
# an optional capability without breaking when it is absent.
BUILTIN: dict[str, dict[str, Any]] = {
    "sitrep": {
        "description": "Where things stand: time, machine, network, what's pending.",
        "steps": [
            {"skill": "clock"}, {"skill": "machine_health"},
            {"skill": "network_status", "args": {"include_public": False}},
            {"skill": "list_timers"},
        ],
    },
    "morning": {
        "description": "Time, weather, headlines and anything already scheduled.",
        "steps": [
            {"skill": "clock"}, {"skill": "weather"},
            {"skill": "news_briefing", "args": {"count": 4}},
            {"skill": "list_timers"},
        ],
    },
    "diagnostics": {
        "description": "Full self-check: config, health, network, own recent failures.",
        "steps": [
            {"skill": "status"}, {"skill": "machine_health"},
            {"skill": "network_status"}, {"skill": "self_diagnose"},
        ],
    },
    "workspace": {
        "description": "State of the project in the current folder.",
        "steps": [{"skill": "project_scan"}, {"skill": "git_status"}],
    },
    "wind_down": {
        "description": "Dark mode, then read back what's pending and noted.",
        "steps": [
            {"skill": "dark_mode", "args": {"state": "on"}},
            {"skill": "list_timers"},
            {"skill": "note_read", "args": {"limit": 5}},
        ],
    },
}


def _store(ctx: Context) -> Path:
    ctx.cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return ctx.cfg.state_dir / "protocols.json"


def load_protocols(ctx: Context) -> dict[str, dict[str, Any]]:
    """Built-ins overlaid with the user's own. A user protocol of the same name
    replaces the built-in entirely, so a default can always be overridden."""
    merged = {k: {**v, "builtin": True} for k, v in BUILTIN.items()}
    path = _store(ctx)
    if path.exists():
        try:
            saved = json.loads(path.read_text())
            if isinstance(saved, dict):
                for name, body in saved.items():
                    if isinstance(body, dict) and isinstance(body.get("steps"), list):
                        merged[name] = {**body, "builtin": False}
        except Exception:
            pass
    return merged


def _save_user(ctx: Context, protocols: dict[str, dict[str, Any]]) -> None:
    user = {k: {kk: vv for kk, vv in v.items() if kk != "builtin"}
            for k, v in protocols.items() if not v.get("builtin")}
    _store(ctx).write_text(json.dumps(user, indent=2, ensure_ascii=False))


def normalise_name(raw: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (raw or "").strip().lower()).strip("_")


def parse_steps(spec: Any) -> tuple[list[dict[str, Any]], str]:
    """Accept steps as structured data or as one readable line.

    The compact form — "clock; weather location=Kochi; news_briefing count=3" —
    exists so a protocol can be defined by voice, or by the offline brain, which
    has no way to emit nested JSON. Returns (steps, error).
    """
    steps: list[dict[str, Any]] = []
    if isinstance(spec, str):
        spec = [chunk for chunk in re.split(r"[;\n]|(?:\s+then\s+)", spec) if chunk.strip()]
    if not isinstance(spec, list):
        return [], "I need a list of steps."
    for raw in spec:
        if isinstance(raw, dict):
            name = normalise_name(str(raw.get("skill", "")))
            if not name:
                return [], "A step is missing its skill name."
            args = raw.get("args") or {}
            if not isinstance(args, dict):
                return [], f"The arguments for {name} aren't a mapping."
            steps.append({"skill": name, "args": args})
            continue
        text = str(raw).strip()
        if not text:
            continue
        # shlex, not split(), so a value can contain spaces:
        #   note_add text='buy milk' notebook=todo
        try:
            parts = shlex.split(text)
        except ValueError:
            return [], f"There's an unbalanced quote in '{text}'."
        if not parts:
            continue
        name = normalise_name(parts[0])
        if not name:
            return [], f"I can't read '{text}' as a step."
        args: dict[str, Any] = {}
        for token in parts[1:]:
            if "=" not in token:
                return [], f"'{token}' in step '{name}' should look like key=value."
            k, _, v = token.partition("=")
            args[k.strip()] = coerce_value(v.strip())
        steps.append({"skill": name, "args": args})
    if not steps:
        return [], "That protocol has no steps."
    if len(steps) > MAX_STEPS:
        return [], f"That's more than {MAX_STEPS} steps; break it into two protocols."
    return steps, ""


def coerce_value(v: str) -> Any:
    """'3' → 3, 'true' → True, 'x' → 'x'. Quoted values stay strings.

    Shared with the offline brain, which parses the same key=value syntax when a
    skill is invoked by name, so both paths coerce identically.
    """
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    low = v.lower()
    # Deliberately only true/false. "on", "off", "yes" and "no" are legitimate
    # string values here — dark_mode, wifi_power and keep_awake all take
    # state=on — so coercing them to booleans would hand those skills the wrong
    # type. A declared boolean parameter is written true or false.
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d*\.\d+", v):
        return float(v)
    return v


# ── skills ──────────────────────────────────────────────────────────────────
def _protocol_list(a: dict, ctx: Context) -> Result:
    protos = load_protocols(ctx)
    if not protos:
        return Result.say("I have no protocols defined.")
    name = normalise_name(a.get("name") or "")
    if name:
        body = protos.get(name)
        if body is None:
            return Result.fail(f"I have no protocol called {name}.")
        lines = [f"{i+1}. {s['skill']}" + (f"  {s.get('args')}" if s.get("args") else "")
                 for i, s in enumerate(body["steps"])]
        origin = "built in" if body.get("builtin") else "yours"
        return Result.say(
            f"{name}: {body.get('description') or len(body['steps'])} steps.",
            detail=f"{body.get('description','')}\n({origin}, {len(body['steps'])} steps)\n"
                   + "\n".join(lines),
            data=body,
        )
    rows = []
    for n, body in sorted(protos.items()):
        tag = " " if body.get("builtin") else "*"
        rows.append(f"{tag} {n:<14} {len(body['steps']):>2} steps  {body.get('description','')}")
    return Result.say(
        f"I have {len(protos)} protocols. Say 'run the sitrep protocol' to use one.",
        detail="\n".join(rows) + "\n\n(* = defined by you)",
        data=sorted(protos),
    )


def _protocol_define(a: dict, ctx: Context) -> Result:
    name = normalise_name(a.get("name") or "")
    if not name:
        return Result.fail("What should the protocol be called?")
    steps, err = parse_steps(a.get("steps"))
    if err:
        return Result.fail(err)

    known = ctx.registry.all() if ctx.registry else []
    known_names = {s.name for s in known}
    unknown = [s["skill"] for s in steps if s["skill"] not in known_names]
    if unknown and not a.get("allow_unknown"):
        return Result.fail(
            f"I don't have a skill called {unknown[0]}.",
            detail="Unknown steps: " + ", ".join(unknown)
                   + "\nPass allow_unknown to save it anyway.",
        )
    if any(s["skill"] == "protocol_run" for s in steps):
        # Cheap self-reference check; depth limiting in _protocol_run covers
        # the indirect cases this cannot see.
        if any((s.get("args") or {}).get("name") in (name, a.get("name")) for s in steps):
            return Result.fail("A protocol can't be its own first step.")

    protos = load_protocols(ctx)
    replacing = name in protos
    protos[name] = {
        "description": (a.get("description") or "").strip() or f"{len(steps)} steps",
        "steps": steps,
        "created": round(time.time(), 3),
        "builtin": False,
    }
    _save_user(ctx, protos)
    ctx.log.event("protocol_define", name=name, steps=[s["skill"] for s in steps])
    verb = "Replaced" if replacing else "Learned"
    return Result.say(
        f"{verb} the {name} protocol, {len(steps)} steps.",
        detail="\n".join(f"{i+1}. {s['skill']} {s.get('args') or ''}".rstrip()
                         for i, s in enumerate(steps)),
    )


def _protocol_delete(a: dict, ctx: Context) -> Result:
    name = normalise_name(a.get("name") or "")
    protos = load_protocols(ctx)
    body = protos.get(name)
    if body is None:
        return Result.fail(f"I have no protocol called {name}.")
    if body.get("builtin"):
        return Result.fail(
            f"{name} is built in, so there's nothing of yours to delete.",
            detail="Define one with the same name to override it instead.",
        )
    del protos[name]
    _save_user(ctx, protos)
    return Result.say(f"Deleted the {name} protocol.")


def _protocol_run(a: dict, ctx: Context) -> Result:
    """Run a protocol's steps in order, each through the registry's gate.

    Failures do not stop the run by default: a sitrep whose network check fails
    should still report the machine's health. `stop_on_error` opts into halting
    for protocols where a later step depends on an earlier one.
    """
    name = normalise_name(a.get("name") or "")
    if not name:
        return Result.fail("Run which protocol?")
    protos = load_protocols(ctx)
    body = protos.get(name)
    if body is None:
        close = [n for n in protos if n.startswith(name[:4])]
        hint = f" Did you mean {close[0]}?" if close else ""
        return Result.fail(f"I have no protocol called {name}.{hint}")
    if ctx.registry is None:
        return Result.fail("I can't reach my own registry, so I can't run a protocol.")
    if _DEPTH["n"] >= MAX_DEPTH:
        return Result.fail(f"Protocols are nested {MAX_DEPTH} deep; I've stopped to avoid a loop.")

    stop_on_error = bool(a.get("stop_on_error"))
    known = {s.name for s in ctx.registry.all()}
    lines: list[str] = []
    spoken: list[str] = []
    ran = failed = skipped = 0

    _DEPTH["n"] += 1
    ctx.log.event("protocol_start", name=name, depth=_DEPTH["n"])
    try:
        for i, step in enumerate(body["steps"], 1):
            skill_name = step["skill"]
            if skill_name not in known:
                skipped += 1
                lines.append(f"{i}. {skill_name} — skipped, not available here")
                continue
            result = ctx.registry.execute(skill_name, dict(step.get("args") or {}), ctx)
            ran += 1
            mark = "ok " if result.ok else "err"
            lines.append(f"{i}. [{mark}] {skill_name}: {result.speech}")
            if result.detail and result.detail != result.speech:
                lines.append("      " + result.detail.replace("\n", "\n      ")[:1200])
            if result.ok:
                spoken.append(result.speech.rstrip("."))
            else:
                failed += 1
                if stop_on_error:
                    lines.append(f"   stopped: {skill_name} failed and stop_on_error was set")
                    break
    finally:
        _DEPTH["n"] -= 1

    ctx.log.event("protocol_done", name=name, ran=ran, failed=failed, skipped=skipped)
    # The spoken form is the protocol's point: one readable sentence per step,
    # joined, rather than a list of statuses nobody wants read aloud.
    speech = ". ".join(spoken[:6]) + "." if spoken else f"The {name} protocol produced nothing."
    if failed:
        speech += f" {failed} of {ran} steps failed."
    if skipped:
        speech += f" {skipped} skipped."
    header = f"protocol {name} — {ran} run, {failed} failed, {skipped} skipped"
    return Result(failed == 0, speech, detail=header + "\n" + "\n".join(lines),
                  data={"protocol": name, "ran": ran, "failed": failed, "skipped": skipped})


def skills() -> list[Skill]:
    return [
        Skill(
            "protocol_list", "List the named protocols, or show one's steps.",
            _protocol_list, parameters={"name": {"type": "string"}}, risk=Risk.READ_ONLY,
        ),
        Skill(
            "protocol_run",
            "Run a named protocol — an ordered set of skills saved under one name. "
            "Each step is confirmed on its own terms, exactly as if requested directly.",
            _protocol_run,
            parameters={
                "name": {"type": "string", "required": True},
                "stop_on_error": {"type": "boolean"},
            },
            risk=Risk.READ_ONLY,      # authority comes from the steps, not from here
            caution="each step is gated individually",
        ),
        Skill(
            "protocol_define",
            "Teach a new protocol. Steps may be structured, or written compactly as "
            "'clock; weather location=Kochi; news_briefing count=3'.",
            _protocol_define,
            parameters={
                "name": {"type": "string", "required": True},
                "steps": {"type": "string", "required": True},
                "description": {"type": "string"},
                "allow_unknown": {"type": "boolean"},
            },
            risk=Risk.REVERSIBLE,
        ),
        Skill(
            "protocol_delete", "Delete a protocol you defined.", _protocol_delete,
            parameters={"name": {"type": "string", "required": True}},
            risk=Risk.MUTATING,
        ),
    ]
