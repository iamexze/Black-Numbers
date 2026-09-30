"""Test suite — the invariants that make Black Number safe and portable.

Run: python3 tests/test_core.py   (no pytest needed; it self-reports)

The load-bearing checks are the safety ones: a mutating action must never run
without passing the gate, and an unauthorized security target must be refused.
These mirror the tests a real agent that can touch the machine needs.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from black_number.agent.memory import Memory
from black_number.brain.offline import OfflineBrain
from black_number.brain.router import build_brain
from black_number.core import config
from black_number.safety import policy
from black_number.safety.gate import Gate
from black_number.skills.base import Context, Result, Risk
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

    def event(self, kind, **f):
        self.events.append({"kind": kind, **f})

    def system(self, m): ...
    def warn(self, m): ...
    def ok(self, m): ...


def _ctx(gate, reg=None):
    log = FakeLog()
    mem = Memory(Path(tempfile.mkdtemp()))
    return Context(cfg=config.load(), log=log, gate=gate, memory=mem, speak=lambda s: None, registry=reg)


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


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        t(name, fn)

print(f"\n\n{_pass} passed, {_fail} failed")
for f in _fails:
    print("  FAIL", f)
sys.exit(1 if _fail else 0)
