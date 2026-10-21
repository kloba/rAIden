"""Dreams: once a night rAIden turns what happened into what it knows (nanobot's Dream, for an on-call DBA).

A dream reads what is new since the last one: people's decisions on rAIden's plans and the reasons they gave, their notes
on incidents, what they said in chats, the incidents resolved and the suggestions dismissed. Then it makes the smallest
honest change that keeps rAIden's knowledge coherent:

* actions people approved several times in a row become proposals not to ask again (counted, not inferred);
* people's words become rules, facts and preferences, with the scope they were said for;
* kinds of incident fixed at least twice get a playbook (a skill), kept up to date;
* a correction replaces what it corrects.

What a person said or wrote becomes active at once; what rAIden only inferred waits for a person on the Knowledge page.
Every dream keeps a journal of its changes with the version before, so a person can undo it. With nothing new to read, a
dream makes no model call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from . import fmt, learning
from .agent.loop import AgentLoop
from .agent.prompts import SYSTEM_DREAM
from .agent.tools import CATEGORIES, ToolContext, ToolOutcome, tools_for
from .db import utcnow
from .drivers.base import ACTIONS
from .models import AgentSession, Dream, Knowledge, LearnedApproval, Server
from .policy import ENVIRONMENTS, merged
from .services import audit, get_setting, put_setting

log = logging.getLogger("raiden.console.dreams")

MAX_TURNS = 14
CURSOR = "dream_cursor"
READ_LABELS = {"decisions": "decision", "notes": "note", "statements": "chat message", "incidents": "resolved incident", "dismissed": "dismissed suggestion"}


class DreamBusy(RuntimeError):
    """A dream is already running."""


def _approval_change(op: str, la: LearnedApproval) -> dict[str, Any]:
    return {"op": op, "type": "approval", "id": la.id, "kind": "learned approval", "title": learning.describe_approval(la), "status": la.status,
            "before": None, "after": None}


def _knowledge_change(op: str, k: Knowledge, before: dict[str, Any] | None) -> dict[str, Any]:
    return {"op": op, "type": "knowledge", "id": k.id, "kind": k.kind, "title": k.title, "status": k.status, "before": before,
            "after": learning.snapshot(k)}


def read_text(counts: dict[str, int]) -> str:
    parts = [fmt.plural(n, READ_LABELS[k]) for k, n in counts.items() if n]
    return ", ".join(parts) if parts else "nothing new"


def report(read: dict[str, int], changes: list[dict[str, Any]]) -> str:
    verbs = {"add": "added", "update": "updated", "retire": "retired", "propose": "proposed not to ask again:"}
    lines = [f"**Read:** {read_text(read)}."]
    if changes:
        lines.append("**Changed:**")
        for c in changes:
            what = c["title"] if c["type"] == "approval" else f"{c['kind']} #{c['id']} “{c['title']}”" + (" (waits for a person)" if c["status"] == "proposed" else "")
            lines.append(f"- {verbs.get(c['op'], c['op'])} {what}")
    else:
        lines.append("**Changed:** nothing.")
    return "\n".join(lines)


class Dreamer:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.lock.locked()

    async def run(self, run_id: int | None = None, by: str = "schedule") -> dict[str, Any]:
        if self.lock.locked():
            raise DreamBusy("rAIden is already dreaming")
        async with self.lock:
            return await self._dream(run_id, by)

    # ================================================================================================ one dream
    async def _dream(self, run_id: int | None, by: str) -> dict[str, Any]:
        with self.rt.db.session() as s:
            policy = merged(get_setting(s, "autonomy"))
            cursor = get_setting(s, CURSOR)
            m, new_cursor = learning.material(s, cursor)
            d = Dream(run_id=run_id, triggered_by=by, cursor=cursor, read=m["counts"], status="running")
            s.add(d)
            s.flush()
            did = d.id
            changes: list[dict[str, Any]] = []
            if policy.get("learn_approvals", True):  # counted, not inferred: no model needed
                for la in learning.propose_approvals(s, learning.approval_patterns(s, int(policy.get("learn_after", 3))), by=f"dream #{did}"):
                    changes.append(_approval_change("propose", la))
                    # the model sees tonight's proposals too, so it does not ask for them again
                    m["learned_approvals"].append({"id": la.id, "action": la.action, "category": la.category, "environment": la.environment,
                                                   "server": la.server_id, "status": la.status})
            d.changes = changes
        self.rt.bus.publish("knowledge", dream=did, status="running")
        if not learning.is_new(m):
            return self._finish(did, new_cursor, changes, m["counts"], "Nothing new to read since the last dream" +
                                (f"; proposed not to ask again for {', '.join(c['title'] for c in changes)}." if changes else "."))
        idx = learning.evidence_index(m)
        brief = {k: v for k, v in m.items() if k not in ("counts", "omitted") and v}
        if m["omitted"]:
            brief["omitted_older_items"] = m["omitted"]
        since = cursor.get("decided_at") or cursor.get("resolved_at")
        prompt = (f"Material since your last dream{f' ({since[:16]} UTC)' if since else ''}: {read_text(m['counts'])}. JSON:\n"
                  + json.dumps(brief, default=str))
        with self.rt.db.session() as s:
            sess = AgentSession(id=uuid.uuid4().hex, kind="dream", status="idle",
                                messages=[{"role": "system", "content": SYSTEM_DREAM}, {"role": "user", "content": prompt}])
            s.add(sess)
            sess_id = sess.id
        box: dict[str, Any] = {}

        async def special(name: str, args: dict[str, Any], _tc: str) -> ToolOutcome:
            try:
                with self.rt.db.session() as s:
                    text = self._tool(s, did, name, args, m, idx, changes, box)
                    s.get(Dream, did).changes = list(changes)
            except (ValueError, KeyError, TypeError) as exc:
                return ToolOutcome(text=f"ERROR: {exc}. Fix it and call {name} again, or leave it.", ok=False)
            if name == "finish_dream":
                return ToolOutcome(text="recorded", done={"tool": name, "args": args})
            self.rt.bus.publish("knowledge", dream=did)
            return ToolOutcome(text=text)

        loop = AgentLoop(self.rt, sess_id, "routines", ToolContext(self.rt, None, session_id=sess_id, actor=f"dream #{did}", on_special=special))
        res = await loop.run(tools_for("dream"), MAX_TURNS)
        with self.rt.db.session() as s:
            sess = s.get(AgentSession, sess_id)
            sess.status = "done"
            usage = (sess.tokens_in, sess.tokens_out, sess.cost_usd, sess.model)
        if res.status == "no_endpoint":  # nothing was read: the next dream reads the same material
            return self._finish(did, None, changes, m["counts"], f"Only counted: the AI pass needs an endpoint for routines ({res.error}).", usage=usage)
        if res.status != "done":
            return self._finish(did, new_cursor, changes, m["counts"], f"The dream stopped early ({res.error or res.status}); its changes so far are kept.",
                                usage=usage, status="error")
        return self._finish(did, new_cursor, changes, m["counts"], str(box.get("summary") or "").strip() or "Done.", usage=usage)

    def _finish(self, did: int, cursor: dict[str, Any] | None, changes: list[dict[str, Any]], read: dict[str, int], summary: str,
                usage: tuple[int, int, float, str] = (0, 0, 0.0, ""), status: str = "done") -> dict[str, Any]:
        with self.rt.db.session() as s:
            d = s.get(Dream, did)
            d.finished_at, d.status, d.summary, d.changes = utcnow(), status, summary, changes
            d.tokens_in, d.tokens_out, d.cost_usd, d.model = usage
            if cursor is not None:
                put_setting(s, CURSOR, cursor, by=f"dream #{did}")
            audit(s, "raiden", "dream", f"dream #{did}", "allow" if status == "done" else "error", actor_type="raiden", read=read, changes=len(changes),
                  summary=summary[:300])
        self.rt.bus.publish("knowledge", dream=did, status=status)
        return {"id": did, "status": status, "summary": summary, "report": report(read, changes), "changes": changes,
                "tokens_in": usage[0], "tokens_out": usage[1]}

    # ================================================================================================ the dream's tools
    def _evidence(self, idx: dict[tuple[str, str], dict[str, Any]], items: Any) -> list[dict[str, Any]]:
        out = []
        for e in items or []:
            key = (str(e.get("type")), str(e.get("id")))
            hit = idx.get(key)
            if hit is None:
                raise ValueError(f"{key[0]} {key[1]} is not in the material")
            out.append({"type": key[0], "id": key[1], "by": hit["by"], "text": str(hit["text"])[:300], "words": hit["words"]})
        if not out:
            raise ValueError("cite the evidence: the ids of the decisions, notes, chat messages, incidents or suggestions it comes from")
        return out

    def _tool(self, s: Any, did: int, name: str, args: dict[str, Any], m: dict[str, Any], idx: dict[tuple[str, str], dict[str, Any]],
              changes: list[dict[str, Any]], box: dict[str, Any]) -> str:
        me = f"dream #{did}"
        if name == "finish_dream":
            box["summary"] = str(args.get("summary") or "")
            return "recorded"
        if name == "add_instruction":
            ev = self._evidence(idx, args.get("evidence"))
            if args.get("kind") not in learning.INSTRUCTION_KINDS:
                raise ValueError(f"kind must be one of {', '.join(learning.INSTRUCTION_KINDS)}")
            status = "active" if any(e["words"] for e in ev) else "proposed"
            k, new = learning.remember(s, kind=args["kind"], title=str(args.get("title") or ""), body=str(args.get("body") or ""), scope=args.get("scope"),
                                       source="dream", author=me, evidence=ev, retention=str(args.get("retention") or "durable"), status=status)
            if not new:
                return f"already known as #{k.id}; if it changed, use update_instruction"
            changes.append(_knowledge_change("add", k, None))
            return f"added #{k.id}" + ("" if status == "active" else " as a proposal: a person confirms it, since nobody said it in words")
        if name in ("update_instruction", "retire_instruction"):
            k = s.get(Knowledge, int(args["id"]))
            if k is None or k.kind not in learning.INSTRUCTION_KINDS or k.status == "retired":
                raise ValueError(f"no rule, fact or preference #{args['id']} to change")
            ev = self._evidence(idx, args.get("evidence"))
            if learning.people_own(k) and not any(e["words"] for e in ev):
                raise ValueError(f"#{k.id} was taught or confirmed by a person: only a person's own words can correct it")
            before = learning.revise(k, me, str(args.get("why") or ""))
            if name == "retire_instruction":
                k.status = "retired"
            else:
                if args.get("title"):
                    k.title = str(args["title"]).strip()[:300]
                if "body" in args:
                    k.body = str(args.get("body") or "").strip()[:6000]
                if args.get("scope") is not None:
                    k.scope = learning.check_scope(s, args["scope"])
                if args.get("retention") in learning.RETENTION:
                    k.retention, k.expires_at = args["retention"], learning.expiry(args["retention"])
            k.evidence = list(k.evidence or []) + ev
            changes.append(_knowledge_change("retire" if name == "retire_instruction" else "update", k, before))
            return f"{'retired' if name == 'retire_instruction' else 'updated'} #{k.id}"
        if name == "write_skill":
            cand = next((c for c in m["skill_candidates"] if c["category"] == args.get("category")), None)
            if cand is None:
                raise ValueError(f"no playbook to write for {args.get('category')!r}: a skill needs a category in skill_candidates")
            ev = self._evidence(idx, args.get("evidence"))
            title, body = str(args.get("title") or "").strip()[:300], str(args.get("body") or "").strip()[:6000]
            if not title or not body:
                raise ValueError("a playbook needs a title and a body")
            sk = s.get(Knowledge, cand["skill_id"]) if cand["skill_id"] else None
            if sk is not None and learning.people_own(sk):
                sk.evidence = (list(sk.evidence or []) + [e for e in ev if e not in (sk.evidence or [])])[-20:]
                sk.updated_at = utcnow()  # seen: these incidents do not make it a candidate again
                return f"skill #{sk.id} is the team's own: the new incidents are added to its evidence, its text stays as they wrote it"
            if sk is not None:
                before = learning.revise(sk, me, "new incidents of this kind")
                sk.title, sk.body = title, body
                sk.evidence = (list(sk.evidence or []) + [e for e in ev if e not in (sk.evidence or [])])[-20:]
                changes.append(_knowledge_change("update", sk, before))
                cand["skill_id"] = sk.id
                return f"updated skill #{sk.id}"
            sk, _ = learning.remember(s, kind="skill", title=title, body=body, scope={"category": cand["category"]}, source="dream", author=me, evidence=ev)
            changes.append(_knowledge_change("add", sk, None))
            cand["skill_id"] = sk.id
            return f"wrote skill #{sk.id}"
        if name == "propose_learned_approval":
            action, category, env, server = str(args.get("action")), str(args.get("category")), str(args.get("environment")), args.get("server") or None
            if action not in ACTIONS or learning.risk(action) == "high":
                raise ValueError(f"{action} cannot be learned: high-risk actions always wait for a person")
            if category not in CATEGORIES or env not in ENVIRONMENTS:
                raise ValueError("unknown category or environment")
            if server and s.get(Server, server) is None:
                raise ValueError(f"unknown server {server!r}")
            ev = self._evidence(idx, args.get("evidence"))
            if not any(e["words"] for e in ev):
                raise ValueError("only a person's own words can ask for this (what people approved often enough is proposed without you)")
            if learning._matching(s, action, category, env, server, ("proposed", "active", "declined")):
                raise ValueError("already proposed, accepted or declined")
            la = LearnedApproval(action=action, category=category, environment=env, server_id=server, status="proposed", approvals=0,
                                 approvers=sorted({e["by"] for e in ev}), evidence=[{"by": e["by"], "comment": e["text"], "type": e["type"], "id": e["id"]} for e in ev],
                                 proposed_by=me, reason=str(args.get("why") or "")[:1000])
            s.add(la)
            s.flush()
            changes.append(_approval_change("propose", la))
            return f"proposed learned approval #{la.id}: a person accepts it on the Knowledge page"
        raise ValueError(f"unknown tool {name}")

    # ================================================================================================ undo
    def undo(self, dream_id: int, user: str) -> dict[str, Any]:
        """Take a dream's changes back, newest first. What a person changed since stays as they left it."""
        with self.rt.db.session() as s:
            d = s.get(Dream, dream_id)
            if d is None:
                raise KeyError("no such dream")
            if d.status not in ("done", "error"):
                raise ValueError(f"this dream is {d.status}")
            kept = []
            for c in reversed(d.changes or []):
                if c["type"] == "knowledge":
                    k = s.get(Knowledge, c["id"])
                    if k is None:
                        continue
                    if learning.snapshot(k) != c["after"]:
                        kept.append(c["title"])
                        continue
                    if c["op"] == "add":
                        learning.revise(k, user, f"dream #{d.id} undone")
                        k.status = "retired"
                    else:
                        learning.restore(k, c["before"], user, f"dream #{d.id} undone")
                elif c["type"] == "approval":
                    la = s.get(LearnedApproval, c["id"])
                    if la is None:
                        continue
                    if la.status != "proposed":
                        kept.append(c["title"])
                        continue
                    s.delete(la)
            d.status, d.undone_by, d.undone_at = "undone", user, utcnow()
            audit(s, user, "dream.undo", f"dream #{d.id}", "allow", kept=kept)
        self.rt.bus.publish("knowledge", dream=dream_id, status="undone")
        return {"id": dream_id, "status": "undone", "kept": kept}
