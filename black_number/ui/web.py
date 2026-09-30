"""The local web console — the assistant in a browser, on 127.0.0.1:3002.

This is a second front end, not a second assistant. It builds the same runtime as
the terminal does, so the gate, the registry and the risk policy are identical;
what it supplies is a browser-shaped way to ask a yes/no question and a browser-
shaped way to display a turn.

Security is the interesting part, because this endpoint can control the machine.
Four properties, each load-bearing and each tested:

  It binds loopback only. 127.0.0.1, never 0.0.0.0. A machine-control API must
  not be reachable from the LAN, and that is not a configuration option here.

  Every /api/ call needs a token in the `X-BN-Token` header. A page on another
  origin cannot set a custom header without a CORS preflight, and this server
  answers no preflight and sends no CORS headers — so the browser refuses the
  request before it is made. Without this, any website open in your browser could
  POST to localhost:3002 and run skills.

  A cross-origin `Origin` header is rejected outright, as a second line.

  Confirmation fails closed. The gate is synchronous: it calls `ask`, which
  publishes a prompt to the browser and blocks. If the answer does not arrive
  within the timeout — the tab was closed, the browser crashed, nobody was
  looking — `ask` returns False. Silence is never consent.

One assistant, one lock. `Agent` and `Memory` hold conversation state and are not
thread-safe, so requests that drive the agent are serialised. Confirmations and
event polling deliberately do *not* take that lock, because they must be able to
answer while a turn is blocked waiting on them — that ordering is what stops the
whole thing deadlocking on its own safety prompt.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..core import config, theme
from ..core.log import open_log
from ..core.scheduler import Job, human_delta
from . import runtime

DEFAULT_PORT = 3002
HOST = "127.0.0.1"                 # loopback only — deliberately not configurable
CONFIRM_TIMEOUT = 120.0            # seconds before an unanswered prompt is denied
MAX_EVENTS = 800
MAX_BODY = 64 * 1024
PAGE = Path(__file__).with_name("console.html")


class Hub:
    """Bridges the synchronous assistant to an asynchronous browser.

    Events are a monotonically-numbered ring buffer the browser polls with a
    cursor, so a client that misses a poll catches up on the next one instead of
    losing anything, and a client that has been away a long time is told to
    resynchronise rather than silently skipping events.
    """

    def __init__(self, confirm_timeout: float = CONFIRM_TIMEOUT):
        self.confirm_timeout = confirm_timeout
        self._cv = threading.Condition()
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._next_id = 1
        self._pending: dict[int, dict[str, Any]] = {}

    # ── publishing ──────────────────────────────────────────────────────────
    def _append_locked(self, kind: str, **fields: Any) -> int:
        eid = self._next_id
        self._next_id += 1
        self._events.append({"id": eid, "ts": round(time.time(), 3), "kind": kind, **fields})
        self._cv.notify_all()
        return eid

    def publish(self, kind: str, **fields: Any) -> int:
        with self._cv:
            return self._append_locked(kind, **fields)

    def since(self, cursor: int) -> dict[str, Any]:
        with self._cv:
            rows = [e for e in self._events if e["id"] > cursor]
            oldest = self._events[0]["id"] if self._events else self._next_id
            return {
                "events": rows,
                "cursor": self._next_id - 1,
                # True when the client's cursor fell off the back of the buffer.
                "gap": cursor + 1 < oldest and cursor != 0,
                "pending": self.pending(),
            }

    # ── confirmation, the safety boundary ───────────────────────────────────
    def ask(self, prompt: str) -> bool:
        """`Gate.ask` for a browser. Blocks the calling turn; denies on timeout."""
        answered = threading.Event()
        with self._cv:
            confirm_id = self._append_locked("confirm", prompt=prompt, confirm_id=self._next_id)
            self._pending[confirm_id] = {"prompt": prompt, "event": answered, "approved": False}

        in_time = answered.wait(self.confirm_timeout)

        with self._cv:
            record = self._pending.pop(confirm_id, None)
            approved = bool(in_time and record and record["approved"])
            self._append_locked("confirm_done", confirm_id=confirm_id, approved=approved,
                                timed_out=not in_time, prompt=prompt)
        return approved

    def resolve(self, confirm_id: int, approved: bool) -> bool:
        """Answer a pending prompt. False if there is no such prompt (or it expired)."""
        with self._cv:
            record = self._pending.get(confirm_id)
            if record is None:
                return False
            record["approved"] = bool(approved)
            record["event"].set()
            return True

    def pending(self) -> list[dict[str, Any]]:
        with self._cv:
            return [{"confirm_id": cid, "prompt": rec["prompt"]}
                    for cid, rec in sorted(self._pending.items())]


class HubLog:
    """Wraps the real transcript log so console output also reaches the browser.

    Mirrors `Log`'s interface exactly, including `event`'s positional-only first
    parameter — the collision that made that necessary is documented in core/log.py
    and a wrapper that got it wrong would reintroduce the same crash.
    """

    def __init__(self, log, hub: Hub):
        self._log = log
        self._hub = hub

    @property
    def path(self):
        return self._log.path

    def event(self, kind: str, /, **fields: Any) -> None:
        self._log.event(kind, **fields)

    def _both(self, kind: str, msg: str) -> None:
        getattr(self._log, kind)(msg)
        self._hub.publish(kind, text=msg)

    def system(self, msg: str) -> None:
        self._both("system", msg)

    def you(self, msg: str) -> None:
        self._both("you", msg)

    def bn(self, msg: str) -> None:
        self._both("bn", msg)

    def warn(self, msg: str) -> None:
        self._both("warn", msg)

    def ok(self, msg: str) -> None:
        self._both("ok", msg)

    def close(self) -> None:
        self._log.close()


class Console(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, *, rt, hub, token):
        super().__init__(addr, handler)
        self.rt = rt
        self.hub = hub
        self.token = token
        # Read the port back from the socket rather than from `addr`. Asking for
        # port 0 means "any free port", and the requested value stays 0 — so
        # building the origin list from `addr` produced origins matching nothing
        # and the console rejected its own browser.
        self.port = self.server_address[1]
        self.allowed_origins = {
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        }
        self.agent_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "BlackNumber"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ── plumbing ────────────────────────────────────────────────────────────
    def log_message(self, fmt, *args):       # the transcript is the log, not stderr
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # No CORS headers, ever: another origin must not be able to read this.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                              # the tab closed mid-response

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _authorized(self) -> bool:
        if self.headers.get("X-BN-Token") != self.server.token:
            return False
        origin = self.headers.get("Origin")
        return not origin or origin in self.server.allowed_origins

    # ── routes ──────────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._page()
        if path == "/health":                 # unauthenticated liveness only
            return self._json(200, {"ok": True, "service": "black-number"})
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._authorized():
            return self._json(403, {"error": "bad or missing token"})
        if path == "/api/state":
            return self._json(200, self._state())
        if path == "/api/events":
            query = parse_qs(urlparse(self.path).query)
            try:
                cursor = int((query.get("since") or ["0"])[0])
            except ValueError:
                cursor = 0
            return self._json(200, self.server.hub.since(max(0, cursor)))
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._authorized():
            return self._json(403, {"error": "bad or missing token"})
        body = self._body()

        if path == "/api/confirm":
            # Deliberately takes no agent lock: this must answer while a turn is
            # blocked waiting for it.
            try:
                confirm_id = int(body.get("confirm_id"))
            except (TypeError, ValueError):
                return self._json(400, {"error": "confirm_id must be a number"})
            resolved = self.server.hub.resolve(confirm_id, bool(body.get("approved")))
            return self._json(200 if resolved else 409,
                              {"resolved": resolved,
                               "error": None if resolved else "no such prompt (it may have expired)"})

        if path == "/api/ask":
            text = str(body.get("text") or "").strip()
            if not text:
                return self._json(400, {"error": "text is required"})
            if len(text) > 4000:
                return self._json(400, {"error": "that request is too long"})
            rt = self.server.rt
            if not self.server.agent_lock.acquire(timeout=0.1):
                return self._json(429, {"error": "still working on the previous request"})
            try:
                rt.log.you(text)
                result = rt.agent.handle(text)
                rt.log.bn(result.speech)
                rt.speak(result.speech)
                payload = {"ok": result.ok, "speech": result.speech, "detail": result.detail}
            finally:
                self.server.agent_lock.release()
            return self._json(200, payload)

        return self._json(404, {"error": "not found"})

    # ── payloads ────────────────────────────────────────────────────────────
    def _page(self) -> None:
        try:
            html = PAGE.read_text(encoding="utf-8")
        except OSError:
            return self._send(500, b"console.html is missing", "text/plain; charset=utf-8")
        # The token is injected server-side so it never travels in a URL, where it
        # would end up in history and in any Referer header.
        html = html.replace("__BN_TOKEN__", self.server.token)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _state(self) -> dict[str, Any]:
        rt = self.server.rt
        by_risk: dict[str, list[str]] = {}
        for skill in rt.registry.all():
            by_risk.setdefault(skill.risk.name, []).append(skill.name)
        from ..skills import protocols as P

        protos = P.load_protocols(rt.ctx)
        now = time.time()
        return {
            "config": [{"key": k, "value": v} for k, v in rt.cfg.describe()],
            "boot": [{"label": lbl, "state": st, "note": note}
                     for lbl, st, note in rt.boot_checks()],
            "skills": {level: sorted(names) for level, names in by_risk.items()},
            "skill_count": len(rt.registry.all()),
            "gated_count": sum(1 for s in rt.registry.all() if s.risk.value >= 1),
            "protocols": [{"name": n, "description": b.get("description", ""),
                           "steps": [s["skill"] for s in b["steps"]],
                           "builtin": bool(b.get("builtin"))}
                          for n, b in sorted(protos.items())],
            "timers": [{"id": j.id, "label": j.label, "kind": j.kind,
                        "due_in": human_delta(j.remaining(now))}
                       for j in rt.scheduler.pending()],
            "memory": {"facts": len([f for f in rt.memory.facts if f.get("kind") != "lesson"]),
                       "lessons": len(rt.memory.of_kind("lesson"))},
            "brain": rt.brain.name,
        }


def serve(port: int = DEFAULT_PORT, *, open_browser: bool = True) -> int:
    """Run the local console. Returns a process exit code."""
    import secrets

    cfg = config.load()
    base_log = open_log(cfg.log_dir)
    hub = Hub()
    log = HubLog(base_log, hub)
    token = secrets.token_urlsafe(24)

    def on_due(job: Job, late: float) -> None:
        prefix = {"reminder": "Reminder", "timer": "Timer", "focus": "Focus"}.get(job.kind, "")
        line = f"{prefix}: {job.label}" if prefix else job.label
        if late > 90:
            line += f"  (this was due {int(late // 60)} minutes ago)"
        log.bn(line)
        hub.publish("fired", label=job.label, kind=job.kind, late_seconds=round(late, 1))
        rt.speak(line)

    rt = runtime.build(
        cfg,
        hub.ask,                                   # the browser is the safety boundary
        log=log,
        speak_hook=lambda text: hub.publish("speech", text=text),
        on_due=on_due,
        want_stt=False,                            # the browser supplies the input
    )

    url = f"http://{HOST}:{port}"
    try:
        httpd = Console((HOST, port), Handler, rt=rt, hub=hub, token=token)
    except OSError as e:
        base_log.warn(f"Can't listen on {HOST}:{port} — {e}")
        base_log.system(f"Something else may already be using port {port}. "
                        f"Try: python3 -m black_number serve --port {port + 1}")
        rt.shutdown(drain=False)
        return 1

    theme.boot(rt.boot_checks())
    print(theme.panel("web console", [
        ("url", url),
        ("bound to", f"{HOST} only (not reachable from the network)"),
        ("auth", "token injected into the page; /api needs the X-BN-Token header"),
        ("confirmations", f"asked in the browser, denied after {int(CONFIRM_TIMEOUT)}s of silence"),
    ]))
    base_log.system(f"Web console on {url} — ctrl-c to stop.")

    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
        base_log.system("Stopping the web console.")
    finally:
        httpd.shutdown()
        httpd.server_close()
        rt.shutdown(drain=False)
    return 0
