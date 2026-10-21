"""The scripted demo model: plays the demo scenarios like an experienced DBA, through the same tools and gates as a real
model. Every number it says comes from the tool results it just read, so the timeline matches the charts.

It is labelled 'simulated' everywhere and costs nothing; connect a real endpoint for open-ended work.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Any, Callable

from .. import fmt
from ..ai.providers import LLMReply

Step = Callable[["Ctx"], tuple[str, list[tuple[str, dict[str, Any]]]]]


class Ctx:
    def __init__(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> None:
        self.messages = messages
        self.tool_names = {t["function"]["name"] for t in tools or []}
        system = next((str(m.get("content") or "") for m in messages if m.get("role") == "system"), "")
        first_user = next((str(m.get("content") or "") for m in messages if m.get("role") == "user"), "")
        self.context = system + "\n" + first_user
        # every stage (and every chat question) starts with a new user message: count the turns since the last one
        start = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=0)
        self.stage_msgs = messages[start + 1:]
        self.step = sum(1 for m in self.stage_msgs if m.get("role") == "assistant")
        names: dict[str, str] = {}
        for m in messages:
            for tc in m.get("tool_calls") or []:
                names[tc["id"]] = tc["function"]["name"]
        self.results: list[tuple[str, Any]] = []
        for m in messages:
            if m.get("role") == "tool":
                content = str(m.get("content") or "")
                try:
                    data: Any = json.loads(content)
                except json.JSONDecodeError:
                    data = content
                self.results.append((names.get(m.get("tool_call_id", ""), "?"), data))
        sc = re.search(r"\[scenario: ([\w-]+)\]", self.context)
        self.scenario = sc.group(1) if sc else ""
        self.user_text = next((str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"), "")

    def last(self, tool: str) -> Any:
        for name, data in reversed(self.results):
            if name == tool:
                return data
        return None

    def metric(self, key: str) -> float | None:
        data = self.last("current_metrics")
        if isinstance(data, dict):
            for row in data.get("rows", []):
                if row[0] == key:
                    return row[1]
        return None

    def readable(self, key: str) -> str:
        data = self.last("current_metrics")
        if isinstance(data, dict):
            for row in data.get("rows", []):
                if row[0] == key:
                    return row[2]
        return "n/a"


def _col(table: Any, name: str) -> int:
    return table["columns"].index(name) if isinstance(table, dict) and name in table.get("columns", []) else -1


def _rows(table: Any) -> list[list[Any]]:
    return table.get("rows", []) if isinstance(table, dict) else []


# ============================================================================================================ playbooks
def _role_setting(c: Ctx, role: str, setting: str, value: str, title: str, detail: str = "") -> dict[str, Any]:
    """A role timeout as an action when rAIden's login may change roles (the server brief says so), else for a person."""
    if "cannot change role limits and timeouts" in c.context:
        return {"kind": "recommendation", "title": title, "detail": detail + " rAIden's login has no CREATEROLE here, so an admin runs it.",
                "spec": {"how": f"ALTER ROLE {role} SET {setting} = '{value}';"}}
    return {"kind": "action", "title": title, "detail": detail, "spec": {"action": "set_role_timeout", "args": {"role": role, "setting": setting, "value": value}}}


def lock_queue(c: Ctx, mode: str) -> tuple[str, list]:
    lt = c.last("lock_tree")
    if mode == "triage":
        if c.step == 0:
            return "A lock queue on a production OLTP server stops every write behind it. Looking at the metrics and the lock graph.", [
                ("current_metrics", {}), ("lock_tree", {})]
        blocked = (lt.get("waiting_total") if isinstance(lt, dict) else 0) or len([r for r in _rows(lt) if r[_col(lt, "blocked_by")]])
        roots = lt.get("roots", []) if isinstance(lt, dict) else []
        return "", [("submit_triage", {"severity": "sev2", "category": "locks",
                                       "summary": f"{c.readable('blocked_sessions')} sessions are blocked and TPS dropped to {c.readable('tps')}. "
                                                  f"The lock graph has {fmt.plural(len(roots), 'root blocker')} and {fmt.plural(blocked, 'waiting session')}.",
                                       "hypothesis": "A DDL statement waits for a lock held by an older transaction, and every writer queues behind the DDL."})]
    if mode == "diagnose":
        if c.step == 0:
            return "Following the chain from the waiting writers to the root.", [("lock_tree", {}), ("list_sessions", {"state": "idle in transaction"})]
        if c.step == 1:
            return "", [("recent_incidents", {"limit": 3})]
        root = (lt.get("roots") or [None])[0] if isinstance(lt, dict) else None
        rows = {r[0]: r for r in _rows(lt)}
        r = rows.get(root)
        ddl = next((x for x in _rows(lt) if x[_col(lt, "blocked_by")] == [root] and "ALTER" in str(x[-1]).upper()), None)
        age = fmt.duration(r[_col(lt, "xact_age_s")] or 0) if r else "a long time"
        who = f"{r[_col(lt, 'usename')]} ({r[_col(lt, 'application_name')]})" if r else "an old session"
        return "", [("submit_diagnosis", {
            "root_cause": f"pid {root}, role {who}, has been idle in transaction for {age} holding AccessShareLock on orders.orders. "
                          f"The migration{' (pid ' + str(ddl[0]) + ', role migrator)' if ddl else ''} runs ALTER TABLE orders.orders ADD COLUMN, needs "
                          f"AccessExclusiveLock and waits behind it; every INSERT/UPDATE of the application queues behind the ALTER TABLE.",
            "evidence": [f"lock_tree: root blocker pid {root}, {(lt.get('waiting_total') if isinstance(lt, dict) else 0) or sum(1 for x in _rows(lt) if x[_col(lt, 'blocked_by')])} sessions waiting",
                         f"pid {root} state 'idle in transaction', transaction open for {age}, wait ClientRead",
                         "the waiting DDL holds the queue: RowExclusiveLock requests of orders_app are behind its AccessExclusiveLock request",
                         f"TPS {c.readable('tps')} instead of the usual ~1,400/s"],
            "confidence": "high",
            "plan": [{"action": "terminate_backend", "args": {"pid": root},
                      "rationale": "End the forgotten transaction at the root; it rolls back (it only read data). The ALTER TABLE then takes its lock in milliseconds and the queue drains."}],
        })]
    if mode == "remediate":
        if c.step == 0:
            plan_pid = int(re.search(r"terminate_backend\(\{\"pid\": (\d+)", c.user_text).group(1)) if re.search(r"terminate_backend\(\{\"pid\": (\d+)", c.user_text) else 0
            return "Ending the root blocker.", [("execute_action", {"action": "terminate_backend", "args": {"pid": plan_pid},
                                                                     "rationale": "root of the lock queue, idle in transaction"})]
        if c.step == 1:
            return "Checking that the queue drained.", [("lock_tree", {}), ("current_metrics", {})]
        left = c.metric("blocked_sessions") or 0
        queue = "nobody waits for a lock any more" if left < 1 else f"the {c.readable('blocked_sessions')} sessions still queued a moment ago are draining"
        return "", [("submit_remediation", {"summary": f"Terminated the idle-in-transaction root blocker: the lock graph has no root left and {queue}; "
                                                      "the migration's ALTER TABLE completed and TPS is recovering."})]
    if mode == "verify":
        if c.step == 0:
            return "", [("metric_history", {"metric": "blocked_sessions", "minutes": 15}), ("metric_history", {"metric": "tps", "minutes": 15})]
        return "", [("submit_verification", {"passed": True, "evidence": [
            f"blocked_sessions back to 0 (max {fmt.big(_hist(c, 'blocked_sessions', 'max'))} during the incident)", f"tps last {_hist_last(c, 'tps')}",
            "no new lock waits in the window"], "summary": "The queue is gone and throughput is back to normal."})]
    if mode == "harden":
        return "Two cheap protections stop this from freezing the shop again, and one lesson for next time.", [("propose_hardening", {
            "summary": "A migration should give up instead of queueing everybody; an analyst's forgotten transaction should not live for half an hour.",
            "items": [
                _role_setting(c, "migrator", "lock_timeout", "3s", "lock_timeout = 3s for the migrator role",
                              "The deploy fails fast and retries instead of freezing every writer."),
                _role_setting(c, "analyst", "idle_in_transaction_session_timeout", "10min", "idle_in_transaction_session_timeout = 10min for analyst",
                              "Forgotten transactions of ad-hoc tools end on their own."),
                {"kind": "monitor", "title": "Transaction open > 15 min on orders-prod", "detail": "Catch the root cause before a deploy trips over it.",
                 "spec": {"metric": "longest_transaction_time_sec", "operator": ">", "threshold": 900, "window_minutes": 2, "severity": "sev3", "aggregation": "min"}},
            ]})]
    return _report(c, "Lock queue behind a migration", "orders.orders writes stalled; checkout requests timed out while the queue lasted.")


