"""Self-inspection and self-improvement — the loop that closes on itself.

"Self-improving" is easy to claim and easy to fake. Here it means five concrete,
inspectable things:

  self_diagnose   read the assistant's own transcripts, rank what went wrong, and
                  say what would fix it
  self_metrics    measure its own behaviour — volume, success rate, what it is
                  actually used for
  self_inventory  compare what it can do against what it has ever done, which
                  surfaces capability that is registered but unreachable in
                  practice (usually a missing offline-brain rule)
  self_lesson     record a correction that is injected into the system prompt on
                  every later turn, so a mistake pointed out once changes
                  behaviour from then on, across restarts
  self_test       run its own test suite and report honestly

The transcript is the substrate for all of it. That is why core/log.py writes
every turn, tool call, confirmation and outcome: not for debugging, but so the
assistant has a record of itself to read.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .base import Context, Result, Risk, Skill

MAX_LINES_PER_LOG = 20_000


def _read_transcripts(ctx: Context, days: int = 14) -> list[dict[str, Any]]:
    """Every event from the last `days` of logs, oldest first."""
    log_dir: Path = ctx.cfg.log_dir
    if not log_dir.is_dir():
        return []
    cutoff = time.time() - days * 86400
    rows: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("bn-*.jsonl")):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            with path.open(encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i >= MAX_LINES_PER_LOG:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict) and row.get("ts", cutoff) >= cutoff:
                        row["_file"] = path.name
                        rows.append(row)
        except OSError:
            continue
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows


_NO_MATCH = "I don't have a language model connected"


def _self_diagnose(a: dict, ctx: Context) -> Result:
    days = max(1, min(int(a.get("days") or 14), 90))
    rows = _read_transcripts(ctx, days)
    if not rows:
        return Result.say("I have no transcript history to learn from yet.")

    results = [r for r in rows if r.get("kind") == "skill_result"]
    errors = [r for r in rows if r.get("kind") == "skill_error"]
    denials = [r for r in rows if r.get("kind") == "gate" and r.get("decision") == "denied"]
    failures = [r for r in results if not r.get("ok")]
    unmatched = [r for r in rows
                 if r.get("kind") == "assistant" and _NO_MATCH in str(r.get("text", ""))]
    missing = [r for r in failures if "don't have a skill called" in str(r.get("speech", ""))]

    by_skill = Counter(r.get("name", "?") for r in failures)
    reasons: Counter[str] = Counter()
    for r in failures:
        msg = str(r.get("speech", "")).strip()
        # Collapse to a shape so "no file at /a" and "no file at /b" group.
        # The quote pattern requires a non-letter before the opening quote, or
        # the apostrophe in "Couldn't" is read as one and the message is mangled
        # instead of normalised — which silently defeated all grouping, since
        # nearly every failure message contains a contraction.
        shape = re.sub(r"(?<![A-Za-z])'[^']*'", "'…'", msg)
        shape = re.sub(r"/\S+", "<path>", shape)
        shape = re.sub(r"\b\d+\b", "N", shape)
        reasons[shape[:110]] += 1

    n_logs = len({r.get("_file") for r in rows})
    lines = [f"window: last {days} days across {n_logs} log{'s' if n_logs != 1 else ''}",
             f"actions run: {len(results)}   failed: {len(failures)}   "
             f"crashed: {len(errors)}   you declined: {len(denials)}"]
    if by_skill:
        lines.append("\nmost failure-prone skills:")
        lines += [f"  {n:<22} {c}" for n, c in by_skill.most_common(8)]
    if reasons:
        lines.append("\nrecurring reasons:")
        lines += [f"  {c}×  {why}" for why, c in reasons.most_common(6)]
    if errors:
        lines.append("\nunhandled exceptions (these are bugs, not user error):")
        lines += [f"  {r.get('name')}: {str(r.get('error'))[:110]}" for r in errors[-5:]]

    # Concrete, actionable conclusions rather than a restatement of the counts.
        # Each one names the file a fix would go in.
    advice: list[str] = []
    if errors:
        worst = Counter(r.get("name") for r in errors).most_common(1)[0]
        advice.append(f"{worst[0]} raised an exception {worst[1]}× — it needs its own "
                      f"error handling rather than relying on the registry's catch-all.")
    if unmatched:
        advice.append(f"{len(unmatched)} requests reached no skill at all because the offline "
                      f"brain had no rule for them. Either connect a model (ANTHROPIC_API_KEY "
                      f"in .env) or add patterns to brain/offline.py RULES.")
    if missing:
        wanted = Counter(re.findall(r"skill called (\w+)", str(r.get("speech", "")))[0]
                         for r in missing if re.findall(r"skill called (\w+)", str(r.get("speech", ""))))
        if wanted:
            advice.append("a model asked for skills that don't exist: "
                          + ", ".join(f"{k} ({v}×)" for k, v in wanted.most_common(4))
                          + " — either build them or tighten the tool descriptions.")
    if denials:
        top = Counter(r.get("action", "?").split(" (")[0] for r in denials).most_common(1)[0]
        advice.append(f"you declined '{top[0]}' {top[1]}× — if that is never wanted, it is worth "
                      f"removing; if it is always wanted, the phrasing of the prompt is wrong.")
    for why, c in reasons.most_common(3):
        if c >= 3:
            advice.append(f"'{why}' recurred {c}× — a predictable failure worth handling up front.")
    if not advice:
        advice.append("nothing systematic to fix — no repeated failures in this window.")

    lines.append("\nwhat I'd change:")
    lines += [f"  - {t}" for t in advice]

    rate = (len(results) - len(failures)) / len(results) * 100 if results else 100.0
    if results:
        # The advice lines are phrased for a bulleted list; spoken as a sentence
        # the first one needs a capital.
        lead = advice[0][:160]
        lead = lead[0].upper() + lead[1:] if lead else ""
        speech = (f"Over {days} days I ran {len(results)} actions with a {rate:.0f} percent "
                  f"success rate. {lead}")
    else:
        speech = "No actions to review yet."
    return Result.say(speech, detail="\n".join(lines),
                      data={"runs": len(results), "failures": len(failures),
                            "errors": len(errors), "advice": advice})


def _self_metrics(a: dict, ctx: Context) -> Result:
    days = max(1, min(int(a.get("days") or 30), 365))
    rows = _read_transcripts(ctx, days)
    if not rows:
        return Result.say("No history recorded yet.")
    results = [r for r in rows if r.get("kind") == "skill_result"]
    utterances = [r for r in rows if r.get("kind") == "user" and r.get("text")]
    sessions = len({r.get("_file") for r in rows})
    ok_n = sum(1 for r in results if r.get("ok"))
    busiest = Counter(r.get("name", "?") for r in results)
    gates = [r for r in rows if r.get("kind") == "gate"]
    approved = sum(1 for r in gates if r.get("decision") == "approved")
    prompted = sum(1 for r in gates if r.get("decision") in ("approved", "denied"))
    fires = [r for r in rows if r.get("kind") == "schedule_fire"]
    protos = Counter(r.get("name") for r in rows if r.get("kind") == "protocol_start")

    rows_out = [
        ("window", f"{days} days, {sessions} log {'file' if sessions == 1 else 'files'}"),
        ("requests", str(len(utterances))),
        ("actions", f"{len(results)} ({ok_n} succeeded, {len(results)-ok_n} failed)"),
        ("success rate", f"{ok_n/len(results)*100:.0f}%" if results else "—"),
        ("confirmations", f"{prompted} asked, {approved} approved"
                          + (f" ({approved/prompted*100:.0f}%)" if prompted else "")),
        ("reminders fired", str(len(fires))),
        ("protocols run", str(sum(protos.values())) + (f"  (top: {protos.most_common(1)[0][0]})"
                                                       if protos else "")),
    ]
    detail = "\n".join(f"{k:16} {v}" for k, v in rows_out)
    if busiest:
        detail += "\n\nmost-used skills:\n" + "\n".join(
            f"  {n:<22} {c}" for n, c in busiest.most_common(10))
    top = busiest.most_common(1)[0][0] if busiest else "nothing"
    return Result.say(
        f"{len(utterances)} requests and {len(results)} actions in {days} days. "
        f"You use {top} most.",
        detail=detail, data=dict(rows_out),
    )


def _self_inventory(a: dict, ctx: Context) -> Result:
    """What I can do against what I have ever done. The gap is the finding."""
    if ctx.registry is None:
        return Result.fail("I can't see my own registry.")
    rows = _read_transcripts(ctx, int(a.get("days") or 90))
    used = Counter(r.get("name") for r in rows if r.get("kind") == "skill_run")
    all_skills = ctx.registry.all()

    by_risk: dict[str, list[str]] = {}
    for s in all_skills:
        by_risk.setdefault(s.risk.name, []).append(s.name)
    never = sorted(s.name for s in all_skills if not used.get(s.name))

    detail = [f"{len(all_skills)} skills registered"]
    for level in ("READ_ONLY", "REVERSIBLE", "MUTATING", "DESTRUCTIVE"):
        names = sorted(by_risk.get(level, []))
        if names:
            detail.append(f"\n{level} ({len(names)}):\n  " + ", ".join(names))
    if never:
        detail.append(f"\nnever used ({len(never)}):\n  " + ", ".join(never))
        detail.append("\nA registered skill that never runs is usually unreachable rather than "
                      "unwanted — most often the offline brain has no pattern for it, so it can "
                      "only be triggered by a model or by name.")
    gated = sum(1 for s in all_skills if s.risk.value >= 1)
    return Result.say(
        f"{len(all_skills)} skills, {gated} of them gated. "
        f"{len(never)} have never run.",
        detail="\n".join(detail),
        data={"total": len(all_skills), "never_used": never},
    )


def _self_lesson(a: dict, ctx: Context) -> Result:
    """Record a correction. Lessons are surfaced in the system prompt on every
    later turn, which is what makes this different from a note: it changes what
    the assistant does next, not just what it can look up."""
    text = (a.get("lesson") or a.get("text") or "").strip()
    if not text:
        return Result.fail("What should I learn from?")
    if len(text) > 500:
        return Result.fail("Keep a lesson to a sentence or two so it stays useful in context.")
    ctx.memory.remember(text, kind="lesson")
    n = sum(1 for f in ctx.memory.facts if f.get("kind") == "lesson")
    return Result.say(
        f"Learned. I'll apply that from now on — {n} {'lesson' if n == 1 else 'lessons'} held.",
        detail=f"lesson: {text}",
    )


def _self_lessons(_a, ctx: Context) -> Result:
    lessons = [f for f in ctx.memory.facts if f.get("kind") == "lesson"]
    if not lessons:
        return Result.say("I haven't been corrected on anything yet.")
    detail = "\n".join(
        f"- {f['text']}   ({time.strftime('%Y-%m-%d', time.localtime(f.get('ts', 0)))})"
        for f in lessons[-30:])
    return Result.say(f"I'm applying {len(lessons)} "
                      f"{'lesson' if len(lessons) == 1 else 'lessons'}.", detail=detail)


def _self_test(a: dict, ctx: Context) -> Result:
    """Run the assistant's own suite. The honest answer to 'are you working'."""
    root: Path = ctx.cfg.root
    suite = root / "tests" / "test_core.py"
    if not suite.exists():
        return Result.fail("I can't find my own test suite.")
    ctx.speak("Running my own tests.")
    try:
        p = subprocess.run(["python3", str(suite)], cwd=str(root),
                           capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return Result.fail("My test suite timed out.")
    except Exception as e:
        return Result.fail(f"I couldn't run my tests: {e}")
    out = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"(\d+) passed, (\d+) failed", out)
    if not m:
        return Result(p.returncode == 0,
                      "My tests ran but didn't report a count.", detail=out[-2000:])
    passed, failed = int(m.group(1)), int(m.group(2))
    speech = (f"All {passed} of my tests pass." if not failed
              else f"{failed} of my {passed + failed} tests are failing.")
    return Result(failed == 0, speech, detail=out[-3000:],
                  data={"passed": passed, "failed": failed})


def skills() -> list[Skill]:
    return [
        Skill(
            "self_diagnose",
            "Read my own transcripts, rank what has been going wrong, and say what "
            "would fix it.",
            _self_diagnose, parameters={"days": {"type": "integer"}}, risk=Risk.READ_ONLY,
        ),
        Skill(
            "self_metrics", "Measure my own usage: requests, actions, success rate.",
            _self_metrics, parameters={"days": {"type": "integer"}}, risk=Risk.READ_ONLY,
        ),
        Skill(
            "self_inventory",
            "Compare what I can do against what I have ever done, and name the gap.",
            _self_inventory, parameters={"days": {"type": "integer"}}, risk=Risk.READ_ONLY,
        ),
        Skill(
            "self_lesson",
            "Record a correction to apply from now on, in this and future sessions.",
            _self_lesson, parameters={"lesson": {"type": "string", "required": True}},
            risk=Risk.READ_ONLY,
        ),
        Skill("self_lessons", "List the corrections I am currently applying.",
              _self_lessons, risk=Risk.READ_ONLY),
        Skill("self_test", "Run my own test suite and report the result.",
              _self_test, risk=Risk.READ_ONLY),
    ]
