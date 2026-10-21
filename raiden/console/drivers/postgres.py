"""PostgresDriver: the SQL collector and toolbox for ANY PostgreSQL 13+ (on-prem, VM, Kubernetes, RDS, Cloud SQL, Azure).

Only standard catalog views are used. The login needs the `pg_monitor` role to see everything; remediation actions need
more (pg_signal_backend, CREATEROLE, pg_maintain, ...): `test()` reports which capabilities the login has.

    CREATE ROLE raiden_monitor LOGIN PASSWORD '...' IN ROLE pg_monitor;
    GRANT pg_signal_backend TO raiden_monitor;   -- optional: cancel/terminate sessions
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import decimal
import ipaddress
import re
import time
import uuid
from typing import Any, AsyncIterator

from .. import fmt, sqlguard
from .base import ACTIONS, RELOADABLE_SETTINGS, ROLE_TIMEOUTS, CollectResult, DriverError, table

STATEMENT_TIMEOUT_MS = 15_000
READONLY_TIMEOUT_MS = 5_000
LOCK_TIMEOUT_MS = 3_000
# CREATE INDEX and REINDEX CONCURRENTLY wait for every transaction older than themselves: in production that takes a while
MAINTENANCE_LOCK_TIMEOUT_MS = 300_000
MAX_ROWS = 200
# short-lived connections open at once per server, next to the collector's own: the login the onboarding page sets up
# has CONNECTION LIMIT 5, and one slot stays free for the connection test
SHORT_CONNECTIONS = 3

SERVER_SQL = """
SELECT current_setting('server_version_num')::int,
       version(),
       pg_is_in_recovery(),
       extract(epoch FROM now() - pg_postmaster_start_time())::bigint,
       current_setting('max_connections')::int,
       current_setting('superuser_reserved_connections')::int,
       (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'),
       (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'autovacuum worker'),
       (SELECT coalesce(max(extract(epoch FROM now() - query_start)), 0) FROM pg_stat_activity
         WHERE state = 'active' AND backend_type = 'client backend' AND pid <> pg_backend_pid()),
       (SELECT coalesce(max(extract(epoch FROM now() - xact_start)), 0) FROM pg_stat_activity
         WHERE xact_start IS NOT NULL AND backend_type = 'client backend' AND pid <> pg_backend_pid()),
       (SELECT coalesce(max(extract(epoch FROM now() - backend_start)), 0) FROM pg_stat_activity WHERE backend_type = 'client backend'),
       (SELECT coalesce(max(age(backend_xmin)), 0) FROM pg_stat_activity WHERE backend_xmin IS NOT NULL),
       (SELECT count(*) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0),
       (SELECT coalesce(max(extract(epoch FROM now() - query_start)), 0) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0),
       (SELECT coalesce(max(age(datfrozenxid)), 0) FROM pg_database WHERE datallowconn),
       (SELECT coalesce(max(mxid_age(datminmxid)), 0) FROM pg_database WHERE datallowconn),
       (SELECT coalesce(max(extract(epoch FROM now() - prepared)), 0) FROM pg_prepared_xacts),
       (SELECT count(*) FROM pg_settings WHERE pending_restart),
       (SELECT backend_xmin::text::bigint FROM pg_stat_activity WHERE backend_xmin IS NOT NULL ORDER BY age(backend_xmin) DESC LIMIT 1)
"""

STATES_SQL = """
SELECT coalesce(state, 'unknown'), count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' GROUP BY 1
"""

WAITS_SQL = """
SELECT coalesce(wait_event_type, 'CPU'), count(*) FROM pg_stat_activity
WHERE backend_type = 'client backend' AND state = 'active' AND pid <> pg_backend_pid() GROUP BY 1
"""

DB_COLS = ["numbackends", "xact_commit", "xact_rollback", "blks_read", "blks_hit", "tup_returned", "tup_fetched", "tup_inserted",
           "tup_updated", "tup_deleted", "temp_files", "temp_bytes", "deadlocks", "checksum_failures"]
DB_COLS_14 = ["sessions_fatal", "sessions_killed"]

TABLES_SQL = """
SELECT count(*), coalesce(sum(n_live_tup), 0), coalesce(sum(n_dead_tup), 0), coalesce(sum(n_mod_since_analyze), 0),
       coalesce(sum(autovacuum_count), 0), coalesce(sum(autoanalyze_count), 0), coalesce(sum(vacuum_count), 0), coalesce(sum(analyze_count), 0),
       count(*) FILTER (WHERE vacuum_count > 0), count(*) FILTER (WHERE autovacuum_count > 0),
       count(*) FILTER (WHERE analyze_count > 0), count(*) FILTER (WHERE autoanalyze_count > 0)
FROM pg_stat_user_tables
"""

PRIMARY_REPL_SQL = """
SELECT count(*), coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)), 0),
       coalesce(max(extract(epoch FROM replay_lag)), 0)
FROM pg_stat_replication
"""

SLOTS_SQL = """
SELECT coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)), 0),
       coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)) FILTER (WHERE slot_type = 'logical'), 0),
       count(*) FILTER (WHERE NOT active)
FROM pg_replication_slots
"""

REPLICA_LAG_SQL = """
SELECT CASE WHEN pg_last_wal_receive_lsn() IS NULL OR pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn() THEN 0
            ELSE coalesce(extract(epoch FROM now() - pg_last_xact_replay_timestamp()), 0) END