def connection_storm(c: Ctx, mode: str) -> tuple[str, list]:
    if mode == "triage":
        if c.step == 0:
            return "Connection slots are almost gone; new clients will be refused. Who holds them?", [("current_metrics", {}), ("list_sessions", {"limit": 30})]
        return "", [("submit_triage", {"severity": "sev2", "category": "connections",
                                       "summary": f"{c.readable('active_connections')} connections, {c.readable('connections_percent')} of max_connections. "
                                                  "New clients are about to get 'remaining connection slots are reserved'.",
                                       "hypothesis": "One role or application opens connections without pooling."})]
    if mode == "diagnose":
        ls = c.last("list_sessions")
        if c.step == 0:
            return "Grouping the sessions by role and state.", [("list_sessions", {"role": "reporting", "limit": 20})]
        by = ls.get("by_role_state", []) if isinstance(ls, dict) else []
        top = by[0] if by else ["reporting", "idle", 0]
        return "", [("submit_diagnosis", {
            "root_cause": f"Role {top[0]} holds {top[2]} {top[1]} connections from 'metabase 0.51 (dashboard refresh)': the dashboard opens a connection per tile and never closes it.",
            "evidence": [f"by role/state: {top[0]}/{top[1]} = {top[2]}", "application_name 'metabase 0.51 (dashboard refresh)', query 'SELECT 1'",
                         f"connections at {c.readable('connections_percent')}"],
            "confidence": "high",
            "plan": [{"action": "terminate_sessions", "args": {"role": "reporting", "state": "idle", "older_than_s": 60},
                      "rationale": "Free the slots held by idle reporting sessions (nothing in flight is lost)."},
                     {"action": "set_role_connection_limit", "args": {"role": "reporting", "limit": 20},
                      "rationale": "Without a cap the dashboard reconnects within a minute and the storm returns."}],
        })]
    if mode == "remediate":
        if c.step == 0:
            return "Freeing the slots first, then capping the role so it cannot come back.", [
                ("execute_action", {"action": "terminate_sessions", "args": {"role": "reporting", "state": "idle", "older_than_s": 60}, "rationale": "free the slots"})]
        if c.step == 1:
            return "", [("execute_action", {"action": "set_role_connection_limit", "args": {"role": "reporting", "limit": 20}, "rationale": "stop the reconnect storm"})]
        if c.step == 2:
            return "", [("current_metrics", {})]
        return "", [("submit_remediation", {"summary": f"Ended the idle reporting sessions and capped the role at 20 connections. Connections now {c.readable('connections_percent')}."})]
    if mode == "verify":
        if c.step == 0:
            return "", [("metric_history", {"metric": "connections_percent", "minutes": 15})]
        return "", [("submit_verification", {"passed": True, "evidence": [f"connections_percent last {_hist_last(c, 'connections_percent')}, peak {_hist_max(c, 'connections_percent')}",
                                                                           "role reporting stays at its cap of 20"], "summary": "Slots are free and the cap holds."})]
    if mode == "harden":
        return "", [("propose_hardening", {"summary": "Catch pressure early and tell the data team why their dashboard got capped.", "items": [
            {"kind": "monitor", "title": "Connections above 75% for 5 min on analytics-staging", "spec": {"metric": "connections_percent", "operator": ">", "threshold": 75,
                                                                                                       "window_minutes": 5, "severity": "sev3"}},
            {"kind": "lesson", "title": "Metabase dashboards open a connection per tile", "spec": {"text": "Role reporting (Metabase) floods connections on dashboard refresh; it is capped at 20 - raise only with PgBouncer in front."}},
        ]})]
    return _report(c, "Connection storm from the reporting role", "Staging ETL jobs and analysts were refused new connections while it lasted.")


