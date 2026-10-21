"""Monitors: metric rules evaluated after every collection. When a rule holds for its window, rAIden reacts:
`investigate` opens an incident and starts the pipeline, `notify` opens an incident for people only, `suggest` adds an
issue or suggestion chip to the server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fmt
from .catalog import BY_ID
from .db import utcnow
from .models import Monitor, MonitorState, Server

GIB = 1024**3

BUILTIN: list[dict[str, Any]] = [
    dict(key="db-down", name="Database not reachable", metric="is_db_alive", aggregation="max", operator="<", threshold=1, window_minutes=1,
         severity="sev1", category="availability", action="investigate", title="{server} is not accepting connections",
         description="No successful connection for a whole minute.", runbook="Crash loop, disk full, max_connections, a hung shutdown: check the log and the host."),
    dict(key="connections-saturated", name="Connections near the limit", metric="connections_percent", aggregation="avg", operator=">", threshold=90,
         window_minutes=2, severity="sev2", category="connections", action="investigate", title="Connections at {value} of max_connections",
         description="New clients start to be refused with 'remaining connection slots are reserved'.",
         runbook="Find the role or application holding the slots (pg_stat_activity by usename/application_name/state), end only the culprit, cap that role."),
    dict(key="connections-pressure", name="Connection pressure", metric="connections_percent", aggregation="avg", operator=">", threshold=80,
         window_minutes=15, severity="sev3", category="connections", action="suggest", title="Connections stay above 80% of max_connections",
         description="A connection pooler (PgBouncer) or lower pool sizes leave headroom for spikes."),
    dict(key="lock-queue", name="Lock queue", metric="blocked_sessions", aggregation="avg", operator=">", threshold=5, window_minutes=1,
         severity="sev2", category="locks", action="investigate", title="Lock queue: {value} sessions blocked",
         description="Sessions wait for a heavyweight lock. A DDL statement queued behind an old transaction freezes every writer.",
         runbook="pg_blocking_pids: find the root blocker (often idle in transaction), cancel the waiting DDL or end the root, then lock_timeout for DDL roles."),
    dict(key="long-transaction", name="Long-running transaction", metric="longest_transaction_time_sec", aggregation="min", operator=">", threshold=1800,
         window_minutes=2, severity="sev3", category="activity", action="investigate", title="A transaction has been open for {value}",
         description="It holds locks and the xmin horizon: vacuum cannot clean up behind it."),
    dict(key="idle-in-transaction", name="Idle in transaction", metric="idle_in_transaction", aggregation="avg", operator=">", threshold=20,
         window_minutes=5, severity="sev3", category="activity", action="investigate", title="{value} sessions idle in transaction",
         description="Applications open transactions and stop talking: locks and bloat follow.",
         runbook="Group by usename/application_name; set idle_in_transaction_session_timeout for the role."),
    dict(key="cpu-high", name="CPU saturated", metric="cpu_percent", aggregation="avg", operator=">", threshold=90, window_minutes=3,
         severity="sev2", category="cpu", action="investigate", title="CPU at {value} for 3 minutes",
         runbook="Top queries by total time and calls since the spike; a new query without an index is the usual suspect."),
    dict(key="memory-high", name="Memory exhausted", metric="memory_percent", aggregation="avg", operator=">", threshold=95, window_minutes=5,
         severity="sev2", category="memory", action="investigate", title="Memory at {value}"),
    dict(key="storage-critical", name="Storage almost full", metric="storage_percent", aggregation="avg", operator=">", threshold=90, window_minutes=2,
         severity="sev2", category="storage", action="investigate", title="Storage at {value}",
         runbook="What grows: pg_wal (archiving failing, slots retaining WAL), temp files, a table or index. Never delete files in PGDATA."),
    dict(key="storage-warning", name="Storage filling up", metric="storage_percent", aggregation="avg", operator=">", threshold=80, window_minutes=15,
         severity="sev3", category="storage", action="suggest", title="Storage above 80%"),
    dict(key="replica-lag", name="Replica lag", metric="physical_replication_delay_in_seconds", aggregation="avg", operator=">", threshold=60,
         window_minutes=2, severity="sev2", category="replication", action="investigate", title="Replica lag {value}", scope={"role": "replica"},
         runbook="On the standby: replay conflicts with long queries (max_standby_streaming_delay), I/O, network. On the primary: pg_stat_replication."),
    dict(key="slot-retention", name="Replication slot retains WAL", metric="replication_slot_retained_bytes", aggregation="max", operator=">",
         threshold=20 * GIB, window_minutes=5, severity="sev2", category="replication", action="investigate", title="Replication slots retain {value} of WAL",
         runbook="An inactive slot keeps WAL forever: find its consumer; dropping it is a decision for a person; cap max_slot_wal_keep_size."),
    dict(key="wraparound", name="Transaction ID wraparound risk", metric="maximum_used_transactionIDs", aggregation="last", operator=">", threshold=1_000_000_000,
         window_minutes=1, severity="sev2", category="wraparound", action="investigate", title="Transaction ID age {value}",
         runbook="Find what holds the xmin horizon: long transactions, prepared transactions, replication slots; then VACUUM (FREEZE) the oldest tables."),
    dict(key="wraparound-watch", name="Transaction ID age growing", metric="maximum_used_transactionIDs", aggregation="avg", operator=">", threshold=500_000_000,
         window_minutes=30, severity="sev3", category="wraparound", action="suggest", title="Transaction ID age above 500 million"),
    dict(key="prepared-xact", name="Orphaned prepared transaction", metric="prepared_xact_oldest_seconds", aggregation="min", operator=">", threshold=3600,
         window_minutes=2, severity="sev3", category="wraparound", action="investigate", title="A prepared transaction has been waiting for {value}"),
    dict(key="deadlocks", name="Deadlocks", metric="deadlocks", aggregation="avg", operator=">", threshold=4, window_minutes=5, severity="sev3",
         category="locks", action="investigate", title="{value} deadlocks per minute"),
    dict(key="temp-spill", name="Queries spilling to disk", metric="temp_bytes", aggregation="avg", operator=">", threshold=4 * GIB, window_minutes=5,
         severity="sev3", category="performance", action="investigate", title="{value} of temporary files per minute"),
    dict(key="cache-hit", name="Low cache hit ratio", metric="cache_hit_ratio", aggregation="avg", operator="<", threshold=90, window_minutes=15,
         severity="sev4", category="performance", action="suggest", title="Cache hit ratio {value}"),
    dict(key="checkpoint-pressure", name="Checkpoints forced by WAL volume", metric="checkpoints_requested", aggregation="avg", operator=">", threshold=1,
         window_minutes=15, severity="sev4", category="configuration", action="suggest", title="Checkpoints are forced by WAL volume: max_wal_size is too small"),
    dict(key="archive-failing", name="WAL archiving fails", metric="archive_failed", aggregation="avg", operator=">", threshold=0, window_minutes=5,
         severity="sev2", category="backup", action="investigate", title="WAL archiving is failing",
         runbook="archive_command errors in the log; until it works, pg_wal grows and point-in-time recovery has a gap."),
    dict(key="pending-restart", name="Settings waiting for a restart", metric="pending_restart_settings", aggregation="last", operator=">", threshold=0,
         window_minutes=1, severity="sev3", category="configuration", action="suggest",
         title="Settings waiting for a restart: {value}", description="Somebody changed the configuration files; the next restart activates it. Review before the maintenance window."),
    dict(key="checksum-failures", name="Data checksum failures", metric="checksum_failures", aggregation="max", operator=">", threshold=0, window_minutes=1,
         severity="sev1", category="corruption", action="investigate", title="Data page checksum failures",
         runbook="Storage corruption: find the relation (amcheck, the log), repair from a point-in-time clone; never pg_resetwal."),
    dict(key="bloat", name="Dead rows piling up", metric="bloat_percent", aggregation="avg", operator=">", threshold=35, window_minutes=30, severity="sev4",
         category="vacuum", action="suggest", title="Dead rows at {value} of user tables"),
]

OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}


@dataclass
class Transition:
    monitor_id: int
    server_id: str
    old: str
    new: str
    value: float | None


def seed_builtin(s: Session) -> int:
    added = 0
    have = {m.key for m in s.execute(select(Monitor)).scalars()}
    for b in BUILTIN:
        if b["key"] in have:
            continue
        s.add(Monitor(key=b["key"], name=b["name"], description=b.get("description", ""), metric=b["metric"], aggregation=b["aggregation"],
                      operator=b["operator"], threshold=float(b["threshold"]), window_minutes=b["window_minutes"], severity=b["severity"],
                      category=b["category"], action=b["action"], runbook=b.get("runbook", ""), builtin=True, scope=b.get("scope", {})))
        added += 1
    return added


def title_for(mon: Monitor, server_name: str, value: float | None) -> str:
    template = next((b["title"] for b in BUILTIN if b["key"] == mon.key), None) or f"{mon.name}: {{value}}"
    return template.format(server=server_name, value=fmt.value(mon.metric, value))


def in_scope(mon: Monitor, srv: Server) -> bool:
    scope = mon.scope or {}
    envs, servers, tags = scope.get("environments") or [], scope.get("servers") or [], scope.get("tags") or {}
    if envs and srv.environment not in envs:
        return False
    if servers and srv.id not in servers:
        return False
    if srv.id in (scope.get("exclude") or []):
        return False
    role = scope.get("role")  # "replica" | "primary": a standby's own lag is its incident, not its primary's
    if role and (role == "replica") != bool((srv.info or {}).get("in_recovery")):
        return False
    return all((srv.tags or {}).get(k) == v for k, v in tags.items())


def aggregate(samples: list[tuple[int, dict[str, float]]], key: str, agg: str) -> tuple[float | None, int]:
    vals = [d[key] for _, d in samples if key in d]
    if not vals:
        return None, 0
    if agg == "max":
        return max(vals), len(vals)
    if agg == "min":
        return min(vals), len(vals)
    if agg == "last":
        return vals[-1], len(vals)
    return sum(vals) / len(vals), len(vals)


def evaluate_server(
    s: Session, srv: Server, monitors: list[Monitor], window_of: Any, now_ts: int, interval_s: int
) -> list[Transition]:
    """window_of(seconds) -> recent samples of this server. Returns the state changes (and persists the states)."""
    out: list[Transition] = []
    states = {st.monitor_id: st for st in s.execute(select(MonitorState).where(MonitorState.server_id == srv.id)).scalars()}
    for mon in monitors:
        if not mon.enabled or not in_scope(mon, srv) or mon.metric not in BY_ID:
            continue
        key = f"{mon.metric}[{mon.dimension}]" if mon.dimension else mon.metric
        win = max(1, mon.window_minutes) * 60
        samples = window_of(win)
        value, n = aggregate(samples, key, mon.aggregation)
        need = 1 if win <= 60 else max(2, int(0.6 * win / max(1, interval_s)))
        st = states.get(mon.id)
        if st is None:
            st = MonitorState(monitor_id=mon.id, server_id=srv.id, state="ok", since=utcnow(), value=None)
            s.add(st)
        if value is None:
            new = "nodata"
        elif n < need:
            new = st.state if st.state != "nodata" else "ok"  # not enough samples yet: keep the state
        else:
            new = "firing" if OPS.get(mon.operator, OPS[">"])(value, mon.threshold) else "ok"
        st.value = value
        st.evaluated_at = utcnow()
        if new != st.state:
            out.append(Transition(mon.id, srv.id, st.state, new, value))
            st.state = new
            st.since = utcnow()
    return out
