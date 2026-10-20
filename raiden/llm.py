"""Azure OpenAI chat client authenticated with the VM's managed identity (no API keys anywhere)."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from .config import Config


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

    def complete_sync(self, messages: list[dict], tools: list[dict] | None = None, max_completion_tokens: int = 16000) -> Any:
        import openai

        client = self._get()
        retryable = (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)
        delay, last = 2.0, None
        for attempt in range(6):
            try:
                kwargs: dict[str, Any] = dict(
                    model=self.cfg.deployment,
                    messages=messages,
                    reasoning_effort=self.cfg.reasoning_effort,
                    max_completion_tokens=max_completion_tokens,
                )
                if tools:
                    kwargs["tools"] = tools
                return client.chat.completions.create(**kwargs)
            except retryable as exc:
                last = exc
                time.sleep(delay + random.random())
                delay = min(delay * 2, 30)
        raise RuntimeError(f"LLM unavailable after retries: {last}")

    async def complete(self, messages: list[dict], tools: list[dict] | None = None, max_completion_tokens: int = 16000) -> Any:
        return await asyncio.to_thread(self.complete_sync, messages, tools, max_completion_tokens)