"""

CAPABILITIES_SQL = """
SELECT current_user,
       (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
       pg_has_role(current_user, 'pg_monitor', 'member'),
       pg_has_role(current_user, 'pg_signal_backend', 'member'),
       (SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user),
       EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'),
       current_setting('shared_preload_libraries'),
       current_setting('data_checksums'),
       current_setting('archive_mode'),
       current_setting('wal_level'),
       current_setting('server_encoding'),
       (SELECT json_agg(extname || ' ' || extversion ORDER BY extname) FROM pg_extension)::text
"""


def jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return round(v.total_seconds(), 3)
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (bytes, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    if isinstance(v, (uuid.UUID, ipaddress.IPv4Address, ipaddress.IPv6Address, ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return str(v)
    return str(v)


def _ident_parts(name: str) -> list[str]:
    """'schema.table' or '"Odd Name"' -> identifier parts; anything else is refused."""
    parts = re.findall(r'"((?:[^"]|"")+)"|([A-Za-z_][A-Za-z0-9_$]*)', name.strip())
    rebuilt = ".".join(f'"{a}"' if a else b for a, b in parts)
    if not parts or len(parts) > 3 or rebuilt.replace('"', "").lower() != name.strip().replace('"', "").lower():
        raise DriverError(f"not a valid object name: {name!r}")
    return [a.replace('""', '"') if a else b for a, b in parts]


class PostgresDriver:
    def __init__(self, server_id: str, conninfo: dict[str, Any], databases: list[str] | None = None) -> None:
        self.server_id = server_id
        self.conninfo = {k: v for k, v in conninfo.items() if v not in (None, "")}
        self.table_dbs = list(databases or [])  # where table statistics are read (not `databases`: that is a tool)
        self._conn: Any = None
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(SHORT_CONNECTIONS)
        self._prev: dict[str, Any] | None = None
        self._ticks = 0
        self._table_stats: dict[str, tuple[float, tuple]] = {}
        self._no_waldir = False
        self.version_num = 0

    # ---------------------------------------------------------------------------------------------------- connections
    async def _connect(self, dbname: str | None = None, timeout_ms: int = STATEMENT_TIMEOUT_MS, lock_timeout_ms: int = LOCK_TIMEOUT_MS) -> Any:
        import psycopg

        params = dict(self.conninfo)
        if dbname:
            params["dbname"] = dbname
        try:
            conn = await psycopg.AsyncConnection.connect(**params, connect_timeout=5, application_name="raiden-console", autocommit=True)
        except psycopg.Error as exc:
            raise DriverError(_clean_error(exc)) from exc
        for stmt in (f"SET statement_timeout = {int(timeout_ms)}", f"SET lock_timeout = {int(lock_timeout_ms)}", "SET idle_in_transaction_session_timeout = 60000"):
            await conn.execute(stmt)
        return conn

    @contextlib.asynccontextmanager
    async def _short(self, dbname: str | None = None, timeout_ms: int = STATEMENT_TIMEOUT_MS, lock_timeout_ms: int = LOCK_TIMEOUT_MS) -> AsyncIterator[Any]:
        """A connection for one task, closed afterwards. Waits up to 30 s for one of the SHORT_CONNECTIONS slots."""
        try:
            await asyncio.wait_for(self._slots.acquire(), 30)
        except asyncio.TimeoutError:
            raise DriverError(f"all {SHORT_CONNECTIONS} of rAIden's connections to this server are busy (a long VACUUM or index build?): "
                              "try again in a moment") from None
        try:
            conn = await self._connect(dbname, timeout_ms, lock_timeout_ms)
            try:
                yield conn
            finally:
                await conn.close()
        finally:
            self._slots.release()

    async def _collector(self) -> Any:
        if self._conn is None or self._conn.closed:
            self._conn = await self._connect()
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None

    @staticmethod
    async def _rows(conn: Any, sql: str, params: Any = None) -> list[tuple]:
        cur = await conn.execute(sql, params)
        return await cur.fetchall() if cur.description else []

    async def _query(self, sql: str, params: Any = None, dbname: str | None = None) -> tuple[list[str], list[tuple]]:
        import psycopg

        try:
            async with self._short(dbname) as conn:
                cur = await conn.execute(sql, params)
                cols = [c.name for c in cur.description] if cur.description else []
                rows = await cur.fetchall() if cur.description else []
                return cols, rows
        except psycopg.Error as exc:
            raise DriverError(_clean_error(exc)) from exc

    # ---------------------------------------------------------------------------------------------------- collection
    async def collect(self) -> CollectResult:
        import psycopg

        async with self._lock:
            now = time.time()
            try:
                conn = await self._collector()
                metrics, info = await self._collect(conn, now)
                self._ticks += 1
                return CollectResult(ok=True, metrics=metrics, info=info)
            except (psycopg.Error, DriverError, OSError) as exc:
                await self.close()
                return CollectResult(ok=False, metrics={"is_db_alive": 0.0}, error=_clean_error(exc))

    async def _collect(self, conn: Any, now: float) -> tuple[dict[str, float], dict[str, Any]]:
        import psycopg

        m: dict[str, float] = {"is_db_alive": 1.0}
        (vnum, version, in_recovery, uptime, max_conn, reserved, clients, av_workers, q_age, x_age, b_age, xmin_age, blocked,
         lock_wait, xid_age, mxid_age, prepared_age, pending, oldest_xmin) = (await self._rows(conn, SERVER_SQL))[0]
        self.version_num = int(vnum)
        usable = max(1, int(max_conn) - int(reserved))
        m.update(
            uptime_seconds=float(uptime), max_connections=float(max_conn), active_connections=float(clients),
            connections_percent=round(100.0 * clients / usable, 2), autovacuum_workers=float(av_workers),
            longest_query_time_sec=float(q_age), longest_transaction_time_sec=float(x_age), oldest_backend_time_sec=float(b_age),
            oldest_backend_xmin_age=float(xmin_age), blocked_sessions=float(blocked), lock_wait_max_sec=float(lock_wait),
            maximum_used_transactionIDs=float(xid_age), max_multixact_age=float(mxid_age), prepared_xact_oldest_seconds=float(prepared_age),
            pending_restart_settings=float(pending),
        )
        if oldest_xmin is not None:
            m["oldest_backend_xmin"] = float(oldest_xmin)
        idle_tx = 0.0
        for state, n in await self._rows(conn, STATES_SQL):
            m[f"sessions_by_state[{state}]"] = float(n)
            if state.startswith("idle in transaction"):
                idle_tx += n
        m["idle_in_transaction"] = idle_tx
        for wtype, n in await self._rows(conn, WAITS_SQL):
            m[f"sessions_by_wait_event_type[{wtype}]"] = float(n)

        cols = DB_COLS + (DB_COLS_14 if self.version_num >= 140000 else [])
        db_rows = await self._rows(
            conn,
            "SELECT datname, " + ", ".join(f"coalesce({c}, 0)" for c in cols) + " FROM pg_stat_database "
            "WHERE datname IS NOT NULL AND datname NOT IN ('template0', 'template1')",
        )
        counters: dict[str, dict[str, float]] = {r[0]: dict(zip(cols, map(float, r[1:]))) for r in db_rows}
        extra: dict[str, float] = {}
        if self.version_num >= 140000:
            try:
                extra["wal_bytes"] = float((await self._rows(conn, "SELECT wal_bytes FROM pg_stat_wal"))[0][0])
            except psycopg.Error:
                pass
        ck_sql = "SELECT num_requested FROM pg_stat_checkpointer" if self.version_num >= 170000 else "SELECT checkpoints_req FROM pg_stat_bgwriter"
        try:
            extra["ckpt_req"] = float((await self._rows(conn, ck_sql))[0][0])
        except psycopg.Error:
            pass
        try:
            archived, failed, last_age, mode = (await self._rows(
                conn, "SELECT archived_count, failed_count, extract(epoch FROM now() - last_archived_time), current_setting('archive_mode') FROM pg_stat_archiver"
            ))[0]
            extra["arch_failed"] = float(failed)
            if mode != "off" and last_age is not None and not in_recovery:
                m["archive_lag_seconds"] = float(last_age)
        except psycopg.Error:
            pass
        self._rates(m, counters, extra, now)

        if self._ticks % 5 == 0:  # sizes walk the file system: every fifth collection is plenty
            for name, size in await self._rows(conn, "SELECT datname, pg_database_size(datname) FROM pg_database WHERE datallowconn AND NOT datistemplate"):
                m[f"database_size_bytes[{name}]"] = float(size)
            m["database_size_bytes"] = sum(v for k, v in m.items() if k.startswith("database_size_bytes["))
            if not self._no_waldir:
                try:
                    m["txlogs_storage_used"] = float((await self._rows(conn, "SELECT coalesce(sum(size), 0) FROM pg_ls_waldir()"))[0][0])
                except psycopg.Error:
                    self._no_waldir = True  # no pg_monitor (or a managed service that hides pg_wal)

        if in_recovery:
            m["physical_replication_delay_in_seconds"] = float((await self._rows(conn, REPLICA_LAG_SQL))[0][0] or 0)
        else:
            n_rep, lag_bytes, lag_s = (await self._rows(conn, PRIMARY_REPL_SQL))[0]
            m["replicas_connected"] = float(n_rep)
            if n_rep:
                m["physical_replication_delay_in_bytes"] = float(lag_bytes)
                m["physical_replication_delay_in_seconds"] = float(lag_s)
            retained, logical, inactive = (await self._rows(conn, SLOTS_SQL))[0]
            m["replication_slot_retained_bytes"] = float(retained or 0)
            m["logical_replication_delay_in_bytes"] = float(logical or 0)
            m["inactive_replication_slots"] = float(inactive)

        await self._table_metrics(m, list(counters), now)
        info = {"version": version.split(" on ")[0] if version else "", "version_num": self.version_num, "in_recovery": bool(in_recovery),
                "max_connections": int(max_conn), "uptime_s": int(uptime)}
        if self._ticks % 30 == 0:
            info.update(await self._capabilities(conn))
        return m, info

    def _rates(self, m: dict[str, float], counters: dict[str, dict[str, float]], extra: dict[str, float], now: float) -> None:
        prev, self._prev = self._prev, {"ts": now, "db": counters, "extra": extra}
        for db, c in counters.items():
            m[f"numbackends[{db}]"] = c["numbackends"]
        m["checksum_failures"] = sum(c.get("checksum_failures", 0) for c in counters.values())
        if not prev:
            return
        dt_s = max(1e-3, now - prev["ts"])
        per_min = 60.0 / dt_s
        tot: dict[str, float] = {}
        for db, c in counters.items():
            p = prev["db"].get(db)
            if not p:
                continue
            d = {k: c[k] - p.get(k, 0) for k in c}
            if any(d[k] < 0 for k in ("xact_commit", "xact_rollback", "blks_hit")):
                continue  # statistics were reset or the server restarted
            for k, v in d.items():
                tot[k] = tot.get(k, 0) + v
            m[f"tps[{db}]"] = round((d["xact_commit"] + d["xact_rollback"]) / dt_s, 3)
            m[f"xact_total[{db}]"] = round((d["xact_commit"] + d["xact_rollback"]) * per_min, 1)
            m[f"xact_commit[{db}]"] = round(d["xact_commit"] / dt_s, 3)
            m[f"xact_rollback[{db}]"] = round(d["xact_rollback"] / dt_s, 3)
            m[f"deadlocks[{db}]"] = round(d["deadlocks"] * per_min, 3)
            m[f"temp_bytes[{db}]"] = round(d["temp_bytes"] * per_min, 1)
        if tot:
            xacts = tot["xact_commit"] + tot["xact_rollback"]
            m["tps"] = round(xacts / dt_s, 3)
            m["xact_total"] = round(xacts * per_min, 1)
            m["xact_commit"] = round(tot["xact_commit"] / dt_s, 3)
            m["xact_rollback"] = round(tot["xact_rollback"] / dt_s, 3)
            if xacts > 0:
                m["rollback_percent"] = round(100.0 * tot["xact_rollback"] / xacts, 2)
            reads = tot["blks_hit"] + tot["blks_read"]
            if reads > 0:
                m["cache_hit_ratio"] = round(100.0 * tot["blks_hit"] / reads, 3)
            m["blks_hit"] = round(tot["blks_hit"] / dt_s, 2)
            m["blks_read"] = round(tot["blks_read"] / dt_s, 2)
            for k in ("tup_returned", "tup_fetched", "tup_inserted", "tup_updated", "tup_deleted"):
                m[k] = round(tot[k] / dt_s, 2)
            for k in ("deadlocks", "temp_files", "temp_bytes", "sessions_fatal", "sessions_killed"):
                if k in tot:
                    m[k] = round(tot[k] * per_min, 3)
        pe, ce = prev.get("extra", {}), extra
        if "wal_bytes" in ce and "wal_bytes" in pe and ce["wal_bytes"] >= pe["wal_bytes"]:
            m["wal_bytes_per_second"] = round((ce["wal_bytes"] - pe["wal_bytes"]) / dt_s, 1)
        if "ckpt_req" in ce and "ckpt_req" in pe and ce["ckpt_req"] >= pe["ckpt_req"]:
            m["checkpoints_requested"] = round((ce["ckpt_req"] - pe["ckpt_req"]) * per_min, 3)
        if "arch_failed" in ce and "arch_failed" in pe and ce["arch_failed"] >= pe["arch_failed"]:
            m["archive_failed"] = round((ce["arch_failed"] - pe["arch_failed"]) * per_min, 3)

    async def _table_metrics(self, m: dict[str, float], discovered: list[str], now: float) -> None:
        """pg_stat_user_tables is per database: refresh every fifth collection, carry the last values in between."""
        dbs = (self.table_dbs or [d for d in discovered if d not in ("postgres", "rdsadmin", "azure_maintenance", "azure_sys", "cloudsqladmin")])[:10]
        if self._ticks % 5 == 0 and not self._slots.locked():  # busy slots: keep the last values rather than wait
            for db in dbs:
                try:
                    async with self._short(db) as conn:
                        self._table_stats[db] = (now, (await self._rows(conn, TABLES_SQL))[0])
                except Exception:
                    self._table_stats.pop(db, None)
        live_total = dead_total = 0.0
        for db, (at, row) in list(self._table_stats.items()):
            if now - at > 900:
                continue
            n_tables, live, dead, mod, av, aa, vac, ana, t_vac, t_av, t_ana, t_aa = map(float, row)
            m[f"tables_counter_user_tables[{db}]"] = n_tables
            m[f"vacuum_count_user_tables[{db}]"] = vac
            m[f"analyze_count_user_tables[{db}]"] = ana
            m[f"tables_vacuumed_user_tables[{db}]"] = t_vac
            m[f"tables_autovacuumed_user_tables[{db}]"] = t_av
            m[f"tables_analyzed_user_tables[{db}]"] = t_ana
            m[f"tables_autoanalyzed_user_tables[{db}]"] = t_aa
            m[f"n_live_tup_user_tables[{db}]"] = live
            m[f"n_dead_tup_user_tables[{db}]"] = dead
            m[f"n_mod_since_analyze_user_tables[{db}]"] = mod
            m[f"autovacuum_count_user_tables[{db}]"] = av
            m[f"autoanalyze_count_user_tables[{db}]"] = aa
            if live + dead > 0:
                m[f"bloat_percent[{db}]"] = round(100.0 * dead / (live + dead), 2)
            live_total += live
            dead_total += dead
        if live_total + dead_total > 0:
            m["n_live_tup_user_tables"] = live_total
            m["n_dead_tup_user_tables"] = dead_total
            m["bloat_percent"] = round(100.0 * dead_total / (live_total + dead_total), 2)

    async def _capabilities(self, conn: Any) -> dict[str, Any]:
        import json

        import psycopg

        try:
            (user, superuser, monitor, signal, createrole, pgss, preload, checksums, archive_mode, wal_level, encoding,
             exts) = (await self._rows(conn, CAPABILITIES_SQL))[0]
        except psycopg.Error as exc:
            return {"capabilities_error": _clean_error(exc)}
        # created is not enough: a standby inherits the extension from its primary without loading the library
        loaded = "pg_stat_statements" in [x.strip().strip('"') for x in (preload or "").split(",")]
        return {
            "login": user, "capabilities": {
                "superuser": bool(superuser), "pg_monitor": bool(monitor or superuser), "signal_backend": bool(signal or superuser),
                "create_role": bool(createrole or superuser), "pg_stat_statements": bool(pgss) and loaded,
            },
            "shared_preload_libraries": preload, "data_checksums": checksums, "archive_mode": archive_mode, "wal_level": wal_level,
            "encoding": encoding, "extensions": json.loads(exts) if exts else [],
        }

    # ---------------------------------------------------------------------------------------------------- test
    async def test(self) -> dict[str, Any]:
        t0 = time.monotonic()
        checks: list[dict[str, Any]] = []
        try:
            conn = await self._connect()
        except DriverError as exc:
            return {"ok": False, "message": str(exc), "checks": [{"name": "connect", "ok": False, "detail": str(exc)}]}
        try:
            version = (await self._rows(conn, "SELECT version(), current_setting('server_version_num')::int, pg_is_in_recovery()"))[0]
            latency = int((time.monotonic() - t0) * 1000)
            checks.append({"name": "connect", "ok": True, "detail": f"{version[0].split(' on ')[0]} in {latency} ms"})
            caps = await self._capabilities(conn)
            c = caps.get("capabilities", {})
            checks.append({"name": "pg_monitor", "ok": c.get("pg_monitor", False),
                           "detail": "can read every statistic" if c.get("pg_monitor") else "GRANT pg_monitor TO the login to see all sessions and pg_wal"})
            loaded = "pg_stat_statements" in (caps.get("shared_preload_libraries") or "")
            checks.append({"name": "pg_stat_statements", "ok": c.get("pg_stat_statements", False),
                           "detail": "top queries available" if c.get("pg_stat_statements") else
                           "CREATE EXTENSION pg_stat_statements for top queries" if loaded else
                           "add pg_stat_statements to shared_preload_libraries and restart, then CREATE EXTENSION pg_stat_statements"})
            checks.append({"name": "signal_backend", "ok": c.get("signal_backend", False),
                           "detail": "can cancel/terminate sessions" if c.get("signal_backend") else "remediation of sessions needs pg_signal_backend"})
            checks.append({"name": "create_role", "ok": c.get("create_role", False),
                           "detail": "can cap roles" if c.get("create_role") else "role limits/timeouts need CREATEROLE"})
            return {"ok": True, "message": f"Connected: {version[0].split(' on ')[0]}", "version_num": version[1], "in_recovery": version[2],
                    "latency_ms": latency, "checks": checks, "info": caps}
        except Exception as exc:
            return {"ok": False, "message": _clean_error(exc), "checks": checks}
        finally:
            await conn.close()

    # ---------------------------------------------------------------------------------------------------- read tools
    async def overview(self) -> dict[str, Any]:
        cols, rows = await self._query(
            "SELECT version(), pg_is_in_recovery(), pg_postmaster_start_time(), current_setting('max_connections'), "
            "current_setting('shared_buffers'), current_setting('work_mem'), current_setting('max_wal_size'), "
            "(SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'), "
            "(SELECT count(*) FROM pg_database WHERE NOT datistemplate), current_setting('shared_preload_libraries')"
        )
        r = rows[0]
        return {
            "version": r[0].split(" on ")[0], "in_recovery": r[1], "started_at": jsonable(r[2]), "max_connections": int(r[3]),
            "shared_buffers": r[4], "work_mem": r[5], "max_wal_size": r[6], "client_sessions": r[7], "databases": r[8], "shared_preload_libraries": r[9],
        }

    async def sessions(self, state: str | None = None, min_age_s: int = 0, role: str | None = None, limit: int = 50) -> dict[str, Any]:
        cols, rows = await self._query(
            """
            SELECT pid, usename, datname, application_name, client_addr::text, state, wait_event_type, wait_event,
                   extract(epoch FROM now() - backend_start)::int AS backend_age_s,
                   extract(epoch FROM now() - xact_start)::int AS xact_age_s,
                   extract(epoch FROM now() - query_start)::int AS query_age_s,
                   pg_blocking_pids(pid) AS blocked_by,
                   left(regexp_replace(query, '\\s+', ' ', 'g'), 300) AS query
            FROM pg_stat_activity
            WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
              AND (%(state)s::text IS NULL OR state = %(state)s)
              AND (%(role)s::text IS NULL OR usename = %(role)s)
              AND coalesce(extract(epoch FROM now() - coalesce(xact_start, query_start, backend_start)), 0) >= %(min_age)s
            ORDER BY coalesce(xact_start, query_start, backend_start) NULLS LAST
            LIMIT %(limit)s
            """,
            {"state": state, "role": role, "min_age": int(min_age_s), "limit": max(1, min(int(limit), MAX_ROWS))},
        )
        _, summary = await self._query(
            "SELECT usename, coalesce(state, 'unknown'), count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20"
        )
        return table(cols, [[jsonable(v) for v in r] for r in rows], by_role_state=[[jsonable(v) for v in r] for r in summary])

    async def lock_tree(self) -> dict[str, Any]:
        cols, rows = await self._query(
            """
            WITH blocked AS (SELECT pid, pg_blocking_pids(pid) AS blockers FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0)
            SELECT a.pid, a.usename, a.datname, a.application_name, a.state, a.wait_event_type, a.wait_event,
                   extract(epoch FROM now() - a.xact_start)::int AS xact_age_s,
                   extract(epoch FROM now() - a.query_start)::int AS query_age_s,
                   pg_blocking_pids(a.pid) AS blocked_by,
                   (SELECT count(*) FROM blocked b WHERE a.pid = ANY (b.blockers)) AS blocks,
                   left(regexp_replace(a.query, '\\s+', ' ', 'g'), 240) AS query
            FROM pg_stat_activity a
            WHERE a.pid IN (SELECT pid FROM blocked) OR a.pid IN (SELECT unnest(blockers) FROM blocked)
            ORDER BY blocks DESC, xact_age_s DESC NULLS LAST
            LIMIT 100
            """
        )
        _, waits = await self._query(
            "SELECT pid, mode, locktype, relation::regclass::text FROM pg_locks WHERE NOT granted LIMIT 100"
        )
        data = [[jsonable(v) for v in r] for r in rows]
        return _with_roots(table(cols, data, waiting_for=[[jsonable(v) for v in w] for w in waits]))

    async def top_queries(self, order_by: str = "total_time", limit: int = 10) -> dict[str, Any]:
        col = {"total_time": "total_exec_time", "mean_time": "mean_exec_time", "calls": "calls", "rows": "rows", "io": "shared_blks_read",
               "temp": "temp_blks_written"}.get(order_by, "total_exec_time")
        _, has = await self._query("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements')")
        if not has[0][0]:
            return table([], [], note="pg_stat_statements is not installed in this database: add it to shared_preload_libraries and CREATE EXTENSION pg_stat_statements.")
        try:
            cols, rows = await self._query(
                f"""
                SELECT queryid::text AS queryid, (SELECT rolname FROM pg_roles WHERE oid = userid) AS role,
                       (SELECT datname FROM pg_database WHERE oid = dbid) AS database, calls,
                       round(total_exec_time::numeric, 1) AS total_ms, round(mean_exec_time::numeric, 2) AS mean_ms, rows,
                       round(100.0 * shared_blks_hit / nullif(shared_blks_hit + shared_blks_read, 0), 1) AS hit_pct,
                       temp_blks_written, left(regexp_replace(query, '\\s+', ' ', 'g'), 300) AS query
                FROM pg_stat_statements ORDER BY {col} DESC NULLS LAST LIMIT %s
                """,
                (max(1, min(int(limit), 50)),),
            )
        except DriverError as exc:
            if "shared_preload_libraries" not in str(exc):
                raise
            # the extension exists (a standby inherits it from the primary) but this server does not load the library
            return table([], [], note="pg_stat_statements is created but not loaded on this server: add it to shared_preload_libraries and restart.")
        return table(cols, [[jsonable(v) for v in r] for r in rows])

    async def table_health(self, database: str | None = None, limit: int = 15) -> dict[str, Any]:
        cols, rows = await self._query(
            """
            SELECT s.schemaname || '.' || s.relname AS table_name, n_live_tup, n_dead_tup,
                   round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
                   pg_total_relation_size(s.relid) AS total_bytes,
                   greatest(last_vacuum, last_autovacuum) AS last_vacuum, greatest(last_analyze, last_autoanalyze) AS last_analyze,
                   n_mod_since_analyze, age(c.relfrozenxid) AS xid_age, seq_scan, idx_scan
            FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid
            ORDER BY n_dead_tup DESC, total_bytes DESC LIMIT %s
            """,
            (max(1, min(int(limit), 100)),),
            dbname=database,
        )
        return table(cols, [[jsonable(v) for v in r] for r in rows], database=database or self.conninfo.get("dbname"))

    async def wraparound(self) -> dict[str, Any]:
        cols, rows = await self._query(
            "SELECT datname, age(datfrozenxid) AS xid_age, mxid_age(datminmxid) AS mxid_age, "
            "round(100.0 * age(datfrozenxid) / 2147483648, 1) AS pct_of_limit FROM pg_database ORDER BY 2 DESC"
        )
        hcols, holders = await self._query(
            """
            SELECT kind, id, owner, xmin_age, age_s, detail FROM (
              SELECT 'backend' AS kind, pid::text AS id, usename::text AS owner, age(backend_xmin) AS xmin_age,
                     extract(epoch FROM now() - xact_start)::int AS age_s, left(state || ': ' || query, 160) AS detail
              FROM pg_stat_activity WHERE backend_xmin IS NOT NULL
              UNION ALL
              SELECT 'prepared', gid, owner::text, age(transaction), extract(epoch FROM now() - prepared)::int, 'database ' || database
              FROM pg_prepared_xacts
              UNION ALL
              SELECT 'slot', slot_name::text, slot_type, greatest(age(xmin), age(catalog_xmin)), NULL,
                     CASE WHEN active THEN 'active' ELSE 'inactive' END
              FROM pg_replication_slots WHERE xmin IS NOT NULL OR catalog_xmin IS NOT NULL
            ) h ORDER BY xmin_age DESC NULLS LAST LIMIT 10
            """
        )
        _, fma = await self._query("SELECT current_setting('autovacuum_freeze_max_age')::bigint")
        return table(cols, [[jsonable(v) for v in r] for r in rows], xmin_holders=table(hcols, [[jsonable(v) for v in r] for r in holders]),
                     autovacuum_freeze_max_age=int(fma[0][0]))

    async def replication(self) -> dict[str, Any]:
        _, rec = await self._query("SELECT pg_is_in_recovery()")
        if rec[0][0]:
            cols, rows = await self._query(
                "SELECT status, sender_host, sender_port, slot_name, last_msg_receipt_time, latest_end_lsn::text FROM pg_stat_wal_receiver"
            )
            _, lag = await self._query(REPLICA_LAG_SQL)
            paused_sql = "SELECT pg_get_wal_replay_pause_state()" if self.version_num >= 140000 or not self.version_num else "SELECT pg_is_wal_replay_paused()::text"
            try:
                _, paused = await self._query(paused_sql)
                pause_state = paused[0][0]
            except DriverError:
                pause_state = "unknown"
            return table(cols, [[jsonable(v) for v in r] for r in rows], role="standby", replay_lag_s=float(lag[0][0] or 0), replay_pause_state=pause_state)
        cols, rows = await self._query(
            """
            SELECT application_name, client_addr::text, state, sync_state,
                   pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn) AS send_lag_bytes,
                   pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag_bytes,
                   extract(epoch FROM replay_lag) AS replay_lag_s
            FROM pg_stat_replication
            """
        )
        scols, slots = await self._query(
            """
            SELECT slot_name, slot_type, database, active, wal_status,
                   pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes,
                   CASE WHEN slot_type = 'logical' THEN pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) END AS logical_lag_bytes
            FROM pg_replication_slots ORDER BY retained_bytes DESC NULLS LAST
            """
        )
        return table(cols, [[jsonable(v) for v in r] for r in rows], role="primary", slots=table(scols, [[jsonable(v) for v in r] for r in slots]))

    async def settings(self, names: list[str] | None = None, pattern: str | None = None, pending_only: bool = False) -> dict[str, Any]:
        cols, rows = await self._query(
            """
            SELECT name, setting, unit, source, sourcefile, sourceline, pending_restart, context, short_desc
            FROM pg_settings
            WHERE (%(pending)s AND pending_restart)
               OR (NOT %(pending)s AND (%(names)s::text[] IS NULL OR name = ANY (%(names)s::text[]))
                                   AND (%(pattern)s::text IS NULL OR name ILIKE %(pattern)s))
            ORDER BY pending_restart DESC, name
            LIMIT 400
            """,
            {"pending": bool(pending_only), "names": names or None, "pattern": f"%{pattern}%" if pattern else None},
        )
        return table(cols, [[jsonable(v) for v in r] for r in rows])

    async def databases(self) -> dict[str, Any]:
        cols, rows = await self._query(
            """
            SELECT d.datname, pg_database_size(d.datname) AS size_bytes, s.numbackends, age(d.datfrozenxid) AS xid_age,
                   s.xact_commit, s.xact_rollback, s.deadlocks, s.temp_bytes,
                   round(100.0 * s.blks_hit / nullif(s.blks_hit + s.blks_read, 0), 2) AS hit_pct
            FROM pg_database d LEFT JOIN pg_stat_database s ON s.datid = d.oid
            WHERE d.datallowconn AND NOT d.datistemplate ORDER BY size_bytes DESC
            """
        )
        return table(cols, [[jsonable(v) for v in r] for r in rows])

    async def run_sql(self, sql: str, database: str | None = None) -> dict[str, Any]:
        import psycopg

        problem = sqlguard.check_readonly(sql)
        if problem:
            raise DriverError(f"refused: {problem}")
        try:
            async with self._short(database, timeout_ms=READONLY_TIMEOUT_MS) as conn:
                await conn.set_autocommit(False)
                await conn.execute("SET TRANSACTION READ ONLY")
                cur = await conn.execute(sql.strip().rstrip(";"))
                cols = [c.name for c in cur.description] if cur.description else []
                rows = await cur.fetchmany(MAX_ROWS + 1) if cur.description else []
                await conn.rollback()
        except psycopg.Error as exc:
            raise DriverError(_clean_error(exc)) from exc
        note = f"first {MAX_ROWS} rows" if len(rows) > MAX_ROWS else ""
        return table(cols, [[jsonable(v) for v in r] for r in rows[:MAX_ROWS]], note=note)

    # ---------------------------------------------------------------------------------------------------- assessment
    async def facts(self) -> dict[str, Any]:
        """Everything the assessment checks look at. Each query is optional: a managed service may hide some catalogs."""
        import psycopg

        f: dict[str, Any] = {"dbname": self.conninfo.get("dbname", "postgres")}
        async with self._short() as conn:
            async def q(sql: str, params: Any = None) -> list[tuple]:
                try:
                    return await self._rows(conn, sql, params)
                except psycopg.Error:
                    return []

            r = await q("SELECT current_setting('server_version_num')::int, version(), pg_is_in_recovery()")
            if r:
                f["version_num"], f["version"], f["in_recovery"] = r[0][0], r[0][1].split(" on ")[0], r[0][2]
            f["settings"] = {n: {"setting": s, "unit": u, "source": src, "pending_restart": pr, "context": ctx}
                             for n, s, u, src, pr, ctx in await q("SELECT name, setting, unit, source, pending_restart, context FROM pg_settings")}
            f["extensions"] = [f"{n} {v}" for n, v in await q("SELECT extname, extversion FROM pg_extension ORDER BY 1")]
            f["superusers"] = [n for (n,) in await q("SELECT rolname FROM pg_roles WHERE rolsuper AND rolcanlogin ORDER BY 1")]
            f["databases"] = [{"name": n, "size_bytes": s, "xid_age": a} for n, s, a in
                              await q("SELECT datname, pg_database_size(datname), age(datfrozenxid) FROM pg_database WHERE datallowconn AND NOT datistemplate")]
            acl = await q("SELECT coalesce(nspacl::text, '') FROM pg_namespace WHERE nspname = 'public'")
            f["public_schema_create"] = bool(acl and re.search(r'(^|[{,])"?=[A-Za-z*]*C', acl[0][0]))
            ssl = await q("SELECT count(*) FILTER (WHERE s.ssl), count(*) FROM pg_stat_ssl s JOIN pg_stat_activity a USING (pid) WHERE a.backend_type = 'client backend'")
            if ssl and ssl[0][1]:
                f["ssl_pct"] = round(100.0 * ssl[0][0] / ssl[0][1], 1)
            rep = await q("SELECT count(*) FROM pg_stat_replication")
            f["replicas"] = int(rep[0][0]) if rep else 0
            f["slots"] = [{"name": n, "type": t, "active": a, "retained_bytes": float(b or 0)} for n, t, a, b in
                          await q("SELECT slot_name, slot_type, active, pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) FROM pg_replication_slots")] \
                if not f.get("in_recovery") else []
            f["tables_without_pk"] = [n for (n,) in await q(
                "SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%' "
                "AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary) ORDER BY 1 LIMIT 50")]
            f["invalid_indexes"] = [n for (n,) in await q("SELECT indexrelid::regclass::text FROM pg_index WHERE NOT indisvalid LIMIT 50")]
            f["unused_indexes"] = [{"name": n, "table": t, "size_bytes": s, "idx_scan": sc} for n, t, s, sc in await q(
                "SELECT s.schemaname || '.' || s.indexrelname, s.schemaname || '.' || s.relname, pg_relation_size(s.indexrelid), s.idx_scan "
                "FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid = s.indexrelid "
                "WHERE s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary AND pg_relation_size(s.indexrelid) > 10485760 ORDER BY 3 DESC LIMIT 20")]
            f["duplicate_indexes"] = [{"indexes": list(ix), "table": t} for ix, t in await q(
                "SELECT array_agg(indexrelid::regclass::text ORDER BY indexrelid), indrelid::regclass::text FROM pg_index "
                "GROUP BY indrelid, indkey::text, indclass::text, coalesce(indexprs::text, ''), coalesce(indpred::text, '') HAVING count(*) > 1 LIMIT 20")]
            f["fk_without_index"] = [{"table": t, "constraint": c, "columns": cols} for t, c, cols in await q(
                "SELECT c.conrelid::regclass::text, c.conname, (SELECT string_agg(a.attname, ',' ORDER BY a.attnum) FROM pg_attribute a "
                "WHERE a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)) FROM pg_constraint c WHERE c.contype = 'f' AND NOT EXISTS ("
                "SELECT 1 FROM pg_index i WHERE i.indrelid = c.conrelid AND (i.indkey::int2[])[0:cardinality(c.conkey) - 1] @> c.conkey) LIMIT 20")]
            f["largest_tables"] = [{"name": n, "size_bytes": s, "live": lv, "dead": d, "dead_pct": float(p or 0)} for n, s, lv, d, p in await q(
                "SELECT schemaname || '.' || relname, pg_total_relation_size(relid), n_live_tup, n_dead_tup, "
                "round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 1) FROM pg_stat_user_tables ORDER BY 2 DESC LIMIT 15")]
        return f

    # ---------------------------------------------------------------------------------------------------- actions
    async def execute_action(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        import psycopg
        from psycopg import sql

        spec = ACTIONS.get(action)
        if spec is None:
            raise DriverError(f"unknown action {action!r}")
        missing = [k for k in spec["required"] if args.get(k) in (None, "")]
        if missing:
            raise DriverError(f"{action}: missing {', '.join(missing)}")
        db = args.get("database") or None
        try:
            if action == "cancel_backend":
                _, rows = await self._query("SELECT pg_cancel_backend(%s)", (int(args["pid"]),))
                return {"ok": bool(rows[0][0]), "message": f"cancel sent to pid {args['pid']}" if rows[0][0] else f"pid {args['pid']} not found"}
            if action == "terminate_backend":
                _, rows = await self._query("SELECT pg_terminate_backend(%s)", (int(args["pid"]),))
                return {"ok": bool(rows[0][0]), "message": f"pid {args['pid']} terminated" if rows[0][0] else f"pid {args['pid']} not found"}
            if action == "terminate_sessions":
                if not args.get("role") and not args.get("state"):
                    raise DriverError("terminate_sessions needs a role or a state filter")
                cols, rows = await self._query(
                    """
                    SELECT pid, usename, state, pg_terminate_backend(pid) FROM pg_stat_activity
                    WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
                      AND (%(role)s::text IS NULL OR usename = %(role)s) AND (%(state)s::text IS NULL OR state = %(state)s)
                      AND (%(db)s::text IS NULL OR datname = %(db)s)
                      AND now() - coalesce(state_change, backend_start) >= make_interval(secs => %(age)s::double precision)
                    LIMIT 500
                    """,
                    {"role": args.get("role"), "state": args.get("state"), "db": db, "age": int(args.get("older_than_s") or 0)},
                )
                return {"ok": True, "message": f"terminated {fmt.plural(sum(1 for r in rows if r[3]), 'session')}", "pids": [r[0] for r in rows if r[3]]}
            if action == "set_role_connection_limit":
                stmt = sql.SQL("ALTER ROLE {} CONNECTION LIMIT {}").format(sql.Identifier(args["role"]), sql.Literal(int(args["limit"])))
                await self._exec(stmt)
                return {"ok": True, "message": f"role {args['role']} capped at {int(args['limit'])} connections"}
            if action == "set_role_timeout":
                if args["setting"] not in ROLE_TIMEOUTS:
                    raise DriverError(f"setting must be one of {sorted(ROLE_TIMEOUTS)}")
                if not re.fullmatch(r"\d+\s*(ms|s|min|h)?", str(args["value"]).strip()):
                    raise DriverError("value must look like 3s, 500ms, 5min or 1h")
                stmt = sql.SQL("ALTER ROLE {} SET {} = {}").format(sql.Identifier(args["role"]), sql.Identifier(args["setting"]), sql.Literal(str(args["value"]).strip()))
                await self._exec(stmt)
                return {"ok": True, "message": f"{args['setting']} = {args['value']} for new sessions of {args['role']}"}
            if action == "vacuum_table":
                opts = "ANALYZE, FREEZE" if args.get("freeze") else "ANALYZE"
                await self._exec(sql.SQL("VACUUM (" + opts + ") {}").format(sql.Identifier(*_ident_parts(args["table"]))), db, timeout_ms=3_600_000)
                return {"ok": True, "message": f"VACUUM ({opts}) {args['table']} finished"}
            if action == "analyze_table":
                await self._exec(sql.SQL("ANALYZE {}").format(sql.Identifier(*_ident_parts(args["table"]))), db, timeout_ms=600_000)
                return {"ok": True, "message": f"ANALYZE {args['table']} finished"}
            if action == "create_index_concurrently":
                cols_ = [sql.Identifier(c) for c in args["columns"]]
                name = args.get("name") or ("idx_" + "_".join(_ident_parts(args["table"])[-1:] + list(args["columns"])))[:63]
                stmt = sql.SQL("CREATE INDEX CONCURRENTLY IF NOT EXISTS {} ON {} ({})").format(
                    sql.Identifier(name), sql.Identifier(*_ident_parts(args["table"])), sql.SQL(", ").join(cols_))
                await self._concurrently(stmt, db, args["table"], [name])
                return {"ok": True, "message": f"index {name} built"}
            if action == "reindex_index_concurrently":
                parts = _ident_parts(args["index"])
                _, rows = await self._query("SELECT indrelid::regclass::text FROM pg_index WHERE indexrelid = %s::regclass", (args["index"],), db)
                if not rows:
                    raise DriverError(f"{args['index']} is not an index")
                await self._concurrently(sql.SQL("REINDEX INDEX CONCURRENTLY {}").format(sql.Identifier(*parts)), db, rows[0][0],
                                         [parts[-1] + "_ccnew", parts[-1] + "_ccold"], prefix=True)
                return {"ok": True, "message": f"index {args['index']} rebuilt"}
            if action == "alter_system_set":
                name = str(args["name"]).strip().lower()
                if name not in RELOADABLE_SETTINGS:
                    raise DriverError(f"{name} is not on the list of reloadable settings rAIden may change; plan it with a person")
                problem = sqlguard.check_statement(f"ALTER SYSTEM SET {name} = '{args['value']}'")
                if problem:
                    raise DriverError(problem)
                await self._exec(sql.SQL("ALTER SYSTEM SET {} = {}").format(sql.Identifier(name), sql.Literal(str(args["value"]))))
                await self._exec(sql.SQL("SELECT pg_reload_conf()"))
                return {"ok": True, "message": f"{name} = {args['value']} (reloaded)"}
            if action == "reload_config":
                await self._exec(sql.SQL("SELECT pg_reload_conf()"))
                return {"ok": True, "message": "configuration reloaded"}
            if action == "resume_wal_replay":
                await self._exec(sql.SQL("SELECT pg_wal_replay_resume()"))
                return {"ok": True, "message": "WAL replay resumed"}
            if action == "drop_replication_slot":
                _, rows = await self._query("SELECT active FROM pg_replication_slots WHERE slot_name = %s", (args["slot"],))
                if not rows:
                    raise DriverError(f"no replication slot named {args['slot']}")
                if rows[0][0]:
                    raise DriverError(f"slot {args['slot']} is active: its consumer is connected")
                await self._query("SELECT pg_drop_replication_slot(%s)", (args["slot"],))
                return {"ok": True, "message": f"replication slot {args['slot']} dropped"}
            if action == "rollback_prepared":
                await self._exec(sql.SQL("ROLLBACK PREPARED {}").format(sql.Literal(args["gid"])), db)
                return {"ok": True, "message": f"prepared transaction {args['gid']} rolled back"}
            if action == "execute_sql":
                problem = sqlguard.check_statement(args["sql"])
                if problem:
                    raise DriverError(f"refused: {problem}")
                cols, rows = await self._query(args["sql"], dbname=db)
                return {"ok": True, "message": f"statement executed ({fmt.plural(len(rows), 'row')} returned)", "columns": cols, "rows": [[jsonable(v) for v in r] for r in rows[:50]]}
        except psycopg.Error as exc:
            raise DriverError(_clean_error(exc)) from exc
        raise DriverError(f"action {action} is not implemented for this server")

    async def _exec(self, stmt: Any, dbname: str | None = None, timeout_ms: int = STATEMENT_TIMEOUT_MS, lock_timeout_ms: int = LOCK_TIMEOUT_MS) -> None:
        # VACUUM and ANALYZE without the privilege do not fail: PostgreSQL warns "skipping ..." and does nothing
        skipped: list[str] = []
        async with self._short(dbname, timeout_ms=timeout_ms, lock_timeout_ms=lock_timeout_ms) as conn:
            conn.add_notice_handler(lambda d: skipped.append(d.message_primary or "")
                                    if d.severity_nonlocalized == "WARNING" and "skipping" in (d.message_primary or "") else None)
            await conn.execute(stmt)
        if skipped:
            raise DriverError(skipped[0])

    async def _concurrently(self, stmt: Any, db: str | None, table_: str, names: list[str], prefix: bool = False) -> None:
        """CREATE INDEX / REINDEX CONCURRENTLY. Both wait for every older transaction, and both leave an INVALID index behind
        when they fail (it slows every write and helps no read): remove it, or say exactly what is left. IF NOT EXISTS also
        "succeeds" on top of an invalid index from an earlier attempt, so the result is checked, not assumed."""
        import psycopg

        invalid_sql = ("SELECT format('%%I.%%I', n.nspname, c.relname) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                       "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE NOT i.indisvalid AND i.indrelid = %s::regclass AND "
                       + ("(" + " OR ".join(["c.relname LIKE %s"] * len(names)) + ")" if prefix else "c.relname = ANY (%s)"))
        params = (table_, *[n + "%" for n in names]) if prefix else (table_, names)

        async def drop_invalid() -> tuple[list[str], list[str]]:
            dropped, kept = [], []
            for (ix,) in (await self._query(invalid_sql, params, db))[1]:
                try:
                    await self._exec(sql_drop_index(ix), db)
                    dropped.append(ix)
                except (psycopg.Error, DriverError) as exc:  # not the owner, or the same old transactions are in the way
                    kept.append(f"{ix} ({_clean_error(exc)})")
            return dropped, kept

        _, kept = await drop_invalid()  # the leftovers of an earlier attempt first
        if kept:
            raise DriverError(f"the unfinished index {', '.join(kept)} of an earlier attempt could not be dropped")
        try:
            await self._exec(stmt, db, timeout_ms=6 * 3_600_000, lock_timeout_ms=MAINTENANCE_LOCK_TIMEOUT_MS)
            failure = None
        except psycopg.Error as exc:
            failure = _clean_error(exc)
        dropped, kept = await drop_invalid()
        if failure is None and not dropped and not kept:
            return
        msg = failure or "the index is not valid"
        if dropped:
            msg += f"; the unfinished index {', '.join(dropped)} was dropped"
        if kept:
            msg += f"; the unfinished index {', '.join(kept)} is INVALID and still there: DROP INDEX CONCURRENTLY it once nothing holds the table"
        raise DriverError(msg)


def sql_drop_index(qualified: str) -> Any:
    from psycopg import sql

    return sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(sql.Identifier(*_ident_parts(qualified)))


def _with_roots(data: dict[str, Any]) -> dict[str, Any]:
    cols = data["columns"]
    if not data["rows"]:
        data["roots"], data["edges"] = [], []
        data["note"] = "no session is waiting for a lock"
        return data
    i_pid, i_by = cols.index("pid"), cols.index("blocked_by")
    edges = [[r[i_pid], b] for r in data["rows"] for b in (r[i_by] or [])]
    waiting = {e[0] for e in edges}
    blockers = {e[1] for e in edges}
    data["edges"] = edges
    data["roots"] = sorted(blockers - waiting)
    return data


def _clean_error(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    msg = text[0] if text else exc.__class__.__name__
    return re.sub(r"password=\S+", "password=***", msg)[:400]
