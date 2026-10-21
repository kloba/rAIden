"""What rAIden knows and how it learned it: instructions and skills, learned approvals (not asking again) and dreams."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import fmt, learning
from ..agent.tools import CATEGORIES
from ..cron import Cron, CronError
from ..db import iso, utcnow
from ..drivers.base import ACTIONS
from ..models import Dream, Knowledge, LearnedApproval, Routine, Server, User
from ..policy import merged
from ..services import audit, get_setting
from .deps import admin, operator, rt, viewer

router = APIRouter(prefix="/api/v1")


class KnowledgeIn(BaseModel):
    kind: str = Field(pattern="^(rule|fact|preference|skill)$")
    title: str = Field(min_length=3, max_length=300)
    body: str = Field(default="", max_length=6000)
    scope: dict[str, str | None] = {}
    retention: str = Field(default="durable", pattern="^(permanent|durable|ephemeral)$")


class KnowledgePatch(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=300)
    body: str | None = Field(default=None, max_length=6000)
    scope: dict[str, str | None] | None = None
    retention: str | None = Field(default=None, pattern="^(permanent|durable|ephemeral)$")
    status: str | None = Field(default=None, pattern="^(active|retired)$")
    why: str = Field(default="", max_length=500)


class Reason(BaseModel):
    reason: str = Field(default="", max_length=1000)


# ============================================================================================================ shapes
def knowledge(k: Knowledge) -> dict[str, Any]:
    return {"id": k.id, "kind": k.kind, "title": k.title, "body": k.body, "scope": k.scope or {}, "scope_text": learning.scope_text(k.scope),
            "source": k.source, "author": k.author, "evidence": k.evidence or [], "retention": k.retention, "expires_at": iso(k.expires_at),
            "status": "expired" if k.status == "active" and not learning.live(k) else k.status, "uses": k.uses, "last_used_at": iso(k.last_used_at),
            "history": (k.revisions or [])[-10:], "created_at": iso(k.created_at), "updated_at": iso(k.updated_at)}


def learned(la: LearnedApproval) -> dict[str, Any]:
    spec = ACTIONS.get(la.action, {})
    return {"id": la.id, "action": la.action, "action_title": spec.get("title", la.action), "risk": spec.get("risk", "high"), "category": la.category,
            "environment": la.environment, "server_id": la.server_id, "description": learning.describe_approval(la), "status": la.status,
            "approvals": la.approvals, "approvers": la.approvers or [], "evidence": la.evidence or [], "proposed_by": la.proposed_by,
            "decided_by": la.decided_by, "decided_at": iso(la.decided_at), "reason": la.reason, "uses": la.uses, "last_used_at": iso(la.last_used_at),
            "created_at": iso(la.created_at)}


def dream(d: Dream) -> dict[str, Any]:
    from ..dreams import read_text

    return {"id": d.id, "run_id": d.run_id, "started_at": iso(d.started_at), "finished_at": iso(d.finished_at), "status": d.status,
            "triggered_by": d.triggered_by, "read": d.read or {}, "read_text": read_text(d.read or {}), "changes": d.changes or [], "summary": d.summary,
            "tokens": d.tokens_in + d.tokens_out, "cost_usd": round(d.cost_usd, 4), "model": d.model, "undone_by": d.undone_by, "undone_at": iso(d.undone_at)}


def _schedule(r: Routine | None) -> dict[str, Any] | None:
    if r is None:
        return None
    try:
        text = Cron(r.cron).describe()
    except CronError:
        text = r.cron
    return {"id": r.id, "enabled": r.enabled, "cron": r.cron, "schedule": text, "next_run_at": iso(r.next_run_at), "last_run_at": iso(r.last_run_at)}


# ============================================================================================================ knowledge
@router.get("/knowledge")
def overview(runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    """The Knowledge page: everything rAIden was taught or learned, what it no longer asks, and its dream journal."""
    with runtime.db.session() as s:
        policy = merged(get_setting(s, "autonomy"))
        routine = s.execute(select(Routine).where(Routine.key == "dream")).scalar_one_or_none()
        return {
            "items": [knowledge(k) for k in s.execute(select(Knowledge).order_by(Knowledge.id.desc())).scalars()],
            "learned_approvals": [learned(la) for la in s.execute(select(LearnedApproval).order_by(LearnedApproval.id.desc())).scalars()],
            "dreams": [dream(d) for d in s.execute(select(Dream).order_by(Dream.id.desc()).limit(30)).scalars()],
            "policy": {"learn_approvals": policy.get("learn_approvals", True), "learn_after": policy.get("learn_after", 3)},
            "dreaming": runtime.dreamer.running, "dream_routine": _schedule(routine),
            "categories": [{"id": c, "name": fmt.category(c)} for c in CATEGORIES],  # the kinds of incident a scope can name
        }


@router.post("/knowledge")
def teach(body: KnowledgeIn, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    with runtime.db.session() as s:
        try:
            k, new = learning.remember(s, kind=body.kind, title=body.title, body=body.body, scope=dict(body.scope), source="person", author=who.email,
                                       retention=body.retention)
        except ValueError as exc:
            raise HTTPException(400, str(exc)[:1].upper() + str(exc)[1:] + ".") from None
        if not new:
            raise HTTPException(409, f"rAIden already knows this (#{k.id}).")
        audit(s, who.email, "knowledge.create", f"knowledge #{k.id}", "allow", kind=k.kind, title=k.title, scope=k.scope)
        out = knowledge(k)
    runtime.bus.publish("knowledge")
    return out


@router.patch("/knowledge/{kid}")
def edit(kid: int, body: KnowledgePatch, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    with runtime.db.session() as s:
        k = s.get(Knowledge, kid)
        if k is None:
            raise HTTPException(404, "No such instruction or skill.")
        changes = body.model_dump(exclude_unset=True, exclude={"why"})
        try:
            scope = learning.check_scope(s, body.scope) if body.scope is not None else None
        except ValueError as exc:
            raise HTTPException(400, str(exc)[:1].upper() + str(exc)[1:] + ".") from None
        if k.kind == "skill" and scope is not None and not scope.get("category"):
            raise HTTPException(400, "A skill is the playbook for one kind of incident: keep its category.")
        learning.revise(k, who.email, body.why or ("confirmed" if body.status == "active" and k.status == "proposed" else "edited"))
        if body.title is not None:
            k.title = body.title.strip()
        if body.body is not None:
            k.body = body.body.strip()
        if scope is not None:
            k.scope = scope
        if body.retention is not None:
            k.retention, k.expires_at = body.retention, learning.expiry(body.retention)
        if body.status is not None:
            k.status = body.status
            if body.status == "active" and k.retention == "ephemeral" and not learning.live(k):
                k.expires_at = learning.expiry("ephemeral")  # restored: another 30 days
        audit(s, who.email, "knowledge.update", f"knowledge #{k.id}", "allow", **{c: v for c, v in changes.items() if c != "body"})
        out = knowledge(k)
    runtime.bus.publish("knowledge")
    return out


@router.delete("/knowledge/{kid}")
def forget(kid: int, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, bool]:
    with runtime.db.session() as s:
        k = s.get(Knowledge, kid)
        if k is None:
            raise HTTPException(404, "No such instruction or skill.")
        audit(s, who.email, "knowledge.delete", f"knowledge #{k.id}", "allow", kind=k.kind, title=k.title)
        s.delete(k)
    runtime.bus.publish("knowledge")
    return {"ok": True}


# ============================================================================================================ learned approvals
VERBS = {"active": "accept", "declined": "decline", "revoked": "revoke"}


def _decide(runtime: Any, lid: int, who: User, to: str, allowed: tuple[str, ...], reason: str) -> dict[str, Any]:
    with runtime.db.session() as s:
        la = s.get(LearnedApproval, lid)
        if la is None:
            raise HTTPException(404, "No such learned approval.")
        if la.status not in allowed:
            raise HTTPException(409, f"This learned approval is {la.status}.")
        if to == "active" and learning.risk(la.action) == "high":
            raise HTTPException(400, "High-risk actions always wait for a person.")
        if to == "active" and la.server_id and s.get(Server, la.server_id) is None:
            raise HTTPException(409, f"The server {la.server_id} is gone.")
        la.status, la.decided_by, la.decided_at, la.reason = to, who.email, utcnow(), reason or la.reason
        audit(s, who.email, f"learned_approval.{VERBS[to]}", f"learned approval #{la.id}", "allow", what=la.action, category=la.category,
              environment=la.environment, server=la.server_id, reason=reason)
        out = learned(la)
    runtime.bus.publish("knowledge")
    runtime.bus.publish("autonomy")
    return out


@router.post("/learned-approvals/{lid}/accept")
def accept(lid: int, body: Reason, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    """Not asking again changes who decides: an admin's call, like the autonomy policy."""
    return _decide(runtime, lid, who, "active", ("proposed", "declined"), body.reason)


