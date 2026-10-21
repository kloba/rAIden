"""Seed the demo workspace: seven servers on six platforms, 30 days of metrics, the incident history that matches them,
AI endpoints with their usage, routine runs, assessments, an audit trail, a few past chat investigations, and what rAIden
learned over the month (taught instructions, lessons, playbooks, ten nights of dreams and a proposal not to ask again).

Everything here is simulated and labelled so in the UI (servers and endpoints carry `simulated`)."""

from __future__ import annotations

import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from .. import assessment, fmt
from ..cron import Cron
from ..db import iso, utcnow
from ..models import (AIEndpoint, Approval, Assessment, AuditEntry, ChatMessage, Conversation, Incident, IncidentEvent, MetricRollup, Routine, RoutineRun,
                      Server, TokenUsage, User)
from ..security import hash_password
from ..timeseries import ROLLUP_S
from .driver import DemoDriver
from .fleet import HISTORY
from .profiles import PROFILES

DEMO_USERS = [("alex@contoso.com", "Alex Morgan", "admin"), ("sam@contoso.com", "Sam Lee", "operator"), ("jordan@contoso.com", "Jordan Diaz", "viewer")]
DEMO_PASSWORD = "raiden-demo"

ENDPOINTS = [
    dict(name="Azure OpenAI · gpt-6.1-sol", provider="azure_openai", base_url="https://raiden-foundry.openai.azure.com/", model="gpt-6.1-sol",
         api_version="2025-04-01-preview", wire_api="auto", auth="entra", reasoning_effort="medium", purposes=["incidents", "hardening"], priority=1,
         price_in_per_m=1.25, price_out_per_m=10.0, price_cached_per_m=0.125, daily_token_budget=6_000_000, monthly_cost_budget=400.0),
    dict(name="Anthropic · claude-sonnet-5", provider="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-5", auth="api_key",
         reasoning_effort="medium", purposes=["chat", "incidents"], priority=2, price_in_per_m=3.0, price_out_per_m=15.0, price_cached_per_m=0.3,
         monthly_cost_budget=150.0),
    dict(name="On-prem GPU · qwen3-coder (Ollama)", provider="openai_compatible", base_url="http://gpu-01.fra1.internal:11434/v1", model="qwen3-coder:30b",
         auth="none", reasoning_effort="none", purposes=["routines"], priority=1, price_in_per_m=0.0, price_out_per_m=0.0),
]