def cpu_spike(c: Ctx, mode: str) -> tuple[str, list]:
    tq = c.last("top_queries")
    if mode == "triage":
        if c.step == 0:
            return "CPU near saturation on the payments primary. Which statements are eating it?", [("current_metrics", {}), ("top_queries", {"order_by": "total_time", "limit": 5})]
        return "", [("submit_triage", {"severity": "sev2", "category": "cpu",
                                       "summary": f"CPU {c.readable('cpu_percent')} for several minutes, TPS down to {c.readable('tps')}, cache hit ratio {c.readable('cache_hit_ratio')}.",
                                       "hypothesis": "A new or changed query scans a large table; payments.transactions is the likely target."})]
    if mode == "diagnose":
        if c.step == 0:
            return "Comparing the top statements by mean time and checking the plan of the new one.", [("top_queries", {"order_by": "mean_time", "limit": 5})]
        if c.step == 1:
            return "", [("run_sql", {"sql": "EXPLAIN SELECT * FROM payments.transactions WHERE merchant_ref = 'M-4471' ORDER BY created_at DESC LIMIT 20", "database": "payments"})]
        if c.step == 2:
            return "", [("table_health", {"database": "payments", "limit": 5})]
        q = _rows(tq)[0] if _rows(tq) else None
        mean = q[_col(tq, "mean_ms")] if q else 2140
        calls = q[_col(tq, "calls")] if q else 9000
        return "", [("submit_diagnosis", {
            "root_cause": "The payments-api 4.12.0 deploy added `SELECT * FROM payments.transactions WHERE merchant_ref = $1`; merchant_ref has no index, so each "
                          f"call is a parallel sequential scan of ~880 million rows ({fmt.big(mean)} ms mean, {fmt.big(calls)} calls since the deploy).",
            "evidence": [f"top statement by mean time: {fmt.big(mean)} ms, {fmt.big(calls)} calls", "EXPLAIN: Parallel Seq Scan on transactions, Filter: (merchant_ref = 'M-4471')",
                         "no index on payments.transactions(merchant_ref)", f"CPU {c.readable('cpu_percent')}, cache hit ratio {c.readable('cache_hit_ratio')}"],
            "confidence": "high",
            "plan": [{"action": "create_index_concurrently", "args": {"database": "payments", "table": "payments.transactions", "columns": ["merchant_ref"],
                                                                     "name": "idx_transactions_merchant_ref"},
                      "rationale": "CREATE INDEX CONCURRENTLY does not block writes; the planner switches to an index scan as soon as it is valid."}],
        })]
    if mode == "remediate":
        if c.step == 0:
            return "Building the index concurrently; payments keep flowing while it builds.", [("execute_action", {"action": "create_index_concurrently", "args": {
                "database": "payments", "table": "payments.transactions", "columns": ["merchant_ref"], "name": "idx_transactions_merchant_ref"}, "rationale": "the approved plan"})]
        if c.step == 1:
            return "", [("current_metrics", {}), ("top_queries", {"order_by": "mean_time", "limit": 3})]
        return "", [("submit_remediation", {"summary": f"idx_transactions_merchant_ref is valid; CPU is down to {c.readable('cpu_percent')} and the statement no longer tops mean time."})]
    if mode == "verify":
        if c.step == 0:
            return "", [("metric_history", {"metric": "cpu_percent", "minutes": 15})]
        return "", [("submit_verification", {"passed": True, "evidence": [f"cpu_percent last {_hist_last(c, 'cpu_percent')}, peak {_hist_max(c, 'cpu_percent')}"],
                                             "summary": "CPU is back to its daily pattern."})]
    if mode == "harden":
        return "", [("propose_hardening", {"summary": "Catch the next unindexed query before it saturates the primary.", "items": [
            {"kind": "routine", "title": "Weekly index advisor on payments-prod", "spec": {"template": "index-advisor"}},
            {"kind": "monitor", "title": "Queries running longer than 5 s on payments-prod", "spec": {"metric": "longest_query_time_sec", "operator": ">", "threshold": 5,
                                                                                                  "window_minutes": 5, "severity": "sev3"}},
            {"kind": "lesson", "title": "Review EXPLAIN for new queries on payments.transactions", "spec": {"text": "payments.transactions has ~880M rows: any new filter column needs an index before the deploy."}},
        ]})]
    return _report(c, "CPU saturation from an unindexed query", "Card authorizations were slower (p99 up) for the duration; no errors.")


def replication_lag(c: Ctx, mode: str) -> tuple[str, list]:
    rs = c.last("replication_status")
    if mode == "triage":
        if c.step == 0:
            return "The standby is falling behind. Is WAL arriving and not replayed, or not arriving?", [("current_metrics", {}), ("replication_status", {})]
        return "", [("submit_triage", {"severity": "sev2", "category": "replication",
                                       "summary": f"Replay lag {c.readable('physical_replication_delay_in_seconds')} and growing; the receiver is streaming, so WAL arrives but is not replayed.",
                                       "hypothesis": "Replay waits for a conflicting query on the standby."})]
    if mode == "diagnose":
        if c.step == 0:
            return "Looking for the query replay is waiting for.", [("list_sessions", {"state": "active", "limit": 10}),
                                                                     ("get_settings", {"names": ["max_standby_streaming_delay", "hot_standby_feedback"]})]
        ls = c.last("list_sessions")
        top = _rows(ls)[0] if _rows(ls) else None
        pid = top[0] if top else 0
        age = fmt.duration(top[_col(ls, "query_age_s")] or 0) if top else "minutes"
        return "", [("submit_diagnosis", {
            "root_cause": f"The quarter-close report (pid {pid}, role finance_report) has been running for {age} on the standby. Replay of a VACUUM cleanup record "
                          "conflicts with it, and max_standby_streaming_delay = -1 makes replay wait for the query indefinitely.",
            "evidence": [f"replay_waiting_for: {rs.get('replay_waiting_for') if isinstance(rs, dict) else 'a conflicting query'}", f"pid {pid}: active {age}, finance-quarter-close",
                         "max_standby_streaming_delay = -1, hot_standby_feedback = off"],
            "confidence": "high",
            "plan": [{"action": "cancel_backend", "args": {"pid": pid}, "rationale": "Cancel the report so replay can continue; the finance job can re-run it after the standby catches up."}],
        })]
    if mode == "remediate":
        pid = int(re.search(r"cancel_backend\(\{\"pid\": (\d+)", c.user_text).group(1)) if re.search(r"cancel_backend\(\{\"pid\": (\d+)", c.user_text) else 0
        if c.step == 0:
            return "Cancelling the conflicting report.", [("execute_action", {"action": "cancel_backend", "args": {"pid": pid}, "rationale": "the approved plan"})]
        if c.step == 1:
            return "", [("replication_status", {})]
        return "", [("submit_remediation", {"summary": "Cancelled the report; replay resumed and the standby is catching up."})]
    if mode == "verify":
        if c.step == 0:
            return "", [("metric_history", {"metric": "physical_replication_delay_in_seconds", "minutes": 15})]
        return "", [("submit_verification", {"passed": True, "evidence": [f"replay lag last {_hist_last(c, 'physical_replication_delay_in_seconds')}, peak {_hist_max(c, 'physical_replication_delay_in_seconds')}"],
                                             "summary": "The standby is current again."})]
    if mode == "harden":
        return "", [("propose_hardening", {"summary": "Bound how long replay may wait, and how long a report may run on the standby.", "items": [
            {"kind": "action", "title": "max_standby_streaming_delay = 30s", "detail": "Replay cancels conflicting queries after 30 s instead of waiting forever.",
             "spec": {"action": "alter_system_set", "args": {"name": "max_standby_streaming_delay", "value": "30s"}}},
            {"kind": "action", "title": "statement_timeout = 15min for finance_report", "spec": {"action": "set_role_timeout", "args": {"role": "finance_report", "setting": "statement_timeout", "value": "15min"}}},
            {"kind": "lesson", "title": "Quarter-close reports conflict with replay", "spec": {"text": "finance-quarter-close runs long aggregates on payments-replica; replay conflicts are expected at quarter end."}},
        ]})]
    return _report(c, "Standby replay blocked by a long report", "BI dashboards on payments-replica showed stale data while the lag lasted.")


