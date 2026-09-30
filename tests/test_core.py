"""Test suite — the invariants that make Black Number safe and portable.

Run: python3 tests/test_core.py   (no pytest needed; it self-reports)

The load-bearing checks are the safety ones: a mutating action must never run
without passing the gate, and an unauthorized security target must be refused.
These mirror the tests a real agent that can touch the machine needs.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from black_number.agent.memory import Memory
from black_number.brain.offline import OfflineBrain
from black_number.brain.router import build_brain
from black_number.core import config
from black_number.safety import policy
from black_number.safety.gate import Gate
from black_number.core import theme
from black_number.core.scheduler import Scheduler, human_delta
from black_number.skills.base import Context, Result, Risk, Skill
from black_number.skills.registry import Registry
from black_number.ui.cli import build_registry

_pass = 0
_fail = 0
_fails: list[str] = []


def t(name, fn):
    global _pass, _fail
    try:
        fn()
        _pass += 1
        sys.stdout.write(".")
    except AssertionError as e:
        _fail += 1
        _fails.append(f"{name}: {e}")
        sys.stdout.write("F")
    except Exception as e:
        _fail += 1
        _fails.append(f"{name}: unexpected {type(e).__name__}: {e}")
        sys.stdout.write("E")


class FakeLog:
    def __init__(self):
        self.events = []
        self.path = Path(tempfile.gettempdir()) / "bn-test.jsonl"

    def event(self, kind, /, **f):
        self.events.append({"kind": kind, **f})

    def system(self, m): ...
    def warn(self, m): ...
    def ok(self, m): ...


def _ctx(gate, reg=None, scheduler=None):
    """A context whose every writable path is a fresh temp dir, so no test can
    touch the real notes, protocols, logs or timer queue."""
    log = FakeLog()
    cfg = config.load()
    for field in ("notes_dir", "state_dir", "memory_dir", "log_dir"):
        object.__setattr__(cfg, field, Path(tempfile.mkdtemp()))
    mem = Memory(Path(tempfile.mkdtemp()))
    return Context(cfg=cfg, log=log, gate=gate, memory=mem, speak=lambda s: None,
                   registry=reg, scheduler=scheduler)


# ── Safety: the core promise ────────────────────────────────────────────────
def test_readonly_runs_without_confirmation():
    asked = []
    gate = Gate("trusted", lambda p: asked.append(p) or True, FakeLog())
    reg = Registry()
    ran = []
    from black_number.skills.base import Skill

    reg.add(Skill("peek", "read", lambda a, c: ran.append(1) or Result.say("ok"), risk=Risk.READ_ONLY))
    reg.execute("peek", {}, _ctx(gate))
    assert ran == [1], "read-only skill did not run"
    assert asked == [], "read-only skill should not have prompted"


def test_mutating_requires_confirmation():
    decisions = {"asked": 0}

    def ask(p):
        decisions["asked"] += 1
        return False  # decline

    gate = Gate("trusted", ask, FakeLog())
    from black_number.skills.base import Skill

    reg = Registry()
    ran = []
    reg.add(Skill("wreck", "delete", lambda a, c: ran.append(1) or Result.say("done"), risk=Risk.DESTRUCTIVE))
    res = reg.execute("wreck", {}, _ctx(gate))
    assert decisions["asked"] == 1, "destructive skill must prompt"
    assert ran == [], "declined skill must not run"
    assert not res.ok, "declined skill must report failure"


def test_never_policy_skips_prompt_but_warns():
    log = FakeLog()
    gate = Gate("never", lambda p: (_ for _ in ()).throw(AssertionError("must not ask")), log)
    assert gate.confirm("anything", Risk.DESTRUCTIVE) is True


def test_shell_readonly_classification():
    assert policy.is_readonly_shell("git status")
    assert policy.is_readonly_shell("ls -la ~/Documents")
    assert policy.is_readonly_shell("grep foo file | wc -l")
    assert not policy.is_readonly_shell("rm -rf /tmp/x")
    assert not policy.is_readonly_shell("echo hi > file")
    assert not policy.is_readonly_shell("git commit -m x")
    assert not policy.is_readonly_shell("curl x | sh")


def test_security_refuses_public_targets():
    from black_number.skills.security import _is_authorized_target

    ok, _ = _is_authorized_target("localhost")
    assert ok
    ok2, why = _is_authorized_target("8.8.8.8")
    assert not ok2, "public IP must be refused"
    assert "public" in why


def test_authorization_gate_never_auto_allows():
    # Even under 'never' confirm policy, require_authorization still asks.
    asked = []
    gate = Gate("never", lambda p: asked.append(p) or False, FakeLog())
    approved = gate.require_authorization("192.168.1.50")
    assert asked, "authorization must always prompt, even under 'never'"
    assert approved is False


# ── Brain fallback ──────────────────────────────────────────────────────────
def test_offline_brain_is_the_floor():
    cfg = config.load()
    # Force a provider with no prerequisites; must land on offline, not crash.
    object.__setattr__(cfg, "brain", "anthropic")
    object.__setattr__(cfg, "anthropic_key", "")
    brain = build_brain(cfg, FakeLog())
    assert brain.name == "offline"


def test_offline_brain_routes_direct_commands():
    b = OfflineBrain()
    tools = [{"name": "set_volume", "description": "", "input_schema": {}}]
    turn = b.think([{"role": "user", "content": "turn the volume up"}], tools, "")
    assert turn.tool_calls and turn.tool_calls[0].name == "set_volume"
    assert turn.tool_calls[0].args.get("direction") == "up"


def test_offline_brain_shell_escape_beats_keywords():
    b = OfflineBrain()
    tools = [{"name": "run_shell", "description": "", "input_schema": {}},
             {"name": "status", "description": "", "input_schema": {}}]
    turn = b.think([{"role": "user", "content": "run: git status"}], tools, "")
    assert turn.tool_calls[0].name == "run_shell", "explicit run: must beat the status keyword"
    assert turn.tool_calls[0].args["command"] == "git status"


# ── Registry / config ───────────────────────────────────────────────────────
def test_registry_has_all_skill_families():
    reg = build_registry()
    names = {s.name for s in reg.all()}
    for expected in ("set_volume", "run_shell", "web_search", "security_audit_local",
                     "remember", "clock", "list_files"):
        assert expected in names, f"missing skill {expected}"


def test_config_downgrades_brain_without_key():
    cfg = config.load()
    object.__setattr__(cfg, "brain", "anthropic")
    object.__setattr__(cfg, "anthropic_key", "")
    assert cfg.effective_brain() == "offline"


def test_unknown_skill_fails_gracefully():
    reg = build_registry()
    gate = Gate("trusted", lambda p: True, FakeLog())
    res = reg.execute("does_not_exist", {}, _ctx(gate, reg))
    assert not res.ok


def test_memory_persists_to_disk():
    d = Path(tempfile.mkdtemp())
    m1 = Memory(d)
    m1.remember("the user's name is Test")
    m2 = Memory(d)  # fresh load
    assert any("Test" in f["text"] for f in m2.facts), "facts must survive reload"


# ── Safety: the new surfaces must not open a hole ────────────────────────────
def test_protocol_run_does_not_bypass_the_gate():
    """The load-bearing test for protocols. A protocol is a list of skill calls
    saved under a name; if running one skipped confirmation, it would be a
    trivial way to launder a destructive action past the user."""
    asked = []

    def ask(prompt):
        asked.append(prompt)
        # Key on the skill being confirmed, not on the word "wreck": the define
        # prompt names its steps, so it contains "wreck" too.
        return prompt.startswith("protocol define")

    gate = Gate("trusted", ask, FakeLog())
    reg = Registry()
    ran = []
    reg.add(Skill("wreck", "deletes things",
                  lambda a, c: ran.append(1) or Result.say("done"), risk=Risk.DESTRUCTIVE))
    from black_number.skills import protocols as P
    reg.add_all(P.skills())
    ctx = _ctx(gate, reg)

    defined = reg.execute("protocol_define", {"name": "boom", "steps": "wreck"}, ctx)
    assert defined.ok, f"could not define the protocol: {defined.speech}"
    reg.execute("protocol_run", {"name": "boom"}, ctx)
    assert any("wreck" in a for a in asked), "a destructive step inside a protocol must prompt"
    assert ran == [], "a declined step must not run, even from inside a protocol"


def test_every_state_changing_skill_is_gated():
    """Sweep the real registry: every skill declaring REVERSIBLE or worse must
    prompt before running. Declining means nothing actually executes, so this is
    safe to run against the live skill set — and it cannot be forgotten when a
    new skill is added."""
    reg = build_registry()
    asked = []
    gate = Gate("trusted", lambda p: asked.append(p) or False, FakeLog())
    ctx = _ctx(gate, reg)
    checked = 0
    for skill in reg.all():
        if skill.risk < Risk.REVERSIBLE:
            continue
        asked.clear()
        res = reg.execute(skill.name, {}, ctx)
        assert asked, f"{skill.name} is {skill.risk.name} but ran with no confirmation"
        assert not res.ok, f"{skill.name} reported success after being declined"
        checked += 1
    assert checked >= 20, f"only {checked} gated skills swept; the registry looks wrong"


def test_protocol_nesting_is_bounded():
    from black_number.skills import protocols as P
    gate = Gate("trusted", lambda p: True, FakeLog())
    reg = Registry()
    reg.add_all(P.skills())
    ctx = _ctx(gate, reg)
    reg.execute("protocol_define",
                {"name": "loop", "steps": "protocol_run name=loop", "allow_unknown": True}, ctx)
    res = reg.execute("protocol_run", {"name": "loop"}, ctx)
    assert res.detail is not None
    assert P._DEPTH["n"] == 0, "depth counter must unwind even when a run is refused"


def test_calculator_refuses_everything_but_arithmetic():
    from black_number.skills.world import _Unsafe, calculate
    for hostile in ("__import__('os').system('x')", "open('/etc/passwd')", "(1).__class__",
                    "lambda: 1", "2**99999999", "'a'*1000000", "factorial(100000)"):
        try:
            calculate(hostile)
        except (_Unsafe, SyntaxError, ValueError, TypeError):
            continue
        raise AssertionError(f"calculator evaluated hostile input: {hostile}")


def test_applescript_literals_are_escaped():
    from black_number.skills.mac_personal import _as_literal
    out = _as_literal('he said "stop"; do evil\\path')
    assert '\\"stop\\"' in out, out
    assert "\n" not in out
    assert _as_literal("a\nb") == "a b", "a newline would terminate the literal"


def test_notebook_names_cannot_escape_the_notes_folder():
    from black_number.skills.clipboard_notes import _notebook
    gate = Gate("trusted", lambda p: True, FakeLog())
    ctx = _ctx(gate)
    book = _notebook(ctx, "../../../etc/passwd")
    assert book.parent == ctx.cfg.notes_dir, f"escaped to {book}"
    assert "/" not in book.name


# ── Scheduler ───────────────────────────────────────────────────────────────
def test_scheduler_fires_cancels_and_survives_restart():
    d = Path(tempfile.mkdtemp())
    store = d / "timers.json"
    fired = []
    s = Scheduler(store, lambda job, late: fired.append(job.label))
    s.start()
    s.add("due", time.time() + 0.25)
    s.add("overdue", time.time() - 30)              # must still fire, not vanish
    doomed = s.add("cancelled", time.time() + 0.3)
    s.cancel(doomed.id)
    deadline = time.time() + 3
    while len(fired) < 2 and time.time() < deadline:
        time.sleep(0.05)
    s.stop()
    assert "due" in fired, f"a due job did not fire: {fired}"
    assert "overdue" in fired, "an overdue job must still fire rather than be dropped"
    assert "cancelled" not in fired, "a cancelled job fired anyway"

    s2 = Scheduler(store, lambda j, l: None)
    s2.add("kept", time.time() + 600)
    s3 = Scheduler(store, lambda j, l: None)
    assert [j.label for j in s3.pending()] == ["kept"], "the queue must survive a restart"
    assert s3.add("another", time.time() + 600).id != "t1" or len(s3.pending()) == 2


def test_scheduler_survives_a_failing_sink():
    d = Path(tempfile.mkdtemp())
    seen = []

    def bad_sink(job, late):
        seen.append(job.label)
        raise RuntimeError("the sink is broken")

    s = Scheduler(d / "t.json", bad_sink, FakeLog())
    s.start()
    s.add("first", time.time() + 0.1)
    time.sleep(0.4)
    s.add("second", time.time() + 0.1)
    deadline = time.time() + 2
    while len(seen) < 2 and time.time() < deadline:
        time.sleep(0.05)
    s.stop()
    assert seen == ["first", "second"], f"one bad job stopped the clock: {seen}"


def test_human_delta_reads_like_speech():
    assert human_delta(2) == "now"
    assert human_delta(45) == "in 45 seconds"
    assert human_delta(3600) == "in 1 hour"
    assert human_delta(-120) == "2 minutes ago"


# ── Parsers: the places a wrong answer is worse than no answer ───────────────
def test_duration_parsing():
    from black_number.skills.time_focus import parse_duration
    assert parse_duration("10 min") == 600
    assert parse_duration("1h30m") == 5400
    assert parse_duration("half an hour") == 1800, "a fraction plus an article is one quantity"
    assert parse_duration("an hour and a half") == 5400
    assert parse_duration("20") == 1200, "a bare number means minutes"
    assert parse_duration("banana") is None
    assert parse_duration("0") is None


def test_clock_time_always_resolves_forward():
    from black_number.skills.time_focus import parse_clock_time
    now = time.mktime(time.strptime("2026-09-30 21:00:00", "%Y-%m-%d %H:%M:%S"))
    morning = parse_clock_time("8am", now)
    assert morning > now, "a time already past today must resolve to tomorrow"
    assert time.localtime(morning).tm_hour == 8
    assert time.localtime(parse_clock_time("11:30pm", now)).tm_hour == 23
    assert parse_clock_time("25:00", now) is None
    assert parse_clock_time("nonsense", now) is None


def test_arithmetic_is_exact():
    from black_number.skills.world import calculate
    assert calculate("2+2") == 4
    assert calculate("15% of 200") == 30
    assert calculate("1,250 * 4") == 5000, "thousands separators must not break parsing"
    assert calculate("max(3,9,2)") == 9, "commas between arguments are not separators to strip"
    assert abs(calculate("sqrt(144)") - 12) < 1e-9


def test_unit_conversion_uses_defined_constants():
    from black_number.skills.world import convert
    assert abs(convert(12, "miles", "km")[0] - 19.312128) < 1e-9
    assert abs(convert(1, "kg", "lb")[0] - 2.20462262184878) < 1e-9
    assert convert(100, "c", "f")[0] == 212
    assert convert(1, "gib", "mib")[0] == 1024, "binary and decimal prefixes must stay distinct"
    assert convert(1, "gb", "mb")[0] == 1000
    assert convert(1, "kg", "m")[1], "a dimension mismatch must be refused with a reason"
    assert convert(1, "c", "kg")[1], "temperature is not interchangeable with mass"


# ── Offline brain routing: order is meaning ─────────────────────────────────
def _route(utterance, tools, brain):
    turn = brain.think([{"role": "user", "content": utterance}], tools, "")
    return turn.tool_calls[0].name if turn.tool_calls else None


def test_offline_routing_covers_the_skill_set():
    brain = OfflineBrain()
    tools = build_registry().tool_schemas()
    expected = {
        "what's the weather": "weather", "set a timer for 10 minutes": "set_timer",
        "remind me to call mom in 20 minutes": "set_reminder",
        "run the sitrep protocol": "protocol_run", "am i online": "network_status",
        "what's 15% of 200": "calculate", "what's 12 miles in km": "convert_units",
        "define serendipity": "define", "how's the machine": "machine_health",
        "what's on my clipboard": "clipboard_read", "note down buy coffee": "note_add",
        "diagnose yourself": "self_diagnose", "run your tests": "self_test",
        "what's playing": "now_playing", "lock the screen": "lock_screen",
        "git status": "git_status", "ping 1.1.1.1": "ping_host",
        "what's on my calendar": "calendar_events", "headlines": "news_briefing",
        "start a focus session": "focus_session", "open Safari": "open_app",
        "turn the volume up": "set_volume", "what's the time": "clock",
    }
    for utterance, want in expected.items():
        got = _route(utterance, tools, brain)
        assert got == want, f"{utterance!r} routed to {got}, expected {want}"


def test_offline_routing_order_traps():
    """Each of these was a real misroute during development. Ordering is the
    whole correctness story for a first-match table, so it is pinned here."""
    brain = OfflineBrain()
    tools = build_registry().tool_schemas()
    assert _route("run: git status", tools, brain) == "run_shell", "explicit shell beats keywords"
    assert _route("run your tests", tools, brain) == "self_test", "'run X' is not always a shell"
    assert _route("start a focus session", tools, brain) == "focus_session", \
        "must not open an app called 'a focus session'"
    assert _route("start the stopwatch", tools, brain) == "stopwatch"
    assert _route("what's the time", tools, brain) == "clock", "not arithmetic"
    assert _route("what's 12 miles in km", tools, brain) == "convert_units", "not arithmetic"
    assert _route("what's the weather", tools, brain) == "weather"


def test_offline_timer_reads_the_duration_not_the_article():
    brain = OfflineBrain()
    tools = build_registry().tool_schemas()
    turn = brain.think([{"role": "user", "content": "set a timer for 10 minutes"}], tools, "")
    args = turn.tool_calls[0].args
    from black_number.skills.time_focus import parse_duration
    assert parse_duration(args.get("duration", "")) == 600, f"read {args} as the duration"


def test_offline_can_invoke_any_skill_by_name():
    """`skill_name key=value` is the escape hatch that makes every registered
    skill reachable with no model attached. It must be strict enough not to
    swallow ordinary English that happens to start with a skill name."""
    brain = OfflineBrain()
    reg = build_registry()
    tools = reg.tool_schemas()
    turn = brain.think([{"role": "user", "content": "note_add text='buy milk' notebook=todo"}],
                       tools, "")
    call = turn.tool_calls[0]
    assert call.name == "note_add"
    assert call.args == {"text": "buy milk", "notebook": "todo"}
    turn = brain.think([{"role": "user", "content": "self_metrics days=7"}], tools, "")
    assert turn.tool_calls[0].args == {"days": 7}, "values must be coerced to their types"
    # Not a direct call: the remainder is prose, not key=value.
    assert _route("define serendipity", tools, brain) == "define"
    assert _route("weather in Paris", tools, brain) == "weather"
    assert _route("remember that I prefer tea", tools, brain) == "remember"
    assert _route("run: git status", tools, brain) == "run_shell", "the shell escape stays first"
    # Every skill should now be reachable by its own name.
    unreachable = [s.name for s in reg.all()
                   if _route(s.name, tools, brain) != s.name]
    assert not unreachable, f"not reachable by name: {unreachable}"


def test_offline_can_define_a_protocol_in_words():
    brain = OfflineBrain()
    tools = build_registry().tool_schemas()
    turn = brain.think(
        [{"role": "user", "content": "define protocol coffee as clock; git_status"}], tools, "")
    call = turn.tool_calls[0]
    assert call.name == "protocol_define", f"routed to {call.name}"
    assert call.args["name"] == "coffee" and "git_status" in call.args["steps"]
    # "with steps:" must not leave the word "steps" inside the step list.
    from black_number.skills.protocols import parse_steps
    turn = brain.think(
        [{"role": "user", "content": "teach a new protocol standup with steps: clock; git_status"}],
        tools, "")
    steps, err = parse_steps(turn.tool_calls[0].args["steps"])
    assert not err, err
    assert [s["skill"] for s in steps] == ["clock", "git_status"], steps
    # The more specific define rule must not steal plain runs.
    assert _route("run the sitrep protocol", tools, brain) == "protocol_run"
    assert _route("protocol sitrep", tools, brain) == "protocol_run"


def test_offline_rules_may_decline():
    """A builder returning None means 'not mine after all', and matching must
    continue rather than committing to a wrong tool."""
    brain = OfflineBrain()
    tools = build_registry().tool_schemas()
    assert _route("what's for dinner", tools, brain) != "calculate"
    assert _route("convert this document to a pdf", tools, brain) != "convert_units"
    # These two need a specific guard each, so a weakened builder is caught:
    # "over" reads as division, and only the digit check rejects it.
    assert _route("what's left over", tools, brain) != "calculate", \
        "arithmetic needs a number, not just an operator word"
    # "plus 3 days" is a digit and an operator, so only the date/time exclusion
    # keeps this out of the calculator and lets it reach the clock.
    assert _route("what's the date plus 3 days", tools, brain) == "clock", \
        "a date question is not arithmetic, even with a number in it"


def test_offline_brain_declines_unknown_tools():
    """A rule must never emit a call for a skill that isn't registered."""
    brain = OfflineBrain()
    tools = [{"name": "clock", "description": "", "input_schema": {}}]
    turn = brain.think([{"role": "user", "content": "what's the weather"}], tools, "")
    assert not turn.tool_calls or turn.tool_calls[0].name == "clock"


