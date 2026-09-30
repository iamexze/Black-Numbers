"""Security skills — defensive auditing and authorized checks.

Scope, stated plainly and enforced in code:

  This skill audits the security posture of systems the user owns or is
  explicitly authorized to test. It is built for host hardening review, local
  configuration checks, and reachability checks against targets the user
  affirms, in the moment, they are allowed to test. Every action against a
  named target passes `gate.require_authorization`, which is never auto-allowed
  regardless of the confirmation policy.

  It does not include, and this module will not grow to include, mass or
  untargeted scanning, exploitation of third-party systems, credential attacks,
  or techniques whose purpose is evading detection on systems the user does not
  control. Those are out of scope by design, not by omission.

The local audit is the useful default: it reviews this Mac's own security
settings — firewall, disk encryption, screen lock, software updates, sharing
services — and reports what is weak, the way a hardening checklist would.
"""

from __future__ import annotations

import ipaddress
import shutil
import socket
import subprocess

from .base import Context, Result, Risk, Skill


def _run(cmd: list[str], timeout: int = 15) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except Exception as e:
        return False, str(e)


def _audit_local(_a, ctx: Context) -> Result:
    """Review this machine's own hardening. Read-only; nothing is changed."""
    findings: list[str] = []
    good: list[str] = []

    ok, fw = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    if ok:
        (good if "enabled" in fw.lower() else findings).append(
            "Firewall: " + ("on" if "enabled" in fw.lower() else "OFF — turn it on")
        )

    ok, fv = _run(["fdesetup", "status"])
    if ok:
        (good if "On" in fv else findings).append(
            "FileVault disk encryption: " + ("on" if "On" in fv else "OFF — encrypt the disk")
        )

    ok, sleep = _run(["pmset", "-g"])
    # screen lock after sleep
    ok2, lock = _run(["sysadminctl", "-screenLock", "status"])
    if ok2 and "off" in lock.lower():
        findings.append("Screen lock on sleep: OFF — enable it")
    elif ok2:
        good.append("Screen lock: on")

    ok, updates = _run(["softwareupdate", "-l"], timeout=40)
    if ok:
        if "No new software" in updates or "no updates" in updates.lower():
            good.append("Software updates: current")
        else:
            findings.append("Software updates: pending — install them")

    ok, sharing = _run(["launchctl", "list"])
    risky = [s for s in ("ssh", "smbd", "ARDAgent", "screensharing") if s in sharing]
    if risky:
        findings.append(f"Sharing services running: {', '.join(risky)} — disable any you don't use")
    else:
        good.append("No obvious sharing services exposed")

    detail = "NEEDS ATTENTION:\n" + ("\n".join(f"  ✗ {f}" for f in findings) or "  none") + \
             "\n\nOK:\n" + ("\n".join(f"  ✓ {g}" for g in good) or "  none")
    speech = (
        f"Local audit done. {len(findings)} things need attention and {len(good)} look fine."
        if findings
        else f"Local audit done. Everything I checked looks fine — {len(good)} checks passed."
    )
    return Result.say(speech, detail=detail, data={"findings": findings, "ok": good})


def _is_authorized_target(target: str) -> tuple[bool, str]:
    """Only allow targets that are plausibly the user's own: localhost, private
    LAN ranges, or a hostname the user will confirm. Public internet targets are
    refused here regardless of confirmation."""
    t = target.strip()
    if t in ("localhost", "127.0.0.1", "::1", ""):
        return True, "localhost"
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(t))
        if ip.is_private or ip.is_loopback:
            return True, "private network"
        return False, "public internet address"
    except Exception:
        # unresolved hostname — let the authorization prompt decide
        return True, "unresolved host (will require authorization)"


def _check_ports(a: dict, ctx: Context) -> Result:
    """Reachability check: which of a small set of common ports are open on a
    target the user is authorized to test. Deliberately narrow — this is a
    'what am I exposing' check, not a scanner."""
    target = a.get("target", "localhost").strip() or "localhost"

    allowed, why = _is_authorized_target(target)
    if not allowed:
        return Result.fail(
            f"I won't check {target}: it's a {why}. This tool is for systems you own "
            "or are authorized to test — local host and your own network only."
        )

    if target not in ("localhost", "127.0.0.1", "::1"):
        if not ctx.gate.require_authorization(target):
            return Result.fail("Not authorized — skipping.")

    common = {22: "ssh", 80: "http", 443: "https", 445: "smb", 3306: "mysql",
              5432: "postgres", 5900: "vnc", 8080: "http-alt", 3000: "dev", 6379: "redis"}
    open_ports = []
    for port, svc in common.items():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.4)
        try:
            if s.connect_ex((target if target != "localhost" else "127.0.0.1", port)) == 0:
                open_ports.append(f"{port} ({svc})")
        except Exception:
            pass
        finally:
            s.close()
    if not open_ports:
        return Result.say(f"None of the common ports are open on {target}. Good.")
    return Result.say(
        f"{len(open_ports)} common ports are open on {target}.",
        detail="Open: " + ", ".join(open_ports) +
        "\nReview whether each of these should be reachable.",
        data=open_ports,
    )


def skills() -> list[Skill]:
    return [
        Skill(
            "security_audit_local",
            "Audit THIS Mac's own security posture (firewall, encryption, screen "
            "lock, updates, sharing) and report weaknesses. Read-only.",
            _audit_local,
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "security_check_ports",
            "Check which common ports are open on a target you own or are authorized "
            "to test (localhost or your own network only).",
            _check_ports,
            parameters={"target": {"type": "string"}},
            risk=Risk.MUTATING,
            caution="requires you to confirm authorization for any non-local target",
        ),
    ]
