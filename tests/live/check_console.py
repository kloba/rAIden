"""Live end-to-end check of the console against real PostgreSQL servers and a real model.

Runs on the lab VM next to a console started there (deploy/vm/console-lab.sh): it signs up the first admin, adds the
AI endpoint and every lab server through the API exactly as a person would in the UI, checks onboarding, collection,
the live views, a routine and a chat, then builds a real lock queue and follows the incident through every stage,
approving the gates a person approves in production.

    python tests/live/check_console.py --console http://127.0.0.1:8080 --servers /etc/raiden-lab/servers.json \
        --endpoint https://<account>.openai.azure.com/ --model gpt-6.1-sol --report /var/lib/raiden-lab/console.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

RESULTS: list[dict[str, Any]] = []
H = {"X-Requested-With": "raiden"}


def record(check: str, ok: bool, detail: str = "", data: Any = None) -> bool:
    RESULTS.append({"check": check, "ok": ok, "detail": detail, "data": data})
    print(f"{'PASS' if ok else 'FAIL'} {check:<40} {detail[:120]}", flush=True)
    return ok


def wait_for(fn: Any, timeout: float, every: float = 3.0) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = fn()
        if got:
            return got
        time.sleep(every)
    return None


async def lock_queue(admin: dict[str, Any], db: str, password: str, hold_s: float) -> dict[str, Any]:
    """A forgotten transaction, a migration's ALTER TABLE waiting behind it and writers queued behind the ALTER."""
    import psycopg

    async def conn(user: str, autocommit: bool = True) -> Any:
        return await psycopg.AsyncConnection.connect(**{**admin, "user": user, "password": password, "dbname": db}, autocommit=autocommit)
    holder = await conn("analyst", autocommit=False)
    await holder.execute("SELECT count(*) FROM pgbench_accounts")
    migrator = await conn("migrator")
    alter = asyncio.create_task(migrator.execute("ALTER TABLE pgbench_accounts ADD COLUMN IF NOT EXISTS lab_note text"))
    writers = []
    for i in range(8):
        w = await conn("app")
        writers.append((w, asyncio.create_task(w.execute("UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE aid = %s", (i + 1,)))))
    started = time.monotonic()
    # keep the queue until rAIden ends the root blocker (or the time is up)
    while time.monotonic() - started < hold_s and not alter.done():
        await asyncio.sleep(2)
    out = {"holder_pid": holder.pgconn.backend_pid, "alter_done": alter.done(), "waited_s": round(time.monotonic() - started)}
    if alter.done():  # cancelled by rAIden, or completed after it ended the blocker
        exc = alter.exception()
        out["alter"] = f"{exc.__class__.__name__}: {exc}" if exc else "completed"
    out["writers_done"] = sum(t.done() for _, t in writers)
    for _, t in writers:
        if t.done():
            t.exception()
    for c in [holder, migrator] + [w for w, _ in writers]:
        try:
            await c.close()
        except Exception:  # noqa: BLE001 - the session may already be gone, which is the point
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--console", default="http://127.0.0.1:8080")
    ap.add_argument("--servers", required=True)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--incident-on", default="pg17")
    args = ap.parse_args()
    servers = json.loads(Path(args.servers).read_text())
    c = httpx.Client(base_url=args.console + "/api/v1", timeout=60, headers=H)

    # -- first run: the setup page creates the admin ---------------------------------------------------------------
    st = c.get("/auth/status").json()
    if st["needs_setup"]:
        r = c.post("/auth/setup", json={"email": "lab@example.com", "password": "lab console password 2026", "name": "Lab Admin"})
    else:
        r = c.post("/auth/login", json={"email": "lab@example.com", "password": "lab console password 2026"})
    record("sign in", r.status_code == 200, f"{r.status_code} demo={st['demo']}")

    # -- the model rAIden thinks with ---------------------------------------------------------------------------
    ep = c.post("/ai/endpoints", json={"name": f"Azure OpenAI · {args.model}", "provider": "azure_openai", "base_url": args.endpoint, "model": args.model,
                                        "api_version": "2025-04-01-preview", "auth": "entra", "reasoning_effort": "medium",
                                        "purposes": ["incidents", "chat", "routines", "hardening"], "priority": 1}).json()
    t = c.post(f"/ai/endpoints/{ep['id']}/test").json()
    record("AI endpoint test", bool(t.get("ok")), f"{t.get('message', '')[:80]} in {t.get('latency_ms')} ms", t)
    c.put("/autonomy", json={"verify_window_s": 60})

    # -- onboarding: test, add, assess every server ------------------------------------------------------------
    for s in servers:
        mon = s["monitor"]
        conn = {"host": mon["host"], "port": mon["port"], "dbname": mon["dbname"], "username": mon["user"], "password": mon["password"], "sslmode": mon.get("sslmode", "prefer")}
        test = c.post("/servers/test", json=conn).json()
        record(f"{s['name']}: connection test", bool(test.get("ok")), "; ".join(f"{x['name']}={'ok' if x['ok'] else 'no'}" for x in test.get("checks", [])), test)
        srv = c.post("/servers", json={**conn, "id": s["name"], "name": s["name"], "environment": "production", "platform": "vm",
                                       "collect_interval_s": 15, "host_info": {"ram_gb": 16, "vcpus": 4, "storage": "ssd", "workload": "oltp"}})
        if srv.status_code != 200:
            record(f"{s['name']}: add server", False, srv.text[:120])
            continue
        aid = c.post(f"/servers/{s['name']}/assess?trigger=onboarding").json()["id"]
        a = wait_for(lambda: (x := c.get(f"/assessments/{aid}").json())["status"] != "running" and x, 120, 2)
        record(f"{s['name']}: assessment", bool(a) and a["status"] == "done",
               f"score {a and a.get('score')}, {a and a.get('counts')}, summary {len((a or {}).get('summary') or '')} chars", a)

    # -- collection and the live views -------------------------------------------------------------------------
    time.sleep(40)  # two collections at 15 s
    estate = {e["id"]: e for e in c.get("/estate").json()}
    for s in servers:
        e = estate.get(s["name"], {})
        record(f"{s['name']}: collecting", e.get("status") in ("healthy", "warning") and bool(e.get("metrics")),
               f"status {e.get('status')} {e.get('status_reason', '')[:50]}; {len(e.get('metrics') or {})} metrics; version {e.get('version')}")
        for kind in ("sessions", "locks", "queries", "tables", "replication", "settings", "databases", "wraparound"):
            r = c.get(f"/servers/{s['name']}/live/{kind}")
            if r.status_code != 200:
                record(f"{s['name']}: live {kind}", False, r.text[:100])
    hist = c.get(f"/servers/{args.incident_on}/metrics", params={"keys": "tps,active_connections,cache_hit_ratio,sessions_by_state[*]", "range": "1h"}).json()
    series = hist.get("series") or {}  # {key: [[ts, value], ...]}
    record("metric history", len(series) >= 3 and all(series.values()), f"{len(series)} series: {[(k, len(v)) for k, v in series.items()][:6]}")

    # -- a routine and a question --------------------------------------------------------------------------------
    patrol = next((r for r in c.get("/routines").json() if r.get("key") == "health-patrol"), None)
    if patrol:
        run_id = c.post(f"/routines/{patrol['id']}/run").json()["run_id"]
        done = wait_for(lambda: (x := c.get(f"/runs/{run_id}").json())["status"] not in ("running", "queued") and x, 180, 3)
        record("routine: health patrol", bool(done), f"{(done or {}).get('status')}: {(done or {}).get('summary')}")
    conv = c.post("/chat/conversations", json={"server_id": args.incident_on, "title": ""}).json()
    with c.stream("POST", f"/chat/conversations/{conv['id']}/messages", json={"content": "Give me a short health summary of this server: connections, longest transaction, replication."}) as resp:
        body = "".join(resp.iter_text())
    msgs = c.get(f"/chat/conversations/{conv['id']}").json().get("messages", [])
    answer = next((x for x in reversed(msgs) if x["role"] == "assistant" and x.get("content")), None)
    record("chat answers from live data", bool(answer) and "event: error" not in body, (answer or {}).get("content", "")[:110].replace("\n", " "))

    # -- an incident end to end: detection, AI diagnosis, gates, fix, verification, protections, report ----------
    target = next(s for s in servers if s["name"] == args.incident_on)
    loop = asyncio.new_event_loop()
    queue = loop.create_task(lock_queue(target["admin"], target["monitor"]["dbname"], target["lab_password"], hold_s=900))
    import threading
    th = threading.Thread(target=loop.run_until_complete, args=(queue,), daemon=True)
    th.start()

    def remediate_gate() -> Any:
        for inc in c.get("/incidents?status=open").json():
            if inc["server_id"] == args.incident_on:
                for a in inc["pending_approvals"]:
                    if a["stage"] == "remediate":
                        return inc, a
        return None
    t0 = time.monotonic()
    got = wait_for(remediate_gate, 600, 5)
    if not got:
        opened = [i for i in c.get("/incidents?status=open").json() if i["server_id"] == args.incident_on]
        record("incident reaches the remediation gate", False, f"open incidents: {[(i['id'], i['stage'], i['state'], i['waiting_reason']) for i in opened]}")
    else:
        inc, appr = got
        plan = appr["payload"].get("plan") or []
        record("incident: detected, triaged, diagnosed", True,
               f"{inc['id']} '{inc['title']}' in {round(time.monotonic() - t0)} s; plan: {[p.get('action') for p in plan]}", inc)
        # from here on, approve what the incident asks a person for (the stage gates, an action outside the plan), as the
        # person on call would after reading it; every approval is in the report
        approved: list[str] = []

        def approve_pending() -> Any:
            d = c.get(f"/incidents/{inc['id']}").json()
            for a in d.get("pending_approvals", []):
                c.post(f"/approvals/{a['id']}/decide", json={"approve": True, "comment": "lab: approved"})
                approved.append(f"{a['stage']}: {a['title']}")
            return d if d["stage"] == "resolved" else None
        final = wait_for(approve_pending, 900, 5)
        detail = final or c.get(f"/incidents/{inc['id']}").json()
        kinds = [e["kind"] for e in detail["events"]]
        actions = [e["title"] for e in detail["events"] if e["kind"] == "action"]
        record("incident: fix carried out and verified", bool(actions) and "verification" in kinds,
               f"actions: {[a[:50] for a in actions]}; root cause: {(detail.get('root_cause') or '')[:70]}")
        items = detail.get("hardening") or []
        # nothing that did not happen may be reported as applied (the first live run had two such "routines")
        honest = all(h.get("status") in ("applied", "suggested", "failed", "skipped", "rejected") for h in items) and \
            not any(h.get("status") == "applied" and "no routine template" in (h.get("result") or "") for h in items)
        record("incident: protections added", "hardened" in kinds and bool(items) and honest, f"approved: {approved}; protections: "
               f"{[(h.get('kind'), h.get('title', '')[:40], h.get('status')) for h in items]}")
        record("incident: resolved with a report", bool(final) and bool(detail.get("report")),
               f"{detail.get('tokens')} tokens, {len(detail.get('events') or [])} events, report {len(detail.get('report') or '')} chars, "
               f"stages {list((detail.get('stage_times') or {}).keys())}", detail)
    th.join(timeout=5)
    record("the lock queue is gone", queue.done() and bool(queue.result().get("alter_done")) if queue.done() else False,
           json.dumps(queue.result()) if queue.done() else "queue still held")
    blocked = c.get(f"/servers/{args.incident_on}").json()["metrics"].get("blocked_sessions")
    record("blocked sessions back to 0", blocked == 0, f"blocked_sessions={blocked}")

    Path(args.report).write_text(json.dumps(RESULTS, default=str, indent=1))
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} console checks passed", flush=True)
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
