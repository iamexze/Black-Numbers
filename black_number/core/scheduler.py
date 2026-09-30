"""The scheduler — the assistant's sense of time.

A voice assistant that cannot say "in ten minutes" is a search box. This is the
piece that lets it wait: one background thread holding a queue of jobs ordered by
due time, waking exactly when the next one is due rather than polling.

Design notes worth knowing:

  * The thread sleeps on a Condition with a timeout, so adding a job that is due
    sooner than the current wait wakes the thread immediately. No polling
    interval to tune, and no drift from repeated short sleeps.
  * Jobs are persisted, so a reminder set before a restart still fires. A job
    whose time passed while the process was down fires on the next start and
    reports how late it is, because silently dropping it would be worse.
  * Firing is isolated: a sink that raises is logged and the scheduler keeps
    running. One bad reminder must not stop the clock.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Job:
    id: str
    due: float                      # epoch seconds
    label: str
    kind: str = "timer"             # timer | reminder | focus
    payload: dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.time)

    def remaining(self, now: float | None = None) -> float:
        return self.due - (now if now is not None else time.time())

    def describe(self, now: float | None = None) -> str:
        left = self.remaining(now)
        return f"[{self.id}] {self.label} — {human_delta(left)}"


def human_delta(seconds: float) -> str:
    """'in 4 minutes', '2 hours ago', 'now' — the phrasing a person would use."""
    s = int(abs(round(seconds)))
    if s < 5:
        return "now"
    if s < 60:
        unit = f"{s} second{'s' if s != 1 else ''}"
    elif s < 3600:
        m, rem = divmod(s, 60)
        unit = f"{m} minute{'s' if m != 1 else ''}"
        if m < 10 and rem:
            unit += f" {rem} second{'s' if rem != 1 else ''}"
    elif s < 86400:
        h, rem = divmod(s, 3600)
        m = rem // 60
        unit = f"{h} hour{'s' if h != 1 else ''}"
        if m:
            unit += f" {m} minute{'s' if m != 1 else ''}"
    else:
        d = s // 86400
        unit = f"{d} day{'s' if d != 1 else ''}"
    return f"in {unit}" if seconds > 0 else f"{unit} ago"


class Scheduler:
    """Holds pending jobs and fires them through `sink(job, late_seconds)`."""

    def __init__(self, store: Path, sink: Callable[[Job, float], None], log=None):
        self.store = store
        self._sink = sink
        self._log = log
        self._jobs: dict[str, Job] = {}
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop = False
        self._seq = 0
        self._load()

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="bn-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

    # ── queue ───────────────────────────────────────────────────────────────
    def add(self, label: str, due: float, kind: str = "timer", payload: dict | None = None) -> Job:
        with self._cv:
            self._seq += 1
            job = Job(id=f"t{self._seq}", due=due, label=label, kind=kind, payload=payload or {})
            self._jobs[job.id] = job
            self._save_locked()
            self._cv.notify_all()   # the new job may be due before the current wait ends
        if self._log:
            self._log.event("schedule_add", id=job.id, label=label, job_kind=kind, due=job.due)
        return job

    def cancel(self, job_id: str) -> Job | None:
        with self._cv:
            job = self._jobs.pop(job_id, None)
            if job is not None:
                self._save_locked()
                self._cv.notify_all()
        if job is not None and self._log:
            self._log.event("schedule_cancel", id=job_id, label=job.label)
        return job

    def cancel_all(self) -> int:
        with self._cv:
            n = len(self._jobs)
            self._jobs.clear()
            self._save_locked()
            self._cv.notify_all()
        return n

    def pending(self) -> list[Job]:
        with self._cv:
            return sorted(self._jobs.values(), key=lambda j: j.due)

    def next_due(self) -> Job | None:
        jobs = self.pending()
        return jobs[0] if jobs else None

    # ── the clock ───────────────────────────────────────────────────────────
    def _run(self) -> None:
        while True:
            with self._cv:
                if self._stop:
                    return
                due_now = [j for j in self._jobs.values() if j.remaining() <= 0]
                if not due_now:
                    nxt = min((j.due for j in self._jobs.values()), default=None)
                    # Cap the wait so a system clock change can't strand the thread.
                    wait = 60.0 if nxt is None else max(0.0, min(nxt - time.time(), 60.0))
                    self._cv.wait(timeout=wait)
                    continue
                for j in due_now:
                    self._jobs.pop(j.id, None)
                self._save_locked()
            # Fire outside the lock: a sink that speaks can take seconds, and it
            # must not block someone setting another timer meanwhile.
            for job in sorted(due_now, key=lambda j: j.due):
                self._fire(job)

    def _fire(self, job: Job) -> None:
        late = max(0.0, time.time() - job.due)
        if self._log:
            self._log.event("schedule_fire", id=job.id, label=job.label,
                            job_kind=job.kind, late_seconds=round(late, 1))
        try:
            self._sink(job, late)
        except Exception as e:                      # one bad job never stops the clock
            if self._log:
                self._log.event("schedule_error", id=job.id, error=str(e))

    # ── persistence ─────────────────────────────────────────────────────────
    def _save_locked(self) -> None:
        """Write the queue. Caller holds the lock. Never raises: losing the file
        is a degraded scheduler, not a crashed assistant."""
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(j) for j in self._jobs.values()], indent=1))
            tmp.replace(self.store)                 # atomic, so a crash can't truncate it
        except Exception:
            pass

    def _load(self) -> None:
        if not self.store.exists():
            return
        try:
            rows = json.loads(self.store.read_text())
        except Exception:
            return
        for r in rows if isinstance(rows, list) else []:
            try:
                job = Job(**r)
            except Exception:
                continue
            self._jobs[job.id] = job
            n = int(job.id.lstrip("t") or 0)
            self._seq = max(self._seq, n)           # never reissue a live id