# What each kind of past incident looked like (titles use the real metric names of the monitors that fire)
PAST: dict[str, dict[str, Any]] = {
    "lock_queue": dict(title="Lock queue: {n} sessions blocked", n=(38, 64), severity="sev2", category="locks", monitor="Lock queue", metric="blocked_sessions",
                       root="A forgotten transaction (role analyst, idle in transaction for {m} min) held AccessShareLock on the table a migration wanted to ALTER; every writer queued behind the ALTER TABLE.",
                       plan=[("terminate_backend", {"pid": "{pid}"}, "End the root blocker; its transaction only read data.")],
                       hard=[("action", "lock_timeout = 3s for the migrator role"), ("monitor", "Transaction open > 15 min")],
                       tools=["Read the current metrics", "Checked who blocks whom", "Listed sessions", "Recalled earlier incidents"]),
    "connection_storm": dict(title="Connections at {n}% of max_connections", n=(91, 97), severity="sev2", category="connections", monitor="Connections near the limit",
                             metric="connections_percent", root="Role reporting (Metabase dashboard refresh) opened one connection per tile and never closed them.",
                             plan=[("terminate_sessions", {"role": "reporting", "state": "idle", "older_than_s": 60}, "Free the slots."),
                                   ("set_role_connection_limit", {"role": "reporting", "limit": 20}, "Stop the reconnect storm.")],
                             hard=[("monitor", "Connections above 75% for 5 min"), ("lesson", "Metabase opens a connection per tile")],
                             tools=["Read the current metrics", "Listed sessions"]),
    "replication_lag": dict(title="Replica lag {n} min", n=(3, 6), severity="sev2", category="replication", monitor="Replica lag", metric="physical_replication_delay_in_seconds",
                            root="A month-end finance report on the standby conflicted with replay; max_standby_streaming_delay = -1 let replay wait indefinitely.",
                            plan=[("cancel_backend", {"pid": "{pid}"}, "Cancel the report so replay continues.")],
                            hard=[("action", "max_standby_streaming_delay = 30s"), ("action", "statement_timeout = 15min for finance_report")],
                            tools=["Replication status", "Listed sessions", "Read settings"]),
    "deadlocks": dict(title="{n} deadlocks per minute", n=(6, 11), severity="sev3", category="locks", monitor="Deadlocks", metric="deadlocks",
                      root="Two order workers update orders and order_lines in opposite order since release 2026.10.2.",
                      plan=[], hard=[("lesson", "Order workers must lock orders before order_lines")], tools=["Read the current metrics", "Top queries by calls"], escalated=True),
    "idle_in_tx": dict(title="{n} sessions idle in transaction", n=(24, 31), severity="sev3", category="activity", monitor="Idle in transaction", metric="idle_in_transaction",
                       root="crm_sync waits on the Salesforce API inside an open transaction.",
                       plan=[("terminate_sessions", {"role": "crm_sync", "state": "idle in transaction", "older_than_s": 300}, "End stale transactions.")],
                       hard=[("action", "idle_in_transaction_session_timeout = 5min for crm_sync")], tools=["Listed sessions", "Read the current metrics"]),
    "bloat": dict(title="Dead rows at {n}% of user tables", n=(36, 41), severity="sev4", category="vacuum", monitor="Dead rows piling up", metric="bloat_percent",
                  root="Autovacuum on inventory.stock_movements was throttled by the default cost limit while the sync rewrote 30% of the rows.",
                  plan=[("vacuum_table", {"database": "inventory", "table": "inventory.stock_movements", "freeze": False}, "Catch up now.")],
                  hard=[("action", "autovacuum_vacuum_cost_limit = 2000 (parameter group)")], tools=["Table health", "Read settings"]),
    "archive_failure": dict(title="WAL archiving is failing", n=(1, 1), severity="sev2", category="backup", monitor="WAL archiving fails", metric="archive_failed",
                            root="The backup repository's NFS mount was remounted read-only after a storage failover; archive_command failed with EROFS.",
                            plan=[], hard=[("monitor", "Last WAL archived > 15 min")], tools=["Read the current metrics", "Read settings"], escalated=True),
    "crash_storm": dict(title="ledger-dev is not accepting connections", n=(0, 0), severity="sev1", category="availability", monitor="Database not reachable", metric="is_db_alive",
                        root="pg_crash was loaded from conf.d/90-experiment.conf and sent signals to random backends every 4 s.",
                        plan=[("alter_system_set", {"name": "shared_preload_libraries", "value": "pg_stat_statements"}, "Pin the preload list.")],
                        hard=[("routine", "Configuration drift every hour"), ("lesson", "Check pg_file_settings for experiments after a crash loop")],
                        tools=["Read the current metrics", "Read settings", "Server overview"]),
    "checkpoint_storm": dict(title="Checkpoints are forced by WAL volume", n=(3, 3), severity="sev4", category="configuration", monitor="Checkpoints forced by WAL volume",
                             metric="checkpoints_requested", root="The nightly bulk load writes 40 GB of WAL; max_wal_size = 4GB forces a checkpoint every 50 s.",
                             plan=[], hard=[("lesson", "Raise max_wal_size to 16GB in the Azure portal")], tools=["Read settings"], escalated=False, suggestion=True),
    "temp_spill": dict(title="{n} GiB of temporary files per minute", n=(7, 9), severity="sev3", category="performance", monitor="Queries spilling to disk", metric="temp_bytes",
                       root="The weekly pipeline report sorts 30M rows with work_mem = 8MB.",
                       plan=[("set_role_timeout", {"role": "crm_app", "setting": "statement_timeout", "value": "5min"}, "Bound the runaway report.")],
                       hard=[("lesson", "Give the reporting role work_mem = 256MB instead of raising it globally")], tools=["Top queries by temp", "Read settings"]),
    "cpu_spike": dict(title="CPU at {n}% for 3 minutes", n=(92, 96), severity="sev2", category="cpu", monitor="CPU saturated", metric="cpu_percent",
                      root="A new reconciliation query filtered payments.transactions on settled_at without an index.",
                      plan=[("create_index_concurrently", {"database": "payments", "table": "payments.transactions", "columns": ["settled_at"]}, "Index the new filter.")],
                      hard=[("routine", "Weekly index advisor"), ("monitor", "Queries running longer than 5 s")], tools=["Top queries by total time", "Ran a read-only query", "Table health"]),
    "long_query": dict(title="A transaction has been open for {n} min", n=(31, 44), severity="sev3", category="activity", monitor="Long-running transaction",
                       metric="longest_transaction_time_sec", root="The nightly mart rebuild ran inside one transaction after a dbt config change.",
                       plan=[], hard=[("lesson", "dbt run with --threads 4 keeps transactions short")], tools=["Listed sessions"], escalated=False),
    "config_bomb": dict(title="3 settings change at the next restart", n=(3, 3), severity="sev3", category="configuration", monitor="Settings waiting for a restart",
                        metric="pending_restart_settings", root="A chaos experiment re-added pg_crash to shared_preload_libraries in conf.d; the reload logged it as pending.",
                        plan=[("alter_system_set", {"name": "shared_preload_libraries", "value": "pg_stat_statements"}, "Pin the preload list before any restart.")],
                        hard=[("monitor", "Pending restart on ledger-dev pages sev2")], tools=["Read settings", "Server overview"]),
    "replication_slot": dict(title="Replication slots retain {n} GiB of WAL", n=(14, 21), severity="sev2", category="replication", monitor="Replication slot retains WAL",
                             metric="replication_slot_retained_bytes", root="A Datastream slot kept WAL after the stream was paused for a schema change.",
                             plan=[], hard=[("monitor", "Inactive replication slot")], tools=["Replication status"], escalated=True),
}

CHATS = [
    ("Why did TPS drop on orders-prod yesterday at 14:10?", "orders-prod",
     "TPS fell from **1,380/s to 210/s at 14:09** and recovered at 14:17. The lock graph from INC-{lq} explains it: an analyst's session sat **idle in transaction "
     "for 31 min** and a migration's `ALTER TABLE orders.orders` queued behind it, taking every writer with it. rAIden ended the analyst's session after Sam approved "
     "the plan; the migrator role now has `lock_timeout = 3s`."),
    ("Which servers are closest to their storage limit?", None,
     "Three servers are above 60%:\n\n- **inventory-prod** 69% (gp3, 2 TB) - grows ~0.2%/day\n- **orders-prod** 61%\n- **ledger-dev** 52%\n\n"
     "At the current rate inventory-prod reaches 85% in about 80 days. Nothing needs action this week."),
    ("Is anything holding back vacuum on inventory-prod?", "inventory-prod",
     "Yes. An **orphaned prepared transaction** (`erp-sync-7f3a9c`, owner erp_sync) has been prepared for **9 days**. It pins the xmin horizon: three "
     "autovacuum workers are busy on `inventory.stock_movements` but cannot freeze anything, and transaction ID age is climbing toward one billion."),
]


def _sig(v: float) -> float:
    """Four significant digits: the long view does not need more, and the rollups stay small."""
    if v == 0 or v != v:
        return 0.0
    from math import floor, log10

    return round(v, max(0, 3 - int(floor(log10(abs(v))))))


