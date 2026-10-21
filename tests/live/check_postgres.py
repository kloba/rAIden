"""Live check of the console's PostgreSQL driver against real servers.

It runs where PostgreSQL runs: on the lab VM (deploy/vm/console-lab.sh sets up PostgreSQL 13 to 18 side by side, a
streaming standby, a least-privilege monitoring login and a small pgbench workload). Never point it at a production
server: it builds a lock queue, a prepared transaction and an abandoned replication slot, then lets the driver clean
them up.

    python tests/live/check_postgres.py --servers /etc/raiden-lab/servers.json --report /var/lib/raiden-lab/driver.json

servers.json: [{"name": "pg17", "monitor": {conninfo of raiden_monitor}, "admin": {conninfo of a superuser}}, ...]
Every check prints one line; the exit code is the number of failed checks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from raiden.console import assessment
from raiden.console.drivers import postgres
from raiden.console.drivers.base import DriverError
from raiden.console.drivers.postgres import PostgresDriver

RESULTS: list[dict[str, Any]] = []


def record(server: str, check: str, ok: bool, detail: str = "", data: Any = None) -> bool:
    RESULTS.append({"server": server, "check": check, "ok": ok, "detail": detail, "data": data})
    print(f"{'PASS' if ok else 'FAIL'} {server:<8} {check:<34} {detail[:110]}", flush=True)
    return ok


async def admin_conn(conninfo: dict[str, Any], dbname: str | None = None, autocommit: bool = True) -> Any:
    import psycopg

    params = dict(conninfo)
    if dbname:
        params["dbname"] = dbname
    return await psycopg.AsyncConnection.connect(**params, autocommit=autocommit, connect_timeout=5)


async def admin_exec(conninfo: dict[str, Any], dbname: str, sql: str) -> None:
    conn = await admin_conn(conninfo, dbname)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def wait_until(fn: Any, timeout: float = 20.0, every: float = 0.5) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = await fn()
        if got:
            return got
        await asyncio.sleep(every)
    return None


async def check_server(cfg: dict[str, Any]) -> None:
    name, mon, adm = cfg["name"], cfg["monitor"], cfg["admin"]
    db = mon.get("dbname", "app")
    drv = PostgresDriver(name, mon)
    admin = PostgresDriver(name + "-admin", adm)  # the same driver code with a superuser: the "it works" path of actions

    # -- connection test (the onboarding wizard's Test connection) -------------------------------------------------
    t = await drv.test()
    checks = {c["name"]: c["ok"] for c in t.get("checks", [])}
    record(name, "test connection", t["ok"] and checks.get("pg_monitor", False), f"{t['message']}; grants {checks}", t)
    standby = bool(t.get("in_recovery"))

    # -- collection: twice, so the rates (tps, cache hit ratio) exist ----------------------------------------------
    r1 = await drv.collect()
    await asyncio.sleep(3)
    r2 = await drv.collect()
    want = ["is_db_alive", "active_connections", "max_connections", "connections_percent", "maximum_used_transactionIDs", "uptime_seconds",
            "longest_transaction_time_sec", "oldest_backend_xmin_age", "blocked_sessions", "database_size_bytes"]
    if not standby:
        want += ["tps", "xact_total", "cache_hit_ratio", "replication_slot_retained_bytes"]
    else:
        want += ["physical_replication_delay_in_seconds"]
    # sizes walk the file system and come with every fifth collection, the first one included
    missing = [k for k in want if k not in r2.metrics and not (k == "database_size_bytes" and k in r1.metrics)]
    dims = sorted({k.split("[")[0] for k in r2.metrics if "[" in k})
    record(name, "collect metrics", r1.ok and r2.ok and not missing,
           f"{len(r2.metrics)} values, missing {missing or 'none'}; dimensional: {', '.join(dims)}", {"metrics": r2.metrics, "info": r2.info, "error": r2.error})
    info = await drv._capabilities(await drv._collector())  # what the Databases page shows
    record(name, "capabilities", bool(info.get("capabilities", {}).get("pg_monitor")) and not info["capabilities"].get("superuser"),
           f"{info.get('capabilities')}", info)

    # -- read-only tools the AI and the live tabs use --------------------------------------------------------------
    tools = (("overview", drv.overview), ("sessions", drv.sessions), ("top queries", drv.top_queries), ("table health", lambda: drv.table_health(db)),
             ("wraparound", drv.wraparound), ("replication", drv.replication), ("settings", lambda: drv.settings(pattern="timeout")),
             ("databases", drv.databases))
    for label, call in tools:
        try:
            res = await call()
            rows = len(res.get("rows", [])) if isinstance(res, dict) else 0
            record(name, f"tool: {label}", True, f"{rows} rows" + (f"; note: {res.get('note')}" if isinstance(res, dict) and res.get("note") else ""), res)
        except Exception as exc:  # noqa: BLE001 - a DriverError is a clean refusal, anything else is a bug; both are failures here
            record(name, f"tool: {label}", False, f"{exc.__class__.__name__}: {exc}")
    for sql_, allowed in (("SELECT datname, numbackends FROM pg_stat_database ORDER BY 1", True), ("WITH x AS (SELECT 1) SELECT * FROM x", True),
                          ("UPDATE pgbench_accounts SET abalance = 0", False), ("SELECT pg_terminate_backend(1)", False),
                          ("SELECT 1; DROP TABLE pgbench_history", False)):
        try:
            await drv.run_sql(sql_, db)
            record(name, "run_sql " + ("allowed" if allowed else "refused"), allowed, sql_[:60])
        except DriverError as exc:
            record(name, "run_sql " + ("allowed" if allowed else "refused"), not allowed, f"{sql_[:40]} -> {str(exc)[:60]}")

    # -- assessment over the real facts ----------------------------------------------------------------------------
    facts = await drv.facts()
    facts.update(platform="vm", environment="production", host={"ram_gb": 16, "vcpus": 4, "storage": "ssd", "workload": "oltp"}, metrics=r2.metrics)
    findings = assessment.run_checks(facts)
    score, per = assessment.scores(findings)
    tune = assessment.tuning(facts)
    record(name, "assessment", bool(facts.get("settings")) and isinstance(score, int),
           f"score {score}, {sum(f['severity'] == 'issue' for f in findings)} issues, {sum(f['severity'] == 'suggestion' for f in findings)} suggestions, "
           f"{len(tune)} sizing changes; areas {per}", {"findings": findings, "tuning": tune})

    if standby:
        await drv.close()
        await admin.close()
        return

    # -- a real lock queue: forgotten transaction -> waiting ALTER TABLE -> writers queued behind it ----------------
    holder = await admin_conn({**adm, "user": "analyst", "password": cfg["lab_password"]}, db, autocommit=False)
    await holder.execute("SELECT count(*) FROM pgbench_accounts")  # AccessShareLock, then idle in transaction
    holder_pid = holder.pgconn.backend_pid
    migrator = await admin_conn({**adm, "user": "migrator", "password": cfg["lab_password"]}, db)
    alter = asyncio.create_task(migrator.execute("ALTER TABLE pgbench_accounts ADD COLUMN IF NOT EXISTS lab_note text"))
    writers = []
    for i in range(6):
        w = await admin_conn({**adm, "user": "app", "password": cfg["lab_password"]}, db)
        writers.append((w, asyncio.create_task(w.execute("UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE aid = %s", (i + 1,)))))

    async def chain() -> Any:
        lt = await drv.lock_tree()
        return lt if lt.get("roots") and len(lt.get("edges", [])) >= 6 else None
    lt = await wait_until(chain, 15)
    roots = (lt or {}).get("roots", [])
    record(name, "lock tree finds the root blocker", holder_pid in roots, f"roots {roots}, holder {holder_pid}, {len((lt or {}).get('edges', []))} waits", lt)
    m = (await drv.collect()).metrics
    record(name, "blocked_sessions metric", m.get("blocked_sessions", 0) >= 6, f"blocked_sessions={m.get('blocked_sessions')}, lock_wait_max_sec={m.get('lock_wait_max_sec')}")
    try:
        res = await drv.execute_action("terminate_backend", {"pid": holder_pid})  # pg_signal_backend is enough for non-superusers
        record(name, "action terminate_backend (monitor role)", res["ok"], res["message"])
    except DriverError as exc:
        record(name, "action terminate_backend (monitor role)", False, str(exc))
    done = await wait_until(lambda: asyncio.sleep(0, alter.done() and all(t.done() for _, t in writers)), 20)
    m = (await drv.collect()).metrics
    record(name, "the queue drains", bool(done) and m.get("blocked_sessions", 1) == 0, f"ALTER done={alter.done()}, blocked_sessions={m.get('blocked_sessions')}")
    for conn in [holder, migrator] + [w for w, _ in writers]:
        await conn.close()
    await admin_exec(adm, db, "ALTER TABLE pgbench_accounts DROP COLUMN IF EXISTS lab_note")

    # -- actions: least privilege refuses cleanly, a superuser succeeds ---------------------------------------------
    # the slot first: creating a logical slot waits for every open transaction, a prepared one included
    slot = facts["settings"].get("wal_level", {}).get("setting") == "logical"
    if slot and not any(s["name"] == "lab_abandoned" for s in facts.get("slots", [])):
        await admin_exec(adm, db, "SELECT pg_create_logical_replication_slot('lab_abandoned', 'pgoutput')")
    gid = f"raiden-lab-{name}-{int(time.time())}"
    prepared = facts["settings"].get("max_prepared_transactions", {}).get("setting", "0") != "0"
    if prepared:
        p = await admin_conn({**adm, "user": "app", "password": cfg["lab_password"]}, db, autocommit=False)
        await p.execute("INSERT INTO pgbench_history (tid, bid, aid, delta, mtime) VALUES (1, 1, 1, 0, now())")
        await p.execute(f"PREPARE TRANSACTION '{gid}'")
        await p.close()
        wr = await drv.wraparound()
        holders = [r for r in (wr.get("xmin_holders") or {}).get("rows", []) if r[0] == "prepared"]
        record(name, "wraparound sees the prepared xact", any(gid in str(r) for r in holders), f"{len(holders)} prepared holder(s)")
        # the forgotten prepared transaction holds a lock on pgbench_history: a concurrent index build there waits for it,
        # fails, and must not leave its INVALID index behind silently (2 s instead of the production 5 min)
        saved, postgres.MAINTENANCE_LOCK_TIMEOUT_MS = postgres.MAINTENANCE_LOCK_TIMEOUT_MS, 2000
        try:
            await admin.execute_action("create_index_concurrently", {"database": db, "table": "public.pgbench_history", "columns": ["delta"], "name": "lab_blocked_ix"})
            record(name, "index build behind an old xact", False, "succeeded although a prepared transaction holds the table")
        except DriverError as exc:
            record(name, "index build behind an old xact", "lock timeout" in str(exc) and "lab_blocked_ix" in str(exc), str(exc)[:110])
        finally:
            postgres.MAINTENANCE_LOCK_TIMEOUT_MS = saved
    actions = [
        ("cancel_backend", {"pid": 2147483000}),
        ("terminate_sessions", {"role": "app", "state": "idle", "older_than_s": 3600}),
        ("set_role_timeout", {"role": "migrator", "setting": "lock_timeout", "value": "3s"}),
        ("set_role_connection_limit", {"role": "app", "limit": 50}),
        ("analyze_table", {"database": db, "table": "public.pgbench_branches"}),
        ("vacuum_table", {"database": db, "table": "public.pgbench_branches"}),
        ("rollback_prepared", {"gid": gid, "database": db}),
        # the same build again: the leftover of the blocked attempt goes first, then the index is built for real
        ("create_index_concurrently", {"database": db, "table": "public.pgbench_history", "columns": ["delta"], "name": "lab_blocked_ix"}),
        ("create_index_concurrently", {"database": db, "table": "public.pgbench_history", "columns": ["aid"], "name": "lab_history_aid"}),
        ("reindex_index_concurrently", {"database": db, "index": "public.lab_history_aid"}),
        ("alter_system_set", {"name": "log_lock_waits", "value": "on"}),
        ("reload_config", {}),
        ("drop_replication_slot", {"slot": "lab_abandoned"}),
        ("execute_sql", {"sql": "DROP INDEX CONCURRENTLY IF EXISTS public.lab_history_aid", "database": db}),
    ]
    actions = [(a, x) for a, x in actions if (a != "rollback_prepared" or prepared) and (a != "drop_replication_slot" or slot)]
    for action, args in actions:
        outcome = {}
        for who, d in (("monitor", drv), ("admin", admin)):
            try:
                outcome[who] = (True, (await d.execute_action(action, args)).get("message", ""))
            except DriverError as exc:
                outcome[who] = (False, str(exc))
        mon_ok, mon_msg = outcome["monitor"]
        adm_ok, adm_msg = outcome["admin"]
        # the admin path must work (or say the object is gone, after the monitor already did it); the monitor path must
        # either work or fail with PostgreSQL's own permission message, never with a crash
        gone = any(w in adm_msg for w in ("not found", "no replication slot", "does not exist"))
        # without pg_maintain (before 17) the monitor's VACUUM or ANALYZE is skipped by PostgreSQL: it must not report success
        honest = not (action in ("analyze_table", "vacuum_table") and mon_ok and int(t.get("version_num") or 0) < 170000)
        record(name, f"action {action}", (adm_ok or (mon_ok and gone)) and honest,
               f"monitor: {'ok' if mon_ok else 'refused'} ({mon_msg[:45]}); admin: {'ok' if adm_ok else 'refused'} ({adm_msg[:45]})")
    _, invalid = await admin._query("SELECT indexrelid::regclass::text FROM pg_index WHERE NOT indisvalid", dbname=db)
    record(name, "no invalid index left behind", not invalid, f"invalid indexes: {[r[0] for r in invalid] or 'none'}")
    await admin_exec(adm, db, "DROP INDEX CONCURRENTLY IF EXISTS public.lab_blocked_ix")
    # the console's lock-queue incident runs on these servers next: give the roles their defaults back
    await admin_exec(adm, db, "ALTER ROLE migrator RESET lock_timeout")
    await admin_exec(adm, db, "ALTER ROLE app CONNECTION LIMIT -1")
    await drv.close()
    await admin.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--servers", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args()
    servers = json.loads(Path(args.servers).read_text())
    for cfg in servers:
        try:
            await check_server(cfg)
        except Exception as exc:  # noqa: BLE001 - one broken server must not hide the others
            record(cfg["name"], "unexpected error", False, f"{exc.__class__.__name__}: {exc}", traceback.format_exc())
    Path(args.report).write_text(json.dumps(RESULTS, default=str, indent=1))
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed on {len(servers)} servers", flush=True)
    return len(failed)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
