"""Ad-hoc investigation in chat: the same tools as the incident pipeline, read-only, streamed step by step to the browser.
Changes are proposed and wait for the person's approval in the conversation."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncIterator

from sqlalchemy import select

from . import learning
from .agent.loop import AgentLoop
from .agent.prompts import PLATFORM_NOTES, SYSTEM_CHAT
from .agent.tools import ToolContext, ToolOutcome, server_brief, tools_for
from .db import utcnow
from .drivers.base import ACTIONS, DriverError
from .models import AgentSession, Approval, ChatMessage, Conversation, Server
from .services import audit, create_incident

MAX_TURNS = 10


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


class ChatService:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.locks: dict[str, asyncio.Lock] = {}

    def _session(self, s: Any, conv: Conversation, user: str) -> AgentSession:
        if conv.session_id:
            sess = s.get(AgentSession, conv.session_id)
            if sess is not None:
                return sess
        srv = s.get(Server, conv.server_id) if conv.server_id else None
        system = SYSTEM_CHAT.format(user=user, server=server_brief(s, conv.server_id) if conv.server_id else "none: use list_servers and fleet_metric, or ask which server",
                                    platform_note=PLATFORM_NOTES.get(srv.platform, "") if srv else "",
                                    knowledge=learning.prompt_block(s, conv.server_id, srv.environment if srv else None))
        sess = AgentSession(id=uuid.uuid4().hex, kind="chat", conversation_id=conv.id, server_id=conv.server_id, messages=[{"role": "system", "content": system}])
        s.add(sess)
        conv.session_id = sess.id
        return sess

    async def stream(self, conv_id: str, user: str, content: str) -> AsyncIterator[str]:
        lock = self.locks.setdefault(conv_id, asyncio.Lock())
        if lock.locked():
            yield sse("error", {"message": "rAIden is still answering the previous message"})
            return
        async with lock:
            with self.rt.db.session() as s:
                conv = s.get(Conversation, conv_id)
                if conv is None:
                    yield sse("error", {"message": "conversation not found"})
                    return
                sess = self._session(s, conv, user)
                sess.messages = list(sess.messages) + [{"role": "user", "content": content}]
                msg = ChatMessage(conversation_id=conv_id, role="user", content=content, data={"user": user})
                s.add(msg)
                if conv.title in ("", "New investigation") or conv.title.endswith(": investigation"):  # a placeholder: the first question names it
                    conv.title = content.strip().splitlines()[0][:70]
                conv.updated_at = utcnow()
                s.flush()
                sess_id, server_id = sess.id, conv.server_id
                yield sse("message", {"id": msg.id, "role": "user", "content": content})
            queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
            task = asyncio.create_task(self._run(conv_id, sess_id, server_id, user, queue, resume_text=None))
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield sse(*item)
            await task

    async def _run(self, conv_id: str, sess_id: str, server_id: str | None, user: str, queue: asyncio.Queue, resume_text: str | None) -> None:
        usage = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "model": "", "simulated": False}

        async def on_event(kind: str, data: dict[str, Any]) -> None:
            if kind == "usage":
                for k in ("tokens_in", "tokens_out"):
                    usage[k] += data[k]
                usage["cost_usd"] += data["cost_usd"]
                usage["model"], usage["simulated"] = data["model"], data["simulated"]
                return
            if kind == "tool_call":
                await queue.put(("step", {"id": data["id"], "name": data["name"], "args": data["args"]}))
            elif kind == "tool_result":
                if data["name"] in ("propose_action",):
                    return
                with self.rt.db.session() as s:
                    m = ChatMessage(conversation_id=conv_id, role="tool", content="", data={"name": data["name"], "args": data["args"], "ok": data["ok"],
                                                                                           "ms": data["ms"], "preview": _trim(data["preview"])})
                    s.add(m)
                    s.flush()
                    mid = m.id
                await queue.put(("tool", {"id": mid, "call_id": data["id"], "name": data["name"], "args": data["args"], "ok": data["ok"], "ms": data["ms"],
                                          "preview": _trim(data["preview"])}))
            elif kind == "thought" and data.get("final"):
                with self.rt.db.session() as s:
                    m = ChatMessage(conversation_id=conv_id, role="assistant", content=data["text"], data=dict(usage))
                    s.add(m)
                    s.flush()
                    mid = m.id
                await queue.put(("message", {"id": mid, "role": "assistant", "content": data["text"], "usage": dict(usage)}))
            elif kind == "paused":
                await queue.put(("approval", {"approval_id": data["approval_id"]}))

        async def special(name: str, args: dict[str, Any], tc_id: str) -> ToolOutcome:
            if name == "propose_action":
                action, act_args = str(args.get("action", "")), dict(args.get("args") or {})
                spec = ACTIONS.get(action)
                if spec is None or not server_id:
                    return ToolOutcome(text="ERROR: unknown action or no server in focus", ok=False)
                with self.rt.db.session() as s:
                    appr = Approval(conversation_id=conv_id, session_id=sess_id, server_id=server_id, kind="action", stage="chat",
                                    title=f"{spec['title']}: " + ", ".join(f"{k}={v}" for k, v in act_args.items()), summary=str(args.get("rationale", "")),
                                    risk=spec["risk"], payload={"action": action, "args": act_args, "rationale": args.get("rationale", ""), "tool_call_id": tc_id})
                    s.add(appr)
                    s.flush()
                    s.add(ChatMessage(conversation_id=conv_id, role="approval", content=appr.title, data={"approval_id": appr.id, "risk": spec["risk"],
                                                                                                          "action": action, "args": act_args,
                                                                                                          "rationale": args.get("rationale", "")}))
                    aid = appr.id
                self.rt.bus.publish("approval", id=aid)
                return ToolOutcome(text="waiting for the person's approval", pause={"approval_id": aid})
            if name == "open_incident":
                with self.rt.db.session() as s:
                    inc = create_incident(s, server_id=server_id, title=str(args.get("title", "Opened from chat")), severity=str(args.get("severity", "sev3")),
                                          category="other", source="chat", source_ref=conv_id, fingerprint=f"chat:{conv_id}:{uuid.uuid4().hex[:6]}",
                                          summary=str(args.get("summary", "")), actor=user)
                    conv = s.get(Conversation, conv_id)
                    conv.incident_id = inc.id
                    iid = inc.id
                    s.add(ChatMessage(conversation_id=conv_id, role="tool", content="", data={"name": "open_incident", "args": args, "ok": True, "ms": 0,
                                                                                               "preview": {"incident_id": iid}}))
                self.rt.bus.publish("incident", id=iid)
                await queue.put(("incident", {"incident_id": iid}))
                return ToolOutcome(text=f"opened {iid}; the incident pipeline takes over")
            if name == "remember":
                with self.rt.db.session() as s:
                    said = s.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv_id, ChatMessage.role == "user")
                                     .order_by(ChatMessage.id.desc()).limit(1)).scalar_one_or_none()
                    try:
                        k, new = learning.remember(s, kind=str(args.get("kind", "")), title=str(args.get("title", "")), body=str(args.get("body") or ""),
                                                   scope=args.get("scope") or {}, source="person", author=user,
                                                   retention="ephemeral" if args.get("retention") == "ephemeral" else "durable",
                                                   evidence=[{"type": "chat", "id": str(said.id) if said else "", "chat": conv_id, "by": user,
                                                              "text": said.content[:300] if said else ""}])
                    except ValueError as exc:
                        return ToolOutcome(text=f"ERROR: {exc}", ok=False)
                    kid, scope = k.id, learning.scope_text(k.scope)
                    if new:
                        audit(s, user, "knowledge.create", f"knowledge #{kid}", "allow", via="chat", conversation=conv_id, title=k.title)
                self.rt.bus.publish("knowledge")
                return ToolOutcome(text=(f"remembered as #{kid} for {scope}" if new else f"already known as #{kid}"),
                                   preview={"knowledge_id": kid, "title": str(args.get("title", "")), "scope": scope, "new": new})
            return ToolOutcome(text=f"ERROR: {name} is not available in chat", ok=False)

        ctx = ToolContext(self.rt, server_id, conversation_id=conv_id, session_id=sess_id, actor=user, on_special=special)
        loop = AgentLoop(self.rt, sess_id, "chat", ctx, on_event=on_event)
        try:
            tools = tools_for("chat", fleet=True)
            res = await (loop.resume(resume_text, tools, MAX_TURNS, nudge=False) if resume_text is not None else loop.run(tools, MAX_TURNS, nudge=False))
            if res.status in ("no_endpoint", "max_turns", "error"):
                text = ("No AI endpoint is available for chat: " + res.error) if res.status == "no_endpoint" else (res.error or "rAIden stopped without an answer")
                with self.rt.db.session() as s:
                    s.add(ChatMessage(conversation_id=conv_id, role="error", content=text))
                await queue.put(("error", {"message": text}))
        except Exception as exc:  # noqa: BLE001
            await queue.put(("error", {"message": f"{exc.__class__.__name__}: {exc}"}))
        finally:
            await queue.put(("done", {"usage": usage}))
            await queue.put(None)
            self.rt.bus.publish("chat", conversation_id=conv_id)

    async def after_decision(self, approval_id: int) -> None:
        """A person decided on an action proposed in chat: run it (if approved) and let rAIden continue the conversation."""
        with self.rt.db.session() as s:
            appr = s.get(Approval, approval_id)
            if appr is None or not appr.conversation_id:
                return
            conv = s.get(Conversation, appr.conversation_id)
            conv_id, sess_id, server_id, status, by, comment, payload = conv.id, appr.session_id, appr.server_id, appr.status, appr.decided_by, appr.comment, dict(appr.payload)
        if status == "approved":
            try:
                res = await self.rt.driver(server_id).execute_action(payload["action"], payload["args"])
                text, ok = f"APPROVED by {by} and executed: {res.get('message', 'done')}", True
            except DriverError as exc:
                text, ok = f"APPROVED by {by} but it failed: {exc}", False
            with self.rt.db.session() as s:
                audit(s, "raiden", f"action.{payload['action']}", server_id or "", "allow" if ok else "error", actor_type="raiden", conversation=conv_id,
                      args=payload["args"], approved_by=by, result=text[:300])
        else:
            text = f"REJECTED by {by}" + (f": {comment}" if comment else "") + ". Do not retry; suggest another route."
        with self.rt.db.session() as s:
            s.add(ChatMessage(conversation_id=conv_id, role="tool", content="", data={"name": "decision", "args": payload, "ok": status == "approved", "ms": 0,
                                                                                       "preview": {"decision": text}}))
        lock = self.locks.setdefault(conv_id, asyncio.Lock())
        async with lock:
            queue: asyncio.Queue = asyncio.Queue()
            await self._run(conv_id, sess_id, server_id, by or "", queue, resume_text=text)


def _trim(p: Any) -> Any:
    if isinstance(p, dict) and "rows" in p:
        out = {k: v for k, v in p.items() if k != "rows"}
        out["rows"] = p["rows"][:30]
        if len(p["rows"]) > 30:
            out["rows_omitted"] = len(p["rows"]) - 30
        return out
    return p


async def list_messages(rt: Any, conv_id: str) -> list[dict[str, Any]]:
    with rt.db.session() as s:
        rows = s.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv_id).order_by(ChatMessage.id)).scalars()
        return [{"id": m.id, "role": m.role, "content": m.content, "data": m.data, "created_at": m.created_at.isoformat() + "Z"} for m in rows]
