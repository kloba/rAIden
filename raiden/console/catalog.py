"""The metric catalog: one vocabulary for every PostgreSQL, wherever it runs.

Metric IDs follow Azure Database for PostgreSQL (https://learn.microsoft.com/azure/postgresql/monitor/concepts-monitoring)
so that a dashboard reads the same for an on-prem server and a flexible server. Every metric says where it can come from:

  sql    the console's own SQL collector (pg_stat_* views): works on ANY PostgreSQL 13+, on-prem, VM, Kubernetes, RDS,
         Cloud SQL, Azure. Needs a login with the pg_monitor role.
  host   the rAIden host agent on a self-managed host (/proc and the data directory's file system)
  azure  Azure Monitor platform metrics, for Azure Database for PostgreSQL flexible servers (optional)

Metrics with a dimension are stored as `metric[value]` (e.g. `sessions_by_state[idle in transaction]`, `tps[orders]`);
the plain `metric` is the server-wide value.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

CATEGORIES: list[tuple[str, str]] = [
    ("availability", "Availability"),
    ("connections", "Connections"),
    ("activity", "Activity"),
    ("transactions", "Transactions"),
    ("io", "Cache & I/O"),
    ("host", "Compute"),
    ("storage", "Storage"),
    ("replication", "Replication"),
    ("wal", "WAL & checkpoints"),
    ("vacuum", "Vacuum & bloat"),
    ("wraparound", "Transaction ID wraparound"),
    ("config", "Configuration"),
    ("pgbouncer", "PgBouncer"),
]


@dataclass(frozen=True)
class Metric:
    id: str
    name: str
    unit: str  # percent | count | per_second | bytes | bytes_per_second | seconds | ratio | xids
    category: str
    description: str
    sources: tuple[str, ...] = ("sql",)
    azure: str | None = None  # Azure Monitor metric name when it maps one to one
    dimension: str | None = None  # database | state | wait_event_type
    warn: float | None = None  # health thresholds for colouring and the built-in monitors
    crit: float | None = None
    lower_is_worse: bool = False
    key: bool = False  # shown on the server overview
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sources"] = list(self.sources)
        return d


def _m(*args, **kw) -> Metric:
    return Metric(*args, **kw)


METRICS: list[Metric] = [
    # -- availability ------------------------------------------------------------------------------------------------
    _m("is_db_alive", "Database is alive", "count", "availability",
       "1 when the server accepted a connection and answered a query in this collection, 0 otherwise. Aggregate with MAX for a "
       "minute, MIN to see any outage.", ("sql", "azure"), azure="is_db_alive", crit=0.5, lower_is_worse=True, key=True),
    _m("uptime_seconds", "Uptime", "seconds", "availability",
       "Seconds since the postmaster started (pg_postmaster_start_time). A drop to near zero is a restart or a crash recovery.",
       lower_is_worse=True),
    # -- connections -------------------------------------------------------------------------------------------------
    _m("active_connections", "Active connections", "count", "connections",
       "All connections in every state (active, idle, idle in transaction ...) as seen in pg_stat_activity: not just the ones "
       "executing a query. See Sessions by state.", ("sql", "azure"), azure="active_connections", key=True),
    _m("max_connections", "Max connections", "count", "connections",
       "The configured max_connections.", ("sql", "azure"), azure="max_connections"),
    _m("connections_percent", "Connection utilization", "percent", "connections",
       "Client backends as a share of max_connections minus the reserved superuser slots. Above ~90% new clients start to be "
       "refused with 'remaining connection slots are reserved'.", warn=80, crit=92, key=True),
    _m("sessions_by_state", "Sessions by state", "count", "connections",
       "Client backends per pg_stat_activity.state: active, idle, idle in transaction, idle in transaction (aborted).",
       ("sql",), azure="sessions_by_state", dimension="state"),
    _m("idle_in_transaction", "Idle in transaction", "count", "connections",
       "Sessions holding an open transaction while doing nothing: they keep locks and hold back vacuum.", warn=10, crit=40),
    _m("connections_failed", "Failed connections", "count", "connections",
       "Failed connection attempts in the interval (authentication failures, refused slots). From the host agent's log reader "
       "or Azure Monitor; not visible to SQL.", ("host", "azure"), azure="connections_failed", warn=20, crit=100),
    _m("connections_succeeded", "Succeeded connections", "count", "connections",
       "Successful connection attempts in the interval.", ("host", "azure"), azure="connections_succeeded"),
    _m("sessions_fatal", "Sessions ended by FATAL", "count", "connections",
       "Sessions per minute terminated by a fatal error (pg_stat_database.sessions_fatal, PostgreSQL 14+).", warn=5, crit=50),
    _m("sessions_killed", "Sessions killed", "count", "connections",
       "Sessions per minute terminated by an operator (pg_stat_database.sessions_killed, PostgreSQL 14+)."),
    _m("tcp_connection_backlog", "TCP connection backlog", "count", "connections",
       "Pending TCP connections waiting to be accepted.", ("azure",), azure="tcp_connection_backlog", warn=10, crit=50),
    # -- activity ----------------------------------------------------------------------------------------------------
    _m("longest_query_time_sec", "Oldest query", "seconds", "activity",
       "Age of the longest running query.", ("sql",), azure="longest_query_time_sec", warn=300, crit=1800, key=True),
    _m("longest_transaction_time_sec", "Oldest transaction", "seconds", "activity",
       "Age of the longest open transaction, idle ones included.", ("sql",), azure="longest_transaction_time_sec", warn=600, crit=3600),
    _m("oldest_backend_time_sec", "Oldest backend", "seconds", "activity",
       "Age of the oldest client backend, in any state.", ("sql",), azure="oldest_backend_time_sec"),
    _m("oldest_backend_xmin_age", "Oldest xmin age", "xids", "activity",
       "Transactions since the oldest backend xmin. A value that keeps growing means a session holds back vacuum.",
       ("sql",), azure="oldest_backend_xmin_age", warn=50_000_000, crit=500_000_000),
    _m("oldest_backend_xmin", "Oldest xmin", "xids", "activity",
       "The oldest backend_xmin itself, a transaction ID. When it stops moving, a long transaction holds back vacuum (see Oldest xmin age).",
       ("sql",), azure="oldest_backend_xmin"),
    _m("sessions_by_wait_event_type", "Sessions by wait event type", "count", "activity",
       "Active sessions per wait_event_type (Lock, LWLock, IO, IPC, Client, Timeout ...).", ("sql",),
       azure="sessions_by_wait_event_type", dimension="wait_event_type"),
    _m("blocked_sessions", "Blocked sessions", "count", "activity",
       "Sessions waiting for a heavyweight lock held by another session (pg_blocking_pids). A queue behind one DDL statement "
       "freezes every writer.", warn=5, crit=20, key=True),
    _m("lock_wait_max_sec", "Longest lock wait", "seconds", "activity",
       "How long the longest-waiting blocked session has been waiting.", warn=30, crit=120),
    # -- transactions (database) -------------------------------------------------------------------------------------
    _m("tps", "Transactions per second", "per_second", "transactions",
       "Commits plus rollbacks per second.", ("sql",), azure="tps", dimension="database", key=True),
    _m("xact_commit", "Transactions committed", "per_second", "transactions",
       "Commits per second.", ("sql",), azure="xact_commit", dimension="database"),
    _m("xact_rollback", "Transactions rolled back", "per_second", "transactions",
       "Rollbacks per second.", ("sql",), azure="xact_rollback", dimension="database"),
    _m("xact_total", "Total transactions", "count", "transactions",
       "Commits plus rollbacks in the last minute.", ("sql",), azure="xact_total", dimension="database"),
    _m("rollback_percent", "Rollback ratio", "percent", "transactions",
       "Rollbacks as a share of all transactions in the interval.", warn=10, crit=30),
    _m("deadlocks", "Deadlocks", "count", "transactions",
       "Deadlocks detected in the interval.", ("sql",), azure="deadlocks", dimension="database", warn=1, crit=10),
    _m("numbackends", "Backends", "count", "transactions",
       "Backends connected to each database.", ("sql",), azure="numbackends", dimension="database"),
    _m("tup_inserted", "Rows inserted", "per_second", "transactions", "Rows inserted per second.", ("sql",), azure="tup_inserted", dimension="database"),
    _m("tup_updated", "Rows updated", "per_second", "transactions", "Rows updated per second.", ("sql",), azure="tup_updated", dimension="database"),
    _m("tup_deleted", "Rows deleted", "per_second", "transactions", "Rows deleted per second.", ("sql",), azure="tup_deleted", dimension="database"),
    _m("tup_fetched", "Rows fetched", "per_second", "transactions", "Rows fetched by index scans per second.", ("sql",), azure="tup_fetched", dimension="database"),
    _m("tup_returned", "Rows returned", "per_second", "transactions", "Rows returned by queries per second.", ("sql",), azure="tup_returned", dimension="database"),
    _m("temp_files", "Temporary files", "count", "transactions",
       "Temporary files created by queries in the interval (sorts and hashes that did not fit in work_mem).", ("sql",), azure="temp_files", dimension="database"),
    _m("temp_bytes", "Temporary files size", "bytes", "transactions",
       "Bytes written to temporary files in the interval.", ("sql",), azure="temp_bytes", dimension="database", warn=1 << 30, crit=10 << 30),
    # -- cache & io --------------------------------------------------------------------------------------------------
    _m("cache_hit_ratio", "Cache hit ratio", "percent", "io",
       "Share of block reads served from shared buffers in the interval (blks_hit / (blks_hit + blks_read)).",
       warn=95, crit=90, lower_is_worse=True, key=True),
    _m("blks_hit", "Disk blocks hit", "per_second", "io", "Buffer cache hits per second.", ("sql",), azure="blks_hit", dimension="database"),
    _m("blks_read", "Disk blocks read", "per_second", "io", "Blocks read from disk (or the OS cache) per second.", ("sql",), azure="blks_read", dimension="database"),
    _m("iops", "IOPS", "per_second", "io", "Disk I/O operations per second (not a saturation percentage).", ("host", "azure"), azure="iops"),
    _m("read_iops", "Read IOPS", "per_second", "io", "Data disk read operations per second.", ("host", "azure"), azure="read_iops"),
    _m("write_iops", "Write IOPS", "per_second", "io", "Data disk write operations per second.", ("host", "azure"), azure="write_iops"),
    _m("read_throughput", "Read throughput", "bytes_per_second", "io", "Bytes read from the data disk per second.", ("host", "azure"), azure="read_throughput"),
    _m("write_throughput", "Write throughput", "bytes_per_second", "io", "Bytes written to the data disk per second.", ("host", "azure"), azure="write_throughput"),
    _m("disk_queue_depth", "Disk queue depth", "count", "io", "Outstanding I/O operations on the data disk.", ("host", "azure"), azure="disk_queue_depth", warn=8, crit=32),
    _m("disk_iops_consumed_percentage", "Disk IOPS consumed", "percent", "io",
       "Share of the provisioned IOPS used.", ("azure",), azure="disk_iops_consumed_percentage", warn=80, crit=95),
    _m("disk_bandwidth_consumed_percentage", "Disk bandwidth consumed", "percent", "io",
       "Share of the provisioned disk bandwidth used.", ("azure",), azure="disk_bandwidth_consumed_percentage", warn=80, crit=95),
    # -- compute -----------------------------------------------------------------------------------------------------
    _m("cpu_percent", "CPU", "percent", "host", "Server CPU utilization.", ("host", "azure"), azure="cpu_percent", warn=80, crit=92, key=True),
    _m("memory_percent", "Memory", "percent", "host", "Server memory utilization (without the page cache).", ("host", "azure"), azure="memory_percent", warn=85, crit=95, key=True),
    _m("network_bytes_ingress", "Network in", "bytes", "host", "Bytes received in the interval.", ("host", "azure"), azure="network_bytes_ingress"),
    _m("network_bytes_egress", "Network out", "bytes", "host", "Bytes sent in the interval (clients, replication, WAL archive).", ("host", "azure"), azure="network_bytes_egress"),
    _m("cpu_credits_remaining", "CPU credits remaining", "count", "host", "Burst credits left (Azure Burstable tier).", ("azure",), azure="cpu_credits_remaining", lower_is_worse=True),
    _m("cpu_credits_consumed", "CPU credits consumed", "count", "host", "Burst credits used (Azure Burstable tier).", ("azure",), azure="cpu_credits_consumed"),
    _m("postmaster_process_cpu_usage_percent", "Postmaster CPU", "percent", "host",
       "CPU used by the postmaster process itself (Azure, not on Burstable). High values point at connection churn.", ("azure",),
       azure="postmaster_process_cpu_usage_percent", warn=50, crit=80),
    _m("load_average", "Load average (1 min)", "count", "host", "Run-queue length of the host (host agent).", ("host",)),
    # -- storage -----------------------------------------------------------------------------------------------------
    _m("storage_percent", "Storage", "percent", "storage",
       "Used share of the data volume: database files, WAL and server logs.", ("host", "azure"), azure="storage_percent", warn=80, crit=90, key=True),
    _m("storage_used", "Storage used", "bytes", "storage", "Bytes used on the data volume.", ("host", "azure"), azure="storage_used"),
    _m("storage_free", "Storage free", "bytes", "storage", "Bytes free on the data volume.", ("host", "azure"), azure="storage_free", lower_is_worse=True),
    _m("database_size_bytes", "Database size", "bytes", "storage", "Size of each database (pg_database_size).", ("sql", "azure"), azure="database_size_bytes", dimension="database"),
    _m("txlogs_storage_used", "WAL storage used", "bytes", "storage",
       "Bytes in pg_wal (pg_ls_waldir). Growth without bound means archiving fails or a replication slot holds WAL.",
       ("sql", "azure"), azure="txlogs_storage_used"),
    _m("backup_storage_used", "Backup storage used", "bytes", "storage", "Backup storage consumed (Azure).", ("azure",), azure="backup_storage_used"),
    # -- replication -------------------------------------------------------------------------------------------------
    _m("physical_replication_delay_in_seconds", "Replica lag", "seconds", "replication",
       "On a replica: time since the last replayed transaction. On a primary: the worst replay_lag of its standbys.",
       ("sql", "azure"), azure="physical_replication_delay_in_seconds", warn=30, crit=300, key=True),
    _m("physical_replication_delay_in_bytes", "Max physical replication lag", "bytes", "replication",
       "Largest WAL distance between this primary and a physical standby (replay position).", ("sql", "azure"),
       azure="physical_replication_delay_in_bytes", warn=256 << 20, crit=4 << 30),
    _m("logical_replication_delay_in_bytes", "Max logical replication lag", "bytes", "replication",
       "Largest WAL distance between the current position and a logical slot's confirmed_flush_lsn.", ("sql", "azure"),
       azure="logical_replication_delay_in_bytes", warn=1 << 30, crit=10 << 30),
    _m("logical_replication_slot_sync_status", "Logical slots synchronized", "count", "replication",
       "1 when the logical replication slots are synchronized to the high-availability standby, 0 when a failover would lose them (Azure HA).",
       ("azure",), azure="logical_replication_slot_sync_status", dimension="slot", crit=0.5, lower_is_worse=True),
    _m("replication_slot_retained_bytes", "WAL retained by slots", "bytes", "replication",
       "WAL kept on disk for the slowest replication slot (restart_lsn). An abandoned slot fills the disk.", warn=5 << 30, crit=20 << 30),
    _m("inactive_replication_slots", "Inactive replication slots", "count", "replication",
       "Replication slots with no consumer connected.", warn=1, crit=3),
    _m("replicas_connected", "Standbys connected", "count", "replication", "Physical standbys streaming from this server.", lower_is_worse=True),
    # -- wal & checkpoints -------------------------------------------------------------------------------------------
    _m("wal_bytes_per_second", "WAL generated", "bytes_per_second", "wal", "WAL bytes generated per second (pg_stat_wal, PostgreSQL 14+)."),
    _m("checkpoints_requested", "Requested checkpoints", "count", "wal",
       "Checkpoints forced by WAL volume rather than the timer, in the interval: max_wal_size is too small for the write load.", warn=1, crit=6),
    _m("archive_failed", "WAL archive failures", "count", "wal", "archive_command failures in the interval (pg_stat_archiver).", warn=1, crit=5),
    _m("archive_lag_seconds", "Last WAL archived", "seconds", "wal", "Seconds since the last WAL segment was archived (when archiving is on).", warn=900, crit=3600),
    # -- vacuum ------------------------------------------------------------------------------------------------------
    _m("n_dead_tup_user_tables", "Dead rows", "count", "vacuum", "Estimated dead rows in user tables.", ("sql",), azure="n_dead_tup_user_tables", dimension="database"),
    _m("n_live_tup_user_tables", "Live rows", "count", "vacuum", "Estimated live rows in user tables.", ("sql",), azure="n_live_tup_user_tables", dimension="database"),
    _m("n_mod_since_analyze_user_tables", "Rows modified since analyze", "count", "vacuum", "Rows changed since the last ANALYZE.", ("sql",), azure="n_mod_since_analyze_user_tables", dimension="database"),
    _m("bloat_percent", "Dead-row ratio", "percent", "vacuum",
       "Dead rows as a share of all rows in user tables: a fast bloat estimate (Azure: Bloat Percent).", ("sql",),
       azure="bloat_percent", dimension="database", warn=20, crit=40),
    _m("autovacuum_count_user_tables", "Autovacuum runs", "count", "vacuum", "Autovacuum runs on user tables (cumulative).", ("sql",), azure="autovacuum_count_user_tables", dimension="database"),
    _m("autoanalyze_count_user_tables", "Autoanalyze runs", "count", "vacuum", "Autoanalyze runs on user tables (cumulative).", ("sql",), azure="autoanalyze_count_user_tables", dimension="database"),
    _m("tables_counter_user_tables", "User tables", "count", "vacuum", "Number of user tables.", ("sql",), azure="tables_counter_user_tables", dimension="database"),
    _m("vacuum_count_user_tables", "Manual vacuum runs", "count", "vacuum", "Manual VACUUM runs on user tables, VACUUM FULL not counted (cumulative).", ("sql",), azure="vacuum_count_user_tables", dimension="database"),
    _m("analyze_count_user_tables", "Manual analyze runs", "count", "vacuum", "Manual ANALYZE runs on user tables (cumulative).", ("sql",), azure="analyze_count_user_tables", dimension="database"),
    _m("tables_vacuumed_user_tables", "User tables vacuumed", "count", "vacuum", "User tables vacuumed by hand at least once since the statistics were reset.", ("sql",), azure="tables_vacuumed_user_tables", dimension="database"),
    _m("tables_autovacuumed_user_tables", "User tables autovacuumed", "count", "vacuum", "User tables autovacuum has vacuumed at least once since the statistics were reset.", ("sql",), azure="tables_autovacuumed_user_tables", dimension="database"),
    _m("tables_analyzed_user_tables", "User tables analyzed", "count", "vacuum", "User tables analyzed by hand at least once since the statistics were reset.", ("sql",), azure="tables_analyzed_user_tables", dimension="database"),
    _m("tables_autoanalyzed_user_tables", "User tables autoanalyzed", "count", "vacuum", "User tables autovacuum has analyzed at least once since the statistics were reset.", ("sql",), azure="tables_autoanalyzed_user_tables", dimension="database"),
    _m("autovacuum_workers", "Autovacuum workers", "count", "vacuum", "Autovacuum workers running right now."),
    # -- wraparound --------------------------------------------------------------------------------------------------
    _m("maximum_used_transactionIDs", "Maximum used transaction IDs", "xids", "wraparound",
       "The oldest age(datfrozenxid) of all databases. PostgreSQL stops assigning transaction IDs near 2^31; autovacuum must freeze long before.",
       ("sql", "azure"), azure="maximum_used_transactionIDs", warn=500_000_000, crit=1_200_000_000, key=True),
    _m("max_multixact_age", "Maximum multixact age", "xids", "wraparound", "The oldest mxid_age(datminmxid) of all databases.", warn=400_000_000, crit=1_000_000_000),
    _m("prepared_xact_oldest_seconds", "Oldest prepared transaction", "seconds", "wraparound",
       "Age of the oldest two-phase transaction left prepared: it holds locks and the xmin horizon until COMMIT/ROLLBACK PREPARED.", warn=300, crit=3600),
    # -- configuration -----------------------------------------------------------------------------------------------
    _m("pending_restart_settings", "Settings waiting for a restart", "count", "config",
       "Settings changed in the configuration files that only take effect at the next restart (pg_settings.pending_restart): "
       "a time bomb for the next maintenance.", warn=1, crit=5),
    _m("checksum_failures", "Checksum failures", "count", "config", "Data page checksum failures (pg_stat_database.checksum_failures): storage corruption.", warn=1, crit=1),
    # -- pgbouncer ---------------------------------------------------------------------------------------------------
    _m("client_connections_active", "PgBouncer active clients", "count", "pgbouncer", "Client connections linked to a server connection.", ("azure",), azure="client_connections_active"),
    _m("client_connections_waiting", "PgBouncer waiting clients", "count", "pgbouncer", "Clients waiting for a server connection.", ("azure",), azure="client_connections_waiting", warn=5, crit=50),
    _m("server_connections_active", "PgBouncer active server connections", "count", "pgbouncer", "Server connections in use by a client.", ("azure",), azure="server_connections_active"),
    _m("server_connections_idle", "PgBouncer idle server connections", "count", "pgbouncer", "Server connections idle and ready.", ("azure",), azure="server_connections_idle"),
    _m("total_pooled_connections", "PgBouncer pooled connections", "count", "pgbouncer", "Current pooled connections.", ("azure",), azure="total_pooled_connections"),
    _m("num_pools", "PgBouncer pools", "count", "pgbouncer", "Number of connection pools.", ("azure",), azure="num_pools"),
    _m("client_connections_total", "PgBouncer client connections", "count", "pgbouncer",
       "All client connections PgBouncer tracks (active, waiting, authenticating), counted against pgbouncer.max_client_conn.", ("azure",), azure="client_connections_total"),
]

BY_ID: dict[str, Metric] = {m.id: m for m in METRICS}
AZURE_METRICS: dict[str, str] = {m.azure: m.id for m in METRICS if m.azure and "azure" in m.sources}

PLATFORMS: dict[str, str] = {
    "self_managed": "Self-managed (on-prem)",
    "vm": "Virtual machine",
    "kubernetes": "Kubernetes",
    "azure_flexible": "Azure Database for PostgreSQL",
    "aws_rds": "Amazon RDS for PostgreSQL",
    "aws_aurora": "Amazon Aurora PostgreSQL",
    "gcp_cloudsql": "Google Cloud SQL",
    "other": "Other",
}


def split_key(key: str) -> tuple[str, str | None]:
    """'tps[orders]' -> ('tps', 'orders'); 'tps' -> ('tps', None)."""
    if key.endswith("]") and "[" in key:
        base, dim = key[:-1].split("[", 1)
        return base, dim
    return key, None


def health(metric_id: str, value: float | None) -> str:
    """ok | warn | crit for a value of a metric with thresholds; 'ok' when there is no threshold."""
    m = BY_ID.get(metric_id)
    if m is None or value is None:
        return "ok"
    if m.lower_is_worse:
        if m.crit is not None and value <= m.crit:
            return "crit"
        if m.warn is not None and value <= m.warn:
            return "warn"
        return "ok"
    if m.crit is not None and value >= m.crit:
        return "crit"
    if m.warn is not None and value >= m.warn:
        return "warn"
    return "ok"


def catalog_payload() -> dict:
    return {"categories": [{"id": c, "name": n} for c, n in CATEGORIES], "metrics": [m.to_dict() for m in METRICS]}