async def seed_demo(rt: Any) -> None:
    with rt.db.session() as s:
        if s.execute(select(func.count(Server.id))).scalar():
            return
    t0 = time.time()
    now = utcnow()
    rng = random.Random(42)
    fleet = rt.fleet
    with rt.db.session() as s:
        for email, name, role in DEMO_USERS:
            s.add(User(email=email, name=name, role=role, password_hash=hash_password(DEMO_PASSWORD), created_at=now - timedelta(days=40)))
        for i, p in enumerate(PROFILES):
            s.add(Server(
                id=p.id, name=p.id, environment=p.environment, platform=p.platform, host=p.host, port=5432, dbname=p.databases[0], username="raiden_monitor",
                sslmode="require", databases=[d for d in p.databases if d != "postgres"], azure_resource_id=p.azure_resource_id, tags=p.tags, owner=p.owner,
                description=p.description, maintenance_window=p.maintenance_window, collect_interval_s=15, enabled=True, color_slot=i, status="unknown",
                # one server pins its own gate: the sales team lets rAIden fix crm-prod without asking (protections still wait)
                autonomy={"remediate": "auto"} if p.id == "crm-prod" else {},
                info={"simulated": True, "version": p.version, "version_num": p.version_num, "region": p.region, "sku": p.sku, "sources": list(p.sources),
                      "in_recovery": bool(p.replica_of), "replica_of": p.replica_of, "max_connections": p.max_connections,
                      "host": {"ram_gb": p.mem_gb, "vcpus": p.cores, "storage": "nvme" if p.platform == "self_managed" else "ssd",
                               "workload": "olap" if p.id == "analytics-staging" else "oltp"},
                      "host_agent": "host" in p.sources and p.platform in ("self_managed", "vm")},
                created_at=now - timedelta(days=35 - i), agent_last_seen_at=now if "host" in p.sources else None))
        for ep in ENDPOINTS:
            s.add(AIEndpoint(**ep, enabled=True, simulated=True, last_test_at=now - timedelta(hours=3), last_test_ok=True, last_test_message="ready (simulated)",
                             last_latency_ms=rng.randint(700, 2400), created_at=now - timedelta(days=35)))
    # ---------------------------------------------------------------------------------------- metrics
    for p in PROFILES:
        end = int(t0) - int(t0) % 60
        raw = [(ts, fleet.metrics(p.id, ts, live=False)) for ts in range(end - 86400, end, 60)]
        rt.store.append_many(p.id, raw)
        rollups = []
        start = end - 30 * 86400
        start -= start % ROLLUP_S
        keep = ("sessions_by_state[", "sessions_by_wait_event_type[", "tps[", "database_size_bytes[")
        for b in range(start, end - 86400, ROLLUP_S):
            sample = fleet.metrics(p.id, b + 420, live=False)  # one sample per 15 minutes: [avg] stands for avg/max/min
            rollups.append(MetricRollup(server_id=p.id, ts=b, data={k: [_sig(v)] for k, v in sample.items() if "[" not in k or k.startswith(keep)}))
        with rt.db.session() as s:
            s.add_all(rollups)
    rt.store.rollup(int(t0) - 86400 + 3600)  # the raw day gets its rollups too
    # ---------------------------------------------------------------------------------------- incidents
    with rt.db.session() as s:
        eps = {e.name: e for e in s.execute(select(AIEndpoint)).scalars()}
        primary = eps[ENDPOINTS[0]["name"]]
        number = 1000
        for sid, kind, hours_ago, minutes in sorted(HISTORY, key=lambda h: -h[2]):
            number += 1
            _past_incident(s, rt, rng, number, sid, kind, datetime.fromtimestamp(fleet.start - hours_ago * 3600, timezone.utc).replace(tzinfo=None), minutes, primary)
    # ---------------------------------------------------------------------------------------- usage, routines, assessments, audit, chat
    _usage(rt, rng, now)
    _routine_runs(rt, rng, now)
    for p in PROFILES:
        facts = DemoDriver(p.id, fleet).facts_sync()
        facts.update(platform=p.platform, environment=p.environment, metrics=fleet.metrics(p.id, t0, live=False),
                     host={"ram_gb": p.mem_gb, "vcpus": p.cores, "storage": "nvme" if p.platform == "self_managed" else "ssd",
                           "workload": "olap" if p.id == "analytics-staging" else "oltp"})
        findings = assessment.run_checks(facts)
        score, per = assessment.scores(findings)
        with rt.db.session() as s:
            created = now - timedelta(days=35 - PROFILES.index(p), hours=-1)
            s.add(Assessment(server_id=p.id, created_at=created, finished_at=created + timedelta(seconds=41), status="done", trigger="onboarding",
                             triggered_by="alex@contoso.com", score=score, scores=per, findings=findings, tuning=assessment.tuning(facts),
                             facts={k: facts.get(k) for k in ("version", "version_num", "in_recovery", "extensions", "superusers", "databases", "replicas", "host", "platform")},
                             summary=assessment.demo_summary(assessment.summary_prompt(p.id, findings, score)), tokens_in=9800, tokens_out=420))
            from ..services import upsert_suggestion

            for f in findings:
                if f["severity"] in ("issue", "suggestion") and not f["id"].startswith("tune-"):
                    upsert_suggestion(s, p.id, kind=f["severity"], category=f["area"], title=f["title"], detail=f"{f['detail']}\n\n**Recommendation:** {f['recommendation']}",
                                      source="assessment", fingerprint=f"assess:{f['id']}", data={"finding": f})
            s.add(AuditEntry(ts=created - timedelta(minutes=3), actor="alex@contoso.com", action="server.create", target=p.id, decision="allow",
                             detail={"platform": p.platform, "environment": p.environment}))
    with rt.db.session() as s:
        s.add(AuditEntry(ts=now - timedelta(days=30), actor="alex@contoso.com", action="autonomy.update", target="production", decision="allow",
                         detail={"remediate": "human", "harden": "human"}))
        s.add(AuditEntry(ts=now - timedelta(days=12), actor="sam@contoso.com", action="ai_endpoint.update", target=ENDPOINTS[2]["name"], decision="allow",
                         detail={"purposes": ["routines"]}))
        lq = s.execute(select(Incident.id).where(Incident.server_id == "orders-prod", Incident.category == "locks").order_by(Incident.number.desc())).scalars().first()
        for i, (q, sid, a) in enumerate(CHATS):
            cid = uuid.uuid4().hex
            at = now - timedelta(days=i + 1, hours=rng.randint(1, 6))
            s.add(Conversation(id=cid, title=q, server_id=sid, user_email="alex@contoso.com", created_at=at, updated_at=at + timedelta(minutes=2)))
            s.flush()
            s.add(ChatMessage(conversation_id=cid, role="user", content=q, data={"user": "alex@contoso.com"}, created_at=at))
            s.add(ChatMessage(conversation_id=cid, role="tool", content="", data={"name": "metric_history" if i != 1 else "fleet_metric",
                                                                                   "args": {"metric": "tps" if i == 0 else "storage_percent"}, "ok": True, "ms": 41,
                                                                                   "preview": {"note": "simulated history"}}, created_at=at + timedelta(seconds=4)))
            s.add(ChatMessage(conversation_id=cid, role="assistant", content=a.replace("{lq}", (lq or "INC-1009").split("-")[1]),
                              data={"model": "claude-sonnet-5", "simulated": True, "tokens_in": 14200, "tokens_out": 380}, created_at=at + timedelta(seconds=11)))
    _knowledge(rt, now)


