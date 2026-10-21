"""LLM providers behind one call: complete(messages, tools) -> LLMReply.

The conversation is kept in chat-completions shape everywhere (system/user/assistant/tool messages); each provider
converts it to its own wire format:

  azure_openai       Azure OpenAI / Foundry, Entra ID (managed identity, workload identity, az login) or an API key;
                     chat completions, or the Responses API for the GPT-6 family (function tools + reasoning)
  openai             api.openai.com
  openai_compatible  anything that speaks /v1/chat/completions: Ollama, vLLM, LM Studio, OpenRouter, an on-prem gateway
  anthropic          the Messages API (tool_use / tool_result blocks), prompt caching on the system prompt
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ...llm import from_responses, to_responses_input, to_responses_tools


@dataclass
class LLMReply:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # {"id", "name", "arguments": json string}
    raw_items: list[dict[str, Any]] | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    model: str = ""
    simulated: bool = False


class ProviderError(Exception):
    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class Provider(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, max_output_tokens: int) -> LLMReply: ...


def _chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip the console's bookkeeping keys (raw_items, stage, ...) before a chat-completions call."""
    out = []
    for m in messages:
        clean = {k: m[k] for k in ("role", "content", "tool_calls", "tool_call_id", "name") if k in m}
        if clean.get("role") == "assistant" and not clean.get("content"):
            clean["content"] = None if clean.get("tool_calls") else ""
        out.append(clean)
    return out


class OpenAIProvider:
    def __init__(self, provider: str, base_url: str, model: str, api_key: str | None, auth: str, api_version: str, wire_api: str,
                 reasoning_effort: str) -> None:
        self.provider, self.base_url, self.model, self.api_key = provider, base_url.rstrip("/"), model, api_key
        self.auth, self.api_version, self.wire_api, self.reasoning_effort = auth, api_version or "2025-04-01-preview", wire_api, reasoning_effort
        self._client: Any = None

    @property
    def use_responses(self) -> bool:
        if self.provider == "openai_compatible":
            return False
        if self.wire_api in ("chat", "responses"):
            return self.wire_api == "responses"
        return self.model.startswith("gpt-6")

    def _get(self) -> Any:
        if self._client is not None:
            return self._client
        import openai

        if self.provider == "azure_openai":
            kwargs: dict[str, Any] = dict(azure_endpoint=self.base_url, api_version=self.api_version, timeout=240, max_retries=0)
            if self.auth == "entra" or not self.api_key:
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider

                cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
                kwargs["azure_ad_token_provider"] = get_bearer_token_provider(cred, "https://cognitiveservices.azure.com/.default")
            else:
                kwargs["api_key"] = self.api_key
            self._client = openai.AzureOpenAI(**kwargs)
        else:
            self._client = openai.OpenAI(base_url=self.base_url or None, api_key=self.api_key or "not-needed", timeout=240, max_retries=0)
        return self._client

    def _call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, max_output_tokens: int) -> LLMReply:
        import openai

        client = self._get()
        effort = self.reasoning_effort if self.reasoning_effort not in ("", "none") else None
        try:
            if self.use_responses:
                instructions, items = to_responses_input(messages)
                kwargs: dict[str, Any] = dict(model=self.model, instructions=instructions, input=items, max_output_tokens=max_output_tokens, store=False)
                if effort:
                    kwargs["reasoning"] = {"effort": effort}
                    kwargs["include"] = ["reasoning.encrypted_content"]
                if tools:
                    kwargs["tools"] = to_responses_tools(tools)
                resp = from_responses(client.responses.create(**kwargs))
            else:
                kwargs = dict(model=self.model, messages=_chat_messages(messages))
                if self.provider == "openai_compatible":
                    kwargs["max_tokens"] = max_output_tokens
                else:
                    kwargs["max_completion_tokens"] = max_output_tokens
                    if effort:
                        kwargs["reasoning_effort"] = effort
                if tools:
                    kwargs["tools"] = tools
                resp = client.chat.completions.create(**kwargs)
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError) as exc:
            raise ProviderError(f"{exc.__class__.__name__}: {str(exc)[:300]}", retryable=True) from exc
        except openai.APIError as exc:
            raise ProviderError(f"{exc.__class__.__name__}: {str(exc)[:300]}") from exc
        msg = resp.choices[0].message
        usage = getattr(resp, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        return LLMReply(
            text=msg.content or "",
            tool_calls=[{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or "{}"} for tc in (msg.tool_calls or [])],
            raw_items=getattr(msg, "raw_items", None),
            tokens_in=int(getattr(usage, "prompt_tokens", 0) or 0), tokens_out=int(getattr(usage, "completion_tokens", 0) or 0),
            tokens_cached=int((getattr(details, "cached_tokens", 0) or 0) if details else 0), model=self.model,
        )

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, max_output_tokens: int) -> LLMReply:
        return await asyncio.to_thread(self._call, messages, tools, max_output_tokens)


def to_anthropic(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Chat-shaped history -> (system, Anthropic messages). Tool results become tool_result blocks in a user turn and
    consecutive user content is merged, because the Messages API needs strictly alternating roles."""
    system, out = "", []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m.get("role")
        if role == "system":
            system = str(m.get("content") or "")
        elif role == "user":
            push("user", [{"type": "text", "text": str(m.get("content") or "")}])
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": str(m.get("content") or "")}])
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": str(m["content"])})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", tc)
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc["id"], "name": fn["name"], "input": args})
            if blocks:
                push("assistant", blocks)
    return system, out


class AnthropicProvider:
    def __init__(self, base_url: str, model: str, api_key: str | None) -> None:
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.model, self.api_key = model, api_key

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, max_output_tokens: int) -> LLMReply:
        system, msgs = to_anthropic(messages)
        body: dict[str, Any] = {"model": self.model, "max_tokens": max_output_tokens, "messages": msgs}
        if system:
            body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if tools:
            body["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                              "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}} for t in tools]
        headers = {"x-api-key": self.api_key or "", "anthropic-version": "2023-06-01", "content-type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=240) as client:
                resp = await client.post(f"{self.base_url}/v1/messages", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{exc.__class__.__name__}: {exc}", retryable=True) from exc
        if resp.status_code >= 400:
            raise ProviderError(f"Anthropic {resp.status_code}: {resp.text[:300]}", retryable=resp.status_code in (408, 429, 500, 502, 503, 529))
        data = resp.json()
        text, calls = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append({"id": block["id"], "name": block["name"], "arguments": json.dumps(block.get("input") or {})})
        u = data.get("usage") or {}
        return LLMReply(text="".join(text), tool_calls=calls, tokens_in=int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0) or 0),
                        tokens_out=int(u.get("output_tokens", 0)), tokens_cached=int(u.get("cache_read_input_tokens", 0) or 0), model=data.get("model", self.model))


async def ping(provider: Provider) -> tuple[bool, str, int]:
    t0 = time.monotonic()
    try:
        reply = await asyncio.wait_for(provider.complete([{"role": "user", "content": "Reply with the single word: ready"}], None, 400), timeout=60)
    except Exception as exc:  # noqa: BLE001 - every failure is a test result
        return False, f"{exc.__class__.__name__}: {str(exc)[:300]}", int((time.monotonic() - t0) * 1000)
    return True, (reply.text or "(empty reply)").strip()[:80], int((time.monotonic() - t0) * 1000)
