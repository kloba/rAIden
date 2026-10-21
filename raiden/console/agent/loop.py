"""One durable tool-calling loop for incidents, chat and routines.

The conversation lives in AgentSession.messages and is saved after every turn. When a tool call has to wait for a person
(an approval), the loop stops with the call still open; resume() appends the decision as that call's result and carries on,
minutes or days later, after a restart if need be.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..ai.router import NoEndpoint
from ..db import utcnow
from ..models import AgentSession
from .tools import ToolContext, ToolOutcome, run_tool

CONTEXT_LIMIT_CHARS = 300_000

EventFn = Callable[[str, dict[str, Any]], "Awaitable[None] | None"]


@dataclass
class LoopResult:
    status: str  # done | paused | answered | max_turns | no_endpoint | error
    text: str = ""
    done: dict[str, Any] | None = None
    error: str = ""


def prune(messages: list[dict[str, Any]]) -> None:
    total = sum(len(str(m.get("content") or "")) for m in messages)
    if total <= CONTEXT_LIMIT_CHARS:
        return
    for m in messages[2:-12]:
        if m.get("role") == "tool" and len(str(m.get("content"))) > 300:
            total -= len(m["content"]) - 30
            m["content"] = "[earlier output elided]"
            if total <= CONTEXT_LIMIT_CHARS:
                return


class AgentLoop:
    def __init__(self, rt: Any, session_id: str, purpose: str, ctx: ToolContext, on_event: EventFn | None = None) -> None:
        self.rt = rt
        self.session_id = session_id
        self.purpose = purpose
        self.ctx = ctx
        self.on_event = on_event

    # ------------------------------------------------------------------------------------------- persistence
    def load(self) -> list[dict[str, Any]]:
        with self.rt.db.session() as s:
            sess = s.get(AgentSession, self.session_id)
            return list(sess.messages or []) if sess else []

    def save(self, messages: list[dict[str, Any]], *, reply: Any = None, ep: Any = None, status: str | None = None,
             pending: dict[str, Any] | None | bool = False, tool_calls: int = 0) -> None:
        with self.rt.db.session() as s:
            sess = s.get(AgentSession, self.session_id)
            if sess is None:
                return
            sess.messages = list(messages)
            if reply is not None:
                sess.turns += 1
                sess.tokens_in += reply.tokens_in
                sess.tokens_out += reply.tokens_out
                sess.tokens_cached += reply.tokens_cached
                if ep is not None:
                    sess.cost_usd = round(sess.cost_usd + self.rt.router.cost_of(ep, reply), 6)
                    sess.endpoint_id = ep.id
                    sess.model = reply.model or ep.model
            sess.tool_calls += tool_calls
            if status:
                sess.status = status
            if pending is not False:
                sess.pending = pending  # type: ignore[assignment]
            sess.updated_at = utcnow()

    async def emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        res = self.on_event(kind, data)
        if inspect.isawaitable(res):
            await res

    # ------------------------------------------------------------------------------------------- the loop
    async def run(self, tools: list[dict[str, Any]], max_turns: int, nudge: bool = True) -> LoopResult:
        messages = self.load()
        nudged = False
        self.save(messages, status="running")
        for _ in range(max_turns):
            try:
                reply, ep = await self.rt.router.complete(self.purpose, messages, tools, server_id=self.ctx.server_id)
            except NoEndpoint as exc:
                self.save(messages, status="idle")
                return LoopResult("no_endpoint", error=str(exc))
            entry: dict[str, Any] = {"role": "assistant", "content": reply.text or ""}
            if reply.tool_calls:
                entry["tool_calls"] = [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                                       for tc in reply.tool_calls]
            if reply.raw_items:
                entry["raw_items"] = reply.raw_items
            messages.append(entry)
            self.save(messages, reply=reply, ep=ep)
            await self.emit("usage", {"tokens_in": reply.tokens_in, "tokens_out": reply.tokens_out, "tokens_cached": reply.tokens_cached,
                                      "cost_usd": self.rt.router.cost_of(ep, reply), "model": reply.model or ep.model, "endpoint": ep.name,
                                      "simulated": reply.simulated})
            if reply.text.strip():
                await self.emit("thought", {"text": reply.text.strip(), "final": not reply.tool_calls})
            if not reply.tool_calls:
                if not nudge or nudged:
                    self.save(messages, status="idle")
                    return LoopResult("answered", text=reply.text)
                nudged = True
                messages.append({"role": "user", "content": "You replied without calling a tool. Continue with the tools, or call the tool that ends this stage."})
                continue
            res = await self._execute(messages, reply.tool_calls)
            if res is not None:
                return res
            prune(messages)
            self.save(messages)
        self.save(messages, status="idle")
        return LoopResult("max_turns", error=f"no conclusion after {max_turns} turns")

    async def _execute(self, messages: list[dict[str, Any]], calls: list[dict[str, Any]]) -> LoopResult | None:
        done: dict[str, Any] | None = None
        executed = 0
        for i, tc in enumerate(calls):
            name, tc_id = tc["name"], tc["id"]
            if done is not None:
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": "skipped: the stage already ended"})
                continue
            try:
                args = json.loads(tc.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
            except (json.JSONDecodeError, ValueError):
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": "ERROR: tool arguments were not a valid JSON object"})
                continue
            await self.emit("tool_call", {"id": tc_id, "name": name, "args": args})
            out: ToolOutcome = await run_tool(self.ctx, name, args, tc_id)
            executed += 1
            if out.pause:
                self.save(messages, status="paused", pending={"tool_call": tc, "remaining": calls[i + 1:], **out.pause}, tool_calls=executed)
                await self.emit("paused", {"id": tc_id, "name": name, "args": args, **out.pause})
                return LoopResult("paused")
            messages.append({"role": "tool", "tool_call_id": tc_id, "content": out.text})
            await self.emit("tool_result", {"id": tc_id, "name": name, "args": args, "ok": out.ok, "preview": out.preview, "ms": out.elapsed_ms})
            if out.done:
                done = out.done
        self.save(messages, tool_calls=executed)
        if done is not None:
            self.save(messages, status="idle")
            return LoopResult("done", done=done)
        return None

    async def resume(self, result_text: str, tools: list[dict[str, Any]], max_turns: int, nudge: bool = True) -> LoopResult:
        """Give the paused tool call its result (the person's decision) and continue."""
        with self.rt.db.session() as s:
            sess = s.get(AgentSession, self.session_id)
            pending = dict(sess.pending or {}) if sess else {}
        messages = self.load()
        tc = pending.get("tool_call")
        if tc:
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_text})
            await self.emit("tool_result", {"id": tc["id"], "name": tc["name"], "args": json.loads(tc.get("arguments") or "{}"), "ok": True,
                                            "preview": {"decision": result_text}, "ms": 0})
        self.save(messages, pending=None, status="running")
        remaining = pending.get("remaining") or []
        if remaining:
            res = await self._execute(messages, remaining)
            if res is not None:
                return res
        return await self.run(tools, max_turns, nudge=nudge)