def wraparound(c: Ctx, mode: str) -> tuple[str, list]:
    wr = c.last("wraparound_status")
    if mode == "triage":
        if c.step == 0:
            return "Transaction ID age passed one billion. That is a slow emergency: find what holds the horizon.", [("current_metrics", {}), ("wraparound_status", {})]
        return "", [("submit_triage", {"severity": "sev2", "category": "wraparound",
                                       "summary": f"Oldest datfrozenxid age is {c.readable('maximum_used_transactionIDs')} and climbing; at ~2.1 billion PostgreSQL stops assigning transaction IDs.",
                                       "hypothesis": "Something holds the xmin horizon so autovacuum cannot freeze."})]
    if mode == "diagnose":
        if c.step == 0:
            return "", [("table_health", {"database": "inventory", "limit": 5})]
        holders = (wr or {}).get("xmin_holders", {}) if isinstance(wr, dict) else {}
        top = _rows(holders)[0] if _rows(holders) else ["prepared", "erp-sync-7f3a9c", "erp_sync", 0, 0, "database inventory"]
        return "", [("submit_diagnosis", {
            "root_cause": f"An orphaned prepared transaction '{top[1]}' (owner {top[2]}) has been prepared for {fmt.duration(top[4] or 0)}: the ERP sync coordinator "
                          "crashed between PREPARE and COMMIT PREPARED. It pins the xmin horizon, so autovacuum runs but cannot freeze anything.",
            "evidence": [f"xmin holder #1: {top[0]} {top[1]}, xmin age {fmt.big(top[3] or 0)}", f"maximum_used_transactionIDs {c.readable('maximum_used_transactionIDs')}",
                         "3 autovacuum workers busy on inventory.stock_movements without advancing relfrozenxid"],
            "confidence": "high",
            "plan": [{"action": "rollback_prepared", "args": {"gid": top[1], "database": "inventory"},
                      "rationale": "The coordinator is gone and its 9-day-old work is obsolete; rolling back releases the horizon. (High risk: a person decides.)"},
                     {"action": "vacuum_table", "args": {"database": "inventory", "table": "inventory.stock_movements", "freeze": True},
                      "rationale": "Freeze the oldest, largest table right away instead of waiting for autovacuum."}],
        })]
    if mode == "remediate":
        gid = re.search(r'"gid": "([^"]+)"', c.user_text)
        if c.step == 0:
            return "Releasing the horizon first.", [("execute_action", {"action": "rollback_prepared", "args": {"gid": gid.group(1) if gid else "erp-sync-7f3a9c", "database": "inventory"},
                                                                         "rationale": "orphaned 2PC transaction"})]
        if c.step == 1:
            return "Now freezing the big table.", [("execute_action", {"action": "vacuum_table", "args": {"database": "inventory", "table": "inventory.stock_movements", "freeze": True},
                                                                         "rationale": "advance relfrozenxid"})]
        if c.step == 2:
            return "", [("wraparound_status", {})]
        return "", [("submit_remediation", {"summary": "Rolled back the orphaned prepared transaction and froze inventory.stock_movements; transaction ID age is falling."})]
    if mode == "verify":
        if c.step == 0:
            return "", [("metric_history", {"metric": "maximum_used_transactionIDs", "minutes": 15})]
        return "", [("submit_verification", {"passed": True, "evidence": [f"maximum_used_transactionIDs last {_hist_last(c, 'maximum_used_transactionIDs')}, was {_hist_max(c, 'maximum_used_transactionIDs')}"],
                                             "summary": "The horizon moves again and the age is dropping toward normal."})]
    if mode == "harden":
        return "", [("propose_hardening", {"summary": "A prepared transaction should page somebody within minutes, not after nine days.", "items": [
            {"kind": "monitor", "title": "Prepared transaction older than 10 min on inventory-prod", "spec": {"metric": "prepared_xact_oldest_seconds", "operator": ">",
                                                                                                          "threshold": 600, "window_minutes": 2, "severity": "sev2", "aggregation": "min"}},
            {"kind": "routine", "title": "Hourly wraparound watch", "spec": {"template": "wraparound-watch"}},
            {"kind": "lesson", "title": "ERP sync leaves prepared transactions when its coordinator crashes", "spec": {"text": "Check pg_prepared_xacts first when xid age climbs on inventory-prod."}},
        ]})]
    return _report(c, "Wraparound risk from an orphaned prepared transaction", "No user impact yet; without action the database would have stopped accepting writes in about 12 days.")


def wal_slot(c: Ctx, mode: str) -> tuple[str, list]:
    rs = c.last("replication_status")
    if mode == "triage":
        if c.step == 0:
            return "Storage above 90% on ledger-dev. WAL or data?", [("current_metrics", {}), ("replication_status", {})]
        return "", [("submit_triage", {"severity": "sev2", "category": "storage",
                                       "summary": f"Storage at {c.readable('storage_percent')}; replication slots retain {c.readable('replication_slot_retained_bytes')} of WAL.",
                                       "hypothesis": "A replication slot without a consumer keeps WAL forever."})]
    if mode == "diagnose":
        if c.step == 0:
            return "", [("get_settings", {"names": ["max_slot_wal_keep_size", "wal_level"]})]
        slots = (rs or {}).get("slots", {}) if isinstance(rs, dict) else {}
        top = next((r for r in _rows(slots) if r[3] is False), _rows(slots)[0] if _rows(slots) else ["debezium_ledger", "logical", "ledger", False, "extended", 0, 0])
        return "", [("submit_diagnosis", {
            "root_cause": f"Logical slot {top[0]} has been inactive for about six days (its Debezium connector was decommissioned) and retains {fmt.size(top[5] or 0)} of WAL; "
                          "max_slot_wal_keep_size = -1 lets it grow without limit.",
            "evidence": [f"slot {top[0]}: active={top[3]}, wal_status={top[4]}, retained {fmt.size(top[5] or 0)}", "max_slot_wal_keep_size = -1",
                         f"storage {c.readable('storage_percent')}"],
            "confidence": "high",
            "plan": [{"action": "drop_replication_slot", "args": {"slot": top[0]}, "rationale": "Nothing consumes it; dropping it lets the next checkpoint recycle the WAL. (High risk: a person decides.)"},
                     {"action": "alter_system_set", "args": {"name": "max_slot_wal_keep_size", "value": "20GB"}, "rationale": "A forgotten slot can never fill the disk again."}],
        })]
    if mode == "remediate":
        if c.step == 0:
            return "Dropping the abandoned slot.", [("execute_action", {"action": "drop_replication_slot", "args": {"slot": "debezium_ledger"}, "rationale": "inactive for six days"})]
        if c.step == 1:
            return "", [("execute_action", {"action": "alter_system_set", "args": {"name": "max_slot_wal_keep_size", "value": "20GB"}, "rationale": "cap slot retention"})]
        if c.step == 2:
            return "", [("current_metrics", {})]
        return "", [("submit_remediation", {"summary": f"Dropped debezium_ledger and capped max_slot_wal_keep_size at 20GB; storage now {c.readable('storage_percent')}."})]
    if mode == "verify":
        if c.step == 0:
            return "", [("metric_history", {"metric": "storage_percent", "minutes": 15})]
        return "", [("submit_verification", {"passed": True, "evidence": [f"storage_percent last {_hist_last(c, 'storage_percent')}, peak {_hist_max(c, 'storage_percent')}"],
                                             "summary": "WAL was recycled; storage is back to its usual level."})]
    if mode == "harden":
        return "", [("propose_hardening", {"summary": "Make an orphaned slot visible within an hour.", "items": [
            {"kind": "monitor", "title": "Inactive replication slot on ledger-dev", "spec": {"metric": "inactive_replication_slots", "operator": ">", "threshold": 0, "window_minutes": 60,
                                                                                             "severity": "sev3", "aggregation": "min"}},
            {"kind": "lesson", "title": "Decommissioned CDC connectors leave slots behind", "spec": {"text": "When a Debezium connector is removed, drop its slot in the same change."}},
        ]})]
    return _report(c, "WAL retained by an abandoned replication slot", "Developers' builds against ledger-dev would have failed when the disk filled.")


