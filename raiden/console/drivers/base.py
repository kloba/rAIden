"""The driver interface: everything the console (and the AI) can learn from or do to one server.

PostgresDriver speaks SQL to any PostgreSQL 13+; DemoDriver answers from the simulated demo fleet. Tool results are small
tables ({"columns": [...], "rows": [[...]]}) so the UI and the model read the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class DriverError(Exception):
    """A failure the operator (or the model) should read: connection refused, permission denied, timeout..."""


@dataclass
class CollectResult:
    ok: bool
    metrics: dict[str, float] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def table(columns: list[str], rows: list[list[Any]] | list[tuple], note: str = "", **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"columns": columns, "rows": [list(r) for r in rows]}
    if note:
        out["note"] = note
    out.update(extra)
    return out


ACTIONS: dict[str, dict[str, Any]] = {
    # name: risk, what it does, args schema (JSON schema properties), required
    "cancel_backend": {
        "risk": "low", "title": "Cancel a query", "reversible": True,
        "description": "Cancel the running query of one backend (pg_cancel_backend). The session stays connected.",
        "args": {"pid": {"type": "integer"}}, "required": ["pid"],
    },
    "terminate_backend": {
        "risk": "medium", "title": "Terminate a session", "reversible": False,
        "description": "End one backend (pg_terminate_backend); its open transaction is rolled back.",
        "args": {"pid": {"type": "integer"}}, "required": ["pid"],
    },
    "terminate_sessions": {
        "risk": "medium", "title": "Terminate matching sessions", "reversible": False,
        "description": "End client sessions that match a role and/or state and are older than a minimum age (at most 500). A role or a state is required.",
        "args": {"role": {"type": "string"}, "state": {"type": "string"}, "database": {"type": "string"}, "older_than_s": {"type": "integer", "minimum": 0}},
        "required": [],
    },
    "set_role_connection_limit": {
        "risk": "medium", "title": "Cap a role's connections", "reversible": True,
        "description": "ALTER ROLE ... CONNECTION LIMIT n: new logins above the cap are refused for that role only.",
        "args": {"role": {"type": "string"}, "limit": {"type": "integer", "minimum": -1}}, "required": ["role", "limit"],
    },
    "set_role_timeout": {
        "risk": "low", "title": "Set a timeout for a role", "reversible": True,
        "description": "ALTER ROLE ... SET lock_timeout | statement_timeout | idle_in_transaction_session_timeout | idle_session_timeout (new sessions).",
        "args": {"role": {"type": "string"}, "setting": {"type": "string", "enum": ["lock_timeout", "statement_timeout", "idle_in_transaction_session_timeout", "idle_session_timeout"]}, "value": {"type": "string"}},
        "required": ["role", "setting", "value"],
    },
    "vacuum_table": {
        "risk": "low", "title": "Vacuum a table", "reversible": True,
        "description": "VACUUM (ANALYZE[, FREEZE]) one table. Takes no lock that blocks reads or writes.",
        "args": {"database": {"type": "string"}, "table": {"type": "string"}, "freeze": {"type": "boolean"}}, "required": ["database", "table"],
    },
    "analyze_table": {
        "risk": "low", "title": "Analyze a table", "reversible": True,
        "description": "ANALYZE one table to refresh planner statistics.",
        "args": {"database": {"type": "string"}, "table": {"type": "string"}}, "required": ["database", "table"],
    },
    "create_index_concurrently": {
        "risk": "medium", "title": "Create an index concurrently", "reversible": True,
        "description": "CREATE INDEX CONCURRENTLY IF NOT EXISTS: builds without blocking writes (slower, more I/O).",
        "args": {"database": {"type": "string"}, "table": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}}, "name": {"type": "string"}},
        "required": ["database", "table", "columns"],
    },
    "reindex_index_concurrently": {
        "risk": "medium", "title": "Rebuild an index concurrently", "reversible": True,
        "description": "REINDEX INDEX CONCURRENTLY: replaces a bloated or invalid index without blocking writes.",
        "args": {"database": {"type": "string"}, "index": {"type": "string"}}, "required": ["database", "index"],
    },
    "alter_system_set": {
        "risk": "medium", "title": "Change a server setting", "reversible": True,
        "description": "ALTER SYSTEM SET <reloadable setting> and reload. Settings that need a restart are refused (plan them for the maintenance window).",
        "args": {"name": {"type": "string"}, "value": {"type": "string"}}, "required": ["name", "value"],
    },
    "reload_config": {
        "risk": "low", "title": "Reload the configuration", "reversible": True,
        "description": "pg_reload_conf(): apply reloadable configuration changes.",
        "args": {}, "required": [],
    },
    "resume_wal_replay": {
        "risk": "low", "title": "Resume WAL replay", "reversible": True,
        "description": "pg_wal_replay_resume() on a paused standby.",
        "args": {}, "required": [],
    },
    "drop_replication_slot": {
        "risk": "high", "title": "Drop a replication slot", "reversible": False,
        "description": "pg_drop_replication_slot() for an INACTIVE slot: frees the WAL it holds; its consumer must be re-seeded.",
        "args": {"slot": {"type": "string"}}, "required": ["slot"],
    },
    "rollback_prepared": {
        "risk": "high", "title": "Roll back a prepared transaction", "reversible": False,
        "description": "ROLLBACK PREPARED <gid> for an orphaned two-phase transaction (connects to its database).",
        "args": {"gid": {"type": "string"}, "database": {"type": "string"}}, "required": ["gid", "database"],
    },
    "execute_sql": {
        "risk": "high", "title": "Run a SQL statement", "reversible": False,
        "description": "Any other statement, checked by the SQL guard (no DROP DATABASE/SCHEMA, no DROP/TRUNCATE of application tables, no DELETE/UPDATE without WHERE, no durability weakening). Always needs a person.",
        "args": {"sql": {"type": "string"}, "database": {"type": "string"}}, "required": ["sql"],
    },
}

RELOADABLE_SETTINGS = {
    "work_mem", "maintenance_work_mem", "autovacuum_work_mem", "effective_cache_size", "random_page_cost", "seq_page_cost",
    "effective_io_concurrency", "default_statistics_target", "statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout",
    "idle_session_timeout", "deadlock_timeout", "log_lock_waits", "log_min_duration_statement", "log_autovacuum_min_duration",
    "log_temp_files", "log_checkpoints", "log_connections", "log_disconnections", "track_io_timing", "checkpoint_timeout",
    "checkpoint_completion_target", "max_wal_size", "min_wal_size", "wal_compression", "autovacuum_naptime",
    "autovacuum_vacuum_scale_factor", "autovacuum_vacuum_threshold", "autovacuum_analyze_scale_factor", "autovacuum_vacuum_cost_limit",
    "autovacuum_vacuum_cost_delay", "autovacuum_vacuum_insert_scale_factor",
    "max_slot_wal_keep_size", "hot_standby_feedback", "max_standby_streaming_delay", "max_standby_archive_delay",
    "jit", "parallel_setup_cost", "max_parallel_workers_per_gather", "temp_file_limit",
}
ROLE_TIMEOUTS = {"lock_timeout", "statement_timeout", "idle_in_transaction_session_timeout", "idle_session_timeout"}


class Driver(Protocol):
    server_id: str

    async def collect(self) -> CollectResult: ...
    async def test(self) -> dict[str, Any]: ...
    async def overview(self) -> dict[str, Any]: ...
    async def sessions(self, state: str | None = None, min_age_s: int = 0, role: str | None = None, limit: int = 50) -> dict[str, Any]: ...
    async def lock_tree(self) -> dict[str, Any]: ...
    async def top_queries(self, order_by: str = "total_time", limit: int = 10) -> dict[str, Any]: ...
    async def table_health(self, database: str | None = None, limit: int = 15) -> dict[str, Any]: ...
    async def wraparound(self) -> dict[str, Any]: ...
    async def replication(self) -> dict[str, Any]: ...
    async def settings(self, names: list[str] | None = None, pattern: str | None = None, pending_only: bool = False) -> dict[str, Any]: ...
    async def databases(self) -> dict[str, Any]: ...
    async def run_sql(self, sql: str, database: str | None = None) -> dict[str, Any]: ...
    async def execute_action(self, action: str, args: dict[str, Any]) -> dict[str, Any]: ...
    async def close(self) -> None: ...
