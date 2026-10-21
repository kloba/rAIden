"""rAIden learns from the people it works with.

Instructions (rules, facts, preferences) and skills (the playbook for one kind of incident) are taught by people, or
learned in dreams (dreams.py) from their decisions, notes and chats; each goes into every prompt it applies to.

Learned approvals keep the same question from coming back. When people approved the same action for the same kind of
incident several times in a row, rAIden proposes not to ask again; once a person accepts, the next such gate opens on
its own, and the next rejection revokes it. High-risk actions are never learned, and the rest of the autonomy policy
(the plan as the contract, the plan-size limit) still applies.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from . import fmt
from .agent.tools import CATEGORIES
from .db import iso, utcnow
from .drivers.base import ACTIONS
from .models import (Approval, ChatMessage, Conversation, Incident, IncidentEvent, Knowledge, LearnedApproval, Server, Setting,
                     Suggestion)
from .policy import ENVIRONMENTS

INSTRUCTION_KINDS = ("rule", "fact", "preference")
KINDS = INSTRUCTION_KINDS + ("skill",)
RETENTION = ("permanent", "durable", "ephemeral")
SCOPE_KEYS = ("server", "environment", "category")
EPHEMERAL_DAYS = 30
MAX_IN_PROMPT = 14
WINDOW_DAYS = 90  # decisions older than this do not count towards a learned approval
PEOPLE_NOTES = ("comment", "handback")
NOT_PEOPLE = ("raiden", "system", "autopilot", "host-agent", "on-call")
KIND_ORDER = {"rule": 0, "fact": 1, "preference": 2, "skill": 3}


def is_person(who: str | None) -> bool:
    """Decisions and words of people count; rAIden's own, autopilot's and learned approvals' do not."""
    return bool(who) and who not in NOT_PEOPLE and not who.startswith(("learned approval", "dream"))  # type: ignore[union-attr]


def stamp(dt: datetime | None) -> str | None:
    """A cursor position: full precision, so two decisions in the same second are both read once."""
    return dt.isoformat() if dt else None


def _since(cursor: dict[str, Any], key: str) -> datetime:
    v = cursor.get(key)
    return datetime.fromisoformat(str(v).rstrip("Z")) if v else datetime(1970, 1, 1)


# ================================================================================================ scope
def check_scope(s: Any, scope: dict[str, Any] | None) -> dict[str, str]:
    """The scope without empty keys; unknown servers, environments and categories are refused."""
    out = {k: str(v) for k, v in (scope or {}).items() if k in SCOPE_KEYS and v not in (None, "", "fleet")}
    if "server" in out and s.get(Server, out["server"]) is None:
        named = s.execute(select(Server).where(func.lower(Server.name) == out["server"].lower())).scalars().first()  # a model may know the name only
        if named is None:
            raise ValueError(f"unknown server {out['server']!r}")
        out["server"] = named.id
    if "environment" in out and out["environment"] not in ENVIRONMENTS:
        raise ValueError(f"unknown environment {out['environment']!r}: use one of {', '.join(ENVIRONMENTS)}")
    if "category" in out and out["category"] not in CATEGORIES:
        raise ValueError(f"unknown category {out['category']!r}: use one of {', '.join(CATEGORIES)}")
    return out


def applies(scope: dict[str, Any] | None, server_id: str | None, environment: str | None, category: str | None) -> bool:
    for key, value in (("server", server_id), ("environment", environment), ("category", category)):
        want = (scope or {}).get(key)
        if want and want != value:
            return False
    return True


def scope_text(scope: dict[str, Any] | None) -> str:
    sc = scope or {}
    parts = [sc.get("server") or "", sc.get("environment") or "", fmt.incidents(sc["category"]) if sc.get("category") else ""]
    return " · ".join(p for p in parts if p) or "the fleet"


def _narrow(k: Knowledge) -> int:
    sc = k.scope or {}
    return -(4 * bool(sc.get("server")) + 2 * bool(sc.get("environment")) + bool(sc.get("category")))


def live(k: Knowledge, now: datetime | None = None) -> bool:
    return k.status == "active" and (k.expires_at is None or k.expires_at > (now or utcnow()))


def people_own(k: Knowledge) -> bool:
    """Taught, edited or confirmed by a person: a dream changes it only on a person's own words."""
    return k.source == "person" or any(is_person(r.get("by")) for r in k.revisions or [])