def idle_in_tx(c: Ctx, mode: str) -> tuple[str, list]:
    if mode == "triage":
        if c.step == 0:
            return "", [("current_metrics", {}), ("list_sessions", {"state": "idle in transaction", "limit": 20})]
        return "", [("submit_triage", {"severity": "sev3", "category": "activity", "summary": f"{c.readable('idle_in_transaction')} sessions idle in transaction; the oldest for {c.readable('longest_transaction_time_sec')}.",
                                       "hypothesis": "An application leaves transactions open between requests."})]
    if mode == "diagnose":
        return "", [("submit_diagnosis", {"root_cause": "crm_sync opens a transaction per batch and waits on an external API inside it.",
                                          "evidence": ["sessions idle in transaction are all role crm_sync", f"oldest transaction {c.readable('longest_transaction_time_sec')}"],
                                          "confidence": "medium",
                                          "plan": [{"action": "terminate_sessions", "args": {"role": "crm_sync", "state": "idle in transaction", "older_than_s": 300},
                                                    "rationale": "End transactions idle for more than 5 minutes; the sync retries its batch."}]})]
    if mode == "remediate":
        if c.step == 0:
            return "", [("execute_action", {"action": "terminate_sessions", "args": {"role": "crm_sync", "state": "idle in transaction", "older_than_s": 300}, "rationale": "the approved plan"})]
        return "", [("submit_remediation", {"summary": "Ended the stale crm_sync transactions."})]
    if mode == "verify":
        if c.step == 0:
            return "", [("metric_history", {"metric": "idle_in_transaction", "minutes": 15})]
        return "", [("submit_verification", {"passed": True, "evidence": [f"idle_in_transaction last {_hist_last(c, 'idle_in_transaction')}"], "summary": "No stale transactions left."})]
    if mode == "harden":
        return "", [("propose_hardening", {"summary": "Let the server end forgotten transactions itself.", "items": [
            _role_setting(c, "crm_sync", "idle_in_transaction_session_timeout", "5min", "idle_in_transaction_session_timeout = 5min for crm_sync")]})]
    return _report(c, "Stale transactions from crm_sync", "Vacuum was held back on crm tables; no user-facing errors.")


def pending_restart(c: Ctx, mode: str) -> tuple[str, list]:
    st = c.last("get_settings")
    if mode == "triage":
        if c.step == 0:
            return "Settings that only change at the next restart are a time bomb: whatever is in the files wins on the next failover or patch.", [
                ("get_settings", {"pending_restart_only": True})]
        names = [r[0] for r in _rows(st)]
        return "", [("submit_triage", {"severity": "sev3", "category": "configuration",
                                       "summary": f"{fmt.plural(len(names), 'setting')} {'differs' if len(names) == 1 else 'differ'} between the files and the running server: {', '.join(names)}. Nothing is broken yet.",
                                       "hypothesis": "Somebody edited a conf.d file and reloaded; the next restart activates it."})]
    if mode == "diagnose":
        rows = _rows(st)
        files = sorted({str(r[_col(st, "sourcefile")]) for r in rows if r[_col(st, "sourcefile")]})
        return "", [("escalate", {"reason": f"{', '.join(r[0] for r in rows)} will change at the next restart, set in {', '.join(files) or 'a configuration file'}. "
                                            "shared_preload_libraries is pending: if that file loads an experiment (pg_crash lives on this host), the next restart "
                                            "starts a crash loop. Reverting a file is outside what rAIden can do over SQL: review the file and revert it, or plan the "
                                            "restart deliberately. The host agent on this server can make the change if you approve it there."})]
    return generic(c, mode)


PLAYBOOKS = {"lock_queue": lock_queue, "connection_storm": connection_storm, "cpu_spike": cpu_spike, "replication_lag": replication_lag,
             "wraparound": wraparound, "wal_slot": wal_slot, "idle_in_tx": idle_in_tx, "pending_restart": pending_restart}


def generic(c: Ctx, mode: str) -> tuple[str, list]:
    if mode == "triage":
        if c.step == 0:
            return "Looking at the server before deciding anything.", [("current_metrics", {})]
        return "", [("submit_triage", {"severity": "sev3", "category": "other", "summary": "The signal is real but does not match a pattern I know well.",
                                       "hypothesis": "Needs a closer look by a person or a full model."})]
    if mode == "diagnose":
        if c.step == 0:
            return "", [("list_sessions", {"limit": 20}), ("top_queries", {"order_by": "total_time", "limit": 5})]
    return "", [("escalate", {"reason": "The demo model only knows the demo scenarios. Connect a real AI endpoint (AI endpoints page) for open-ended diagnosis, or take over."})]


# ============================================================================================================ helpers
def _hist(c: Ctx, metric: str, key: str) -> float:
    for name, data in reversed(c.results):
        if name == "metric_history" and isinstance(data, dict) and data.get("metric") == metric:
            return float(data.get(key) or 0)
    return 0.0


def _hist_last(c: Ctx, metric: str) -> str:
    return fmt.value(metric, _hist(c, metric, "last"))


def _hist_max(c: Ctx, metric: str) -> str:
    return fmt.value(metric, _hist(c, metric, "max"))


def _report(c: Ctx, title: str, impact: str) -> tuple[str, list]:
    times = re.findall(r"Detected: (\S+)", c.context)
    detected = times[0] if times else "?"
    actions = []
    for name, data in c.results:
        if name == "execute_action" and isinstance(data, str):
            actions.append(data.split(" (approved by")[0])
    body = (f"## Summary\n{title}.\n\n## Impact\n{impact}\n\n## Timeline\n- {detected} detected by monitoring\n- triage, diagnosis and remediation by rAIden "
            "(see the incident timeline for exact times and approvals)\n\n## Root cause\nSee the diagnosis on the timeline.\n\n## What rAIden did\n" +
            ("\n".join(f"- {a}" for a in actions) or "- see the timeline") + "\n\n## Prevention\nThe protections listed on the incident were added after approval.")
    return "", [("submit_report", {"report_markdown": body})]


# ============================================================================================================ chat
TEACHING = re.compile(r"^\s*(r?ai?den[,:]?\s*)?(please\s+)?(remember|note that|fyi|always|never|do not|don't|we prefer|i prefer)\b|\bfrom now on\b", re.I)


