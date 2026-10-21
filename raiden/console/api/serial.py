"""JSON shapes of the console's objects. Secrets never leave the server: only `has_password` / `has_api_key`."""

from __future__ import annotations

from typing import Any

from ..catalog import PLATFORMS
from ..cron import Cron, CronError
from ..db import iso
from ..models import AIEndpoint, Approval, Assessment, Incident, IncidentEvent, Monitor, Routine, RoutineRun, Server, Suggestion, User


def user(u: User) -> dict[str, Any]:
    return {"id": u.id, "email": u.email, "name": u.name or u.email.split("@")[0], "role": u.role, "disabled": u.disabled,
            "created_at": iso(u.created_at), "last_login_at": iso(u.last_login_at)}


def server(s: Server) -> dict[str, Any]:
    info = dict(s.info or {})
    return {
        "id": s.id, "name": s.name, "environment": s.environment, "platform": s.platform, "platform_label": PLATFORMS.get(s.platform, s.platform),
        "host": s.host, "port": s.port, "dbname": s.dbname, "username": s.username, "has_password": bool(s.password_enc), "sslmode": s.sslmode,
        "databases": s.databases or [], "azure_resource_id": s.azure_resource_id, "tags": s.tags or {}, "owner": s.owner, "description": s.description,
        "maintenance_window": s.maintenance_window, "collect_interval_s": s.collect_interval_s, "enabled": s.enabled, "autonomy": s.autonomy or {},
        "status": s.status, "status_reason": s.status_reason, "last_seen_at": iso(s.last_seen_at), "last_error": s.last_error, "color_slot": s.color_slot,
        "version": info.get("version", ""), "in_recovery": bool(info.get("in_recovery")), "simulated": bool(info.get("simulated")),
        "region": info.get("region", ""), "sku": info.get("sku", ""), "sources": info.get("sources") or (["sql", "azure"] if s.azure_resource_id else ["sql"]),
        "host_agent": bool(info.get("host_agent")) or s.agent_last_seen_at is not None, "agent_last_seen_at": iso(s.agent_last_seen_at),
        "has_agent_token": bool(s.agent_token_hash), "capabilities": info.get("capabilities") or {}, "host_info": info.get("host") or {},
        "replica_of": info.get("replica_of"), "uptime_s": info.get("uptime_s"), "extensions": info.get("extensions") or [],
        "created_at": iso(s.created_at),
    }


def suggestion(x: Suggestion) -> dict[str, Any]:
    return {"id": x.id, "server_id": x.server_id, "kind": x.kind, "category": x.category, "title": x.title, "detail": x.detail, "source": x.source,
            "status": x.status, "data": x.data or {}, "created_at": iso(x.created_at), "updated_at": iso(x.updated_at)}


def incident_card(i: Incident, server_name: str | None = None, pending: list[Approval] | None = None) -> dict[str, Any]:
    return {
        "id": i.id, "number": i.number, "server_id": i.server_id, "server_name": server_name or i.server_id, "title": i.title, "severity": i.severity,
        "category": i.category, "source": i.source, "source_ref": i.source_ref, "stage": i.stage, "step": i.step, "state": i.state, "assignee": i.assignee,
        "waiting_reason": i.waiting_reason, "detected_at": iso(i.detected_at), "updated_at": iso(i.updated_at), "resolved_at": iso(i.resolved_at),
        "mitigated_at": iso(i.mitigated_at), "acknowledged_at": iso(i.acknowledged_at), "verify_until": iso(i.verify_until), "tokens": i.tokens_in + i.tokens_out,
        "cost_usd": round(i.cost_usd, 4), "tool_calls": i.tool_calls, "model": i.model, "attempts": i.attempts, "hypothesis": i.hypothesis,
        "root_cause": i.root_cause, "evidence": {k: v for k, v in (i.evidence or {}).items() if not k.startswith("_")},
        "pending_approvals": [approval(a) for a in pending or []], "plan_size": len(i.plan or []),
    }


def incident_detail(i: Incident, server_name: str | None, events: list[IncidentEvent], approvals: list[Approval]) -> dict[str, Any]:
    out = incident_card(i, server_name, [a for a in approvals if a.status == "pending"])
    out.update(summary=i.summary, plan=i.plan or [], hardening=i.hardening or [], report=i.report, stage_times=i.stage_times or {}, labels=i.labels or [],
               tokens_in=i.tokens_in, tokens_out=i.tokens_out, tokens_cached=i.tokens_cached, recurrence_of=i.recurrence_of,
               events=[event(e) for e in events], approvals=[approval(a) for a in approvals])
    return out