def snapshot(k: Knowledge) -> dict[str, Any]:
    return {"kind": k.kind, "title": k.title, "body": k.body, "scope": dict(k.scope or {}), "status": k.status, "retention": k.retention}


def revise(k: Knowledge, by: str, why: str = "") -> dict[str, Any]:
    """Keep the version before an edit; returns it (the dream journal's 'before')."""
    before = snapshot(k)
    k.revisions = list(k.revisions or []) + [{**before, "at": iso(utcnow()), "by": by, "why": why}]
    k.updated_at = utcnow()
    return before


def restore(k: Knowledge, version: dict[str, Any], by: str, why: str) -> None:
    revise(k, by, why)
    for f in ("kind", "title", "body", "scope", "status", "retention"):
        if f in version:
            setattr(k, f, version[f])
    k.expires_at = expiry(k.retention)


def expiry(retention: str) -> datetime | None:
    return utcnow() + timedelta(days=EPHEMERAL_DAYS) if retention == "ephemeral" else None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def same(s: Any, kind: str, title: str, scope: dict[str, Any]) -> Knowledge | None:
    """What rAIden already knows under this title and scope (taught twice is still one instruction)."""
    want = _norm(title)
    for k in s.execute(select(Knowledge).where(Knowledge.kind == kind, Knowledge.status.in_(("active", "proposed")))).scalars():
        if _norm(k.title) == want and (k.scope or {}) == scope:
            return k
    return None


def remember(s: Any, *, kind: str, title: str, body: str = "", scope: dict[str, Any] | None = None, source: str = "person", author: str = "",
             evidence: list[dict[str, Any]] | None = None, retention: str = "durable", status: str = "active") -> tuple[Knowledge, bool]:
    """Add an instruction or a skill; (the knowledge, True) when it is new, (the existing one, False) when it was known."""
    title, body = (title or "").strip(), (body or "").strip()
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}: use one of {', '.join(KINDS)}")
    if not title:
        raise ValueError("a title is needed")
    if retention not in RETENTION:
        raise ValueError(f"unknown retention {retention!r}: use one of {', '.join(RETENTION)}")
    if kind == "skill" and not (scope or {}).get("category"):
        raise ValueError("a skill is the playbook for one kind of incident: give it a category")
    sc = check_scope(s, scope)
    known = same(s, kind, title, sc)
    if known is not None:
        return known, False
    k = Knowledge(kind=kind, title=title[:300], body=body[:6000], scope=sc, source=source, author=author[:200], evidence=evidence or [],
                  retention=retention, expires_at=expiry(retention), status=status)
    s.add(k)
    s.flush()
    return k, True


def migrate_lessons(s: Any) -> int:
    """Lessons that earlier consoles kept in a setting become facts with the incident they came from."""
    row = s.get(Setting, "lessons")
    if row is None:
        return 0
    n = 0
    for key, items in (row.value or {}).items():
        sid = None if key == "_fleet" else key
        if sid and s.get(Server, sid) is None:
            continue
        for x in items or []:
            text = str(x.get("text") or "").strip()
            if text:
                remember(s, kind="fact", title=text, scope={"server": sid} if sid else {}, source="incident", author=str(x.get("incident") or ""),
                         evidence=[{"type": "incident", "id": x.get("incident"), "text": text}] if x.get("incident") else [])
                n += 1
    s.delete(row)
    return n


# ================================================================================================ into the prompts
def instructions_for(s: Any, server_id: str | None, environment: str | None, category: str | None = None,
                     only_category: bool = False) -> list[Knowledge]:
    """Active rules, facts and preferences that apply here. Without a category, the ones about one kind of incident wait
    until the incident has one; only_category returns just those (they come with the diagnosis)."""
    now = utcnow()
    out = []
    for k in s.execute(select(Knowledge).where(Knowledge.status == "active", Knowledge.kind.in_(INSTRUCTION_KINDS))).scalars():
        has_cat = bool((k.scope or {}).get("category"))
        if not live(k, now) or (only_category and not has_cat) or (category is None and has_cat):
            continue
        if applies(k.scope, server_id, environment, category):
            out.append(k)
    out.sort(key=lambda k: (KIND_ORDER.get(k.kind, 9), _narrow(k), -k.id))
    return out[:MAX_IN_PROMPT]


