"""Meta skills — the assistant reasoning about itself.

This is where "self-improving" starts as something concrete rather than a
slogan. The assistant can list its own capabilities, remember durable facts and
standing instructions, read its own recent transcript to see what it did, and
report its own status. A later version builds on exactly these: reading the
transcript to find where it failed, and proposing new skills.
"""

from __future__ import annotations

import json

from .base import Context, Result, Risk, Skill


def _capabilities(_a, ctx: Context) -> Result:
    reg = ctx.registry
    if reg is None:
        return Result.say("I can't see my own registry right now.")
    by_area: dict[str, list[str]] = {}
    for s in reg.all():
        area = s.name.split("_")[0]
        by_area.setdefault(area, []).append(s.name)
    detail = "\n".join(f"{a}: {', '.join(names)}" for a, names in sorted(by_area.items()))
    count = len(reg.all())
    return Result.say(
        f"I have {count} skills across system control, files and shell, the web, "
        "security auditing, and managing myself.",
        detail=detail,
    )


def _remember(a: dict, ctx: Context) -> Result:
    text = a.get("fact", "").strip()
    if not text:
        return Result.fail("Remember what?")
    ctx.memory.remember(text, kind=a.get("kind", "fact"))
    return Result.say("Got it, I'll remember that.")


def _recall(_a, ctx: Context) -> Result:
    facts = ctx.memory.facts
    if not facts:
        return Result.say("I haven't been told anything to remember yet.")
    detail = "\n".join(f"- {f['text']}" for f in facts[-40:])
    return Result.say(f"I'm holding {len(facts)} {'thing' if len(facts)==1 else 'things'} in long-term memory.", detail=detail)


def _forget(_a, ctx: Context) -> Result:
    n = ctx.memory.forget_all()
    return Result.say(f"Cleared {n} remembered items.")


def _review(a: dict, ctx: Context) -> Result:
    """Read back the recent transcript — the raw material for self-improvement.
    Summarises what tools were run and which failed, so the assistant (or the
    user) can see its own recent behaviour."""
    path = ctx.log.path
    if not path.exists():
        return Result.say("No transcript yet.")
    rows = []
    for line in path.read_text().splitlines()[-400:]:
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    runs = [r for r in rows if r.get("kind") == "skill_result"]
    fails = [r for r in runs if not r.get("ok")]
    ran = {}
    for r in runs:
        ran[r["name"]] = ran.get(r["name"], 0) + 1
    top = ", ".join(f"{k}×{v}" for k, v in sorted(ran.items(), key=lambda x: -x[1])[:8])
    detail = f"Skill runs recently: {top or 'none'}\nFailures: {len(fails)}"
    if fails:
        detail += "\n" + "\n".join(f"- {f['name']}: {f.get('speech','')}" for f in fails[-6:])
    return Result.say(
        f"In recent history I ran {len(runs)} actions, {len(fails)} failed.", detail=detail
    )


def _status(_a, ctx: Context) -> Result:
    lines = [f"{k}: {v}" for k, v in ctx.cfg.describe()]
    lines.append(f"skills: {len(ctx.registry.all())}")
    lines.append(f"remembered facts: {len(ctx.memory.facts)}")
    return Result.say("Here's my status.", detail="\n".join(lines))


def skills() -> list[Skill]:
    return [
        Skill("capabilities", "List what the assistant can do.", _capabilities, risk=Risk.READ_ONLY),
        Skill(
            "remember", "Store a durable fact or standing instruction across sessions.", _remember,
            parameters={"fact": {"type": "string", "required": True}, "kind": {"type": "string"}},
            risk=Risk.READ_ONLY,
        ),
        Skill("recall", "List what the assistant has been asked to remember.", _recall, risk=Risk.READ_ONLY),
        Skill("forget", "Clear all long-term memory.", _forget, risk=Risk.MUTATING),
        Skill("review_self", "Summarise the assistant's own recent actions and failures.", _review, risk=Risk.READ_ONLY),
        Skill("status", "Report the assistant's current configuration and health.", _status, risk=Risk.READ_ONLY),
    ]