def _past_incident(s: Any, rt: Any, rng: random.Random, number: int, sid: str, kind: str, begin: datetime, minutes: float, ep: AIEndpoint) -> None:
    t = PAST[kind]
    srv = s.get(Server, sid)
    n = rng.randint(*t["n"]) if t["n"][1] else 0
    pid = rng.randint(30000, 60000)
    detected = begin + timedelta(seconds=rng.randint(50, 110))
    prod = srv.environment == "production"
    approver = rng.choice(["alex@contoso.com", "sam@contoso.com"])
    title = t["title"].format(n=n)
    plan = []
    for i, (action, args, why) in enumerate(t["plan"]):
        a = {k: (int(v.format(pid=pid)) if isinstance(v, str) and "{pid}" in v else v) for k, v in args.items()}
        from ..drivers.base import ACTIONS

        plan.append({"id": i + 1, "action": action, "args": a, "rationale": why, "risk": ACTIONS[action]["risk"], "title": ACTIONS[action]["title"], "status": "done",
                     "approved_by": approver if prod else "autopilot", "result": "done"})
    escalated = t.get("escalated", False)
    tokens_in, tokens_out = rng.randint(70_000, 190_000), rng.randint(5_000, 14_000)
    cached = int(tokens_in * 0.55)
    cost = round(((tokens_in - cached) * ep.price_in_per_m + cached * ep.price_cached_per_m + tokens_out * ep.price_out_per_m) / 1e6, 4)
    inc = Incident(id=f"INC-{number}", number=number, server_id=sid, title=title, severity=t["severity"], category=t["category"], source="monitor",
                   source_ref=t["monitor"], fingerprint=f"mon:hist:{sid}:{number}", stage="resolved", state="done", assignee="raiden",
                   root_cause=t["root"].format(m=rng.randint(24, 40)), plan=plan, scenario=kind,
                   evidence={"monitor": t["monitor"], "metric": t["metric"], "value": n}, tokens_in=tokens_in, tokens_out=tokens_out, tokens_cached=cached,
                   cost_usd=cost, tool_calls=len(t["tools"]) + len(plan), model=ep.model, detected_at=detected)
    s.add(inc)
    s.flush()
    ev: list[tuple[float, str, str, str, str, str, dict[str, Any]]] = []  # (seconds after detection, kind, actor, stage, title, body, data)
    ev.append((0, "detected", "system", "detected", f"Detected by monitor {t['monitor']}", "", {"metric": t["metric"], "value": n}))
    ev.append((2, "autopilot", "autopilot", "triage", "Autopilot: rAIden starts looking as soon as something is detected.", "", {"gate": "triage"}))
    ev.append((3, "stage", "autopilot", "triage", "Detected → Triage", "", {}))
    clock = 5.0
    for tool in t["tools"][:2]:
        clock += rng.uniform(3, 9)
        ev.append((clock, "tool", "raiden", "triage", tool, "", {"tool": tool, "ok": True, "ms": rng.randint(20, 400)}))
    clock += rng.uniform(8, 20)
    ev.append((clock, "triage", "raiden", "triage", f"Triage: {t['severity'].upper()} · {fmt.category(t['category'])}", title + ".", {}))
    ev.append((clock + 1, "stage", "autopilot", "diagnose", "Triage → Diagnose", "", {}))
    for tool in t["tools"][2:]:
        clock += rng.uniform(5, 15)
        ev.append((clock, "tool", "raiden", "diagnose", tool, "", {"tool": tool, "ok": True, "ms": rng.randint(20, 900)}))
    clock += rng.uniform(15, 40)
    ev.append((clock, "diagnosis", "raiden", "diagnose", "Root cause found", inc.root_cause, {"plan": plan}))
    if escalated or not plan:
        clock += 2
        inc_reason = "No safe action fits this root cause: a person has to decide." if not plan else "Escalated"
        ev.append((clock, "escalated", "raiden", "diagnose", "Escalated to a person", inc_reason, {}))
        clock += rng.uniform(600, 2400)
        ev.append((clock, "takeover", approver, "diagnose", f"{approver} took over the incident", "", {}))
        clock += rng.uniform(300, 1200)
        ev.append((clock, "stage", approver, "resolved", "Diagnose → Resolved", "Fixed by the application team.", {}))
        inc.assignee = approver
    else:
        if prod:
            ev.append((clock + 1, "approval", "system", "diagnose", "Waiting for a person: approve the remediation plan", "", {"gate": "remediate"}))
            clock += rng.uniform(60, 420)
            ev.append((clock, "decision", approver, "diagnose", "Approved the remediation plan", "", {"approved": True}))
        else:
            ev.append((clock + 1, "autopilot", "autopilot", "remediate", "Autopilot: rAIden carries out its plan on its own.", "", {"gate": "remediate"}))
        ev.append((clock + 2, "stage", approver if prod else "autopilot", "remediate", "Diagnose → Remediate", "", {}))
        for p in plan:
            clock += rng.uniform(4, 30)
            ev.append((clock, "action", "raiden", "remediate", f"{p['title']}: " + ", ".join(f"{k}={v}" for k, v in p["args"].items()), "done",
                       {"action": p["action"], "args": p["args"], "ok": True, "approved_by": p["approved_by"], "risk": p["risk"]}))
        clock += rng.uniform(10, 25)
        ev.append((clock, "remediation", "raiden", "remediate", "Remediation done", "", {}))
        ev.append((clock + 1, "stage", "raiden", "verify", "Remediate → Verify", "", {}))
        inc.mitigated_at = detected + timedelta(seconds=clock)
        clock += 180
        ev.append((clock, "verification", "raiden", "verify", "Verified: the fix holds", f"{t['metric']} back to normal.", {"passed": True}))
        ev.append((clock + 1, "stage", "autopilot", "harden", "Verify → Harden", "", {}))
        items = [{"id": i + 1, "kind": k, "title": ti, "status": "applied", "approved_by": approver if prod else "autopilot", "result": "done"}
                 for i, (k, ti) in enumerate(t["hard"])]
        inc.hardening = items
        clock += rng.uniform(20, 50)
        ev.append((clock, "hardening", "raiden", "harden", f"Proposed {fmt.plural(len(items), 'protection')}", "\n".join(f"- **{h['kind']}**: {h['title']}" for h in items), {"items": items}))
        if prod:
            clock += rng.uniform(120, 900)
            ev.append((clock, "decision", approver, "harden", "Approved the protections", "", {"approved": True}))
        clock += 5
        ev.append((clock, "hardened", "raiden", "harden", f"{fmt.plural(len(items), 'protection')} added", "", {"items": items}))
        clock += rng.uniform(15, 40)
        ev.append((clock, "report", "raiden", "harden", "Incident report written", "", {}))
        ev.append((clock + 1, "stage", "autopilot", "resolved", "Harden → Resolved", "", {}))
        inc.report = (f"## Summary\n{title} on {sid}.\n\n## Root cause\n{inc.root_cause}\n\n## What rAIden did\n" +
                      "\n".join(f"- {p['title']} ({', '.join(f'{k}={v}' for k, v in p['args'].items())}), approved by {p['approved_by']}" for p in plan) +
                      "\n\n## Prevention\n" + "\n".join(f"- {h['title']}" for h in items))
    for sec, kind_, actor, stage, title_, body, data in ev:
        s.add(IncidentEvent(incident_id=inc.id, ts=detected + timedelta(seconds=sec), kind=kind_, actor=actor, stage=stage, title=title_, body=body, data=data))
    inc.resolved_at = detected + timedelta(seconds=ev[-1][0])
    inc.acknowledged_at = detected + timedelta(seconds=3)
    inc.summary = inc.root_cause
    inc.stage_times = {"detected": iso(detected), "resolved": iso(inc.resolved_at)}
    inc.updated_at = inc.resolved_at
    if prod and plan:
        s.add(Approval(incident_id=inc.id, server_id=sid, kind="gate", stage="remediate", title="Approve the remediation plan", payload={"plan": plan}, risk=max(p["risk"] for p in plan),
                       status="approved", requested_at=detected + timedelta(seconds=60), decided_at=detected + timedelta(seconds=200), decided_by=approver))
        s.add(AuditEntry(ts=detected + timedelta(seconds=200), actor=approver, action="approval.approve", target=inc.id, decision="allow", detail={"title": "Approve the remediation plan"}))
    for p in plan:
        s.add(AuditEntry(ts=detected + timedelta(seconds=240), actor="raiden", actor_type="raiden", action=f"action.{p['action']}", target=sid, decision="allow",
                         detail={"incident": inc.id, "args": p["args"], "approved_by": p["approved_by"]}))