@router.post("/learned-approvals/{lid}/decline")
def decline(lid: int, body: Reason, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    return _decide(runtime, lid, who, "declined", ("proposed",), body.reason)


@router.post("/learned-approvals/{lid}/revoke")
def revoke(lid: int, body: Reason, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    """Anyone who operates the fleet can make rAIden ask again."""
    return _decide(runtime, lid, who, "revoked", ("active",), body.reason)


# ============================================================================================================ dreams
@router.get("/dreams/{did}")
def get_dream(did: int, runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    with runtime.db.session() as s:
        d = s.get(Dream, did)
        if d is None:
            raise HTTPException(404, "No such dream.")
        return dream(d)


@router.post("/dreams")
async def dream_now(runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    if runtime.dreamer.running:
        raise HTTPException(409, "rAIden is already dreaming.")
    with runtime.db.session() as s:
        r = s.execute(select(Routine).where(Routine.key == "dream")).scalar_one_or_none()
        if r is None:
            raise HTTPException(404, "The Dream routine is missing; restart the console to restore the built-in routines.")
        rid = r.id
        audit(s, who.email, "dream.run", "dream", "allow")
    return {"run_id": await runtime.routines.run_now(rid, who.email)}


@router.post("/dreams/{did}/undo")
def undo(did: int, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    try:
        return runtime.dreamer.undo(did, who.email)
    except KeyError:
        raise HTTPException(404, "No such dream.") from None
    except ValueError as exc:
        raise HTTPException(409, f"This dream cannot be undone: {exc}.") from None