# ── Protocols as data ───────────────────────────────────────────────────────
def test_protocol_step_parsing():
    from black_number.skills.protocols import parse_steps
    steps, err = parse_steps("clock; weather location=Kochi; news_briefing count=3")
    assert not err and len(steps) == 3
    assert steps[1]["args"]["location"] == "Kochi"
    assert steps[2]["args"]["count"] == 3, "numbers must be coerced, not left as strings"
    quoted, err = parse_steps("note_add text='buy milk' notebook=todo")
    assert not err and quoted[0]["args"]["text"] == "buy milk", "a quoted value may contain spaces"
    assert parse_steps("note_add text='unbalanced")[1], "an unbalanced quote must be reported"
    assert parse_steps("; ".join(["clock"] * 40))[1], "an unbounded protocol must be refused"


def test_state_words_are_not_coerced_to_booleans():
    """`dark_mode state=on` must pass the string "on", not True. Several skills
    take a state word, and handing them a bool made them raise."""
    from black_number.skills.protocols import coerce_value, parse_steps
    assert coerce_value("on") == "on"
    assert coerce_value("off") == "off"
    assert coerce_value("true") is True and coerce_value("false") is False
    assert coerce_value("7") == 7 and coerce_value("1.5") == 1.5
    steps, err = parse_steps("dark_mode state=on; network_status include_public=false")
    assert not err, err
    assert steps[0]["args"]["state"] == "on", steps[0]
    assert steps[1]["args"]["include_public"] is False, steps[1]

    # And the readers tolerate a wrong type rather than raising.
    from black_number.skills import media_mac, network, time_focus
    for fn, args in ((media_mac._dark_mode, {"state": True}),
                     (network._wifi_power, {"state": True}),
                     (time_focus._stopwatch, {"action": True})):
        res = fn(args, None)          # must return a Result, not raise
        assert res is not None and hasattr(res, "ok")


