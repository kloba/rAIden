"""DemoDriver: the Driver interface answered from the simulated demo fleet (consistent with its metrics and scenarios)."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from .. import sqlguard
from ..drivers.base import ACTIONS, CollectResult, DriverError, table
from .fleet import DemoFleet, _u
from .profiles import BY_ID, QUERIES, TABLES, UNTUNED, Profile, settings_for

SESSION_COLS = ["pid", "usename", "datname", "application_name", "client_addr", "state", "wait_event_type", "wait_event",
                "backend_age_s", "xact_age_s", "query_age_s", "blocked_by", "query"]


class DemoDriver:
    def __init__(self, server_id: str, fleet: DemoFleet, profile: str | None = None) -> None:
        self.server_id = server_id
        self.fleet = fleet
        self.p = BY_ID[profile or server_id]
        self.sid = self.p.id  # the simulated server behind it (an onboarded demo server may have been given another id)

    async def close(self) -> None:
        return None

    def _now(self) -> float:
        return time.time()

    # ---------------------------------------------------------------------------------------------------- collection
    async def collect(self) -> CollectResult:
        now = self._now()
        m = self.fleet.metrics(self.sid, now)
        p = self.p
        conf = {r["name"]: r["setting"] for r in settings_for(p)}
        info = {
            "version": p.version, "version_num": p.version_num, "in_recovery": bool(p.replica_of), "max_connections": p.max_connections,
            "uptime_s": int(m.get("uptime_seconds", 0)), "login": "raiden_monitor", "capabilities": _capabilities(p),
            **{k: conf[k] for k in ("shared_preload_libraries", "data_checksums", "archive_mode", "wal_level")}, "encoding": "UTF8",
            "extensions": _extensions(p.id), "region": p.region, "sku": p.sku, "sources": list(p.sources),
        }
        return CollectResult(ok=bool(m.get("is_db_alive", 1)), metrics=m, info=info, error="" if m.get("is_db_alive", 1) else "connection refused (simulated crash)")

    async def test(self) -> dict[str, Any]:
        await asyncio.sleep(0.4)
        p = self.p
        c = _capabilities(p)
        return {
            "ok": True, "message": f"Connected: {p.version}", "version_num": p.version_num, "in_recovery": bool(p.replica_of), "latency_ms": 18,
            "checks": [  # the same wording as the PostgreSQL driver
                {"name": "connect", "ok": True, "detail": f"{p.version} in 18 ms (simulated)"},
                {"name": "pg_monitor", "ok": True, "detail": "can read every statistic"},
                {"name": "pg_stat_statements", "ok": c["pg_stat_statements"],
                 "detail": "top queries available" if c["pg_stat_statements"] else "CREATE EXTENSION pg_stat_statements for top queries"},
                {"name": "signal_backend", "ok": True, "detail": "can cancel/terminate sessions"},
                {"name": "create_role", "ok": c["create_role"], "detail": "can cap roles" if c["create_role"] else "role limits/timeouts need CREATEROLE"},
            ],
        }

    # ---------------------------------------------------------------------------------------------------- read tools
    async def overview(self) -> dict[str, Any]:
        p = self.p
        m = self.fleet.metrics(self.sid, self._now())
        return {"version": p.version, "in_recovery": bool(p.replica_of), "platform": p.platform, "region": p.region, "sku": p.sku,
                "max_connections": p.max_connections, "client_sessions": int(m["active_connections"]), "databases": len(p.databases),
                "uptime_s": int(m.get("uptime_seconds", 0)), "shared_preload_libraries": next(r["setting"] for r in settings_for(p) if r["name"] == "shared_preload_libraries")}

    async def sessions(self, state: str | None = None, min_age_s: int = 0, role: str | None = None, limit: int = 50) -> dict[str, Any]:
        rows = self.fleet.sessions(self.sid, self._now())
        if state:
            rows = [r for r in rows if r["state"] == state]
        if role:
            rows = [r for r in rows if r["usename"] == role]
        rows = [r for r in rows if (r["xact_age_s"] or r["query_age_s"] or 0) >= min_age_s]
        summary: dict[tuple[str, str], int] = {}
        m = self.fleet.metrics(self.sid, self._now())
        for r in self.fleet.sessions(self.sid, self._now()):
            summary[(r["usename"], r["state"])] = summary.get((r["usename"], r["state"]), 0) + 1
        sc = self.fleet.active_scenario(self.sid)
        if sc and sc.active and sc.kind == "connection_storm":
            summary[("reporting", "idle")] = int(m["active_connections"] - 30)
        by = sorted(([u, s, n] for (u, s), n in summary.items()), key=lambda r: -r[2])[:20]
        return table(SESSION_COLS, [[r[c] for c in SESSION_COLS] for r in rows[: max(1, min(limit, 200))]], by_role_state=by)

    async def lock_tree(self) -> dict[str, Any]:
        rows = [r for r in self.fleet.sessions(self.sid, self._now()) if r["blocked_by"]]
        blockers = {b for r in rows for b in r["blocked_by"]}
        involved = rows + [r for r in self.fleet.sessions(self.sid, self._now()) if r["pid"] in blockers and not r["blocked_by"]]
        cols = ["pid", "usename", "datname", "application_name", "state", "wait_event_type", "wait_event", "xact_age_s", "query_age_s", "blocked_by", "blocks", "query"]
        out = []
        for r in sorted(involved, key=lambda r: -sum(1 for x in rows if r["pid"] in x["blocked_by"])):
            blocks = sum(1 for x in rows if r["pid"] in x["blocked_by"])
            out.append([r["pid"], r["usename"], r["datname"], r["application_name"], r["state"], r["wait_event_type"], r["wait_event"],
                        r["xact_age_s"], r["query_age_s"], r["blocked_by"], blocks, r["query"]])
        edges = [[r["pid"], b] for r in rows for b in r["blocked_by"]]
        waiting = {e[0] for e in edges}
        sc = self.fleet.active_scenario(self.sid)
        waits = []
        if sc and sc.active and sc.kind == "lock_queue":
            waits = [[sc.data["migrator_pid"], "AccessExclusiveLock", "relation", "orders.orders"]] + [
                [r["pid"], "RowExclusiveLock", "relation", "orders.orders"] for r in rows if r["pid"] != sc.data["migrator_pid"]][:20]
        data = table(cols, out, waiting_for=waits)
        data["edges"] = edges
        data["roots"] = sorted(blockers - waiting)
        total = int(self.fleet.metrics(self.sid, self._now()).get("blocked_sessions", 0))
        data["waiting_total"] = max(total, len(waiting))
        if out and total > len(waiting):
            data["note"] = f"showing {len(waiting)} of {total} waiting sessions"
        if not out:
            data["note"] = "no session is waiting for a lock"
        return data

    async def top_queries(self, order_by: str = "total_time", limit: int = 10) -> dict[str, Any]:
        now = self._now()
        m = self.fleet.metrics(self.sid, now)
        uptime_calls = max(1.0, self.p.base_tps * 3600 * 24 * 3)
        rows = []
        for i, (q, mean_ms, share, nrows) in enumerate(QUERIES[self.sid]):
            calls = int(uptime_calls * share)
            hit = round(m["cache_hit_ratio"] - (8 if mean_ms > 1000 else 0), 1)
            rows.append([str(int(_u(self.sid, i, "qid") * 9e18)), self.p.app_role, self.p.databases[0], calls, round(calls * mean_ms, 1),
                         mean_ms, calls * nrows, hit, int(calls * 0.0001 if mean_ms > 100 else 0), q])
        sc = self.fleet.active_scenario(self.sid)
        if sc and sc.kind == "cpu_spike" and sc.active:
            calls = int(9000 + (now - sc.started) * 40)
            rows.append(["-6311468023361094377", "payments_app", "payments", calls, round(calls * 2140.0, 1), 2140.0, calls * 20, 91.4, 0,
                         "SELECT * FROM payments.transactions WHERE merchant_ref = $1 ORDER BY created_at DESC LIMIT $2"])
        key = {"total_time": 4, "mean_time": 5, "calls": 3, "rows": 6, "io": 7, "temp": 8}.get(order_by, 4)
        rows.sort(key=lambda r: -float(r[key] or 0) if key != 7 else float(r[key] or 0))
        cols = ["queryid", "role", "database", "calls", "total_ms", "mean_ms", "rows", "hit_pct", "temp_blks_written", "query"]
        return table(cols, rows[: max(1, min(limit, 50))])

    async def table_health(self, database: str | None = None, limit: int = 15) -> dict[str, Any]:
        now = self._now()
        rows = []
        slow_wrap = self.sid == "inventory-prod" and "inventory-prod" not in self.fleet.slow_resolved
        for name, live, dead_pct in TABLES[self.sid]:
            dead = int(live * dead_pct / 100 * (1 + 0.2 * _u(self.sid, name, int(now // 600))))
            xid_age = int(self.fleet.metrics(self.sid, now)["maximum_used_transactionIDs"] * (0.98 if "movements" in name else 0.4)) if slow_wrap else int(60e6 + _u(name) * 120e6)
            rows.append([name, live, dead, round(100 * dead / max(1, live + dead), 1), int(live * 180), _ago(now, 3600 * (1 + _u(name, "v") * 20)),
                         _ago(now, 3600 * (1 + _u(name, "a") * 10)), int(dead * 0.7), xid_age, int(_u(name, "s") * 900), int(live * 3)])
        rows.sort(key=lambda r: -r[2])
        cols = ["table_name", "n_live_tup", "n_dead_tup", "dead_pct", "total_bytes", "last_vacuum", "last_analyze", "n_mod_since_analyze", "xid_age", "seq_scan", "idx_scan"]
        return table(cols, rows[:limit], database=database or self.p.databases[0])

    async def wraparound(self) -> dict[str, Any]:
        now = self._now()
        m = self.fleet.metrics(self.sid, now)
        top = m["maximum_used_transactionIDs"]
        rows = [[db, int(top * (1.0 if i == 0 else 0.35)), int(m["max_multixact_age"]), round(100 * top * (1.0 if i == 0 else 0.35) / 2147483648, 1)]
                for i, db in enumerate(self.p.databases)]
        holders = []
        if self.sid == "inventory-prod" and "inventory-prod" not in self.fleet.slow_resolved:
            age_s = int(m.get("prepared_xact_oldest_seconds", 0))
            holders.append(["prepared", "erp-sync-7f3a9c", "erp_sync", int(top - 2e6), age_s, "database inventory"])
        holders.append(["backend", str(20000 + int(_u(self.sid, "pid") * 9000)), self.p.app_role, int(m["oldest_backend_xmin_age"]),
                        int(m["longest_transaction_time_sec"]), "active: " + QUERIES[self.sid][0][0][:80]])
        return table(["datname", "xid_age", "mxid_age", "pct_of_limit"], rows,
                     xmin_holders=table(["kind", "id", "owner", "xmin_age", "age_s", "detail"], holders), autovacuum_freeze_max_age=200_000_000)

    async def replication(self) -> dict[str, Any]:
        return self._replication()

    def _replication(self) -> dict[str, Any]:
        now = self._now()
        m = self.fleet.metrics(self.sid, now)
        p = self.p
        if p.replica_of:
            sc = self.fleet.active_scenario(self.sid)
            return table(["status", "sender_host", "sender_port", "slot_name", "last_msg_receipt_time", "latest_end_lsn"],
                         [["streaming", BY_ID[p.replica_of].host, 5432, "pay_replica_1", _ago(now, 0.4), "3A/9F21C0D8"]],
                         role="standby", replay_lag_s=round(m.get("physical_replication_delay_in_seconds", 0), 1),
                         replay_pause_state="not paused", replay_waiting_for=("conflict with the active query of pid " + str(sc.data["report_pid"]) +
                                                                                  " (max_standby_streaming_delay = -1)") if sc and sc.active and sc.kind == "replication_lag" else None)
        rows = []
        if p.id == "payments-prod":
            lag_s = m.get("physical_replication_delay_in_seconds", 0.3)
            rows.append(["pay_replica_1", "10.40.2.12", "streaming", "async", int(m["wal_bytes_per_second"] * 0.1), int(m.get("physical_replication_delay_in_bytes", 0)), round(lag_s, 2)])
        slots = []
        if p.id == "payments-prod":
            slots.append(["pay_replica_1", "physical", None, True, "reserved", int(m.get("replication_slot_retained_bytes", 6e7)), None])
        if p.id == "ledger-dev":
            gone = "debezium_ledger" in self.fleet.fixes[p.id]["dropped_slots"]
            if not gone:
                active = p.id in self.fleet.slow_resolved or self.fleet._slow_value(p.id, now) <= 0
                slots.append(["debezium_ledger", "logical", "ledger", active, "extended" if not active else "reserved",
                              int(m.get("replication_slot_retained_bytes", 0)), int(m.get("logical_replication_delay_in_bytes", 0))])
        if p.id == "orders-prod":
            slots.append(["fabric_mirror", "logical", "orders", True, "reserved", int(m.get("replication_slot_retained_bytes", 3e7)), int(m.get("replication_slot_retained_bytes", 3e7))])
        return table(["application_name", "client_addr", "state", "sync_state", "send_lag_bytes", "replay_lag_bytes", "replay_lag_s"], rows, role="primary",
                     slots=table(["slot_name", "slot_type", "database", "active", "wal_status", "retained_bytes", "logical_lag_bytes"], slots))

    async def settings(self, names: list[str] | None = None, pattern: str | None = None, pending_only: bool = False) -> dict[str, Any]:
        rows = settings_for(self.p)
        overrides = self.fleet.fixes[self.sid]["settings"]
        for r in rows:
            if r["name"] in overrides:
                r.update(setting=overrides[r["name"]], source="configuration file (postgresql.auto.conf)")
        for name, value in overrides.items():
            if not any(r["name"] == name for r in rows):
                rows.append({"name": name, "setting": value, "unit": None, "source": "configuration file (postgresql.auto.conf)", "pending_restart": False, "context": "sighup"})
        if pending_only:
            rows = [r for r in rows if r["pending_restart"]]
        if names:
            rows = [r for r in rows if r["name"] in names]
        if pattern:
            rows = [r for r in rows if pattern.lower() in r["name"].lower()]
        cols = ["name", "setting", "unit", "source", "sourcefile", "sourceline", "pending_restart", "context", "short_desc"]
        conf = "/etc/postgresql/18/main/conf.d/90-experiment.conf" if self.sid == "ledger-dev" else "/etc/postgresql/postgresql.conf"
        return table(cols, [[r["name"], r["setting"], r["unit"], r["source"], conf if r["pending_restart"] else None, 3 if r["pending_restart"] else None,
                             r["pending_restart"], r["context"], ""] for r in sorted(rows, key=lambda r: (not r["pending_restart"], r["name"]))])

    async def databases(self) -> dict[str, Any]:
        m = self.fleet.metrics(self.sid, self._now())
        rows = [[db, int(m.get(f"database_size_bytes[{db}]", 0)), int(m.get(f"numbackends[{db}]", 0)), int(m["maximum_used_transactionIDs"] * (1 if i == 0 else 0.35)),
                 int(self.p.base_tps * 86400 * 30 * (0.97 if i == 0 else 0.01)), int(self.p.base_tps * 86400 * 0.4 * (0.97 if i == 0 else 0.01)), 0, 0,
                 round(m["cache_hit_ratio"], 2)] for i, db in enumerate(self.p.databases)]
        return table(["datname", "size_bytes", "numbackends", "xid_age", "xact_commit", "xact_rollback", "deadlocks", "temp_bytes", "hit_pct"], rows)

    async def run_sql(self, sql: str, database: str | None = None) -> dict[str, Any]:
        problem = sqlguard.check_readonly(sql)
        if problem:
            raise DriverError(f"refused: {problem}")
        low = sql.lower()
        if low.strip().startswith("explain"):
            indexed = any("merchant_ref" in (ix.get("columns") or []) for ix in self.fleet.fixes[self.sid]["indexes"])
            if "merchant_ref" in low and not indexed:
                plan = ["Limit  (cost=1000.58..2210419.31 rows=20 width=212)", "  ->  Gather Merge  (cost=1000.58..9281533.46 rows=84 width=212)",
                        "        Workers Planned: 4", "        ->  Sort  (cost=0.56..9280523.11 rows=21 width=212)", "              Sort Key: created_at DESC",
                        "              ->  Parallel Seq Scan on transactions  (cost=0.00..9280522.71 rows=21 width=212)",
                        "                    Filter: (merchant_ref = 'M-4471'::text)"]
            elif "merchant_ref" in low:
                plan = ["Limit  (cost=0.57..81.43 rows=20 width=212)", "  ->  Index Scan Backward using idx_transactions_merchant_ref on transactions  (cost=0.57..339.67 rows=84 width=212)",
                        "        Index Cond: (merchant_ref = 'M-4471'::text)"]
            else:
                plan = ["Result  (cost=0.00..0.01 rows=1 width=4)"]
            return table(["QUERY PLAN"], [[line] for line in plan])
        if "pg_locks" in low or "pg_blocking_pids" in low:
            return await self.lock_tree()
        if "pg_stat_activity" in low:
            return await self.sessions(limit=50)
        if "pg_stat_statements" in low:
            return await self.top_queries()
        if "pg_settings" in low or low.strip().startswith("show"):
            name = re.search(r"show\s+(\w+)", low)
            return await self.settings(names=[name.group(1)] if name else None)
        if "pg_stat_user_tables" in low or "n_dead_tup" in low:
            return await self.table_health(database)
        if "pg_replication_slots" in low or "pg_stat_replication" in low or "pg_stat_wal_receiver" in low:
            return await self.replication()
        if "datfrozenxid" in low or "pg_prepared_xacts" in low:
            return await self.wraparound()
        if "pg_database" in low:
            return await self.databases()
        if "version()" in low:
            return table(["version"], [[f"{self.p.version} on x86_64-pc-linux-gnu, compiled by gcc (simulated)"]])
        if re.fullmatch(r"\s*select\s+now\(\)\s*;?\s*", low):
            return table(["now"], [[time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime())]])
        return table([], [], note="The demo fleet is simulated: it answers the typed tools (sessions, locks, top queries, tables, replication, "
                                  "settings, wraparound) and common catalog queries. Free SQL needs a real server.")

    # ---------------------------------------------------------------------------------------------------- assessment
    async def facts(self) -> dict[str, Any]:
        await asyncio.sleep(1.2)
        return self.facts_sync()

    def facts_sync(self) -> dict[str, Any]:
        p = self.p
        now = self._now()
        st = {r["name"]: {k: r[k] for k in ("setting", "unit", "source", "pending_restart", "context")} for r in settings_for(p)}
        for name, value in self.fleet.fixes[p.id]["settings"].items():
            st.setdefault(name, {"unit": None, "source": "configuration file", "pending_restart": False, "context": "sighup"})["setting"] = value
        st.setdefault("autovacuum", {"setting": "on", "unit": None, "source": "default", "pending_restart": False, "context": "sighup"})
        for name, value in (("fsync", "on"), ("full_page_writes", "on"), ("synchronous_commit", "on"), ("track_io_timing", "on"),
                            ("log_autovacuum_min_duration", "-1" if p.id in ("crm-prod", "analytics-staging") else "10000"), ("log_checkpoints", "on"),
                            ("min_wal_size", "1024"), ("max_worker_processes", "8"), ("max_parallel_workers", "8"), ("max_parallel_workers_per_gather", "2"),
                            ("wal_compression", "off"), ("huge_pages", "try" if p.platform not in ("self_managed",) else "off")):
            st.setdefault(name, {"setting": value, "unit": "MB" if name == "min_wal_size" else ("ms" if name == "log_autovacuum_min_duration" else None),
                                 "source": "default", "pending_restart": False, "context": "postmaster" if name in ("huge_pages", "max_worker_processes") else "sighup"})
        m = self.fleet.metrics(p.id, now)
        slots = []
        rep = self._replication()
        for row in rep.get("slots", {}).get("rows", []):
            slots.append({"name": row[0], "type": row[1], "active": row[3], "retained_bytes": row[5]})
        extras = {
            "orders-prod": dict(unused_indexes=[{"name": "orders.idx_orders_legacy_status", "table": "orders.orders", "size_bytes": 2.1 * 1024**3, "idx_scan": 0}],
                                fk_without_index=[{"table": "orders.payments_outbox", "constraint": "payments_outbox_order_id_fkey", "columns": "order_id"}],
                                tables_without_pk=["orders.order_events_archive"]),
            "payments-prod": dict(duplicate_indexes=[{"indexes": ["ledger.entries_account_idx", "ledger.entries_account_id_idx"], "table": "ledger.entries"}]),
            "inventory-prod": dict(invalid_indexes=["inventory.stock_movements_sku_idx_ccnew"], tables_without_pk=["inventory.sync_queue"]),
            "analytics-staging": dict(tables_without_pk=["staging.sales_batch", "staging.sales_batch_errors"]),
            "crm-prod": dict(public_schema_create=True, superusers=["crm_admin", "etl_admin", "postgres"], ssl_pct=72.0),
            "ledger-dev": dict(superusers=["raiden", "postgres"]),
            "billing-prod": dict(public_schema_create=True, superusers=["postgres", "billing_owner", "dba_jan"], ssl_pct=64.0,
                                 unused_indexes=[{"name": "billing.invoices_legacy_number_idx", "table": "billing.invoices", "size_bytes": 3.4 * 1024**3, "idx_scan": 0}],
                                 duplicate_indexes=[{"indexes": ["billing.invoice_lines_invoice_id_idx", "billing.invoice_lines_invoice_idx"], "table": "billing.invoice_lines"}],
                                 fk_without_index=[{"table": "billing.payment_attempts", "constraint": "payment_attempts_invoice_id_fkey", "columns": "invoice_id"}],
                                 tables_without_pk=["billing.audit_log"]),
        }.get(p.id, {})
        largest = [{"name": n, "size_bytes": live * 180, "live": live, "dead": int(live * d / 100), "dead_pct": d} for n, live, d in TABLES[p.id]]
        return {
            "dbname": p.databases[0], "version_num": p.version_num, "version": p.version, "in_recovery": bool(p.replica_of), "settings": st,
            "extensions": _extensions(p.id), "superusers": extras.get("superusers", ["postgres"]),
            "databases": [{"name": db, "size_bytes": m.get(f"database_size_bytes[{db}]", 0), "xid_age": m["maximum_used_transactionIDs"]} for db in p.databases],
            "public_schema_create": extras.get("public_schema_create", False), "ssl_pct": extras.get("ssl_pct", 100.0),
            "replicas": 1 if p.id == "payments-prod" else 0, "slots": slots, "tables_without_pk": extras.get("tables_without_pk", []),
            "invalid_indexes": extras.get("invalid_indexes", []), "unused_indexes": extras.get("unused_indexes", []),
            "duplicate_indexes": extras.get("duplicate_indexes", []), "fk_without_index": extras.get("fk_without_index", []), "largest_tables": largest,
            # what Azure Resource Manager says about an Azure server (the real driver asks it: runtime.server_facts)
            **({"azure": {"entra_auth": True, "password_auth": True, "cmk": False, "public_network": False, "sku": p.sku, "location": p.region}}
               if p.platform == "azure_flexible" else {}),
        }

    # ---------------------------------------------------------------------------------------------------- actions
    async def execute_action(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        spec = ACTIONS.get(action)
        if spec is None:
            raise DriverError(f"unknown action {action!r}")
        missing = [k for k in spec["required"] if args.get(k) in (None, "")]
        if missing:
            raise DriverError(f"{action}: missing {', '.join(missing)}")
        if action == "execute_sql":
            problem = sqlguard.check_statement(str(args.get("sql", "")))
            if problem:
                raise DriverError(f"refused: {problem}")
        await asyncio.sleep({"create_index_concurrently": 4.0, "vacuum_table": 3.0, "reindex_index_concurrently": 3.0}.get(action, 0.6))
        ok, message = self.fleet.apply(self.sid, action, args, self._now())
        if not ok:
            raise DriverError(message)
        return {"ok": True, "message": message, "simulated": True}


def _ago(now: float, seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - seconds))


def _capabilities(p: Profile) -> dict[str, bool]:
    fresh = p.id in UNTUNED  # a monitoring login made the minimal way: pg_monitor and pg_signal_backend, nothing else
    return {"superuser": False, "pg_monitor": True, "signal_backend": True, "pg_stat_statements": not fresh,
            "create_role": not fresh and (p.environment != "production" or p.platform == "self_managed")}


def _extensions(sid: str) -> list[str]:
    if sid in UNTUNED:
        return ["plpgsql 1.0", "pgcrypto 1.3", "btree_gist 1.7"]
    base = ["plpgsql 1.0", "pg_stat_statements 1.11"]
    extra = {"orders-prod": ["pgcrypto 1.3", "uuid-ossp 1.1", "azure 1.1", "pg_partman 5.2"], "payments-prod": ["pgcrypto 1.3", "pg_partman 5.2", "amcheck 1.4"],
             "payments-replica": ["pgcrypto 1.3", "pg_partman 5.2", "amcheck 1.4"], "inventory-prod": ["pg_trgm 1.6", "aws_commons 1.2"],
             "analytics-staging": ["pg_trgm 1.6", "cstore_fdw 1.7", "postgres_fdw 1.1"], "crm-prod": ["pg_trgm 1.6", "citext 1.6"],
             "ledger-dev": ["amcheck 1.4", "pg_surgery 1.0"]}
    return base + extra.get(sid, [])
