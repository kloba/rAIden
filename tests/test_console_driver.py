"""The console's PostgreSQL driver without a server: what the live run on PostgreSQL 13 to 18 (docs/validation.md) found,
pinned with fake connections so it cannot come back unnoticed."""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest

from raiden.console.drivers import postgres
from raiden.console.drivers.base import DriverError
from raiden.console.drivers.postgres import PostgresDriver


class FakeCursor:
    def __init__(self, rows: list[tuple], cols: list[str] | None = None) -> None:
        self._rows = rows
        self.description = [type("C", (), {"name": c})() for c in (cols or ["x"])] if rows is not None else None

    async def fetchall(self) -> list[tuple]:
        return self._rows

    async def fetchmany(self, n: int) -> list[tuple]:
        return self._rows[:n]


class FakeConn:
    """Answers queries with `answer(sql)`; tracks how many connections are open at once."""

    open_now = 0
    open_max = 0

    def __init__(self, answer: Any = None, notices: list[Any] | None = None) -> None:
        self.answer = answer or (lambda sql: [])
        self.notices = notices or []
        self.handlers: list[Any] = []
        self.closed = False
        FakeConn.open_now += 1
        FakeConn.open_max = max(FakeConn.open_max, FakeConn.open_now)

    def add_notice_handler(self, fn: Any) -> None:
        self.handlers.append(fn)

    async def execute(self, sql: Any, params: Any = None) -> FakeCursor:
        await asyncio.sleep(0.01)
        for n in self.notices:
            for h in self.handlers:
                h(n)
        res = self.answer(str(sql))
        if isinstance(res, Exception):
            raise res
        return FakeCursor(res)

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            FakeConn.open_now -= 1


def driver_with(monkeypatch: pytest.MonkeyPatch, make: Any) -> PostgresDriver:
    drv = PostgresDriver("pg17", {"host": "db", "dbname": "app", "user": "raiden_monitor"}, ["app"])

    async def connect(dbname: str | None = None, timeout_ms: int = 0, lock_timeout_ms: int = 0) -> FakeConn:
        return make(lock_timeout_ms)
    monkeypatch.setattr(drv, "_connect", connect)
    return drv


def test_the_databases_tool_is_not_shadowed_by_the_list_of_databases():
    drv = PostgresDriver("pg17", {"host": "db"}, ["app", "billing"])
    assert callable(drv.databases) and drv.table_dbs == ["app", "billing"]


def test_pg_stat_statements_counts_only_when_the_library_is_loaded(monkeypatch):
    def caps(preload: str) -> dict:
        row = [("raiden_monitor", False, True, True, False, True, preload, "on", "off", "replica", "UTF8", '["pg_stat_statements 1.11"]')]
        drv = driver_with(monkeypatch, lambda _: FakeConn(lambda sql: row))
        return asyncio.run(drv._capabilities(FakeConn(lambda sql: row)))["capabilities"]
    assert caps("pg_stat_statements")["pg_stat_statements"] is True
    assert caps("auto_explain, pg_stat_statements")["pg_stat_statements"] is True
    assert caps("")["pg_stat_statements"] is False  # a standby inherits the extension, not the library


def test_top_queries_explains_an_extension_that_is_not_loaded(monkeypatch):
    def answer(sql: str) -> Any:
        if "EXISTS" in sql:
            return [(True,)]
        return psycopg.errors.ObjectNotInPrerequisiteState('pg_stat_statements must be loaded via "shared_preload_libraries"')
    drv = driver_with(monkeypatch, lambda _: FakeConn(answer))
    res = asyncio.run(drv.top_queries())
    assert res["rows"] == [] and "not loaded" in res["note"]


def test_a_skipped_vacuum_is_a_refusal_not_a_success(monkeypatch):
    warning = type("D", (), {"severity_nonlocalized": "WARNING", "message_primary": 'permission denied to vacuum "orders", skipping it'})()
    drv = driver_with(monkeypatch, lambda _: FakeConn(notices=[warning]))
    with pytest.raises(DriverError, match="skipping"):
        asyncio.run(drv.execute_action("vacuum_table", {"database": "app", "table": "public.orders"}))
    notice = type("D", (), {"severity_nonlocalized": "NOTICE", "message_primary": "relation already exists, skipping"})()
    drv = driver_with(monkeypatch, lambda _: FakeConn(notices=[notice]))
    assert asyncio.run(drv.execute_action("analyze_table", {"database": "app", "table": "public.orders"}))["ok"]


def test_the_driver_stays_inside_the_logins_connection_limit(monkeypatch):
    FakeConn.open_now = FakeConn.open_max = 0
    drv = driver_with(monkeypatch, lambda _: FakeConn(lambda sql: [(1,)]))

    async def many() -> None:
        await asyncio.gather(*(drv._query("SELECT 1") for _ in range(12)))
    asyncio.run(many())
    assert FakeConn.open_max == postgres.SHORT_CONNECTIONS == 3 and FakeConn.open_now == 0


def test_a_failed_concurrent_index_build_removes_its_invalid_index(monkeypatch):
    drv = PostgresDriver("pg17", {"host": "db", "dbname": "app"})
    calls: list[str] = []
    leftovers = [[], [("public.ix_orders_customer",)]]  # nothing before the build, the INVALID index after it

    async def fake_query(sql: str, params: Any = None, dbname: str | None = None) -> tuple[list[str], list[tuple]]:
        return ["name"], (leftovers.pop(0) if "indisvalid" in sql else [])

    async def fake_exec(stmt: Any, dbname: str | None = None, timeout_ms: int = 0, lock_timeout_ms: int = 0) -> None:
        text = stmt.as_string(None) if hasattr(stmt, "as_string") else str(stmt)
        calls.append(f"{text} [lock_timeout {lock_timeout_ms}]")
        if text.startswith("CREATE INDEX"):
            raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")
    monkeypatch.setattr(drv, "_query", fake_query)
    monkeypatch.setattr(drv, "_exec", fake_exec)
    with pytest.raises(DriverError) as err:
        asyncio.run(drv.execute_action("create_index_concurrently", {"database": "app", "table": "public.orders", "columns": ["customer_id"], "name": "ix_orders_customer"}))
    assert "lock timeout" in str(err.value) and "public.ix_orders_customer was dropped" in str(err.value)
    assert calls[0].startswith('CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_orders_customer"')
    assert calls[0].endswith(f"[lock_timeout {postgres.MAINTENANCE_LOCK_TIMEOUT_MS}]")  # minutes, not the usual 3 s
    assert calls[1].startswith('DROP INDEX CONCURRENTLY IF EXISTS "public"."ix_orders_customer"')