def test_protocol_round_trip_and_builtins():
    from black_number.skills import protocols as P
    gate = Gate("trusted", lambda p: True, FakeLog())
    reg = build_registry()
    ctx = _ctx(gate, reg)
    listed = reg.execute("protocol_list", {}, ctx)
    assert listed.ok and "sitrep" in listed.data, "built-in protocols must be present"
    made = reg.execute("protocol_define",
                       {"name": "Tea Break", "steps": "clock; list_timers"}, ctx)
    assert made.ok, made.speech
    again = P.load_protocols(ctx)
    assert "tea_break" in again, "a defined protocol must persist to disk"
    assert reg.execute("protocol_delete", {"name": "tea_break"}, ctx).ok
    assert "tea_break" not in P.load_protocols(ctx)
    assert not reg.execute("protocol_delete", {"name": "sitrep"}, ctx).ok, \
        "a built-in protocol has no user copy to delete"


def test_protocol_skips_unavailable_steps_without_failing_the_run():
    from black_number.skills import protocols as P
    gate = Gate("trusted", lambda p: True, FakeLog())
    reg = Registry()
    reg.add(Skill("here", "fine", lambda a, c: Result.say("present"), risk=Risk.READ_ONLY))
    reg.add_all(P.skills())
    ctx = _ctx(gate, reg)
    reg.execute("protocol_define",
                {"name": "mixed", "steps": "here; absent_skill", "allow_unknown": True}, ctx)
    res = reg.execute("protocol_run", {"name": "mixed"}, ctx)
    assert res.data["ran"] == 1 and res.data["skipped"] == 1, res.data
    assert res.ok, "a skipped, unavailable step is not a failure"


