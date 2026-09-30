"""The confirmation gate — the single checkpoint every risky action passes.

Skills do not decide for themselves whether to ask permission. They call
`gate.confirm(...)` and the gate applies the policy uniformly. Centralising it
means there is exactly one place to audit, one place to change the rules, and no
way for a skill to quietly skip the prompt.

The prompt function is injected, so the same gate works from the CLI (reads a
line), from voice (asks aloud and listens for yes/no), and from tests (a stub
that answers automatically).
"""

from __future__ import annotations

from typing import Callable

from ..skills.base import Risk
from . import policy


class Gate:
    def __init__(self, mode: str, ask: Callable[[str], bool], log, speak=None):
        self.mode = mode
        self._ask = ask
        self._log = log
        self._speak = speak or (lambda s: None)
        if mode == "never":
            log.warn("Confirmation policy is 'never' — actions run unattended.")

    def confirm(self, action: str, risk: Risk, caution: str = "") -> bool:
        """Return True if the action may proceed. Logs the decision either way."""
        if not policy.needs_confirmation(risk, self.mode):
            self._log.event("gate", action=action, risk=risk.name, decision="auto-allow")
            return True

        label = f"{action}"
        if caution:
            label += f"  ({caution})"
        self._log.event("gate", action=action, risk=risk.name, decision="prompt", caution=caution)

        # Speak the ask when voice is active, so a hands-free user hears it.
        self._speak(f"Confirm: {action}." + (f" {caution}." if caution else ""))
        approved = self._ask(label)
        self._log.event("gate", action=action, decision="approved" if approved else "denied")
        if not approved:
            self._log.system("Cancelled.")
        return approved

    def require_authorization(self, target: str) -> bool:
        """Extra gate for the security skill. Even under a permissive confirm
        policy, an action against a target requires the user to affirm, in the
        moment, that they are authorized to test it. Never auto-allowed."""
        prompt = (
            f"Confirm you are authorized to run security checks against '{target}'. "
            "Only proceed for systems you own or have written permission to test"
        )
        self._speak(f"Do you have authorization to test {target}?")
        approved = self._ask(prompt)
        self._log.event(
            "authorization", target=target, decision="approved" if approved else "denied"
        )
        return approved
