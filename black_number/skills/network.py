"""Network skills — is it up, how fast, and who am I on this LAN.

Every fact here is read from the system rather than assumed, because the shape
of macOS networking keeps moving. Two examples that shaped this module:

  * The active interface is taken from the default route, not hardcoded to en0.
    On a Mac with a dock, a Thunderbolt bridge and a hotspot, en0 is frequently
    not the interface carrying traffic.
  * The Wi-Fi network name is often withheld by the OS. `networksetup
    -getairportnetwork` reports "not associated" on recent macOS even while the
    link is up, and `system_profiler` returns a literal `<redacted>` unless the
    process holds Location permission. So the SSID is reported when it can be
    read and explicitly described as withheld when it cannot. It is never
    guessed, and its absence never implies the network is down.

Everything is read-only except toggling Wi-Fi power, which is REVERSIBLE and
gated — pulling someone's network out from under them mid-call warrants a
question first.
"""

from __future__ import annotations

import re
import socket
import subprocess
import time
import urllib.error
import urllib.request

from .base import Context, Result, Risk, Skill

_UA = "Black Number/0.2 (+local assistant)"
_PUBLIC_IP_SOURCES = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
)
SSID_WITHHELD = "withheld by macOS (needs Location permission)"


def _sh(cmd: list[str], timeout: int = 8) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except FileNotFoundError:
        return False, f"{cmd[0]} isn't available"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as e:
        return False, str(e)


# ── discovery ───────────────────────────────────────────────────────────────
def active_route() -> tuple[str, str]:
    """(interface, gateway) actually carrying traffic, from the routing table."""
    ok, out = _sh(["route", "-n", "get", "default"], timeout=5)
    iface = gateway = ""
    if ok:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("interface:"):
                iface = line.split(":", 1)[1].strip()
            elif line.startswith("gateway:"):
                gateway = line.split(":", 1)[1].strip()
    return iface, gateway


def local_ip(iface: str = "") -> str:
    if iface:
        ok, out = _sh(["ipconfig", "getifaddr", iface], timeout=5)
        if ok and out:
            return out
    # No route, or a platform without ipconfig: ask the kernel which source
    # address it would pick. This opens no connection — UDP connect() only
    # selects a route — so it works offline and needs no permission.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))          # reserved, never routed anywhere
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()


def wifi_device() -> str:
    """The device name of the Wi-Fi port, e.g. en0."""
    ok, out = _sh(["networksetup", "-listallhardwareports"], timeout=8)
    if not ok:
        return ""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "Hardware Port: Wi-Fi":
            for nxt in lines[i + 1:i + 4]:
                if nxt.strip().startswith("Device:"):
                    return nxt.split(":", 1)[1].strip()
    return ""


def ssid(device: str = "") -> str:
    """The current network name, '' if not associated, or SSID_WITHHELD."""
    dev = device or wifi_device()
    if dev:
        ok, out = _sh(["networksetup", "-getairportnetwork", dev], timeout=6)
        if ok and "Current Wi-Fi Network:" in out:
            return out.split("Current Wi-Fi Network:", 1)[1].strip()
        if "not associated" in out.lower():
            # The link may still be up; the command is unreliable on recent
            # macOS, so fall through to system_profiler rather than conclude.
            pass
    ok, out = _sh(["system_profiler", "SPAirPortDataType"], timeout=20)
    if not ok:
        return ""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "Current Network Information:":
            for nxt in lines[i + 1:i + 3]:
                name = nxt.strip().rstrip(":")
                if name and not name.endswith(":") and ":" not in name:
                    return SSID_WITHHELD if name == "<redacted>" else name
            break
    return ""


def public_ip(timeout: int = 6) -> str:
    for url in _PUBLIC_IP_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ip = r.read(64).decode("utf-8", "replace").strip()
            if re.fullmatch(r"[0-9a-fA-F:.]{3,45}", ip):
                return ip
        except Exception:
            continue
    return ""


# ── skills ──────────────────────────────────────────────────────────────────
def _network_status(a: dict, ctx: Context) -> Result:
    iface, gateway = active_route()
    lan = local_ip(iface)
    name = ssid()
    rows = [
        ("interface", iface or "no default route"),
        ("gateway", gateway or "—"),
        ("local IP", lan or "—"),
    ]
    if name == SSID_WITHHELD:
        rows.append(("network", SSID_WITHHELD))
    elif name:
        rows.append(("network", name))
    wanted_public = a.get("include_public", True)
    pub = public_ip() if wanted_public else ""
    if wanted_public:
        rows.append(("public IP", pub or "unreachable"))

    online = bool(gateway and lan)
    if not online:
        speech = "I can't see a default route — this machine looks offline."
    elif wanted_public and not pub:
        speech = f"On the LAN as {lan}, but I couldn't reach the internet."
    else:
        where = name if name and name != SSID_WITHHELD else f"via {iface}"
        speech = f"Online {where}. Local address {lan}."
    return Result(
        online, speech,
        detail="\n".join(f"{k:12} {v}" for k, v in rows),
        data=dict(rows),
    )


def _public_ip_skill(_a, _c) -> Result:
    ip = public_ip()
    if not ip:
        return Result.fail("I couldn't reach anything that would tell me.")
    return Result.say(f"Your public address is {ip}.", data=ip)