# ── Memory and self-improvement ─────────────────────────────────────────────
def test_lessons_are_separated_from_facts_and_stated_as_binding():
    m = Memory(Path(tempfile.mkdtemp()))
    assert m.recall_block() == ""
    m.remember("the user lives in Kochi")
    m.remember("never read long output aloud", kind="lesson")
    block = m.recall_block()
    assert "Kochi" in block and "never read long output aloud" in block
    assert "standing instructions" in block, "a correction must be presented as binding"
    assert block.index("Kochi") < block.index("never read"), "lessons come last, for weight"
    assert len(m.of_kind("lesson")) == 1


def test_self_lesson_persists_across_sessions():
    reg = build_registry()
    gate = Gate("trusted", lambda p: True, FakeLog())
    ctx = _ctx(gate, reg)
    res = reg.execute("self_lesson", {"lesson": "prefer metric units"}, ctx)
    assert res.ok
    reloaded = Memory(ctx.memory.dir)
    assert any("metric" in f["text"] for f in reloaded.of_kind("lesson")), \
        "a lesson must outlive the session that learned it"


def test_self_diagnose_reads_its_own_transcript():
    reg = build_registry()
    gate = Gate("trusted", lambda p: True, FakeLog())
    ctx = _ctx(gate, reg)
    log_file = ctx.cfg.log_dir / "bn-2026-09-30.jsonl"
    rows = [
        {"ts": time.time(), "kind": "skill_result", "name": "weather", "ok": True, "speech": "x"},
        {"ts": time.time(), "kind": "skill_result", "name": "ping_host", "ok": False,
         "speech": "Couldn't reach 'a'."},
        {"ts": time.time(), "kind": "skill_result", "name": "ping_host", "ok": False,
         "speech": "Couldn't reach 'b'."},
        {"ts": time.time(), "kind": "skill_error", "name": "ping_host", "error": "boom"},
    ]
    log_file.write_text("\n".join(json.dumps(r) for r in rows))
    res = reg.execute("self_diagnose", {}, ctx)
    assert res.ok, res.speech
    assert res.data["runs"] == 3 and res.data["failures"] == 2, res.data
    assert any("ping_host" in a for a in res.data["advice"]), \
        "the diagnosis must name the skill that is actually failing"
    assert "Couldn't reach '…'" in res.detail, "similar failures must collapse into one shape"


