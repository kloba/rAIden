"""Which endpoint pays for which work: purpose routing with priority and fallback, token and cost accounting, budgets."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..db import Database, utcnow
from ..models import AIEndpoint, TokenUsage
from ..security import Keyring
from .providers import AnthropicProvider, LLMReply, OpenAIProvider, Provider, ProviderError

PURPOSES = ["incidents", "chat", "routines", "hardening"]


class NoEndpoint(Exception):
    """No enabled endpoint for this purpose, or every candidate is over budget or failing."""


def hour_bucket(ts: datetime | None = None) -> datetime:
    ts = ts or utcnow()
    return ts.replace(minute=0, second=0, microsecond=0)


class AIRouter:
    def __init__(self, db: Database, keyring: Keyring, demo_provider: Callable[[str], Provider] | None = None) -> None:
        self.db = db
        self.keyring = keyring
        self.demo_provider = demo_provider
        self._providers: dict[tuple[int, str], Provider] = {}
        self._spend_cache: dict[int, tuple[float, int, float]] = {}

    # ------------------------------------------------------------------------------------------- providers
    def provider_for(self, ep: AIEndpoint) -> Provider:
        if ep.simulated or ep.provider == "demo":
            if self.demo_provider is None:
                raise NoEndpoint(f"{ep.name} is a simulated endpoint; the demo model only runs with the demo fleet")
            return self.demo_provider(ep.model)
        version = f"{ep.updated_at}:{ep.base_url}:{ep.model}:{ep.auth}:{ep.wire_api}:{ep.reasoning_effort}"
        key = (ep.id, version)
        if key not in self._providers:
            api_key = self.keyring.decrypt(ep.api_key_enc)
            if ep.provider == "anthropic":
                prov: Provider = AnthropicProvider(ep.base_url, ep.model, api_key)
            elif ep.provider in ("azure_openai", "openai", "openai_compatible"):
                prov = OpenAIProvider(ep.provider, ep.base_url, ep.model, api_key, ep.auth, ep.api_version, ep.wire_api, ep.reasoning_effort)
            else:
                raise NoEndpoint(f"unknown provider {ep.provider}")
            self._providers = {k: v for k, v in self._providers.items() if k[0] != ep.id}
            self._providers[key] = prov
        return self._providers[key]

    # ------------------------------------------------------------------------------------------- budgets
    def spend(self, endpoint_id: int) -> tuple[int, float]:
        """(tokens today, cost this month) - cached for 20 s."""
        hit = self._spend_cache.get(endpoint_id)
        if hit and time.monotonic() - hit[0] < 20:
            return hit[1], hit[2]
        now = utcnow()
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month0 = day0.replace(day=1)
        with self.db.session() as s:
            tokens = s.execute(select(func.coalesce(func.sum(TokenUsage.tokens_in + TokenUsage.tokens_out), 0))
                               .where(TokenUsage.endpoint_id == endpoint_id, TokenUsage.bucket >= day0)).scalar() or 0
            cost = s.execute(select(func.coalesce(func.sum(TokenUsage.cost_usd), 0.0))
                             .where(TokenUsage.endpoint_id == endpoint_id, TokenUsage.bucket >= month0)).scalar() or 0.0
        self._spend_cache[endpoint_id] = (time.monotonic(), int(tokens), float(cost))
        return int(tokens), float(cost)

    def over_budget(self, ep: AIEndpoint) -> str | None:
        tokens, cost = self.spend(ep.id)
        if ep.daily_token_budget and tokens >= ep.daily_token_budget:
            return f"{ep.name}: daily token budget used ({tokens:,} of {ep.daily_token_budget:,})"
        if ep.monthly_cost_budget and cost >= ep.monthly_cost_budget:
            return f"{ep.name}: monthly budget used (${cost:,.2f} of ${ep.monthly_cost_budget:,.2f})"
        return None

    def candidates(self, purpose: str) -> tuple[list[AIEndpoint], list[str]]:
        with self.db.session() as s:
            eps = list(s.execute(select(AIEndpoint).where(AIEndpoint.enabled.is_(True)).order_by(AIEndpoint.priority, AIEndpoint.id)).scalars())
        usable, skipped = [], []
        for ep in eps:
            if purpose not in (ep.purposes or []):
                continue
            reason = self.over_budget(ep)
            if reason:
                skipped.append(reason)
                continue
            usable.append(ep)
        return usable, skipped

    # ------------------------------------------------------------------------------------------- calls
    async def complete(
        self, purpose: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, *, server_id: str | None = None,
        max_output_tokens: int | None = None,
    ) -> tuple[LLMReply, AIEndpoint]:
        eps, skipped = self.candidates(purpose)
        if not eps:
            why = "; ".join(skipped) if skipped else f"no enabled AI endpoint is assigned to {purpose}"
            raise NoEndpoint(why)
        errors = []
        for ep in eps:
            try:
                prov = self.provider_for(ep)
                reply = await prov.complete(messages, tools, max_output_tokens or ep.max_output_tokens or 8000)
            except NoEndpoint as exc:
                errors.append(str(exc))
                continue
            except ProviderError as exc:
                self.record(ep, purpose, server_id, None, error=True)
                errors.append(f"{ep.name}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - a broken endpoint must not take the pipeline down
                self.record(ep, purpose, server_id, None, error=True)
                errors.append(f"{ep.name}: {exc.__class__.__name__}: {str(exc)[:200]}")
                continue
            self.record(ep, purpose, server_id, reply)
            return reply, ep
        raise NoEndpoint("every endpoint failed: " + " | ".join(errors))

    def cost_of(self, ep: AIEndpoint, reply: LLMReply) -> float:
        fresh_in = max(0, reply.tokens_in - reply.tokens_cached)
        return round((fresh_in * ep.price_in_per_m + reply.tokens_cached * (ep.price_cached_per_m or ep.price_in_per_m)
                      + reply.tokens_out * ep.price_out_per_m) / 1e6, 6)

    def record(self, ep: AIEndpoint, purpose: str, server_id: str | None, reply: LLMReply | None, error: bool = False, at: datetime | None = None) -> None:
        bucket = hour_bucket(at)
        sid = server_id or ""
        for _ in range(2):
            try:
                with self.db.session() as s:
                    row = s.execute(select(TokenUsage).where(TokenUsage.endpoint_id == ep.id, TokenUsage.bucket == bucket, TokenUsage.purpose == purpose,
                                                             TokenUsage.server_id == sid)).scalar_one_or_none()
                    if row is None:
                        row = TokenUsage(endpoint_id=ep.id, bucket=bucket, purpose=purpose, server_id=sid, calls=0, errors=0, tokens_in=0, tokens_out=0,
                                         tokens_cached=0, cost_usd=0.0)
                        s.add(row)
                    row.calls += 1
                    if error or reply is None:
                        row.errors += 1
                    else:
                        row.tokens_in += reply.tokens_in
                        row.tokens_out += reply.tokens_out
                        row.tokens_cached += reply.tokens_cached
                        row.cost_usd = round(row.cost_usd + self.cost_of(ep, reply), 6)
                self._spend_cache.pop(ep.id, None)
                return
            except IntegrityError:
                continue  # a concurrent insert of the same bucket: update it on the second try


def usage_window(days: int) -> datetime:
    return hour_bucket() - timedelta(days=days)