def lesson_title(text: str) -> str:
    """'remember that the migrator runs at 14:00 UTC' -> 'The migrator runs at 14:00 UTC.' (the chat and the dream agree)."""
    t = re.sub(r"^\s*(r?ai?den[,:]?\s*)?(please\s+)?(remember( that|:)?|note( that|:)?|fyi[,:]?)\s*", "", text.strip(), flags=re.I)
    t = re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0].strip().rstrip(".!") or text.strip()
    return (t[:1].upper() + t[1:])[:200] + "."


def _focus(c: Ctx) -> str | None:
    m = re.search(r"Server in focus: ([\w.-]+) \(", c.context)
    return m.group(1) if m else None


def chat(c: Ctx) -> tuple[str, list]:
    q = c.user_text.lower()
    if c.step == 0:
        if "remember" in c.tool_names and TEACHING.search(c.user_text) and not q.rstrip().endswith("?"):
            server = _focus(c)
            kind = "preference" if re.search(r"\bprefer\b", q) else "rule" if re.search(r"\b(always|never|do not|don't|must)\b", q) else "fact"
            return "", [("remember", {"kind": kind, "title": lesson_title(c.user_text), "scope": {"server": server} if server else {}})]
        if any(w in q for w in ("block", "lock", "waiting", "stuck", "frozen")):
            return "", [("lock_tree", {})]
        if any(w in q for w in ("cpu", "slow", "top quer", "expensive", "query")):
            return "", [("current_metrics", {}), ("top_queries", {"order_by": "total_time", "limit": 5})]
        if any(w in q for w in ("connection", "session", "pool", "clients")):
            return "", [("current_metrics", {}), ("list_sessions", {"limit": 30})]
        if any(w in q for w in ("replica", "lag", "replication", "standby", "slot")):
            return "", [("replication_status", {}), ("current_metrics", {})]
        if any(w in q for w in ("wraparound", "xid", "freeze", "vacuum", "bloat", "dead")):
            return "", [("wraparound_status", {}), ("table_health", {"limit": 5})]
        if any(w in q for w in ("setting", "config", "tune", "parameter")):
            return "", [("get_settings", {"pattern": ""})]
        if any(w in q for w in ("fleet", "all servers", "overview", "health", "summary", "how are", "status")) or "server in focus: none" in c.context.lower():
            return "", [("list_servers", {})]
        if any(w in q for w in ("fix", "terminate", "kill", "cancel", "end it", "do it")):
            lt = c.last("lock_tree")
            root = (lt.get("roots") or [None])[0] if isinstance(lt, dict) else None
            if root:
                return "", [("propose_action", {"action": "terminate_backend", "args": {"pid": root}, "rationale": "End the root blocker; the queue behind it drains."})]
        return "", [("current_metrics", {})]
    return _chat_answer(c), []


def _chat_answer(c: Ctx) -> str:
    lt, tq, ls, rs, wr, th, srv, cm, st = (c.last(n) for n in ("lock_tree", "top_queries", "list_sessions", "replication_status", "wraparound_status",
                                                                 "table_health", "list_servers", "current_metrics", "get_settings"))
    last_tool = c.results[-1][0] if c.results else ""
    if last_tool == "remember":
        said = str(c.results[-1][1])
        if said.startswith("ERROR"):
            return f"I could not keep that: {said[7:]}"
        args = next((json.loads(tc["function"]["arguments"]) for m in reversed(c.messages) for tc in m.get("tool_calls") or []
                     if tc["function"]["name"] == "remember"), {})
        where = (args.get("scope") or {}).get("server") or "the whole fleet"
        if said.startswith("already known"):
            return f"I already know that ({said.split(' as ')[-1]}): nothing to add."
        return (f"Remembered for **{where}**: *{args.get('title', '')}*\n\nIt is part of every incident, chat and routine from now on, for the whole team. "
                "You can edit or retire it on the Knowledge page.")
    if last_tool == "propose_action":
        outcome = str(c.results[-1][1])
        if outcome.startswith("APPROVED") and "failed" not in outcome:
            done = outcome.split("executed:", 1)[-1].strip()
            return (f"Done: {done[:1].lower() + done[1:]}\n\nThe queue drains in a few seconds; I will not touch anything else. "
                    "To keep a deploy from queueing everyone again, give the migrator role `lock_timeout = 3s`: say *harden it* and I will propose that too.")
        if outcome.startswith("APPROVED"):
            return f"It was approved but did not work: {outcome.split('failed:', 1)[-1].strip()}. Nothing else was changed; let me look again before proposing anything."
        if outcome.startswith("REJECTED"):
            return ("Understood, I will not do that. Safer routes: wait for the analyst to commit (the queue grows meanwhile), "
                    "or cancel only the migration so the writers proceed and the ALTER TABLE runs in the maintenance window.")
        return "I proposed it above; it runs as soon as you approve it."
    if last_tool == "lock_tree" and isinstance(lt, dict):
        if not lt.get("roots"):
            return "No session is waiting for a lock right now. The lock graph is empty."
        root = lt["roots"][0]
        rows = {r[0]: r for r in _rows(lt)}
        r = rows.get(root)
        waiting = lt.get("waiting_total") or sum(1 for x in _rows(lt) if x[_col(lt, "blocked_by")])
        ddl = next((x for x in _rows(lt) if "ALTER" in str(x[-1]).upper()), None)
        txt = (f"**{waiting} sessions are waiting** and they all trace back to **pid {root}**")
        if r:
            txt += (f" (role `{r[_col(lt, 'usename')]}`, {r[_col(lt, 'application_name')]}), which has been **{r[_col(lt, 'state')]} for "
                    f"{fmt.duration(r[_col(lt, 'xact_age_s')] or 0)}**.")
        if ddl:
            txt += f"\n\nThe chain: pid {root} → `{str(ddl[-1])[:70]}` (pid {ddl[0]}, waiting for AccessExclusiveLock) → every writer of `orders.orders`."
        txt += "\n\n**Recommendation:** end pid " + str(root) + " (its transaction only read data), then give the `migrator` role `lock_timeout = 3s` so a deploy fails fast instead of queueing everyone. Say *fix it* and I will propose it."
        return txt
    if last_tool == "top_queries" and isinstance(tq, dict):
        rows = _rows(tq)[:3]
        lines = [f"{i + 1}. `{str(r[-1])[:90]}` - {fmt.big(r[_col(tq, 'calls')])} calls, {fmt.big(r[_col(tq, 'mean_ms')])} ms mean" for i, r in enumerate(rows)]
        return (f"CPU is at **{c.readable('cpu_percent')}**, TPS **{c.readable('tps')}**. The statements with the most total time:\n\n" + "\n".join(lines) +
                "\n\nIf a statement with a high mean time is new, check its plan with `EXPLAIN` - a missing index is the usual cause.")
    if last_tool == "list_sessions" and isinstance(ls, dict):
        by = ls.get("by_role_state", [])[:5]
        lines = [f"- `{u}` · {s}: **{n}**" for u, s, n in by]
        return f"**{c.readable('active_connections')} connections** ({c.readable('connections_percent')} of max_connections). By role and state:\n\n" + "\n".join(lines)
    if last_tool in ("replication_status", "current_metrics") and isinstance(rs, dict):
        if rs.get("role") == "standby":
            return (f"This is a standby. Replay lag is **{fmt.duration(rs.get('replay_lag_s', 0))}**." +
                    (f"\n\nReplay is waiting for: {rs['replay_waiting_for']}." if rs.get("replay_waiting_for") else " Replay is not blocked."))
        slots = _rows(rs.get("slots", {}))
        lines = [f"- slot `{r[0]}` ({r[1]}): {'active' if r[3] else '**inactive**'}, retains {fmt.size(r[5] or 0)}" for r in slots]
        return f"Primary with {fmt.plural(len(_rows(rs)), 'standby')}.\n\n" + ("\n".join(lines) if lines else "No replication slots.")
    if last_tool == "table_health" and isinstance(wr, dict):
        top = _rows(wr)[0] if _rows(wr) else None
        holders = _rows(wr.get("xmin_holders", {}))
        txt = f"Oldest database: **{top[0]}** at **{fmt.big(top[1])} transactions** ({top[3]}% of the wraparound limit)." if top else ""
        if holders:
            h = holders[0]
            txt += f"\n\nWhat holds the horizon: {h[0]} `{h[1]}` (owner {h[2]}, age {fmt.duration(h[4] or 0)})."
        rows = _rows(th)[:3] if isinstance(th, dict) else []
        if rows:
            txt += "\n\nTables with the most dead rows:\n" + "\n".join(f"- `{r[0]}`: {fmt.big(r[2])} dead ({r[3]}%)" for r in rows)
        return txt
    if last_tool == "get_settings" and isinstance(st, dict):
        pend = [r for r in _rows(st) if r[_col(st, "pending_restart")]]
        txt = f"{len(_rows(st))} settings read."
        if pend:
            txt += "\n\n**Waiting for the next restart:** " + ", ".join(f"`{r[0]}` (from {r[_col(st, 'sourcefile')]})" for r in pend) + \
                   ". Somebody changed the files; review them before the maintenance window."
        return txt + "\n\nFor a full review with sizing advice, run **Assess configuration** on the server page."
    if last_tool == "list_servers" and isinstance(srv, dict):
        bad = [x for x in srv.get("servers", []) if x["status"] not in ("healthy",)]
        lines = [f"- **{x['name']}** ({x['environment']}, {x['platform_label']}): {x['status']}" + (f" - {x['status_reason']}" if x.get("status_reason") else "") for x in bad]
        return (f"{len(srv.get('servers', []))} servers watched; {len(bad)} need attention:\n\n" + "\n".join(lines)) if bad else "Every server is healthy right now."
    if isinstance(cm, dict):
        warn = [r for r in _rows(cm) if r[3] != "ok"]
        if warn:
            return "Out of the ordinary right now:\n\n" + "\n".join(f"- {r[0]}: **{r[2]}** ({r[3]})" for r in warn) + \
                   "\n\n*(Demo model: ask about locks, connections, top queries, replication, wraparound or settings.)*"
        return "All key metrics are within their normal ranges. *(Demo model: ask about locks, connections, top queries, replication, wraparound or settings.)*"
    return "I could not find anything specific. *(Demo model: ask about locks, connections, top queries, replication, wraparound or settings.)*"