def test_self_inventory_reports_the_gap():
    reg = build_registry()
    gate = Gate("trusted", lambda p: True, FakeLog())
    ctx = _ctx(gate, reg)
    res = reg.execute("self_inventory", {}, ctx)
    assert res.ok and res.data["total"] == len(reg.all())
    assert len(res.data["never_used"]) == len(reg.all()), \
        "with an empty transcript, nothing has been used yet"


# ── Registry hygiene, swept across every skill ──────────────────────────────
def test_skill_contracts_are_well_formed():
    reg = build_registry()
    seen = set()
    for s in reg.all():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", s.name), f"{s.name} is not snake_case"
        assert s.name not in seen, f"duplicate skill name {s.name}"
        seen.add(s.name)
        assert s.description and s.description[0].isupper(), f"{s.name} needs a real description"
        assert callable(s.run), f"{s.name} has no implementation"
        schema = s.tool_schema()
        assert schema["input_schema"]["type"] == "object"
        for pname, spec in s.parameters.items():
            assert isinstance(spec, dict) and "type" in spec, f"{s.name}.{pname} has no type"
        for required in schema["input_schema"]["required"]:
            assert required in s.parameters, f"{s.name} requires undeclared {required}"


def test_registry_covers_every_family():
    names = {s.name for s in build_registry().all()}
    for expected in ("set_volume", "run_shell", "web_search", "security_audit_local",
                     "remember", "clock", "list_files", "set_timer", "focus_session",
                     "clipboard_read", "note_add", "network_status", "ping_host",
                     "media_control", "dark_mode", "git_status", "machine_health",
                     "weather", "calculate", "convert_units", "reminder_create",
                     "calendar_events", "protocol_run", "self_diagnose", "self_lesson"):
        assert expected in names, f"missing skill {expected}"
    assert len(names) >= 75, f"only {len(names)} skills registered"