def skill_for(s: Any, category: str | None, server_id: str | None, environment: str | None) -> Knowledge | None:
    if not category:
        return None
    rows = [k for k in s.execute(select(Knowledge).where(Knowledge.status == "active", Knowledge.kind == "skill")).scalars()
            if (k.scope or {}).get("category") == category and applies(k.scope, server_id, environment, category)]
    return min(rows, key=lambda k: (_narrow(k), -k.id), default=None)


def _used(items: list[Knowledge]) -> None:
    now = utcnow()
    for k in items:
        k.uses = (k.uses or 0) + 1
        k.last_used_at = now


def line(k: Knowledge) -> str:
    who = {"person": f"taught by {k.author}" if k.author else "taught by the team", "incident": f"learned in {k.author}" if k.author else "learned in an incident",
           "dream": "learned from the team's decisions"}.get(k.source, k.source)
    detail = f" {k.body.strip()}" if k.body.strip() else ""
    return f"- [{k.kind} #{k.id}, {scope_text(k.scope)}, {who}] {k.title.strip()}{detail}"


def prompt_block(s: Any, server_id: str | None, environment: str | None, category: str | None = None, only_category: bool = False) -> str:
    items = instructions_for(s, server_id, environment, category, only_category)
    _used(items)
    if not items:
        return ""
    head = (f"Your team's instructions for {fmt.incidents(category or 'other')}:" if only_category else
            "What your team taught you (follow it; when an instruction decides what you do, name it by its #):")
    return head + "\n" + "\n".join(line(k) for k in items)


def skill_block(s: Any, category: str | None, server_id: str | None, environment: str | None) -> str:
    sk = skill_for(s, category, server_id, environment)
    if sk is None:
        return ""
    _used([sk])
    seen = ", ".join(str(e.get("id")) for e in (sk.evidence or []) if e.get("type") == "incident")
    return (f"Your playbook #{sk.id} \"{sk.title}\" for {fmt.incidents(category or 'other')}" + (f" (it worked in {seen})" if seen else "")
            + ". Follow it unless the evidence here says otherwise, and say so when it does:\n" + sk.body.strip())


# ================================================================================================ learned approvals
def risk(action: str) -> str:
    return ACTIONS.get(action, {}).get("risk", "high")


def environment_of(s: Any, server_id: str | None) -> str:
    srv = s.get(Server, server_id) if server_id else None
    return srv.environment if srv else "production"


def describe_approval(la: LearnedApproval) -> str:
    return f"{ACTIONS.get(la.action, {}).get('title', la.action)} for {fmt.incidents(la.category)} on {la.server_id or la.environment}"


def _matching(s: Any, action: str, category: str, environment: str, server_id: str | None, statuses: tuple[str, ...]) -> list[LearnedApproval]:
    return [la for la in s.execute(select(LearnedApproval).where(LearnedApproval.action == action, LearnedApproval.category == category,
                                                                 LearnedApproval.environment == environment, LearnedApproval.status.in_(statuses))).scalars()
            if la.server_id in (None, server_id)]


def covering(s: Any, plan: list[dict[str, Any]], category: str, server_id: str | None, environment: str, policy: dict[str, Any]) -> list[LearnedApproval] | None:
    """The accepted learned approvals that cover every action of this plan, or None (then a person decides as usual)."""
    if not policy.get("learn_approvals", True) or not plan:
        return None
    found: dict[int, LearnedApproval] = {}
    for p in plan:
        action = str(p.get("action", ""))
        if risk(action) == "high":
            return None
        hits = sorted(_matching(s, action, category, environment, server_id, ("active",)), key=lambda x: x.server_id is None)
        if not hits:
            return None
        found[hits[0].id] = hits[0]
    return list(found.values())


def use(items: list[LearnedApproval]) -> None:
    now = utcnow()
    for la in items:
        la.uses = (la.uses or 0) + 1
        la.last_used_at = now


def revoke_for(s: Any, actions: list[str], category: str, server_id: str | None, environment: str, by: str, why: str) -> list[LearnedApproval]:
    """A rejection corrects what was learned: approvals learned for these actions here stop opening gates, and proposals
    for them are dropped."""
    out = []
    for action in sorted(set(actions)):
        for la in _matching(s, action, category, environment, server_id, ("active", "proposed")):
            la.status, la.decided_by, la.decided_at, la.reason = "revoked", by, utcnow(), why or "rejected"
            out.append(la)
    return out