# ============================================================================================================ routines
def routine(c: Ctx) -> tuple[str, list]:
    task = c.context.lower()
    if c.step == 0:
        if "index" in task:
            return "", [("top_queries", {"order_by": "mean_time", "limit": 5}), ("table_health", {"limit": 5})]
        if "vacuum" in task or "bloat" in task:
            return "", [("table_health", {"limit": 8}), ("wraparound_status", {})]
        return "", [("current_metrics", {}), ("top_queries", {"order_by": "total_time", "limit": 3})]
    findings = []
    th, cm = c.last("table_health"), c.last("current_metrics")
    for r in _rows(th)[:8]:
        if (r[3] or 0) > 20:
            findings.append({"kind": "suggestion", "category": "vacuum", "title": f"{r[0]}: {r[3]}% dead rows",
                             "detail": "Lower autovacuum_vacuum_scale_factor for this table (ALTER TABLE ... SET (autovacuum_vacuum_scale_factor = 0.02))."})
        if (r[_col(th, "seq_scan")] or 0) > 500 and (r[1] or 0) > 1_000_000:
            findings.append({"kind": "suggestion", "category": "performance", "title": f"{r[0]}: frequent sequential scans", "detail": "Check the top queries on this table for a missing index."})
    for r in _rows(cm):
        if r[3] != "ok":
            findings.append({"kind": "issue" if r[3] == "crit" else "suggestion", "category": "performance", "title": f"{r[0]} is {r[2]}", "detail": "See the metric on the server page."})
    status = "crit" if any(f["kind"] == "issue" for f in findings) else "warn" if findings else "ok"
    summary = "Nothing needs attention." if not findings else f"{fmt.plural(len(findings), 'finding')}; the most important: {findings[0]['title']}."
    return "", [("submit_findings", {"status": status, "summary": summary, "findings": findings[:6]})]


# ============================================================================================================ dreams
NO_ASK = re.compile(r"\b(don't|do not|no need to|stop|never) (ask|asking)\b|\bwithout asking\b|\bjust do it\b", re.I)
LOOK_FIRST = {"locks": "lock_tree for the root blocker, then list_sessions for how long its transaction has been open and what it last ran",
              "connections": "list_sessions grouped by role and state: one role holding most of the slots is the usual answer",
              "replication": "replication_status on the standby, then list_sessions there for the query replay waits for",
              "activity": "list_sessions with state 'idle in transaction', oldest first, and the application behind them",
              "cpu": "top_queries by total time, then EXPLAIN (run_sql) the new statement at the top",
              "vacuum": "table_health for the dead rows, then get_settings pattern autovacuum for the cost limit",
              "performance": "top_queries by temp, then get_settings work_mem",
              "configuration": "get_settings with pending_restart_only, and where each setting comes from",
              "wraparound": "wraparound_status for what holds the xmin horizon"}
SKILL_TITLES = {"locks": "Lock queue behind a DDL", "connections": "One role floods the connection slots", "replication": "A standby falls behind on replay",
                "activity": "Sessions left idle in transaction", "cpu": "One statement saturates the CPU", "vacuum": "Dead rows outpace autovacuum",
                "performance": "Queries spill to disk", "configuration": "Settings waiting for a restart", "wraparound": "Transaction ID age climbing"}


def _material(c: Ctx) -> dict[str, Any]:
    first = next((str(m.get("content") or "") for m in c.messages if m.get("role") == "user"), "")
    try:
        return json.loads(first.split("JSON:\n", 1)[1])
    except (IndexError, json.JSONDecodeError):
        return {}


def playbook(cand: dict[str, Any]) -> str:
    cat, incs = cand["category"], cand["incidents"]
    actions = list(dict.fromkeys(a.split("(")[0] for i in incs for a in i["actions"]))
    protections = list(dict.fromkeys(p for i in incs for p in i["protections"] if p))[:4]
    return "\n".join([
        f"Fixed this way {fmt.plural(len(incs), 'time')} ({', '.join(i['id'] for i in incs)}).",
        f"1. **Look first:** {LOOK_FIRST.get(cat, 'current_metrics, then the read tool for the signal')}.",
        f"2. **Usual root cause:** {incs[-1]['root_cause'] or 'see the incidents'}",
        "3. **The plan people approved:** " + ", ".join(f"`{a}`" for a in actions) + ", smallest first.",
        "4. **Protections that held:** " + ("; ".join(protections) if protections else "none yet: propose one in Harden") + ".",
        "5. **Ask a person when** the evidence points somewhere else, or the fix needs more than these actions.",
    ])


