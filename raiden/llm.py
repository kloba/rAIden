"""Azure OpenAI client authenticated with the VM's managed identity (no API keys anywhere).

Two wire formats behind one chat-completions-shaped result, so the agent loop does not care which one it talks to:
  chat       /chat/completions: GPT-5.x and older
  responses  /responses: required for function tools together with reasoning on the GPT-6 family. The loop keeps its
             chat-shaped history; every turn it is converted to Responses input items (stateless, store=False), and the
             model's reasoning items travel back with the assistant message so the model keeps its train of thought.
"""

from __future__ import annotations

import asyncio
import random
import time
from types import SimpleNamespace as NS
from typing import Any

from .config import Config


def to_responses_tools(tools: list[dict] | None) -> list[dict]:
    out = []
    for t in tools or []:
        fn = t["function"]
        out.append({"type": "function", "name": fn["name"], "description": fn.get("description", ""), "parameters": fn.get("parameters", {"type": "object", "properties": {}})})
    return out


def to_responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
    """(instructions, input items) for a chat-shaped history. An assistant entry that carries `raw_items` (the model's own
    output items of that turn, reasoning included) is replayed verbatim."""
    instructions, items = "", []
    for m in messages:
        role = m.get("role")
        if role == "system":
            instructions = str(m.get("content") or "")
        elif role == "user":
            items.append({"role": "user", "content": str(m.get("content") or "")})
        elif role == "assistant":
            if m.get("raw_items"):
                items.extend(m["raw_items"])
                continue
            if m.get("content"):
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": str(m["content"])}]})
            for tc in m.get("tool_calls") or []:
                items.append({"type": "function_call", "call_id": tc["id"], "name": tc["function"]["name"], "arguments": tc["function"].get("arguments") or "{}"})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": str(m.get("content") or "")})
    return instructions, items


def _plain(item: Any) -> dict:
    return item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item)


def from_responses(resp: Any) -> Any:
    """A Responses result as a chat-completions-shaped object: choices[0].message.content/.tool_calls/.raw_items, usage."""
    text, calls, raw = [], [], []
    for item in resp.output or []:
        d = _plain(item)
        kind = d.get("type")
        if kind == "reasoning":
            raw.append({k: v for k, v in d.items() if k != "status"})
        elif kind == "message":
            parts = [c.get("text", "") for c in d.get("content") or [] if c.get("type") == "output_text"]
            text.extend(parts)
            raw.append({"role": "assistant", "content": [{"type": "output_text", "text": t} for t in parts]})
        elif kind == "function_call":
            calls.append(NS(id=d["call_id"], type="function", function=NS(name=d["name"], arguments=d.get("arguments") or "{}")))
            raw.append({"type": "function_call", "call_id": d["call_id"], "name": d["name"], "arguments": d.get("arguments") or "{}"})
    u = getattr(resp, "usage", None)
    cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
    usage = NS(prompt_tokens=getattr(u, "input_tokens", 0) or 0, completion_tokens=getattr(u, "output_tokens", 0) or 0, prompt_tokens_details=NS(cached_tokens=cached))
    msg = NS(content="".join(text) or None, tool_calls=calls or None, raw_items=raw)
    return NS(choices=[NS(message=msg, finish_reason="tool_calls" if calls else "stop")], usage=usage)


class AzureLLM:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client: Any = None

    def _get(self) -> Any:
        if self._client is None:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            from openai import AzureOpenAI

            cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
            provider = get_bearer_token_provider(cred, "https://cognitiveservices.azure.com/.default")
            self._client = AzureOpenAI(
                azure_endpoint=self.cfg.endpoint,
                azure_ad_token_provider=provider,
                api_version=self.cfg.api_version,
                timeout=240,
                max_retries=0,
            )
        return self._client

    def _call(self, client: Any, messages: list[dict], tools: list[dict] | None, max_completion_tokens: int) -> Any:
        if self.cfg.use_responses:
            instructions, items = to_responses_input(messages)
            kwargs: dict[str, Any] = dict(
                model=self.cfg.deployment,
                instructions=instructions,
                input=items,
                reasoning={"effort": self.cfg.reasoning_effort},
                max_output_tokens=max_completion_tokens,
                store=False,
                include=["reasoning.encrypted_content"],
            )
            if tools:
                kwargs["tools"] = to_responses_tools(tools)
            return from_responses(client.responses.create(**kwargs))
        kwargs = dict(
            model=self.cfg.deployment,
            messages=messages,
            reasoning_effort=self.cfg.reasoning_effort,
            max_completion_tokens=max_completion_tokens,
        )
        if tools:
            kwargs["tools"] = tools
        return client.chat.completions.create(**kwargs)

    def complete_sync(self, messages: list[dict], tools: list[dict] | None = None, max_completion_tokens: int = 16000) -> Any:
        import openai

        client = self._get()
        retryable = (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)
        delay, last = 2.0, None
        for attempt in range(6):
            try:
                return self._call(client, messages, tools, max_completion_tokens)
            except retryable as exc:
                last = exc
                time.sleep(delay + random.random())
                delay = min(delay * 2, 30)
        raise RuntimeError(f"LLM unavailable after retries: {last}")

    async def complete(self, messages: list[dict], tools: list[dict] | None = None, max_completion_tokens: int = 16000) -> Any:
        return await asyncio.to_thread(self.complete_sync, messages, tools, max_completion_tokens)