def test_skills_never_crash_the_assistant():
    """A broken skill must surface as a failed Result, not an exception."""
    reg = Registry()
    reg.add(Skill("explodes", "Raises on purpose.",
                  lambda a, c: (_ for _ in ()).throw(RuntimeError("boom")),
                  risk=Risk.READ_ONLY))
    gate = Gate("trusted", lambda p: True, FakeLog())
    res = reg.execute("explodes", {}, _ctx(gate, reg))
    assert not res.ok and "boom" in res.detail + res.speech


# ── Presentation degrades ───────────────────────────────────────────────────
def test_panels_measure_visible_width_not_bytes():
    coloured = theme.paint("abc", theme.BRASS)
    assert theme.visible_len(coloured) == 3
    out = theme.panel("status", [("brain", "offline"), ("voice", "Daniel")])
    widths = {len(ln) for ln in out.splitlines()}
    assert len(widths) == 1, f"panel rows are ragged: {widths}"
    theme.boot([("a", True, "up"), ("b", None, "degraded"), ("c", False, "missing")],
               out=lambda *_: None, animate=False)


def test_config_reports_the_brain_that_will_actually_run():
    """A key with no SDK installed is the commonest half-configured state, and
    the startup banner must not claim a brain that cannot run."""
    cfg = config.load()
    object.__setattr__(cfg, "brain", "anthropic")
    object.__setattr__(cfg, "anthropic_key", "sk-not-a-real-key")
    import importlib.util
    sdk_present = importlib.util.find_spec("anthropic") is not None
    expected = "anthropic" if sdk_present else "offline"
    assert cfg.effective_brain() == expected
    note = dict(cfg.describe())["brain"]
    if not sdk_present:
        assert "package" in note, f"the reason must be stated, got {note!r}"
    object.__setattr__(cfg, "anthropic_key", "")
    assert cfg.effective_brain() == "offline"
    assert "no API key" in dict(cfg.describe())["brain"]


def test_config_creates_its_directories_and_reports_them():
    cfg = config.load()
    for d in (cfg.log_dir, cfg.memory_dir, cfg.notes_dir, cfg.state_dir):
        assert d.is_dir(), f"{d} was not created"
    keys = {k for k, _ in cfg.describe()}
    assert {"brain", "confirm policy", "home city"} <= keys


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        t(name, fn)

print(f"\n\n{_pass} passed, {_fail} failed")
for f in _fails:
    print("  FAIL", f)
sys.exit(1 if _fail else 0)