def dream(c: Ctx) -> tuple[str, list]:
    """A careful night: people's words become instructions with their scope, a plan someone asked not to be asked about
    becomes a proposal, and every kind of incident fixed twice gets its playbook."""
    if c.step > 0:
        said = [str(d) for name, d in c.results if name != "finish_dream"]
        added = sum(s.startswith("added") for s in said)
        skills = sum(s.startswith(("wrote skill", "updated skill")) for s in said)
        asked = sum(s.startswith("proposed learned approval") for s in said)
        known = sum(s.startswith("already known") for s in said)
        parts = [fmt.plural(added, "new instruction")] + ([fmt.plural(skills, "playbook") + " written or brought up to date"] if skills else []) + \
                ([fmt.plural(asked, "request") + " not to be asked again, for a person to accept"] if asked else [])
        return "", [("finish_dream", {"summary": "Learned " + ", ".join(parts) + "." + (f" {fmt.plural(known, 'thing')} I already knew." if known else "")
                                                 + " Questions and small talk left alone."})]
    m = _material(c)
    known = {re.sub(r"[^a-z0-9]+", " ", k["title"].lower()).strip() for k in m.get("knowledge", [])}
    calls: list[tuple[str, dict[str, Any]]] = []

    def teach(kind: str, title: str, scope: dict[str, Any], ev: dict[str, Any], body: str = "") -> None:
        if re.sub(r"[^a-z0-9]+", " ", title.lower()).strip() not in known:
            calls.append(("add_instruction", {"kind": kind, "title": title, "body": body, "scope": {k: v for k, v in scope.items() if v}, "evidence": [ev]}))

    for st in m.get("statements", []):
        if TEACHING.search(st["text"]) and not st["text"].rstrip().endswith("?"):
            q = st["text"].lower()
            kind = "preference" if "prefer" in q else "rule" if re.search(r"\b(always|never|do not|don't|must)\b", q) else "fact"
            teach(kind, lesson_title(st["text"]), {"server": st.get("server")}, {"type": "chat", "id": str(st["id"])})
    for n in m.get("notes", []):
        if len(n["text"]) >= 20:
            rule = re.search(r"\b(always|never|do not|don't|must)\b", n["text"], re.I)
            teach("rule" if rule else "fact", lesson_title(n["text"]), {"server": n.get("server"), "category": n.get("category") if rule else None},
                  {"type": "note", "id": str(n["id"])})
    for d in m.get("decisions", []):
        comment = d.get("comment", "").strip()
        if len(comment) < 12 or not d.get("category"):
            continue
        if d["decision"] == "approved" and NO_ASK.search(comment):
            have = {(x["action"], x["category"], x["environment"]) for x in m.get("learned_approvals", [])}
            for a in dict.fromkeys(x.split("(")[0] for x in d.get("actions", [])):
                if (a, d["category"], d["environment"]) not in have and a in ("cancel_backend", "terminate_backend", "terminate_sessions", "set_role_timeout",
                                                                               "set_role_connection_limit", "vacuum_table", "analyze_table"):
                    calls.append(("propose_learned_approval", {"action": a, "category": d["category"], "environment": d["environment"],
                                                               "why": f"{d['by']}: “{comment}”", "evidence": [{"type": "decision", "id": str(d["id"])}]}))
            continue
        teach("rule" if d["decision"] == "rejected" else "fact", lesson_title(comment), {"server": d.get("server"), "category": d.get("category")},
              {"type": "decision", "id": str(d["id"])}, body=f"Said when {d['by']} {d['decision']} “{d['what']}”.")
    for cand in m.get("skill_candidates", []):
        calls.append(("write_skill", {"category": cand["category"], "title": SKILL_TITLES.get(cand["category"], f"{fmt.category(cand['category'])} incidents"),
                                      "body": playbook(cand), "evidence": [{"type": "incident", "id": i["id"]} for i in cand["incidents"][-3:]]}))
    if not calls:
        return "", [("finish_dream", {"summary": "Nothing worth keeping: the new material was questions and routine work, already covered by what I know."})]
    return "Going through what the team decided and said since the last dream.", calls


# ============================================================================================================ provider
class DemoProvider:
    """The scripted stand-in for a model: it answers in the name of the endpoint it replaces (marked simulated)."""

    def __init__(self, speed: float = 1.0, model: str = "raiden-demo") -> None:
        self.speed = max(0.1, speed)
        self.model = model

    def as_model(self, model: str) -> "DemoProvider":
        return DemoProvider(self.speed, model or self.model)

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, max_output_tokens: int) -> LLMReply:
        c = Ctx(messages, tools)
        names = c.tool_names
        if "finish_dream" in names:
            mode = "dream"
        elif "propose_action" in names:
            mode = "chat"
        elif "submit_findings" in names:
            mode = "routine"
        elif not names or not any(n.startswith("submit_") or n in ("execute_action", "propose_hardening") for n in names):
            if c.user_text.startswith("ASSESSMENT SUMMARY"):
                from ..assessment import demo_summary

                await asyncio.sleep(1.5 / self.speed)
                text = demo_summary(c.user_text)
                return LLMReply(text=text, tokens_in=len(c.user_text) // 4 + 400, tokens_out=len(text) // 4, model=self.model, simulated=True)
            await asyncio.sleep(0.3)
            return LLMReply(text="ready", tokens_in=12, tokens_out=1, model=self.model, simulated=True)
        else:
            mode = next(m for m, t in (("triage", "submit_triage"), ("diagnose", "submit_diagnosis"), ("remediate", "execute_action"),
                                       ("verify", "submit_verification"), ("harden", "propose_hardening"), ("report", "submit_report")) if t in names)
        await asyncio.sleep(random.uniform(1.2, 3.2) / self.speed if mode not in ("chat",) else random.uniform(0.6, 1.4))
        if mode == "chat":
            text, calls = chat(c)
        elif mode == "routine":
            text, calls = routine(c)
        elif mode == "dream":
            text, calls = dream(c)
        else:
            text, calls = PLAYBOOKS.get(c.scenario, generic)(c, mode)
        chars_in = sum(len(str(m.get("content") or "")) for m in messages) + 6000  # system prompt and tool schemas
        out = text + json.dumps([a for _, a in calls])
        return LLMReply(
            text=text, tool_calls=[{"id": f"call_{int(time.time() * 1000) % 10**9}_{i}", "name": n, "arguments": json.dumps(a)} for i, (n, a) in enumerate(calls)],
            tokens_in=chars_in // 4, tokens_out=max(20, len(out) // 4) + 120, tokens_cached=int(chars_in // 4 * 0.6), model=self.model, simulated=True,
        )