def event(e: IncidentEvent) -> dict[str, Any]:
    return {"id": e.id, "ts": iso(e.ts), "kind": e.kind, "actor": e.actor, "stage": e.stage, "title": e.title, "body": e.body, "data": e.data or {}}


def approval(a: Approval) -> dict[str, Any]:
    return {"id": a.id, "incident_id": a.incident_id, "conversation_id": a.conversation_id, "server_id": a.server_id, "kind": a.kind, "stage": a.stage,
            "title": a.title, "summary": a.summary, "payload": a.payload or {}, "risk": a.risk, "status": a.status, "requested_at": iso(a.requested_at),
            "decided_at": iso(a.decided_at), "decided_by": a.decided_by, "comment": a.comment}


def monitor(m: Monitor, firing: list[str] | None = None) -> dict[str, Any]:
    return {"id": m.id, "key": m.key, "name": m.name, "description": m.description, "metric": m.metric, "dimension": m.dimension, "aggregation": m.aggregation,
            "operator": m.operator, "threshold": m.threshold, "window_minutes": m.window_minutes, "severity": m.severity, "category": m.category,
            "scope": m.scope or {}, "action": m.action, "runbook": m.runbook, "enabled": m.enabled, "builtin": m.builtin, "created_by": m.created_by,
            "firing": firing or [], "created_at": iso(m.created_at)}


def routine(r: Routine, runs: list[RoutineRun] | None = None) -> dict[str, Any]:
    try:
        schedule = Cron(r.cron).describe()
    except CronError:
        schedule = r.cron
    return {"id": r.id, "key": r.key, "name": r.name, "description": r.description, "kind": r.kind, "checks": r.checks or [], "prompt": r.prompt, "cron": r.cron,
            "schedule": schedule, "timezone": r.timezone, "scope": r.scope or {}, "on_finding": r.on_finding, "enabled": r.enabled, "builtin": r.builtin,
            "last_run_at": iso(r.last_run_at), "last_status": r.last_status, "next_run_at": iso(r.next_run_at),
            "recent_runs": [run(x) for x in runs or []]}


def run(x: RoutineRun, full: bool = False) -> dict[str, Any]:
    out = {"id": x.id, "routine_id": x.routine_id, "started_at": iso(x.started_at), "finished_at": iso(x.finished_at), "status": x.status, "summary": x.summary,
           "triggered_by": x.triggered_by, "tokens": x.tokens_in + x.tokens_out}
    if full:
        out.update(results=x.results or [], report=x.report)
    return out


def endpoint(e: AIEndpoint, spend: tuple[int, float] | None = None) -> dict[str, Any]:
    tokens_today, cost_month = spend or (0, 0.0)
    return {"id": e.id, "name": e.name, "provider": e.provider, "base_url": e.base_url, "model": e.model, "api_version": e.api_version, "wire_api": e.wire_api,
            "auth": e.auth, "has_api_key": bool(e.api_key_enc), "reasoning_effort": e.reasoning_effort, "max_output_tokens": e.max_output_tokens,
            "price_in_per_m": e.price_in_per_m, "price_out_per_m": e.price_out_per_m, "price_cached_per_m": e.price_cached_per_m,
            "daily_token_budget": e.daily_token_budget, "monthly_cost_budget": e.monthly_cost_budget, "purposes": e.purposes or [], "priority": e.priority,
            "enabled": e.enabled, "simulated": e.simulated, "last_test_at": iso(e.last_test_at), "last_test_ok": e.last_test_ok,
            "last_test_message": e.last_test_message, "last_latency_ms": e.last_latency_ms, "tokens_today": tokens_today, "cost_month": round(cost_month, 2)}


def assessment(a: Assessment, full: bool = True) -> dict[str, Any]:
    out = {"id": a.id, "server_id": a.server_id, "created_at": iso(a.created_at), "finished_at": iso(a.finished_at), "status": a.status, "trigger": a.trigger,
           "triggered_by": a.triggered_by, "score": a.score, "scores": a.scores or {}, "error": a.error,
           "counts": {"issue": sum(1 for f in a.findings or [] if f["severity"] == "issue"), "suggestion": sum(1 for f in a.findings or [] if f["severity"] == "suggestion")}}
    if full:
        out.update(findings=a.findings or [], tuning=a.tuning or [], facts=a.facts or {}, summary=a.summary, tokens=a.tokens_in + a.tokens_out)
    return out
