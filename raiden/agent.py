"""The tool-calling loop: one session = one incident (or one evolve run)."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .events import EventLog, JsonlLog, utcnow_iso
from .llm import AzureLLM
from .tools import TOOL_SPECS, ToolBox

CONTEXT_LIMIT_CHARS = 380_000


@dataclass
class SessionResult:
    resolution: str | None = None
    summary: str = ""
    turns: int = 0
    tool_calls: int = 0
    denied_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    error: str | None = None
    timed_out: bool = False


def _prune(messages: list[dict]) -> None:
    """Keep the context bounded: elide the oldest tool outputs first (system + task + last 10 messages stay)."""
    total = sum(len(str(m.get("content") or "")) for m in messages)
    if total <= CONTEXT_LIMIT_CHARS:
        return
    for m in messages[2:-10]:
        if m.get("role") == "tool" and len(str(m.get("content"))) > 200:
            total -= len(m["content"]) - 24
            m["content"] = "[earlier output elided]"
            if total <= CONTEXT_LIMIT_CHARS:
                return


async def run_session(
    cfg: Config,
    llm: AzureLLM,
    toolbox: ToolBox,
    system: str,
    user: str,
    *,
    max_turns: int,
    timeout_s: float,
    transcript_path: Path,
    events: EventLog,
    kind: str,
) -> SessionResult:
    res = SessionResult()
    t0 = time.monotonic()
    transcript = JsonlLog(transcript_path)
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    nudged = False
    for turn in range(1, max_turns + 1):
        remaining = timeout_s - (time.monotonic() - t0)
        if remaining <= 0:
            res.timed_out = True
            break
        try:
            resp = await asyncio.wait_for(llm.complete(messages, TOOL_SPECS), timeout=max(45.0, remaining))
        except asyncio.TimeoutError:
            res.timed_out = True
            break
        except Exception as exc:
            res.error = f"{exc.__class__.__name__}: {exc}"
            break
        res.turns = turn
        usage = getattr(resp, "usage", None)
        if usage:
            res.prompt_tokens += usage.prompt_tokens or 0
            res.completion_tokens += usage.completion_tokens or 0
            details = getattr(usage, "prompt_tokens_details", None)
            res.cached_tokens += (getattr(details, "cached_tokens", 0) or 0) if details else 0
        msg = resp.choices[0].message
        entry: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            entry["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        raw_items = getattr(msg, "raw_items", None)
        if raw_items:  # Responses API: the model's own output items (reasoning included) go back with the next request
            entry["raw_items"] = raw_items
        messages.append(entry)
        transcript.write(
            {
                "t": utcnow_iso(), "turn": turn, "role": "assistant", "text": (msg.content or "")[:3000],
                "calls": [{"name": tc.function.name, "args": (tc.function.arguments or "")[:600]} for tc in (msg.tool_calls or [])],
            }
        )
        if not msg.tool_calls:
            if toolbox.finished:
                break
            if not nudged:
                nudged = True
                messages.append({"role": "user", "content": "You replied without calling a tool. If you are done, call `finish` now; otherwise continue with tool calls."})
                continue
            res.summary = (msg.content or "")[:500]
            break
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                result = await toolbox.call(tc.function.name, args)
            except json.JSONDecodeError:
                result = "ERROR: tool arguments were not valid JSON"
                args = {}
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            denied = result.startswith("DENIED")
            transcript.write({"t": utcnow_iso(), "turn": turn, "role": "tool", "name": tc.function.name, "denied": denied, "output": result[:700]})
            events.emit(
                "agent_tool", incident=toolbox.incident_id, session=kind, tool=tc.function.name,
                summary=toolbox._summary(tc.function.name, args)[:160], denied=denied,
            )
        if toolbox.finished:
            break
        _prune(messages)

    res.tool_calls, res.denied_calls = toolbox.calls, toolbox.denied
    res.duration_s = time.monotonic() - t0
    if toolbox.finished:
        res.resolution, res.summary = toolbox.finished[0], toolbox.finished[1]
    res.cost_usd = round(res.prompt_tokens * cfg.price_in_per_m / 1e6 + res.completion_tokens * cfg.price_out_per_m / 1e6, 4)
    return res