def _ping(a: dict, _c) -> Result:
    host = (a.get("host") or "1.1.1.1").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:\-\[\]]{1,253}", host):
        return Result.fail("That isn't a hostname I can ping.")
    count = max(1, min(int(a.get("count") or 3), 10))
    ok, out = _sh(["ping", "-c", str(count), "-t", "10", host], timeout=20)
    loss = re.search(r"([\d.]+)% packet loss", out)
    avg = re.search(r"= [\d.]+/([\d.]+)/", out)
    if not ok and not loss:
        return Result.fail(f"Couldn't reach {host}.", detail=out[:1200])
    lost = float(loss.group(1)) if loss else 100.0
    if lost >= 100:
        return Result(False, f"{host} is not answering — 100% loss.", detail=out[:1200])
    ms = f"{float(avg.group(1)):.0f} ms" if avg else "unknown latency"
    tail = f", {lost:.0f}% loss" if lost else ""
    return Result.say(f"{host} is up, {ms} average{tail}.", detail=out[:1200],
                      data={"host": host, "avg_ms": float(avg.group(1)) if avg else None,
                            "loss_pct": lost})


def _dns_lookup(a: dict, _c) -> Result:
    host = (a.get("host") or "").strip()
    if not host:
        return Result.fail("Look up which hostname?")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return Result.fail(f"{host} doesn't resolve.", detail=str(e))
    addrs = sorted({i[4][0] for i in infos})
    return Result.say(
        f"{host} resolves to {addrs[0]}" + (f" and {len(addrs) - 1} more." if len(addrs) > 1 else "."),
        detail="\n".join(addrs), data=addrs,
    )


def _port_check(a: dict, _c) -> Result:
    """What is listening on a local port — the answer to 'why is 3000 taken'."""
    try:
        port = int(a.get("port"))
    except (TypeError, ValueError):
        return Result.fail("Which port number?")
    if not 1 <= port <= 65535:
        return Result.fail("Ports run from 1 to 65535.")
    ok, out = _sh(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=10)
    if not out.strip():
        return Result.say(f"Nothing is listening on port {port}.", data={"port": port, "free": True})
    rows = [ln for ln in out.splitlines()[1:] if ln.strip()]
    first = rows[0].split() if rows else []
    who = f"{first[0]} (pid {first[1]})" if len(first) > 1 else "something"
    return Result.say(f"Port {port} is held by {who}.", detail=out[:1500],
                      data={"port": port, "free": False})


def _wifi_power(a: dict, _c) -> Result:
    state = str(a.get("state") or "").strip().lower()
    dev = wifi_device()
    if not dev:
        return Result.fail("I couldn't find a Wi-Fi interface on this machine.")
    if state in ("", "status"):
        ok, out = _sh(["networksetup", "-getairportpower", dev], timeout=6)
        on = out.strip().endswith("On")
        return Result.say(f"Wi-Fi is {'on' if on else 'off'}.", detail=out)
    if state == "toggle":
        ok, out = _sh(["networksetup", "-getairportpower", dev], timeout=6)
        state = "off" if out.strip().endswith("On") else "on"
    if state not in ("on", "off"):
        return Result.fail("Say on, off, or toggle.")
    ok, out = _sh(["networksetup", "-setairportpower", dev, state], timeout=12)
    if not ok:
        return Result.fail(f"Couldn't change Wi-Fi power: {out}")
    return Result.say(f"Wi-Fi {state}.")


def _throughput(a: dict, ctx: Context) -> Result:
    """Measure real download throughput by timing a fixed-size transfer."""
    megabytes = max(1, min(int(a.get("megabytes") or 3), 25))
    nbytes = megabytes * 1_000_000
    url = f"https://speed.cloudflare.com/__down?bytes={nbytes}"
    ctx.speak(f"Measuring with a {megabytes} megabyte transfer.")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        start = time.perf_counter()
        read = 0
        with urllib.request.urlopen(req, timeout=40) as r:
            while chunk := r.read(65536):
                read += len(chunk)
        elapsed = time.perf_counter() - start
    except Exception as e:
        return Result.fail(f"The throughput test failed: {e}")
    if elapsed <= 0 or read == 0:
        return Result.fail("The transfer returned nothing measurable.")
    mbps = (read * 8) / elapsed / 1_000_000
    return Result.say(
        f"About {mbps:.1f} megabits per second down.",
        detail=f"{read:,} bytes in {elapsed:.2f}s  ({mbps:.2f} Mbps, {read/elapsed/1e6:.2f} MB/s)",
        data={"mbps": round(mbps, 2), "bytes": read, "seconds": round(elapsed, 3)},
    )


def skills() -> list[Skill]:
    return [
        Skill(
            "network_status",
            "Report connectivity: active interface, gateway, local and public IP, "
            "and the Wi-Fi network name when macOS will disclose it.",
            _network_status,
            parameters={"include_public": {"type": "boolean"}},
            risk=Risk.READ_ONLY,
        ),
        Skill("public_ip", "Report this machine's public IP address.",
              _public_ip_skill, risk=Risk.READ_ONLY),
        Skill(
            "ping_host", "Ping a host and report latency and packet loss.", _ping,
            parameters={"host": {"type": "string"}, "count": {"type": "integer"}},
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "dns_lookup", "Resolve a hostname to its IP addresses.", _dns_lookup,
            parameters={"host": {"type": "string", "required": True}},
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "port_check", "Say what is listening on a local TCP port.", _port_check,
            parameters={"port": {"type": "integer", "required": True}},
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "wifi_power", "Turn Wi-Fi on or off, or report its state.", _wifi_power,
            parameters={"state": {"type": "string", "enum": ["on", "off", "toggle", "status"]}},
            risk=Risk.REVERSIBLE,
            caution="turning Wi-Fi off will drop any active connection",
        ),
        Skill(
            "throughput_test", "Measure download speed with a timed transfer.",
            _throughput,
            parameters={"megabytes": {"type": "integer", "minimum": 1, "maximum": 25}},
            risk=Risk.READ_ONLY,
            caution="downloads a few megabytes",
        ),
    ]