def streaks(s: Any, now: datetime | None = None) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Per (action, category, environment): the plans people approved in a row, since the last rejection of that action
    there or the last time a person revoked what was learned (the last 90 days)."""
    now = now or utcnow()
    out: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    envs: dict[str | None, str] = {}
    rows = s.execute(select(Approval, Incident).join(Incident, Incident.id == Approval.incident_id)
                     .where(Approval.status.in_(("approved", "rejected")), Approval.decided_at >= now - timedelta(days=WINDOW_DAYS))
                     .order_by(Approval.decided_at, Approval.id)).all()
    for appr, inc in rows:
        if not is_person(appr.decided_by):
            continue
        if inc.server_id not in envs:
            envs[inc.server_id] = environment_of(s, inc.server_id)
        pl = appr.payload or {}
        if appr.kind == "gate" and appr.stage == "remediate":
            actions = {str(p.get("action")) for p in pl.get("plan") or []}
        elif appr.kind == "action":
            actions = {str(pl.get("action"))}
        else:
            continue
        for action in actions:
            key = (action, inc.category, envs[inc.server_id])
            if appr.status == "rejected":
                out[key] = []
            elif appr.kind == "gate":  # an action approved one by one, outside a plan, does not make the plan's gate safe to skip
                out[key].append({"approval_id": appr.id, "incident_id": inc.id, "server_id": inc.server_id, "by": appr.decided_by,
                                 "at": iso(appr.decided_at), "ts": appr.decided_at, "comment": appr.comment or ""})
    for la in s.execute(select(LearnedApproval).where(LearnedApproval.status == "revoked")).scalars():
        key = (la.action, la.category, la.environment)
        if key in out and la.decided_at:
            out[key] = [e for e in out[key] if e["ts"] > la.decided_at]
    return out


def approval_patterns(s: Any, learn_after: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """Actions people approved often enough in a row for the same kind of incident: the proposals not to ask again.
    Counted, not inferred: a dream (or the decision that reaches the count) proposes them as they are."""
    known = {(la.action, la.category, la.environment) for la in
             s.execute(select(LearnedApproval).where(LearnedApproval.status.in_(("proposed", "active", "declined")))).scalars()}
    out = []
    for (action, category, env), approved in sorted(streaks(s, now).items()):
        if risk(action) == "high" or (action, category, env) in known or len(approved) < max(2, learn_after):
            continue
        servers = {e["server_id"] for e in approved}
        out.append({"action": action, "title": ACTIONS.get(action, {}).get("title", action), "category": category, "environment": env,
                    "server_id": next(iter(servers)) if len(servers) == 1 else None, "approvals": len(approved),
                    "approvers": sorted({e["by"] for e in approved}), "evidence": [{k: v for k, v in e.items() if k != "ts"} for e in approved[-12:]]})
    return out


def propose_approvals(s: Any, candidates: list[dict[str, Any]], by: str) -> list[LearnedApproval]:
    out = []
    for c in candidates:
        who = ", ".join(c["approvers"])
        la = LearnedApproval(action=c["action"], category=c["category"], environment=c["environment"], server_id=c["server_id"], status="proposed",
                             approvals=c["approvals"], approvers=c["approvers"], evidence=c["evidence"], proposed_by=by,
                             reason=f"Approved {c['approvals']} times in a row by {who}, not rejected since.")
        s.add(la)
        out.append(la)
    s.flush()
    return out


def hint(s: Any, appr: Approval, inc: Incident | None, policy: dict[str, Any]) -> dict[str, Any] | None:
    """For a plan waiting for a person: how often people approved its actions here, and what rAIden learned from it."""
    if appr.kind != "gate" or appr.stage != "remediate" or inc is None or not policy.get("learn_approvals", True):
        return None
    env = environment_of(s, inc.server_id)
    st = streaks(s)
    items = []
    for action in sorted({str(p.get("action")) for p in (appr.payload or {}).get("plan") or []}):
        approved = st.get((action, inc.category, env), [])
        la = next(iter(_matching(s, action, inc.category, env, inc.server_id, ("proposed", "active", "declined"))), None)
        items.append({"action": action, "title": ACTIONS.get(action, {}).get("title", action), "high_risk": risk(action) == "high",
                      "approved": len(approved), "approvers": sorted({e["by"] for e in approved}),
                      "learned": {"id": la.id, "status": la.status} if la else None})
    return {"category": inc.category, "environment": env, "learn_after": int(policy.get("learn_after", 3)), "actions": items}


# ================================================================================================ what a dream reads
def skill_candidates(s: Any, since_days: int = WINDOW_DAYS) -> list[dict[str, Any]]:
    """Kinds of incident that were fixed at least twice (nanobot writes a skill only for a workflow seen at least twice):
    a playbook to write, or to bring up to date when incidents came in after it was last written."""
    since = utcnow() - timedelta(days=since_days)
    by_cat: dict[str, list[Incident]] = defaultdict(list)
    for inc in s.execute(select(Incident).where(Incident.stage == "resolved", Incident.resolved_at >= since).order_by(Incident.resolved_at)).scalars():
        if any(p.get("status") == "done" for p in inc.plan or []):
            by_cat[inc.category].append(inc)
    skills = {(k.scope or {}).get("category"): k for k in s.execute(select(Knowledge).where(Knowledge.kind == "skill", Knowledge.status == "active")).scalars()}
    out = []
    for cat, incs in sorted(by_cat.items()):
        sk = skills.get(cat)
        if len(incs) < 2 or (sk is not None and not any(i.resolved_at > sk.updated_at for i in incs if i.resolved_at)):
            continue
        out.append({"category": cat, "skill_id": sk.id if sk else None, "incidents": [
            {"id": i.id, "server": i.server_id, "title": i.title, "root_cause": (i.root_cause or "")[:400],
             "actions": [_call(p) for p in i.plan or [] if p.get("status") == "done"],
             "protections": [h.get("title") for h in i.hardening or [] if h.get("status") == "applied"]} for i in incs[-6:]]})
    return out


def _call(p: dict[str, Any]) -> str:
    return f"{p.get('action')}({', '.join(f'{k}={v}' for k, v in (p.get('args') or {}).items())})"


LIMITS = {"decisions": 80, "notes": 40, "statements": 60, "incidents": 40, "dismissed": 30}


def material(s: Any, cursor: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Everything people decided, wrote and said since the cursor, the incidents resolved since, what rAIden knows now and
    the playbooks the incidents support; and the cursor after it. The newest items are kept when there are too many."""
    new = dict(cursor)
    envs: dict[str | None, str] = {}

    def env(sid: str | None) -> str:
        if sid not in envs:
            envs[sid] = environment_of(s, sid)
        return envs[sid]

    decisions = []
    for appr in s.execute(select(Approval).where(Approval.status.in_(("approved", "rejected")), Approval.decided_at > _since(cursor, "decided_at"))
                          .order_by(Approval.decided_at, Approval.id)).scalars():
        new["decided_at"] = stamp(appr.decided_at)
        if not is_person(appr.decided_by):
            continue
        inc = s.get(Incident, appr.incident_id) if appr.incident_id else None
        pl = appr.payload or {}
        sid = inc.server_id if inc else appr.server_id
        decisions.append({"id": appr.id, "decision": appr.status, "by": appr.decided_by, "comment": appr.comment or "", "what": appr.title,
                          "actions": [_call(p) for p in pl.get("plan") or []] or ([_call(pl)] if pl.get("action") else []),
                          "protections": [h.get("title") for h in pl.get("items") or []], "risk": appr.risk, "incident": inc.id if inc else None,
                          "server": sid, "environment": env(sid), "category": inc.category if inc else None, "chat": appr.conversation_id})
    notes = []
    for ev in s.execute(select(IncidentEvent).where(IncidentEvent.id > int(cursor.get("event") or 0), IncidentEvent.kind.in_(PEOPLE_NOTES))
                        .order_by(IncidentEvent.id)).scalars():
        if not is_person(ev.actor) or not ev.body.strip():
            continue
        inc = s.get(Incident, ev.incident_id)
        notes.append({"id": ev.id, "kind": ev.kind, "by": ev.actor, "text": ev.body[:600], "incident": ev.incident_id,
                      "server": inc.server_id if inc else None, "category": inc.category if inc else None})
    top = s.execute(select(IncidentEvent.id).order_by(IncidentEvent.id.desc()).limit(1)).scalar()
    new["event"] = max(int(cursor.get("event") or 0), top or 0)
    statements = []
    for m in s.execute(select(ChatMessage).where(ChatMessage.id > int(cursor.get("chat") or 0), ChatMessage.role == "user").order_by(ChatMessage.id)).scalars():
        new["chat"] = m.id
        conv = s.get(Conversation, m.conversation_id)
        statements.append({"id": m.id, "chat": m.conversation_id, "by": (m.data or {}).get("user") or (conv.user_email if conv else ""),
                           "server": conv.server_id if conv else None, "text": m.content[:500]})
    incidents = []
    for inc in s.execute(select(Incident).where(Incident.stage == "resolved", Incident.resolved_at > _since(cursor, "resolved_at"))
                         .order_by(Incident.resolved_at)).scalars():
        new["resolved_at"] = stamp(inc.resolved_at)
        closer = s.execute(select(IncidentEvent.actor).where(IncidentEvent.incident_id == inc.id, IncidentEvent.kind == "stage", IncidentEvent.stage == "resolved")
                           .order_by(IncidentEvent.id.desc()).limit(1)).scalar()
        # what a person wrote when resolving it is one of the notes; the summary may be rAIden's own words
        incidents.append({"id": inc.id, "server": inc.server_id, "environment": env(inc.server_id), "category": inc.category, "title": inc.title,
                          "root_cause": (inc.root_cause or "")[:400], "summary": (inc.summary or "")[:300],
                          "resolved_by": closer if is_person(closer) else "raiden", "taken_over_by": inc.assignee if is_person(inc.assignee) else None,
                          "actions": [f"{_call(p)}: {p.get('status')}, approved by {p.get('approved_by')}" for p in inc.plan or []],
                          "protections": [f"{h.get('title')}: {h.get('status')}" for h in inc.hardening or []]})
    dismissed = []
    for sug in s.execute(select(Suggestion).where(Suggestion.status == "dismissed", Suggestion.updated_at > _since(cursor, "dismissed_at"))
                         .order_by(Suggestion.updated_at)).scalars():
        new["dismissed_at"] = stamp(sug.updated_at)
        dismissed.append({"id": sug.id, "server": sug.server_id, "title": sug.title, "from": sug.source, "by": (sug.data or {}).get("dismissed_by", "")})
    found = {"decisions": decisions, "notes": notes, "statements": statements, "incidents": incidents, "dismissed": dismissed}
    out: dict[str, Any] = {k: v[-LIMITS[k]:] for k, v in found.items()}
    out["omitted"] = {k: len(v) - LIMITS[k] for k, v in found.items() if len(v) > LIMITS[k]}
    out["knowledge"] = [{"id": k.id, "kind": k.kind, "status": k.status, "title": k.title, "body": k.body[:400], "scope": k.scope or {}, "source": k.source}
                        for k in s.execute(select(Knowledge).where(Knowledge.status.in_(("active", "proposed"))).order_by(Knowledge.id)).scalars()][-150:]
    out["learned_approvals"] = [{"id": la.id, "action": la.action, "category": la.category, "environment": la.environment, "server": la.server_id,
                                 "status": la.status} for la in s.execute(select(LearnedApproval).where(LearnedApproval.status != "revoked")).scalars()]
    out["skill_candidates"] = skill_candidates(s)
    out["counts"] = {k: len(v) for k, v in found.items()}
    return out, new