def _usage(rt: Any, rng: random.Random, now: datetime) -> None:
    with rt.db.session() as s:
        eps = {e.name: e for e in s.execute(select(AIEndpoint)).scalars()}
        primary, chat, local = (eps[e["name"]] for e in ENDPOINTS)
        incs = list(s.execute(select(Incident)).scalars())
        rows: dict[tuple[int, datetime, str, str], list[float]] = {}

        def add(ep: AIEndpoint, at: datetime, purpose: str, sid: str, tin: int, tout: int) -> None:
            b = at.replace(minute=0, second=0, microsecond=0)
            r = rows.setdefault((ep.id, b, purpose, sid), [0, 0, 0, 0, 0.0])
            cached = int(tin * 0.55)
            r[0] += 1
            r[1] += tin
            r[2] += tout
            r[3] += cached
            r[4] += ((tin - cached) * ep.price_in_per_m + cached * ep.price_cached_per_m + tout * ep.price_out_per_m) / 1e6

        for inc in incs:
            for k in range(14):
                add(primary, inc.detected_at + timedelta(minutes=k // 3), "incidents", inc.server_id, inc.tokens_in // 14, inc.tokens_out // 14)
            add(primary, inc.detected_at + timedelta(minutes=8), "hardening", inc.server_id, 26_000, 1_900)
        for day in range(30, -1, -1):
            d = (now - timedelta(days=day)).replace(hour=7, minute=0, second=0, microsecond=0)
            if d > now:
                continue
            for p in PROFILES:
                add(local, d + timedelta(minutes=PROFILES.index(p) * 2), "routines", p.id, rng.randint(18_000, 32_000), rng.randint(900, 1_600))
            if d.weekday() == 0:
                for p in PROFILES:
                    add(local, d + timedelta(hours=1), "routines", p.id, rng.randint(30_000, 48_000), rng.randint(1_500, 3_000))
            if d.weekday() < 5:
                for _ in range(rng.randint(2, 9)):
                    at = d + timedelta(hours=rng.randint(1, 10), minutes=rng.randint(0, 59))
                    if at < now:
                        add(chat, at, "chat", rng.choice(PROFILES).id, rng.randint(9_000, 40_000), rng.randint(250, 1_200))
        for (eid, b, purpose, sid), (calls, tin, tout, cached, cost) in rows.items():
            s.add(TokenUsage(endpoint_id=eid, bucket=b, purpose=purpose, server_id=sid or "", calls=int(calls), errors=0, tokens_in=int(tin), tokens_out=int(tout),
                             tokens_cached=int(cached), cost_usd=round(cost, 6)))


def _routine_runs(rt: Any, rng: random.Random, now: datetime) -> None:
    with rt.db.session() as s:
        for r in s.execute(select(Routine).where(Routine.enabled.is_(True), Routine.kind != "dream")).scalars():  # the dreams have their own history
            cron = Cron(r.cron)
            at = now - timedelta(days=2)
            times = []
            while True:
                at = cron.next_after(at)
                if at >= now:
                    break
                times.append(at)
            for ts in times[-6:]:
                status = rng.choices(["ok", "warn", "crit"], [0.7, 0.25, 0.05])[0] if r.kind == "check" else "ok"
                if r.key == "security-posture":
                    status = "warn"
                n = {"ok": 0, "warn": rng.randint(1, 4), "crit": rng.randint(1, 2)}[status]
                s.add(RoutineRun(routine_id=r.id, started_at=ts, finished_at=ts + timedelta(seconds=rng.randint(3, 40) if r.kind == "check" else rng.randint(40, 160)),
                                 status=status, summary=f"{fmt.plural(len(PROFILES), 'server')}, " + (f"{fmt.plural(n, 'finding')}; worst: {status.upper()}" if status != "ok" else "no findings"),
                                 results=[], triggered_by="schedule", tokens_in=0 if r.kind == "check" else rng.randint(120_000, 210_000),
                                 tokens_out=0 if r.kind == "check" else rng.randint(6_000, 11_000)))
            if times:
                r.last_run_at, r.last_status = times[-1], "warn" if r.key == "security-posture" else "ok"
            r.next_run_at = cron.next_after(now)


# ---------------------------------------------------------------------------------------------------- what rAIden learned
TAUGHT = [  # (days ago, who, kind, title, body, scope, retention): taught on the Knowledge page
    (30, "alex@contoso.com", "rule", "Never end sessions of the replication or backup roles (replicator, pgbackrest).",
     "A standby or a backup that loses its session starts over. Page the on-call instead.", {}, "permanent"),
    (21, "sam@contoso.com", "preference", "Put the exact SQL in every recommendation, ready to paste.", "", {}, "durable"),
    (12, "alex@contoso.com", "fact", "The migrator role runs schema migrations on weekdays at 14:00 UTC, from the deploy pipeline.", "",
     {"server": "orders-prod"}, "durable"),
]
TAUGHT_IN_CHAT = ("sam@contoso.com", 5, "From now on, page the payments on-call before any change on payments-prod: it is PCI scope.",
                  "rule", "Page the payments on-call before any change on payments-prod.", "PCI scope: every change needs a ticket. Read as much as you need.")
LOCK_COMMENT = "Go. analyst sessions are ad-hoc reporting: ending them is always fine."


def first_name_of(email: str) -> str:
    return next((name.split()[0] for e, name, _ in DEMO_USERS if e == email), email)


def _knowledge(rt: Any, now: datetime) -> None:
    """A month of learning, consistent with the history above: what the team taught (on the Knowledge page and in a chat),
    the lessons incidents left, ten nights of dreams that read what each day brought, and the proposal not to ask again
    that the third approval of the same plan made. It waits for a person, so the live lock queue still stops at the gate."""
    from .. import learning
    from ..dreams import _knowledge_change, read_text, report
    from ..models import Dream, Knowledge
    from .model import SKILL_TITLES, playbook

    with rt.db.session() as s:
        incs = list(s.execute(select(Incident).order_by(Incident.detected_at)).scalars())
        gates = {a.incident_id: a for a in s.execute(select(Approval).where(Approval.kind == "gate", Approval.stage == "remediate")).scalars()}
        made: list[Knowledge] = []

        def know(at: datetime, **kw: Any) -> Knowledge:
            k = Knowledge(created_at=at, updated_at=at, **kw)
            s.add(k)
            s.flush()
            made.append(k)
            return k

        for days, who, kind, title, body, scope, retention in TAUGHT:
            know(now - timedelta(days=days, hours=3), kind=kind, title=title, body=body, scope=scope, source="person", author=who, retention=retention)
        who, days, said, kind, title, body = TAUGHT_IN_CHAT  # the chat's remember tool
        at = now - timedelta(days=days, hours=6)
        cid = uuid.uuid4().hex
        s.add(Conversation(id=cid, title=said[:70], server_id="payments-prod", user_email=who, created_at=at, updated_at=at + timedelta(seconds=9)))
        s.flush()
        msg = ChatMessage(conversation_id=cid, role="user", content=said, data={"user": who}, created_at=at)
        s.add(msg)
        s.flush()
        k = know(at + timedelta(seconds=5), kind=kind, title=title, body=body, scope={"server": "payments-prod"}, source="person", author=who,
                 evidence=[{"type": "chat", "id": str(msg.id), "chat": cid, "by": who, "text": said}])
        s.add(ChatMessage(conversation_id=cid, role="tool", content="", created_at=at + timedelta(seconds=5), data={
            "name": "remember", "args": {"kind": kind, "title": title, "body": body, "scope": {"server": "payments-prod"}}, "ok": True, "ms": 14,
            "preview": {"knowledge_id": k.id, "title": title, "scope": "payments-prod", "new": True}}))
        s.add(ChatMessage(conversation_id=cid, role="assistant", created_at=at + timedelta(seconds=9),
                          content=f"Remembered for **payments-prod**: *{title}*\n\nIt is part of every incident, chat and routine from now on, for the "
                                  "whole team. You can edit or retire it on the Knowledge page.",
                          data={"model": "claude-sonnet-5", "simulated": True, "tokens_in": 9100, "tokens_out": 160}))
        # the lessons incidents left in Harden: a fact about their server
        for inc in incs:
            for h in inc.hardening or []:
                if h.get("kind") != "lesson" or h.get("status") != "applied":
                    continue
                ev = {"type": "incident", "id": inc.id, "by": h.get("approved_by"), "text": h["title"]}
                twin = next((x for x in made if x.title == h["title"] and x.scope == {"server": inc.server_id}), None)
                if twin is not None:
                    twin.evidence = list(twin.evidence) + [ev]
                else:
                    know(inc.resolved_at, kind="fact", title=h["title"], scope={"server": inc.server_id}, source="incident", author=inc.id, evidence=[ev])
        # the words that taught a dream something: a comment on the first lock-queue plan approved on orders-prod
        first_lock = next(i for i in incs if i.server_id == "orders-prod" and i.scenario == "lock_queue")
        gate = gates[first_lock.id]
        gate.comment = LOCK_COMMENT
        for e in s.execute(select(IncidentEvent).where(IncidentEvent.incident_id == first_lock.id, IncidentEvent.kind == "decision")).scalars():
            if e.stage == "diagnose":
                e.body = LOCK_COMMENT
        s.flush()
        # the third approval of the same plan for a lock queue in production: rAIden proposed not to ask again, right then
        for la in learning.propose_approvals(s, learning.approval_patterns(s, 3), by=""):
            last = max(la.evidence, key=lambda e: e["at"])
            la.proposed_by = "raiden"
            la.created_at = la.updated_at = gates[last["incident_id"]].decided_at
            s.add(IncidentEvent(incident_id=last["incident_id"], ts=la.created_at + timedelta(seconds=1), kind="learned", actor="raiden", stage="remediate",
                                title=f"rAIden proposes not to ask again: {learning.describe_approval(la)}",
                                body=f"{la.reason} A person accepts or declines it on the Knowledge page.", data={"learned": [la.id], "proposed": True}))

        # ten nights of dreams, each reading what the day before brought
        last_night = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if last_night > now - timedelta(minutes=5):
            last_night -= timedelta(days=1)
        nights = [last_night - timedelta(days=d) for d in range(9, -1, -1)]
        users_said = list(s.execute(select(ChatMessage).where(ChatMessage.role == "user")).scalars())
        decided = [a for a in s.execute(select(Approval).where(Approval.decided_at.isnot(None))).scalars() if learning.is_person(a.decided_by)]
        routine = s.execute(select(Routine).where(Routine.key == "dream")).scalar_one()
        local = s.execute(select(AIEndpoint).where(AIEndpoint.name == ENDPOINTS[2]["name"])).scalar_one()

        def night_after(t: datetime) -> datetime:
            return next(n for n in nights if n > t)

        def by_category(cat: str, before: datetime) -> dict[str, Any]:
            done = [i for i in incs if i.category == cat and i.resolved_at and i.resolved_at < before and any(p.get("status") == "done" for p in i.plan or [])]
            return {"category": cat, "skill_id": None, "incidents": [
                {"id": i.id, "server": i.server_id, "title": i.title, "root_cause": i.root_cause, "actions": [learning._call(p) for p in i.plan],
                 "protections": [h["title"] for h in i.hardening or [] if h.get("status") == "applied"]} for i in done[-6:]]}

        storm2 = [i for i in incs if i.server_id == "analytics-staging" and i.scenario == "connection_storm"][-1]
        lag = next(i for i in incs if i.server_id == "payments-replica" and i.scenario == "replication_lag")
        deadlock = next(i for i in incs if i.server_id == "orders-prod" and i.scenario == "deadlocks")
        fixed = IncidentEvent(incident_id=deadlock.id, ts=deadlock.resolved_at - timedelta(seconds=20), kind="comment", actor=deadlock.assignee, stage="diagnose",
                              title=f"{first_name_of(deadlock.assignee)} wrote what fixed it",
                              body="Fixed in the order service: release 2026.10.3 locks orders before order_lines. Nothing to do in the database.")
        s.add(fixed)
        s.flush()
        metabase = [x for x in made if x.title.startswith("Metabase")]
        plans: dict[datetime, list[tuple[str, Any]]] = {}
        plans.setdefault(night_after(gate.decided_at), []).extend([("words", gate), ("skill", "locks")])
        plans.setdefault(night_after(storm2.resolved_at), []).extend([("skill", "connections"), ("widen", metabase)])
        plans.setdefault(night_after(lag.resolved_at), []).append(("infer", lag))
        plans.setdefault(night_after(deadlock.resolved_at), []).append(("resolution", deadlock))
        summaries = {"words": "{who} said analyst sessions on orders-prod may always be ended to clear a lock queue: a rule for its lock queues now.",
                     "skill": "Wrote the playbook for {cat}: {n} incidents fixed the same way.",
                     "widen": "Merged the two Metabase facts into one for the fleet: it happened on orders-prod and on analytics-staging.",
                     "infer": "The replay lag on payments-replica was a finance report holding replay back: an inference from one incident, so it waits "
                              "for a person to confirm.",
                     "resolution": "{who} wrote that the orders-prod deadlocks are fixed in the application code: rAIden escalates them from now on."}
        prev = nights[0] - timedelta(days=1)
        for night in nights:
            read = {"decisions": sum(prev < a.decided_at <= night for a in decided), "notes": int(prev < fixed.ts <= night),
                    "statements": sum(prev < m.created_at <= night for m in users_said),
                    "incidents": sum(bool(i.resolved_at) and prev < i.resolved_at <= night for i in incs), "dismissed": 0}
            run = RoutineRun(routine_id=routine.id, started_at=night, status="ok", triggered_by="schedule")
            s.add(run)
            s.flush()
            d = Dream(run_id=run.id, started_at=night, status="done", triggered_by="schedule", read=read)
            s.add(d)
            s.flush()
            me, changes, said_parts = f"dream #{d.id}", [], []
            for what, x in plans.get(night, []):
                if what == "words":
                    k = know(night, kind="rule", title="Sessions of the analyst role are ad-hoc reports: ending one to clear a lock queue is fine.",
                             scope={"server": "orders-prod", "category": "locks"}, source="dream", author=me,
                             evidence=[{"type": "decision", "id": str(x.id), "by": x.decided_by, "text": x.comment, "words": True}])
                    changes.append(_knowledge_change("add", k, None))
                    said_parts.append(summaries["words"].format(who=first_name_of(x.decided_by)))
                elif what == "skill":
                    cand = by_category(x, night)
                    k = know(night, kind="skill", title=SKILL_TITLES[x], body=playbook(cand), scope={"category": x}, source="dream", author=me,
                             evidence=[{"type": "incident", "id": i["id"], "by": "raiden", "text": i["title"], "words": False} for i in cand["incidents"][-3:]])
                    changes.append(_knowledge_change("add", k, None))
                    said_parts.append(summaries["skill"].format(cat=fmt.incidents(x), n=len(cand["incidents"])))
                elif what == "widen" and len(x) == 2:
                    keep, drop = sorted(x, key=lambda k: -len(k.evidence))
                    for k, op, why in ((keep, "update", "the same on orders-prod: one fact for the fleet"), (drop, "retire", "merged into the fleet-wide fact")):
                        before = learning.snapshot(k)
                        k.revisions = [{**before, "at": iso(night), "by": me, "why": why}]
                        if op == "update":
                            k.scope, k.evidence = {}, list(k.evidence) + list(drop.evidence)
                        else:
                            k.status = "retired"
                        k.updated_at = night
                        changes.append(_knowledge_change(op, k, before))
                    said_parts.append(summaries["widen"])
                elif what == "infer":
                    k = know(night, kind="rule", title="On payments-replica, cancel the report that holds replay back, then cap its statement_timeout.",
                             scope={"server": "payments-replica", "category": "replication"}, source="dream", author=me, status="proposed",
                             evidence=[{"type": "incident", "id": x.id, "by": "raiden", "text": x.title, "words": False}] +
                                      ([{"type": "decision", "id": str(gates[x.id].id), "by": gates[x.id].decided_by, "text": f"approved: {gates[x.id].title}", "words": False}]
                                       if x.id in gates else []))
                    changes.append(_knowledge_change("add", k, None))
                    said_parts.append(summaries["infer"])
                elif what == "resolution":
                    k = know(night, kind="fact", title="Deadlocks on orders-prod come from two order workers locking orders and order_lines in opposite order: "
                                                       "the application team fixes them, so escalate instead of ending sessions.",
                             scope={"server": "orders-prod", "category": "locks"}, source="dream", author=me,
                             evidence=[{"type": "note", "id": str(fixed.id), "by": fixed.actor, "text": fixed.body, "words": True},
                                       {"type": "incident", "id": x.id, "by": fixed.actor, "text": x.title, "words": False}])
                    changes.append(_knowledge_change("add", k, None))
                    said_parts.append(summaries["resolution"].format(who=first_name_of(x.assignee)))
            if not any(read.values()):
                summary, tokens = "Nothing new to read since the last dream.", (0, 0)
            elif said_parts:
                summary = " ".join(said_parts)
                tokens = (9_000 + 2_600 * sum(read.values()), 700 + 450 * len(changes))
            else:
                summary = f"Read {read_text(read)}: nothing new worth keeping, what I know already covers it."
                tokens = (8_000 + 2_200 * sum(read.values()), 260)
            took = timedelta(seconds=(20 + 9 * len(changes) + 3 * sum(read.values())) if tokens[0] else 1)
            d.finished_at, d.changes, d.summary, d.tokens_in, d.tokens_out, d.model = night + took, changes, summary, tokens[0], tokens[1], local.model if tokens[0] else ""
            run.finished_at, run.summary, run.report, run.tokens_in, run.tokens_out = night + took, summary, report(read, changes), tokens[0], tokens[1]
            if tokens[0]:
                s.add(TokenUsage(endpoint_id=local.id, bucket=night, purpose="routines", server_id="", calls=2, errors=0, tokens_in=tokens[0],
                                 tokens_out=tokens[1], tokens_cached=0, cost_usd=0.0))
            prev = night
        routine.last_run_at, routine.last_status, routine.next_run_at = nights[-1] + timedelta(seconds=30), "ok", Cron(routine.cron).next_after(now)
        # how often each was in a prompt since it was learned: the incidents and chats it applied to
        chats = {c.id: c for c in s.execute(select(Conversation)).scalars()}
        env = {p.id: p.environment for p in PROFILES}
        for k in made:
            if k.status != "active":
                continue
            hits = [i.detected_at for i in incs if i.detected_at > k.created_at and learning.applies(k.scope, i.server_id, env.get(i.server_id or ""), i.category)]
            if k.kind != "skill" and not (k.scope or {}).get("category"):
                hits += [m.created_at for m in users_said if m.created_at > k.created_at and m.conversation_id in chats
                         and learning.applies(k.scope, chats[m.conversation_id].server_id, env.get(chats[m.conversation_id].server_id or ""), None)]
            k.uses, k.last_used_at = len(hits), max(hits, default=None)
        # where the next dream starts: everything up to last night was read
        events = s.execute(select(func.max(IncidentEvent.id)).where(IncidentEvent.ts <= last_night)).scalar() or 0
        put = {"decided_at": learning.stamp(max((a.decided_at for a in decided if a.decided_at <= last_night), default=None)),
               "event": events, "chat": max((m.id for m in users_said if m.created_at <= last_night), default=0),
               "resolved_at": learning.stamp(max((i.resolved_at for i in incs if i.resolved_at and i.resolved_at <= last_night), default=None))}
        from ..services import put_setting

        put_setting(s, "dream_cursor", {k: v for k, v in put.items() if v}, by="schedule")
