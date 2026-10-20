"""Turn PostgreSQL jsonlog lines into classified events, and bursts of events into incidents.

The policy is deliberately simple and explainable:

* every ERROR/FATAL/PANIC line that is not ordinary application noise carries a weight;
* crash / corruption / destructive-DDL / resource / wraparound lines are *critical* and wake the agent immediately;
* events are debounced into one incident per burst (a crash storm of 400 lines is ONE incident);
* the agent's own SQL errors never wake the agent.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

CRITICAL_WEIGHT = 60

# Exit codes 0 and 1 are normal for backends and background workers (PostgreSQL itself does not treat them as crashes;
# the logical replication launcher logs "exited with exit code 1" on every clean shutdown).
PROCESS_DEATH = r"was terminated by signal|exited with exit code (?![01]\b)\d+"
PROCESS_DEATH_RE = re.compile(PROCESS_DEATH, re.I)
CRASH_RE = re.compile(
    PROCESS_DEATH + r"|terminating any other active server processes|"
    r"all server processes terminated; reinitializing|database system was not properly shut down|"
    r"automatic recovery in progress|terminating connection because of crash of another server process",
    re.I,
)
CORRUPTION_RE = re.compile(
    r"missing chunk number|unexpected chunk number|invalid page in block|could not read block|"
    r"page verification failed|checksum failure|invalid memory alloc request size|"
    r"found xmin .* from before relfrozenxid|could not access status of transaction|"
    r"unexpected data beyond EOF|contains (corrupted|unexpected zero) page|is not a btree|"
    r"could not open (file|relation)|(index|relation) .* (is corrupted|appears to be corrupt)",
    re.I,
)
DESTRUCTIVE_RE = re.compile(r"^\s*(statement:\s*)?(drop\s+(table|database|schema)|truncate)\b", re.I)
RESOURCE_RE = re.compile(
    r"No space left on device|could not extend file|out of memory|too many clients already|"
    r"remaining connection slots are reserved|could not write to file",
    re.I,
)
WRAP_RE = re.compile(r"database is not accepting commands|must be vacuumed within|wraparound", re.I)
ARCHIVE_RE = re.compile(r"archive command (failed|was terminated)", re.I)
RECOVERY_MODE_RE = re.compile(r"the database system is (in recovery mode|starting up|shutting down)", re.I)
ADMIN_SHUTDOWN_RE = re.compile(r"terminating connection due to administrator command", re.I)
# a planned shutdown/restart: the clients it disconnects (57P01, "shutting down", "starting up") are not an incident
SHUTDOWN_REQUEST_RE = re.compile(r"received (fast|smart|immediate) shutdown request", re.I)
# somebody reloaded the configuration: never an incident by itself, but a reason to run the patrol checks right now
RELOAD_RE = re.compile(
    r"received SIGHUP, reloading configuration files|cannot be changed without restarting the server|"
    r"configuration file .* contains errors",
    re.I,
)

# SQLSTATE classes that are ordinary application behaviour and never wake the agent.
IGNORED_CLASSES = {"22", "23", "25", "28", "2B", "3D", "3F", "42"}
IGNORED_MESSAGES = re.compile(
    r"canceling statement due to user request|canceling autovacuum task|"
    r"could not receive data from client: Connection reset by peer|unexpected EOF on client connection",
    re.I,
)
BIG_NUMBER = re.compile(r"\b\d{3,}\b")


def normalize(message: str) -> str:
    """Collapse PIDs, OIDs and similar noise so that repeats of one problem share a signature."""
    return BIG_NUMBER.sub("N", message)[:140]


@dataclass
class LogEvent:
    ts: float
    iso: str
    severity: str
    sqlstate: str
    message: str
    pid: int | None = None
    backend_type: str = ""
    user: str = ""
    db: str = ""
    statement: str = ""
    kind: str = ""
    weight: int = 0
    signature: str = ""

    def one_line(self) -> str:
        who = f" [{self.backend_type or '-'} pid={self.pid}]" if self.pid else ""
        return f"{self.iso} {self.severity} {self.sqlstate or '-----'}{who} {self.message[:220]}"


def _parse_ts(value: str) -> tuple[float, str]:
    try:
        stamp = value.replace(" UTC", "")
        dt = datetime.strptime(stamp[:23], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        return dt.timestamp(), dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:
        now = time.time()
        return now, datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_jsonlog_line(line: str) -> LogEvent | None:
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    ts, iso = _parse_ts(str(d.get("timestamp", "")))
    return LogEvent(
        ts=ts,
        iso=iso,
        severity=str(d.get("error_severity", "")).upper(),
        sqlstate=str(d.get("state_code", "") or ""),
        message=str(d.get("message", "") or ""),
        pid=d.get("pid"),
        backend_type=str(d.get("backend_type", "") or ""),
        user=str(d.get("user", "") or ""),
        db=str(d.get("dbname", "") or ""),
        statement=str(d.get("statement", "") or ""),
    )


def classify(ev: LogEvent) -> LogEvent:
    """Fill in kind / weight / signature. weight == 0 means 'not interesting'."""
    msg, sev, st = ev.message, ev.severity, ev.sqlstate
    kind, weight = "", 0
    own = ev.user == "raiden"  # the agent's own SQL must never wake the agent

    text_for_ddl = ev.statement or msg
    if sev == "PANIC":
        kind, weight = "panic", 100
    elif CRASH_RE.search(msg):
        kind, weight = "crash", 100
    elif st in ("XX001", "XX002") or CORRUPTION_RE.search(msg):
        kind, weight = "corruption", 100
    elif sev == "LOG" and DESTRUCTIVE_RE.search(text_for_ddl):
        kind, weight = "destructive_ddl", 100
    elif WRAP_RE.search(msg):
        kind, weight = "wraparound", 100
    elif RESOURCE_RE.search(msg) or st.startswith("53"):
        kind, weight = "resource", 60
    elif ARCHIVE_RE.search(msg):
        kind, weight = "archive_failure", 60
    elif ADMIN_SHUTDOWN_RE.search(msg) or st == "57P01":
        kind, weight = "fatal_admin", 5
    elif RECOVERY_MODE_RE.search(msg) or st == "57P03":
        kind, weight = "recovery_mode", 3
    elif sev == "LOG" and RELOAD_RE.search(msg):
        kind, weight = "config_reload", 0
    elif sev == "LOG" and SHUTDOWN_REQUEST_RE.search(msg):
        kind, weight = "admin_shutdown", 0
    elif st == "42P01" and not own:
        kind, weight = "missing_relation", 3
    elif sev in ("ERROR", "FATAL") and st[:2] not in IGNORED_CLASSES and not IGNORED_MESSAGES.search(msg):
        kind, weight = "error", 10

    if own and kind in ("error", "fatal_admin", "recovery_mode", "missing_relation", "resource"):
        kind, weight = "", 0
    ev.kind, ev.weight = kind, weight
    ev.signature = f"{kind}:{st or sev}:{normalize(msg)}" if kind else ""
    return ev


@dataclass
class Incident:
    id: str
    opened_ts: float
    opened_iso: str
    reason: str
    kinds: dict[str, int]
    signatures: dict[str, dict]
    events_total: int
    sample: list[str]
    critical: bool
    context: dict = field(default_factory=dict)
    recurrence_of: str | None = None

    @property
    def signature_set(self) -> set[str]:
        return set(self.signatures)

    def prompt_block(self, max_sigs: int = 12, max_samples: int = 25) -> str:
        lines = [
            f"Incident {self.id} opened {self.opened_iso}; {self.events_total} relevant log events; reason: {self.reason}",
            "Event kinds: " + ", ".join(f"{k}={v}" for k, v in sorted(self.kinds.items(), key=lambda kv: -kv[1])),
            "",
            "Top signatures (count | first seen | last seen | sample):",
        ]
        ranked = sorted(self.signatures.values(), key=lambda s: -s["count"])[:max_sigs]
        for s in ranked:
            lines.append(f"- {s['count']:>4} | {s['first']} | {s['last']} | {s['kind']}: {s['sample'][:200]}")
        if self.sample:
            lines += ["", "Sample log lines (earliest first):"]
            lines += [f"  {x}" for x in self.sample[:max_samples]]
        if self.recurrence_of:
            lines += ["", f"NOTE: this is a RECURRENCE of {self.recurrence_of}: the previous fix did not hold."]
        return "\n".join(lines)


class Aggregator:
    """Debounce classified events into incidents."""

    def __init__(
        self,
        trigger_weight: int = 10,
        quiet_s: float = 8.0,
        max_wait_s: float = 30.0,
        suppress_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.trigger_weight = trigger_weight
        self.quiet_s = quiet_s
        self.max_wait_s = max_wait_s
        self.suppress_s = suppress_s
        self.clock = clock
        self._window: deque[tuple[float, LogEvent]] = deque()  # recent events, for weight accumulation
        self._open: list[LogEvent] = []
        self._first = 0.0
        self._last = 0.0
        self._suppressed: dict[str, float] = {}
        self.suppressed_count = 0

    # -- feeding ---------------------------------------------------------------------------------
    def feed(self, ev: LogEvent) -> bool:
        """Returns True if the event was accepted (counted)."""
        if ev.weight <= 0:
            return False
        now = self.clock()
        if ev.weight < CRITICAL_WEIGHT and self._suppressed.get(ev.signature, 0) > now:
            self.suppressed_count += 1
            return False
        if self._open:
            self._open.append(ev)
            self._last = now
            return True
        self._window.append((now, ev))
        while self._window and now - self._window[0][0] > 60:
            self._window.popleft()
        if sum(e.weight for _, e in self._window) >= self.trigger_weight:
            self._open = [e for _, e in self._window]
            self._window.clear()
            self._first = self._last = now
        return True

    def has_open_window(self) -> bool:
        return bool(self._open)

    # -- closing ---------------------------------------------------------------------------------
    def poll(self, recurrence_of: str | None = None) -> Incident | None:
        if not self._open:
            return None
        now = self.clock()
        if now - self._last < self.quiet_s and now - self._first < self.max_wait_s:
            return None
        events, self._open = self._open, []
        return self._build(events, recurrence_of)

    def _build(self, events: Iterable[LogEvent], recurrence_of: str | None) -> Incident:
        events = list(events)
        kinds: Counter[str] = Counter(e.kind for e in events)
        sigs: dict[str, dict] = {}
        for e in events:
            s = sigs.setdefault(
                e.signature, {"count": 0, "kind": e.kind, "first": e.iso, "last": e.iso, "sample": e.message}
            )
            s["count"] += 1
            s["last"] = e.iso
        critical = any(e.weight >= CRITICAL_WEIGHT for e in events)
        top = sorted(kinds.items(), key=lambda kv: -kv[1])[:3]
        reason = "critical event" if critical else "error burst"
        reason += ": " + ", ".join(f"{k} x{v}" for k, v in top)
        # keep a spread of samples: the first few of each kind (crash storms are repetitive)
        picked: list[str] = []
        seen_kind: Counter[str] = Counter()
        for e in events:
            if seen_kind[e.kind] < 6:
                picked.append(e.one_line())
                seen_kind[e.kind] += 1
        iid = "inc-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
        return Incident(
            id=iid,
            opened_ts=events[0].ts,
            opened_iso=events[0].iso,
            reason=reason,
            kinds=dict(kinds),
            signatures=sigs,
            events_total=len(events),
            sample=picked,
            critical=critical,
            recurrence_of=recurrence_of,
        )

    # -- after an incident -----------------------------------------------------------------------
    def mute(self, incident: Incident) -> None:
        """Mute app-level repeats of a handled incident for a while (critical kinds are never muted)."""
        until = self.clock() + self.suppress_s
        for sig, s in incident.signatures.items():
            if s["kind"] in ("error", "fatal_admin", "recovery_mode", "missing_relation"):
                self._suppressed[sig] = until

    def discard_open(
        self,
        handled: set[str] | None = None,
        drop_noncritical: bool = False,
        drop_kinds: Iterable[str] = (),
    ) -> None:
        """After an incident: forget what the agent already saw (signatures in `handled`), low-weight noise caused by
        its own actions (`drop_noncritical`) and stale synthetic events (`drop_kinds`). New critical kinds survive."""
        kinds = set(drop_kinds)
        if handled is None and not drop_noncritical and not kinds:
            self._open, self._window = [], deque()
            return

        def stale(e: LogEvent) -> bool:
            return (
                (handled is not None and e.signature in handled)
                or (drop_noncritical and e.weight < CRITICAL_WEIGHT)
                or e.kind in kinds
            )

        self._open = [e for e in self._open if not stale(e)]
        self._window = deque((t, e) for t, e in self._window if not stale(e))