def is_new(m: dict[str, Any]) -> bool:
    return any(m["counts"].values())


def evidence_index(m: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """What a dream may cite, by (type, id); 'words' marks what a person said or wrote (it may become active at once)."""
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for d in m["decisions"]:
        idx[("decision", str(d["id"]))] = {"by": d["by"], "text": d["comment"] or f"{d['decision']}: {d['what']}", "words": bool(d["comment"].strip()),
                                           "server": d["server"], "category": d["category"]}
    for n in m["notes"]:
        idx[("note", str(n["id"]))] = {"by": n["by"], "text": n["text"], "words": True, "server": n["server"], "category": n["category"]}
    for c in m["statements"]:
        idx[("chat", str(c["id"]))] = {"by": c["by"], "text": c["text"], "words": True, "server": c["server"], "category": None}
    for i in m["incidents"]:
        idx[("incident", str(i["id"]))] = {"by": i["resolved_by"], "text": i["title"], "words": False, "server": i["server"], "category": i["category"]}
    for c in m["skill_candidates"]:
        for i in c["incidents"]:
            idx.setdefault(("incident", str(i["id"])), {"by": "raiden", "text": i["title"], "words": False, "server": i["server"], "category": c["category"]})
    for x in m["dismissed"]:
        idx[("suggestion", str(x["id"]))] = {"by": x["by"] or "a person", "text": f"dismissed: {x['title']}", "words": False, "server": x["server"], "category": None}
    return idx
