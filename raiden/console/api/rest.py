"""Monitors, routines, chat, AI endpoints, audit, settings, live events and the host-agent ingest."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .. import __version__
from ..ai.providers import ping
from ..ai.router import PURPOSES, hour_bucket
from ..catalog import BY_ID, split_key
from ..chat import list_messages
from ..cron import Cron, validate
from ..db import iso, utcnow
from ..models import (AIEndpoint, Approval, AuditEntry, Conversation, Incident, Monitor, MonitorState, Routine, RoutineRun, Server, TokenUsage,
                      User)
from ..routines import TEMPLATES
from ..security import hash_token
from ..services import audit, create_incident, get_setting, put_setting
from . import serial
from .deps import admin, operator, rt, viewer

router = APIRouter(prefix="/api/v1")


# ============================================================================================================ monitors
class MonitorIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str = ""
    metric: str
    dimension: str | None = None
    aggregation: str = Field(default="avg", pattern="^(avg|max|min|last)$")
    operator: str = Field(default=">", pattern="^(>|>=|<|<=)$")
    threshold: float
    window_minutes: int = Field(default=5, ge=1, le=1440)
    severity: str = Field(default="sev3", pattern="^sev[1-4]$")
    category: str = "performance"
    scope: dict[str, Any] = {}
    action: str = Field(default="investigate", pattern="^(investigate|notify|suggest)$")
    runbook: str = ""
    enabled: bool = True


class MonitorPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    aggregation: str | None = Field(default=None, pattern="^(avg|max|min|last)$")
    operator: str | None = Field(default=None, pattern="^(>|>=|<|<=)$")
    threshold: float | None = None
    window_minutes: int | None = Field(default=None, ge=1, le=1440)
    severity: str | None = Field(default=None, pattern="^sev[1-4]$")
    scope: dict[str, Any] | None = None
    action: str | None = Field(default=None, pattern="^(investigate|notify|suggest)$")
    runbook: str | None = None
    enabled: bool | None = None


@router.get("/monitors")
def monitors(runtime: Any = Depends(rt), _: User = Depends(viewer)) -> list[dict[str, Any]]:
    with runtime.db.session() as s:
        firing: dict[int, list[str]] = {}
        for st in s.execute(select(MonitorState).where(MonitorState.state == "firing")).scalars():
            firing.setdefault(st.monitor_id, []).append(st.server_id)
        return [serial.monitor(m, firing.get(m.id)) for m in s.execute(select(Monitor).order_by(Monitor.builtin.desc(), Monitor.severity, Monitor.id)).scalars()]


@router.post("/monitors")
def create_monitor(body: MonitorIn, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    if split_key(body.metric)[0] not in BY_ID:
        raise HTTPException(400, "Unknown metric.")
    with runtime.db.session() as s:
        m = Monitor(**body.model_dump(), builtin=False, created_by=who.email)
        s.add(m)
        s.flush()
        audit(s, who.email, "monitor.create", m.name, "allow", metric=m.metric, threshold=m.threshold)
        return serial.monitor(m)


@router.patch("/monitors/{mid}")
def patch_monitor(mid: int, body: MonitorPatch, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    with runtime.db.session() as s:
        m = s.get(Monitor, mid)
        if m is None:
            raise HTTPException(404, "No such monitor.")
        changes = body.model_dump(exclude_unset=True)
        for k, v in changes.items():
            setattr(m, k, v)
        audit(s, who.email, "monitor.update", m.name, "allow", **{k: v for k, v in changes.items() if k != "runbook"})
        return serial.monitor(m)


@router.delete("/monitors/{mid}")
def delete_monitor(mid: int, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, bool]:
    with runtime.db.session() as s:
        m = s.get(Monitor, mid)
        if m is None:
            raise HTTPException(404, "No such monitor.")
        if m.builtin:
            raise HTTPException(400, "Built-in monitors can be switched off, not deleted.")
        audit(s, who.email, "monitor.delete", m.name, "allow")
        s.delete(m)
    return {"ok": True}


# ============================================================================================================ routines
class RoutineIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str = ""
    kind: str = Field(default="agent", pattern="^(check|agent)$")
    checks: list[str] = []
    prompt: str = ""
    cron: str = "0 7 * * *"
    timezone: str = "UTC"
    scope: dict[str, Any] = {}
    on_finding: str = Field(default="suggest", pattern="^(suggest|incident|report)$")
    enabled: bool = True


class RoutinePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    prompt: str | None = None
    checks: list[str] | None = None
    cron: str | None = None
    timezone: str | None = None
    scope: dict[str, Any] | None = None
    on_finding: str | None = Field(default=None, pattern="^(suggest|incident|report)$")
    enabled: bool | None = None


@router.get("/routines")
def routines(runtime: Any = Depends(rt), _: User = Depends(viewer)) -> list[dict[str, Any]]:
    with runtime.db.session() as s:
        out = []
        for r in s.execute(select(Routine).order_by(Routine.builtin.desc(), Routine.id)).scalars():
            runs = list(s.execute(select(RoutineRun).where(RoutineRun.routine_id == r.id).order_by(RoutineRun.id.desc()).limit(12)).scalars())
            out.append(serial.routine(r, runs))
        return out


@router.get("/routines/templates")
def templates(_: User = Depends(viewer)) -> list[dict[str, Any]]:
    return [{**t, "schedule": Cron(t["cron"]).describe()} for t in TEMPLATES if t["kind"] != "dream"]  # one dream per console, built in


@router.post("/routines")
def create_routine(body: RoutineIn, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    problem = validate(body.cron)
    if problem:
        raise HTTPException(400, f"Schedule: {problem}")
    with runtime.db.session() as s:
        r = Routine(**body.model_dump(), builtin=False, next_run_at=Cron(body.cron).next_after(utcnow(), body.timezone))
        s.add(r)
        s.flush()
        audit(s, who.email, "routine.create", r.name, "allow", cron=r.cron, kind=r.kind)
        return serial.routine(r)


@router.patch("/routines/{rid}")
def patch_routine(rid: int, body: RoutinePatch, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    with runtime.db.session() as s:
        r = s.get(Routine, rid)
        if r is None:
            raise HTTPException(404, "No such routine.")
        changes = body.model_dump(exclude_unset=True)
        if "cron" in changes:
            problem = validate(changes["cron"])
            if problem:
                raise HTTPException(400, f"Schedule: {problem}")
        for k, v in changes.items():
            setattr(r, k, v)
        r.next_run_at = Cron(r.cron).next_after(utcnow(), r.timezone)
        audit(s, who.email, "routine.update", r.name, "allow", **{k: v for k, v in changes.items() if k != "prompt"})
        runs = list(s.execute(select(RoutineRun).where(RoutineRun.routine_id == r.id).order_by(RoutineRun.id.desc()).limit(12)).scalars())
        return serial.routine(r, runs)


@router.delete("/routines/{rid}")
def delete_routine(rid: int, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, bool]:
    with runtime.db.session() as s:
        r = s.get(Routine, rid)
        if r is None:
            raise HTTPException(404, "No such routine.")
        if r.builtin:
            raise HTTPException(400, "Built-in routines can be switched off, not deleted.")
        audit(s, who.email, "routine.delete", r.name, "allow")
        s.delete(r)
    return {"ok": True}


@router.post("/routines/{rid}/run")
async def run_routine(rid: int, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    with runtime.db.session() as s:
        if s.get(Routine, rid) is None:
            raise HTTPException(404, "No such routine.")
        audit(s, who.email, "routine.run", str(rid), "allow")
    run_id = await runtime.routines.run_now(rid, who.email)
    return {"run_id": run_id}


@router.get("/routines/{rid}/runs")
def routine_runs(rid: int, runtime: Any = Depends(rt), _: User = Depends(viewer)) -> list[dict[str, Any]]:
    with runtime.db.session() as s:
        return [serial.run(x) for x in s.execute(select(RoutineRun).where(RoutineRun.routine_id == rid).order_by(RoutineRun.id.desc()).limit(50)).scalars()]


@router.get("/runs/{run_id}")
def get_run(run_id: int, runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    with runtime.db.session() as s:
        x = s.get(RoutineRun, run_id)
        if x is None:
            raise HTTPException(404, "No such run.")
        return serial.run(x, full=True)


# ============================================================================================================ chat
class NewConversation(BaseModel):
    server_id: str | None = None
    title: str = ""


class ChatIn(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


@router.get("/chat/conversations")
def conversations(runtime: Any = Depends(rt), who: User = Depends(viewer)) -> list[dict[str, Any]]:
    with runtime.db.session() as s:
        rows = s.execute(select(Conversation).order_by(Conversation.updated_at.desc()).limit(100)).scalars()
        return [{"id": c.id, "title": c.title, "server_id": c.server_id, "user": c.user_email, "incident_id": c.incident_id, "updated_at": iso(c.updated_at)} for c in rows]


@router.post("/chat/conversations")
def new_conversation(body: NewConversation, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    with runtime.db.session() as s:
        if body.server_id and s.get(Server, body.server_id) is None:
            raise HTTPException(404, "No such server.")
        c = Conversation(id=uuid.uuid4().hex, title=body.title or "New investigation", server_id=body.server_id, user_email=who.email)
        s.add(c)
        return {"id": c.id, "title": c.title, "server_id": c.server_id, "user": c.user_email, "updated_at": iso(utcnow())}


@router.get("/chat/conversations/{cid}")
async def conversation(cid: str, runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    with runtime.db.session() as s:
        c = s.get(Conversation, cid)
        if c is None:
            raise HTTPException(404, "No such conversation.")
        appr = {a.id: serial.approval(a) for a in s.execute(select(Approval).where(Approval.conversation_id == cid)).scalars()}
        head = {"id": c.id, "title": c.title, "server_id": c.server_id, "user": c.user_email, "incident_id": c.incident_id, "updated_at": iso(c.updated_at)}
    return {**head, "messages": await list_messages(runtime, cid), "approvals": appr, "busy": runtime.chat.locks.get(cid) is not None and runtime.chat.locks[cid].locked()}


@router.delete("/chat/conversations/{cid}")
def delete_conversation(cid: str, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, bool]:
    with runtime.db.session() as s:
        c = s.get(Conversation, cid)
        if c is None:
            raise HTTPException(404, "No such conversation.")
        s.delete(c)
    return {"ok": True}


@router.post("/chat/conversations/{cid}/messages")
async def chat_message(cid: str, body: ChatIn, runtime: Any = Depends(rt), who: User = Depends(operator)) -> StreamingResponse:
    return StreamingResponse(runtime.chat.stream(cid, who.email, body.content), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ============================================================================================================ AI endpoints
class EndpointIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    provider: str = Field(pattern="^(azure_openai|openai|anthropic|openai_compatible)$")
    base_url: str = ""
    model: str = Field(min_length=1, max_length=120)
    api_version: str = ""
    wire_api: str = Field(default="auto", pattern="^(auto|chat|responses)$")
    auth: str = Field(default="api_key", pattern="^(entra|api_key|none)$")
    api_key: str | None = None
    reasoning_effort: str = Field(default="medium", pattern="^(none|minimal|low|medium|high|xhigh)$")
    max_output_tokens: int = Field(default=8000, ge=256, le=128000)
    price_in_per_m: float = Field(default=0.0, ge=0)
    price_out_per_m: float = Field(default=0.0, ge=0)
    price_cached_per_m: float = Field(default=0.0, ge=0)
    daily_token_budget: int | None = Field(default=None, ge=0)
    monthly_cost_budget: float | None = Field(default=None, ge=0)
    purposes: list[str] = ["incidents", "chat", "routines", "hardening"]
    priority: int = Field(default=10, ge=1, le=100)
    enabled: bool = True


class EndpointPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_version: str | None = None
    wire_api: str | None = Field(default=None, pattern="^(auto|chat|responses)$")
    auth: str | None = Field(default=None, pattern="^(entra|api_key|none)$")
    api_key: str | None = None
    reasoning_effort: str | None = Field(default=None, pattern="^(none|minimal|low|medium|high|xhigh)$")
    max_output_tokens: int | None = Field(default=None, ge=256, le=128000)
    price_in_per_m: float | None = Field(default=None, ge=0)
    price_out_per_m: float | None = Field(default=None, ge=0)
    price_cached_per_m: float | None = Field(default=None, ge=0)
    daily_token_budget: int | None = None
    monthly_cost_budget: float | None = None
    purposes: list[str] | None = None
    priority: int | None = Field(default=None, ge=1, le=100)
    enabled: bool | None = None


def _check_purposes(p: list[str] | None) -> None:
    if p is not None and any(x not in PURPOSES for x in p):
        raise HTTPException(400, f"Purposes are {', '.join(PURPOSES)}.")


@router.get("/ai/endpoints")
def endpoints(runtime: Any = Depends(rt), _: User = Depends(viewer)) -> list[dict[str, Any]]:
    with runtime.db.session() as s:
        return [serial.endpoint(e, runtime.router.spend(e.id)) for e in s.execute(select(AIEndpoint).order_by(AIEndpoint.priority, AIEndpoint.id)).scalars()]


@router.post("/ai/endpoints")
def create_endpoint(body: EndpointIn, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    _check_purposes(body.purposes)
    data = body.model_dump()
    key = data.pop("api_key")
    with runtime.db.session() as s:
        e = AIEndpoint(**data, api_key_enc=runtime.keyring.encrypt(key) if key else None, simulated=False)
        s.add(e)
        s.flush()
        audit(s, who.email, "ai_endpoint.create", e.name, "allow", provider=e.provider, model=e.model, purposes=e.purposes)
        return serial.endpoint(e)


@router.patch("/ai/endpoints/{eid}")
def patch_endpoint(eid: int, body: EndpointPatch, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    _check_purposes(body.purposes)
    with runtime.db.session() as s:
        e = s.get(AIEndpoint, eid)
        if e is None:
            raise HTTPException(404, "No such endpoint.")
        changes = body.model_dump(exclude_unset=True)
        if "api_key" in changes:
            key = changes.pop("api_key")
            e.api_key_enc = runtime.keyring.encrypt(key) if key else None
            e.simulated = False
        if any(k in changes for k in ("base_url", "model", "auth")):
            e.simulated = False  # a real configuration replaces the demo stand-in
        for k, v in changes.items():
            setattr(e, k, v)
        e.updated_at = utcnow()
        audit(s, who.email, "ai_endpoint.update", e.name, "allow", fields=sorted(changes))
        return serial.endpoint(e, runtime.router.spend(e.id))


@router.delete("/ai/endpoints/{eid}")
def delete_endpoint(eid: int, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, bool]:
    with runtime.db.session() as s:
        e = s.get(AIEndpoint, eid)
        if e is None:
            raise HTTPException(404, "No such endpoint.")
        audit(s, who.email, "ai_endpoint.delete", e.name, "allow")
        s.delete(e)
    return {"ok": True}


@router.post("/ai/endpoints/{eid}/test")
async def test_endpoint(eid: int, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    with runtime.db.session() as s:
        e = s.get(AIEndpoint, eid)
        if e is None:
            raise HTTPException(404, "No such endpoint.")
        s.expunge(e)
    try:
        prov = runtime.router.provider_for(e)
        ok, message, ms = await ping(prov)
    except Exception as exc:  # noqa: BLE001
        ok, message, ms = False, str(exc), 0
    with runtime.db.session() as s:
        e2 = s.get(AIEndpoint, eid)
        e2.last_test_at, e2.last_test_ok, e2.last_test_message, e2.last_latency_ms = utcnow(), ok, message + (" (simulated)" if e.simulated else ""), ms
        audit(s, who.email, "ai_endpoint.test", e.name, "allow" if ok else "error", message=message[:200])
    return {"ok": ok, "message": message, "latency_ms": ms, "simulated": e.simulated}


@router.get("/ai/usage")
def usage(days: int = 30, runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    since = hour_bucket() - timedelta(days=days)
    with runtime.db.session() as s:
        eps = {e.id: e for e in s.execute(select(AIEndpoint)).scalars()}
        rows = s.execute(select(TokenUsage).where(TokenUsage.bucket >= since)).scalars()
        daily: dict[tuple[str, int, str], list[float]] = {}
        by_server: dict[str, list[float]] = {}
        for r in rows:
            day = r.bucket.strftime("%Y-%m-%d")
            d = daily.setdefault((day, r.endpoint_id, r.purpose), [0, 0, 0, 0.0, 0])
            d[0] += r.tokens_in
            d[1] += r.tokens_out
            d[2] += r.tokens_cached
            d[3] += r.cost_usd
            d[4] += r.calls
            b = by_server.setdefault(r.server_id or "", [0, 0.0])
            b[0] += r.tokens_in + r.tokens_out
            b[1] += r.cost_usd
    out = [{"day": d, "endpoint_id": e, "endpoint": eps[e].name if e in eps else str(e), "purpose": p, "tokens_in": int(v[0]), "tokens_out": int(v[1]),
            "tokens_cached": int(v[2]), "cost_usd": round(v[3], 4), "calls": int(v[4])} for (d, e, p), v in sorted(daily.items())]
    return {"days": days, "daily": out, "by_server": [{"server_id": k, "tokens": int(v[0]), "cost_usd": round(v[1], 2)} for k, v in sorted(by_server.items(), key=lambda kv: -kv[1][0])],
            "totals": {"tokens": sum(x["tokens_in"] + x["tokens_out"] for x in out), "cost_usd": round(sum(x["cost_usd"] for x in out), 2),
                       "calls": sum(x["calls"] for x in out)}}


@router.get("/ai/routing")
def routing(runtime: Any = Depends(rt), _: User = Depends(viewer)) -> list[dict[str, Any]]:
    out = []
    for purpose in PURPOSES:
        usable, skipped = runtime.router.candidates(purpose)
        out.append({"purpose": purpose, "endpoints": [{"id": e.id, "name": e.name, "model": e.model, "provider": e.provider} for e in usable], "skipped": skipped})
    return out


# ============================================================================================================ audit & settings
@router.get("/audit")
def audit_log(actor: str | None = None, action: str | None = None, target: str | None = None, limit: int = 200, before: int | None = None,
              runtime: Any = Depends(rt), _: User = Depends(viewer)) -> list[dict[str, Any]]:
    with runtime.db.session() as s:
        stmt = select(AuditEntry).order_by(AuditEntry.id.desc()).limit(min(limit, 1000))
        if actor:
            stmt = stmt.where(AuditEntry.actor.ilike(f"%{actor}%"))
        if action:
            stmt = stmt.where(AuditEntry.action.ilike(f"{action}%"))
        if target:
            stmt = stmt.where(AuditEntry.target.ilike(f"%{target}%"))
        if before:
            stmt = stmt.where(AuditEntry.id < before)
        return [{"id": a.id, "ts": iso(a.ts), "actor": a.actor, "actor_type": a.actor_type, "action": a.action, "target": a.target, "decision": a.decision,
                 "detail": a.detail or {}} for a in s.execute(stmt).scalars()]


class Webhook(BaseModel):
    name: str
    url: str = Field(pattern="^https?://")
    kind: str = Field(default="generic", pattern="^(teams|slack|generic)$")
    events: list[str] = ["approval", "escalated", "incident_opened", "resolved"]
    enabled: bool = True


class NotificationsIn(BaseModel):
    webhooks: list[Webhook] = []


class GeneralIn(BaseModel):
    organization: str | None = None
    retention_raw_days: float | None = Field(default=None, ge=1, le=90)
    retention_rollup_days: float | None = Field(default=None, ge=7, le=3650)
    timezone: str | None = None


@router.get("/settings")
def settings(runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    with runtime.db.session() as s:
        general = get_setting(s, "general")
        notif = get_setting(s, "notifications")
    st = runtime.settings
    return {"general": general, "notifications": notif, "system": {
        "version": __version__, "demo": st.demo, "database": "SQLite" if st.is_sqlite else "PostgreSQL", "data_dir": str(st.data_dir), "base_url": st.base_url,
        "max_ai_sessions": st.max_sessions, "uptime_s": int(time.time() - runtime.started_at), "live_listeners": runtime.bus.listeners}}


@router.put("/settings/general")
def put_general(body: GeneralIn, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    with runtime.db.session() as s:
        cur = get_setting(s, "general")
        cur.update(body.model_dump(exclude_none=True))
        put_setting(s, "general", cur, by=who.email)
        audit(s, who.email, "settings.general", "", "allow", **body.model_dump(exclude_none=True))
    return cur


@router.put("/settings/notifications")
def put_notifications(body: NotificationsIn, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    value = {"webhooks": [w.model_dump() for w in body.webhooks]}
    with runtime.db.session() as s:
        put_setting(s, "notifications", value, by=who.email)
        audit(s, who.email, "settings.notifications", "", "allow", webhooks=[w.name for w in body.webhooks])
    return value


@router.post("/settings/notifications/test")
async def test_notifications(runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    import httpx

    with runtime.db.session() as s:
        hooks = get_setting(s, "notifications").get("webhooks", [])
    results = []
    async with httpx.AsyncClient(timeout=10) as client:
        for h in hooks:
            body = {"text": "rAIden: test notification"} if h.get("kind") == "slack" else {"event": "test", "title": "rAIden: test notification"}
            try:
                r = await client.post(h["url"], json=body)
                results.append({"name": h.get("name"), "ok": r.status_code < 400, "status": r.status_code})
            except httpx.HTTPError as exc:
                results.append({"name": h.get("name"), "ok": False, "status": str(exc)[:120]})
    return {"results": results}


# ============================================================================================================ live events
@router.get("/events")
async def events(request: Request, runtime: Any = Depends(rt), _: User = Depends(viewer)) -> StreamingResponse:
    async def gen():
        yield "event: hello\ndata: {}\n\n"
        with runtime.bus.listen() as q:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {msg['type']}\ndata: {json.dumps(msg, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ============================================================================================================ host agents
class Heartbeat(BaseModel):
    server_id: str
    metrics: dict[str, float] = {}
    agent: dict[str, Any] = {}


class AgentEvents(BaseModel):
    server_id: str
    events: list[dict[str, Any]] = []


def _agent_server(s: Any, server_id: str, authorization: str | None) -> Server:
    token = (authorization or "").removeprefix("Bearer ").strip()
    srv = s.get(Server, server_id)
    if srv is None or not srv.agent_token_hash or not token or hash_token(token) != srv.agent_token_hash:
        raise HTTPException(401, "Unknown server or bad agent token.")
    return srv


@router.post("/agents/heartbeat")
def heartbeat(body: Heartbeat, runtime: Any = Depends(rt), authorization: str | None = Header(default=None)) -> dict[str, Any]:
    with runtime.db.session() as s:
        srv = _agent_server(s, body.server_id, authorization)
        srv.agent_last_seen_at = utcnow()
        info = dict(srv.info or {})
        info["host_agent"] = True
        info["agent"] = body.agent
        info["host_metrics"] = {k: v for k, v in body.metrics.items() if k in BY_ID}
        info["host_metrics_at"] = time.time()
        srv.info = info
    return {"ok": True}


@router.post("/agents/events")
def agent_events(body: AgentEvents, runtime: Any = Depends(rt), authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Incidents the host agent handled on its own (log-driven: crashes, corruption, DROP TABLE) appear on the board."""
    opened = []
    with runtime.db.session() as s:
        _agent_server(s, body.server_id, authorization)
        from ..services import add_event, open_incident_for, set_stage

        for ev in body.events[:200]:
            kind = ev.get("type", "")
            ref = str(ev.get("id") or ev.get("incident") or "")
            fp = f"agent:{body.server_id}:{ref}"
            if kind == "incident_opened":
                if s.execute(select(Incident.id).where(Incident.fingerprint == fp).limit(1)).first():
                    continue  # the agent retried a batch the console already took
                inc = create_incident(s, server_id=body.server_id, title=str(ev.get("reason", "Host agent incident"))[:300],
                                      severity="sev1" if ev.get("critical") else "sev3", category="availability" if ev.get("critical") else "other",
                                      source="host_agent", source_ref=ref, fingerprint=fp, summary="Handled by the rAIden host agent on the server itself.",
                                      evidence={"kinds": ev.get("kinds"), "events": ev.get("events")})
                inc.state, inc.assignee, inc.stage = "running", "host-agent", "diagnose"
                opened.append(inc.id)
                continue
            inc = open_incident_for(s, fp)
            if inc is None:
                continue
            if kind == "agent_tool":
                add_event(s, inc, "tool", f"{ev.get('tool')}: {str(ev.get('summary', ''))[:160]}", "", actor="host-agent",
                          data={"tool": ev.get("tool"), "denied": ev.get("denied"), "ok": not ev.get("denied")})
            elif kind == "guard_denied":
                add_event(s, inc, "denied", f"Guardrail denied {ev.get('tool')}: {ev.get('rule')}", str(ev.get("summary", "")), actor="host-agent")
            elif kind == "escalation":
                inc.state, inc.assignee = "escalated", "on-call"
                add_event(s, inc, "escalated", "The host agent escalated", str(ev.get("summary", "")), actor="host-agent")
            elif kind == "incident_closed":
                inc.summary = str(ev.get("summary", ""))[:2000]
                set_stage(s, inc, "resolved" if ev.get("outcome") in ("resolved", "mitigated") else inc.stage, actor="host-agent", note=f"outcome: {ev.get('outcome')}")
                if ev.get("outcome") not in ("resolved", "mitigated"):
                    inc.state, inc.assignee = "escalated", "on-call"
    for iid in opened:
        runtime.bus.publish("incident", id=iid)
    return {"ok": True, "opened": opened}


# ============================================================================================================ prometheus
@router.get("/prometheus", response_class=PlainTextResponse)
def prometheus(runtime: Any = Depends(rt), authorization: str | None = Header(default=None)) -> str:
    import os

    token = os.environ.get("RAIDEN_METRICS_TOKEN")
    if not token or (authorization or "").removeprefix("Bearer ").strip() != token:
        raise HTTPException(404, "Not found.")
    lines = []
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with runtime.db.session() as s:
        servers = list(s.execute(select(Server)).scalars())
        open_incs = s.execute(select(Incident.server_id, func.count(Incident.id)).where(Incident.stage != "resolved").group_by(Incident.server_id)).all()
        pending = s.execute(select(func.count(Approval.id)).where(Approval.status == "pending")).scalar() or 0
        usage = s.execute(select(TokenUsage.purpose, func.sum(TokenUsage.tokens_in + TokenUsage.tokens_out), func.sum(TokenUsage.errors))
                          .where(TokenUsage.bucket >= today).group_by(TokenUsage.purpose)).all()
    seen: set[str] = set()
    for srv in servers:
        for key, v in runtime.store.latest_values(srv.id).items():
            base, dim = split_key(key)
            if base not in BY_ID:
                continue
            name = f"raiden_pg_{base}"
            if name not in seen:
                seen.add(name)
                lines.append(f"# HELP {name} {BY_ID[base].name}")
                lines.append(f"# TYPE {name} gauge")
            labels = f'server="{srv.id}",environment="{srv.environment}"' + (f',dimension="{dim}"' if dim else "")
            lines.append(f"{name}{{{labels}}} {v}")
    # the console watching itself: alert when a server stops answering or its numbers stop coming
    lines.append(f'# TYPE raiden_console_info gauge\nraiden_console_info{{version="{__version__}"}} 1')
    lines.append("# HELP raiden_server_up 0 while the console cannot reach the server")
    lines.append("# TYPE raiden_server_up gauge")
    lines += [f'raiden_server_up{{server="{srv.id}",environment="{srv.environment}"}} {0 if srv.status == "unreachable" else 1}' for srv in servers if srv.enabled]
    lines.append("# HELP raiden_server_sample_age_seconds Seconds since the server's last metrics were stored")
    lines.append("# TYPE raiden_server_sample_age_seconds gauge")
    for srv in servers:
        last = runtime.store.latest(srv.id) if srv.enabled else None
        if last:
            lines.append(f'raiden_server_sample_age_seconds{{server="{srv.id}",environment="{srv.environment}"}} {max(0, int(time.time()) - last[0])}')
    lines.append("# HELP raiden_ai_tokens_today Tokens used since midnight UTC, per purpose (budgets count the same tokens)")
    lines.append("# TYPE raiden_ai_tokens_today gauge")
    lines += [f'raiden_ai_tokens_today{{purpose="{p}"}} {int(t or 0)}' for p, t, _ in usage]
    lines.append("# HELP raiden_ai_errors_today Model calls that failed since midnight UTC, per purpose")
    lines.append("# TYPE raiden_ai_errors_today gauge")
    lines += [f'raiden_ai_errors_today{{purpose="{p}"}} {int(e or 0)}' for p, _, e in usage]
    lines.append("# TYPE raiden_open_incidents gauge")
    lines += [f'raiden_open_incidents{{server="{sid}"}} {n}' for sid, n in open_incs]
    lines.append("# TYPE raiden_pending_approvals gauge")
    lines.append(f"raiden_pending_approvals {pending}")
    return "\n".join(lines) + "\n"
