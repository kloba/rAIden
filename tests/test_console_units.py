"""Console building blocks that need no database server: schedules, the SQL guard, the autonomy policy, the
assessment rules, the provider message conversion, the metric store and the host agent's metrics."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from raiden.console import assessment
from raiden.console.ai.providers import OpenAIProvider, to_anthropic
from raiden.console.catalog import BY_ID, health, split_key
from raiden.console.cron import Cron, CronError, validate
from raiden.console.db import Database
from raiden.console.fmt import duration, size
from raiden.console.models import Server
from raiden.console.policy import DEFAULT_POLICY, GATED, merged, mode_for
from raiden.console.sqlguard import check_readonly, check_statement
from raiden.console.timeseries import MetricStore
from raiden.console_link import FORWARD, ConsoleLink, HostMetrics


# ------------------------------------------------------------------------------------------------ schedules
def test_cron_next_fire_times():
    assert Cron("*/15 * * * *").next_after(datetime(2026, 10, 21, 10, 7)) == datetime(2026, 10, 21, 10, 15)
    assert Cron("30 5 * * *").next_after(datetime(2026, 10, 21, 5, 30)) == datetime(2026, 10, 22, 5, 30)  # strictly after
    assert Cron("0 8 * * 1").next_after(datetime(2026, 10, 21, 9, 0)) == datetime(2026, 10, 26, 8, 0)  # next Monday
    assert Cron("0 0 1 * *").next_after(datetime(2026, 10, 21)) == datetime(2026, 11, 1)
    assert Cron("@daily").next_after(datetime(2026, 10, 21, 0, 0)) == datetime(2026, 10, 22, 0, 0)


def test_cron_time_zone_follows_daylight_saving():
    # 07:00 in Prague is 05:00 UTC in summer time and 06:00 UTC after the switch on 25 October 2026
    assert Cron("0 7 * * *").next_after(datetime(2026, 10, 21, 4, 0), "Europe/Prague") == datetime(2026, 10, 21, 5, 0)
    assert Cron("0 7 * * *").next_after(datetime(2026, 10, 26, 0, 0), "Europe/Prague") == datetime(2026, 10, 26, 6, 0)


def test_cron_describe_and_validate():
    assert Cron("*/15 * * * *").describe() == "Every 15 minutes"
    assert Cron("5 * * * *").describe() == "Every hour at :05"
    assert Cron("0 7 * * *").describe() == "Every day at 07:00"
    assert Cron("0 7 * * 1-5").describe() == "Weekdays at 07:00"
    assert Cron("0 4 * * 0").describe() == "Every Sunday at 04:00"
    assert validate("0 7 * * *") is None
    for bad in ("61 * * * *", "* * *", "0 25 * * *", "nonsense"):
        assert validate(bad), bad
    with pytest.raises(CronError):
        Cron("* * * * * *")


# ------------------------------------------------------------------------------------------------ SQL guard
@pytest.mark.parametrize("sql", [
    "select 1",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "show work_mem",
    "explain select * from orders where id = 1",
    "select 'pg_terminate_backend(1)' as text_only",       # inside a string literal
    "select 1 -- ; drop table orders",                       # inside a comment
    "select count(*) from pg_stat_activity;",
])
def test_readonly_sql_allowed(sql):
    assert check_readonly(sql) is None


@pytest.mark.parametrize("sql, why", [
    ("delete from orders", "only SELECT"),
    ("update orders set x = 1 where id = 2", "only SELECT"),
    ("select 1; drop table orders", "one statement"),
    ("select pg_terminate_backend(123)", "side effects"),
    ("select pg_reload_conf()", "side effects"),
    ("SELECT pg_sleep(600)", "side effects"),
    ("select * into orders_copy from orders", "creates a table"),
    ("explain analyze delete from orders", "executes the statement"),
    ("", "empty"),
])
def test_readonly_sql_refused(sql, why):
    problem = check_readonly(sql)
    assert problem and why in problem


@pytest.mark.parametrize("sql", [
    "drop database prod", "DROP SCHEMA app CASCADE", "drop table orders", "truncate orders", "delete from orders",
    "update orders set status = 'x'", "alter system set fsync = off", "select pg_terminate_backend(pid) from pg_stat_activity",
    "copy orders to program 'curl x'", "alter role raiden superuser",
])
def test_statement_refused(sql):
    assert check_statement(sql) is not None


@pytest.mark.parametrize("sql", [
    "drop table tmp_import", "delete from orders where id = 42", "create index concurrently ix on orders (merchant_ref)",
    "select pg_terminate_backend(pid) from pg_stat_activity where usename = 'reporting' and state = 'idle'",
])
def test_statement_allowed(sql):
    assert check_statement(sql) is None


# ------------------------------------------------------------------------------------------------ autonomy policy
def test_policy_defaults_keep_people_on_production_fixes():
    p = merged(None)
    assert mode_for(p, "production", None, "remediate") == "human"
    assert mode_for(p, "production", None, "triage") == "auto"
    assert mode_for(p, "development", None, "harden") == "auto"
    assert p["high_risk_always_human"] is True
    assert set(p["environments"]["staging"]) == set(GATED)


def test_policy_server_override_and_partial_settings():
    p = merged({"environments": {"production": {"remediate": "auto", "bogus": "auto"}}})
    assert mode_for(p, "production", None, "remediate") == "auto"
    assert "bogus" not in p["environments"]["production"]
    assert mode_for(p, "production", {"remediate": "human"}, "remediate") == "human"  # a server pins its own gate
    assert mode_for(p, "production", {"remediate": "maybe"}, "remediate") == "auto"   # invalid override is ignored
    assert mode_for(p, "production", None, "detected") == "auto"                       # detection has no gate
    assert DEFAULT_POLICY["environments"]["production"]["remediate"] == "human"           # the defaults are not mutated


# ------------------------------------------------------------------------------------------------ assessment
def _facts(**over):
    settings = {
        "fsync": {"setting": "on"}, "full_page_writes": {"setting": "on"}, "synchronous_commit": {"setting": "on"},
        "password_encryption": {"setting": "md5"}, "ssl": {"setting": "off"}, "autovacuum": {"setting": "on"},
        "shared_buffers": {"setting": "16384", "unit": "8kB", "context": "postmaster"},
        "effective_cache_size": {"setting": "524288", "unit": "8kB"}, "work_mem": {"setting": "4096", "unit": "kB"},
        "maintenance_work_mem": {"setting": "65536", "unit": "kB"}, "max_connections": {"setting": "200"},
        "random_page_cost": {"setting": "4"}, "jit": {"setting": "on"}, "archive_mode": {"setting": "off"},
    }
    f = {"settings": settings, "platform": "self_managed", "environment": "production", "version_num": 170006, "extensions": [],
         "host": {"ram_gb": 64, "vcpus": 16, "storage": "nvme", "workload": "oltp"}, "metrics": {}, "superusers": ["postgres"]}
    f.update(over)
    return f


def test_assessment_finds_real_problems_and_scores_them():
    facts = _facts()
    facts["settings"]["fsync"] = {"setting": "off"}
    findings = assessment.run_checks(facts)
    by_id = {f["id"]: f for f in findings}
    assert by_id["durability-fsync"]["severity"] == "issue"
    assert by_id["password-md5"]["area"] == "security"
    assert "ssl-off" in by_id
    score, per = assessment.scores(findings)
    assert 0 <= score < 100 and per["reliability"] < 100 and per["security"] < 100
    assert set(per) == set(assessment.AREAS)


def test_tuning_sizes_for_the_host_and_respects_managed_platforms():
    rec = {t["name"]: t for t in assessment.tuning(_facts())}
    assert rec["shared_buffers"]["recommended"] == "16GB" and rec["shared_buffers"]["restart"] is True
    assert rec["random_page_cost"]["recommended"] == "1.1"
    assert rec["jit"]["recommended"] == "off"
    assert rec["shared_buffers"]["fix"]["kind"] == "manual"  # needs a restart: rAIden never applies it by itself
    assert rec["jit"]["fix"] == {"kind": "action", "action": "alter_system_set", "args": {"name": "jit", "value": "off"}, "restart": False}
    managed = {t["name"]: t for t in assessment.tuning(_facts(platform="azure_flexible"))}
    assert managed["jit"]["fix"]["kind"] == "provider" and "Server parameters" in managed["jit"]["fix"]["how"]
    assert assessment.tuning(_facts(host={})) == []  # no host profile, no sizing advice


# ------------------------------------------------------------------------------------------------ catalog & formatting
def test_catalog_keys_and_health():
    assert split_key("tps[orders]") == ("tps", "orders")
    assert split_key("cpu_percent") == ("cpu_percent", None)
    cpu = BY_ID["cpu_percent"]
    assert health("cpu_percent", 10) == "ok" and health("cpu_percent", cpu.crit + 1) == "crit"
    assert BY_ID["cache_hit_ratio"].lower_is_worse and health("cache_hit_ratio", 50) == "crit"
    assert health("no_such_metric", 1e9) == "ok"
    assert duration(90) == "1 min 30 s" and size(1536) == "1.5 KiB"


# Every metric of "Monitor using metrics and logs in Azure Database for PostgreSQL flexible server"
# (learn.microsoft.com/azure/postgresql/monitor/concepts-monitoring, revision of 2026-10-03): default, enhanced (activity,
# database, logical replication, read replication, saturation, traffic), autovacuum, PgBouncer and availability metrics.
AZURE_POSTGRESQL_METRICS = """
active_connections backup_storage_used connections_failed connections_succeeded cpu_credits_consumed cpu_credits_remaining cpu_percent
database_size_bytes disk_queue_depth iops maximum_used_transactionIDs memory_percent network_bytes_egress network_bytes_ingress read_iops
read_throughput storage_free storage_percent storage_used txlogs_storage_used write_throughput write_iops
sessions_by_state sessions_by_wait_event_type oldest_backend_time_sec longest_query_time_sec longest_transaction_time_sec oldest_backend_xmin
oldest_backend_xmin_age numbackends deadlocks blks_hit blks_read temp_files temp_bytes xact_total xact_commit tps xact_rollback tup_deleted
tup_fetched tup_inserted tup_returned tup_updated logical_replication_delay_in_bytes logical_replication_slot_sync_status
physical_replication_delay_in_bytes physical_replication_delay_in_seconds disk_bandwidth_consumed_percentage disk_iops_consumed_percentage
postmaster_process_cpu_usage_percent max_connections tcp_connection_backlog analyze_count_user_tables autoanalyze_count_user_tables
autovacuum_count_user_tables bloat_percent n_dead_tup_user_tables n_live_tup_user_tables n_mod_since_analyze_user_tables
tables_analyzed_user_tables tables_autoanalyzed_user_tables tables_autovacuumed_user_tables tables_counter_user_tables
tables_vacuumed_user_tables vacuum_count_user_tables client_connections_active client_connections_waiting server_connections_active
server_connections_idle total_pooled_connections num_pools client_connections_total is_db_alive
""".split()


def test_catalog_covers_every_azure_postgresql_metric_under_its_azure_id():
    assert len(AZURE_POSTGRESQL_METRICS) == 74
    for name in AZURE_POSTGRESQL_METRICS:
        assert name in BY_ID, f"{name} is missing from the catalog"
        assert BY_ID[name].azure == name, f"{name} is not mapped to its Azure Monitor name"
    assert {m.azure for m in BY_ID.values() if m.azure} == set(AZURE_POSTGRESQL_METRICS)  # no invented Azure names


# ------------------------------------------------------------------------------------------------ AI providers
def test_anthropic_conversion_merges_tool_results_and_keeps_the_system_prompt():
    messages = [
        {"role": "system", "content": "You are rAIden."},
        {"role": "user", "content": "Who blocks whom?"},
        {"role": "assistant", "content": "Checking.", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "lock_tree", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "list_sessions", "arguments": "{\"state\": \"active\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "pid 1 blocks 2"},
        {"role": "tool", "tool_call_id": "c2", "content": "2 sessions"},
    ]
    system, out = to_anthropic(messages)
    assert system == "You are rAIden."
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    uses = [b for b in out[1]["content"] if b["type"] == "tool_use"]
    assert [u["name"] for u in uses] == ["lock_tree", "list_sessions"] and uses[1]["input"] == {"state": "active"}
    results = out[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["c1", "c2"]


def test_openai_wire_api_choice():
    def prov(model, wire="auto", provider="azure_openai"):
        return OpenAIProvider(provider, "https://x.openai.azure.com/", model, "k", "api_key", "2025-04-01-preview", wire, "medium")
    assert prov("gpt-6.1-sol").use_responses is True
    assert prov("gpt-4.1").use_responses is False
    assert prov("gpt-4.1", wire="responses").use_responses is True
    assert prov("qwen3", provider="openai_compatible").use_responses is False


# ------------------------------------------------------------------------------------------------ metric store
def test_metric_store_series_rollups_and_latest(tmp_path: Path):
    db = Database(f"sqlite:///{tmp_path / 's.db'}")
    db.create_all()
    with db.session() as s:
        s.add(Server(id="pg1", name="pg1", host="db1.internal", info={}))
    store = MetricStore(db)
    start = 1_790_000_000 - 1_790_000_000 % 900
    store.append_many("pg1", [(start + i * 60, {"cpu_percent": float(i), "tps[app]": 10.0 + i}) for i in range(60)])
    assert store.latest_values("pg1")["cpu_percent"] == 59.0
    assert "tps[app]" in store.keys_for("pg1")
    raw = store.series("pg1", ["cpu_percent"], start, start + 3600, max_points=1000)["cpu_percent"]
    assert len(raw) == 60 and tuple(raw[0]) == (start, 0.0)
    assert store.rollup(start + 7200) > 0
    week = store.series("pg1", ["cpu_percent"], start - 7 * 86400, start + 3600, max_points=100)["cpu_percent"]
    assert week, "long ranges are served from the 15-minute rollups"


# ------------------------------------------------------------------------------------------------ host agent link
def test_host_metrics_and_link_configuration(tmp_path: Path, monkeypatch):
    m = HostMetrics(tmp_path)
    first = m.sample()
    assert 0 < first["storage_percent"] <= 100 and first["storage_free"] > 0
    second = m.sample()
    assert all(k in BY_ID for k in second), "every host metric has a catalog entry"
    monkeypatch.delenv("RAIDEN_CONSOLE_URL", raising=False)
    assert ConsoleLink.from_env(tmp_path) is None
    monkeypatch.setenv("RAIDEN_CONSOLE_URL", "https://console.example/")
    monkeypatch.setenv("RAIDEN_CONSOLE_TOKEN", "rdn_x")
    monkeypatch.setenv("RAIDEN_SERVER_ID", "pg1")
    link = ConsoleLink.from_env(tmp_path, "gpt-6.1-sol")
    assert link and link.url == "https://console.example"
    link.hook({"type": "patrol_run"})
    link.hook({"type": "incident_opened", "id": "inc-1"})
    assert [e["type"] for e in link.pending] == ["incident_opened"] and "incident_closed" in FORWARD


def test_routine_templates_the_model_may_propose_exist():
    from raiden.console import routines
    from raiden.console.agent.tools import ROUTINE_TEMPLATES

    assert ROUTINE_TEMPLATES == [t["key"] for t in routines.TEMPLATES]


def test_fabric_database_hub_security_assessments_from_the_azure_resource():
    """The four PostgreSQL assessments of Fabric's Database Hub, read from a flexible server's ARM resource."""
    from raiden.console.drivers.azure_monitor import posture

    resource = {"sku": {"name": "Standard_D4ds_v5", "tier": "GeneralPurpose"}, "location": "swedencentral", "properties": {
        "version": "17", "state": "Ready", "authConfig": {"activeDirectoryAuth": "Disabled", "passwordAuth": "Enabled"},
        "dataEncryption": {"type": "SystemManaged"}, "network": {"publicNetworkAccess": "Enabled"}}}
    az = posture(resource)
    assert az == {"entra_auth": False, "password_auth": True, "cmk": False, "public_network": True, "sku": "Standard_D4ds_v5", "location": "swedencentral"}
    found = {f["id"]: f for f in assessment.run_checks(_facts(platform="azure_flexible", azure=az))}
    assert found["azure-entra-auth"]["severity"] == "issue" and found["azure-entra-auth"]["fix"]["kind"] == "provider"
    assert found["azure-cmk"]["severity"] == "suggestion" and found["azure-public-network"]["severity"] == "suggestion"
    assert "azure-entra-enforced" not in found  # not enabled is reported, not "not enforced" on top of it

    hardened = posture({"properties": {"authConfig": {"activeDirectoryAuth": "Enabled", "passwordAuth": "Enabled"},
                                       "dataEncryption": {"type": "AzureKeyVault"}, "network": {"publicNetworkAccess": "Disabled"}}})
    assert [f["id"] for f in assessment.azure_checks(hardened)] == ["azure-entra-enforced"]
    assert assessment.azure_checks({**hardened, "password_auth": False}) == []
    assert not [f for f in assessment.run_checks(_facts()) if f["id"].startswith("azure-")]  # no Azure resource, no Azure checks
    unread = assessment.run_checks(_facts(platform="azure_flexible", azure_error="Azure Resource Manager 403: AuthorizationFailed"))
    assert [f["id"] for f in unread if f["id"].startswith("azure-")] == ["azure-unreadable"]


def test_sign_in_throttle_window_and_the_content_security_policy(tmp_path: Path):
    import base64
    import hashlib

    from raiden.console.app import content_security_policy
    from raiden.console.security import LoginThrottle

    t = LoginThrottle()
    for i in range(5):
        assert t.wait("10.0.0.9", "ada@example.com", now=1000 + i) == 0
        t.failed("10.0.0.9", "ada@example.com", now=1000 + i)
    assert t.wait("10.0.0.9", "ada@example.com", now=1010) == 891     # until the first failure is 15 minutes old
    assert t.wait("10.0.0.9", "ada@example.com", now=1901) == 0
    assert t.wait("10.0.0.9", "vic@example.com", now=1010) == 0       # another account from the same address: 5 of 20 used
    assert t.wait("10.0.0.7", "ada@example.com", now=1010) == 0       # the same account from another address

    script = '\n      (function () { document.documentElement.dataset.theme = "dark"; })();\n    '
    index = tmp_path / "index.html"
    index.write_text(f"<html><head><script>{script}</script><script type=\"module\" src=\"/assets/app.js\"></script></head></html>")
    csp = content_security_policy(index)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'self' 'sha256-{digest}';" in csp and "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp


def test_json_logs_for_a_log_pipeline(capsys):
    import json
    import logging

    from raiden.console.settings import ConsoleSettings

    ConsoleSettings.from_env(log_format="json", log_level="info").configure_logging()
    logging.getLogger("raiden.console").info("rAIden Console %s ready", "0.2.0")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["level"] == "info" and line["logger"] == "raiden.console" and line["message"] == "rAIden Console 0.2.0 ready"
    logging.getLogger().handlers.clear()


def test_the_data_directory_from_the_command_line_holds_the_database(tmp_path: Path, monkeypatch):
    from raiden.console.settings import ConsoleSettings

    monkeypatch.delenv("RAIDEN_CONSOLE_DATA", raising=False)
    monkeypatch.delenv("RAIDEN_CONSOLE_DB", raising=False)
    assert ConsoleSettings.from_env(data_dir=tmp_path).database_url == f"sqlite:///{tmp_path / 'console.db'}"  # raiden console --data


def test_the_image_installs_every_dependency_pinned_and_hashed():
    """The image installs requirements/console.lock with --require-hashes: a dependency added to pyproject.toml
    without `make lock` fails here, not in the image build."""
    import re

    tomllib = pytest.importorskip("tomllib")
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    lock = (root / "requirements" / "console.lock").read_text()

    def norm(name):
        return re.sub(r"[-_.]+", "-", name).lower()

    def numbers(version):
        return tuple(int(x) for x in re.findall(r"\d+", version)[:3])

    pins = {norm(n): v for n, v in re.findall(r"^([A-Za-z0-9][\w.-]*)==([^\s;\\]+)", lock, re.M)}
    entries = [e for e in re.split(r"\n(?=[A-Za-z0-9])", lock) if "==" in e]
    assert len(entries) == len(pins) and all("--hash=sha256:" in e for e in entries)
    assert "--hash=md5:" not in lock  # some mirrors list md5 too, and pip refuses it
    for spec in project["dependencies"] + project["optional-dependencies"]["console"]:
        name, minimum = re.match(r"([A-Za-z0-9][\w.-]*)(?:\[[^\]]*\])?>=([\d.]+)", spec).groups()
        pinned = pins.get(norm(name))
        assert pinned and numbers(pinned) >= numbers(minimum), f"{spec} is not in requirements/console.lock: run make lock"


def test_a_webhook_is_retried_and_a_lost_notification_is_in_the_audit_log(tmp_path: Path, monkeypatch):
    import asyncio
    import types

    import httpx
    from sqlalchemy import select

    from raiden.console import notify
    from raiden.console.models import AuditEntry
    from raiden.console.services import put_setting

    answers = {"flaky": [500, 200], "gone": [404], "down": [503, 503, 503]}
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.strip("/"))
        return httpx.Response(answers[calls[-1]].pop(0), text="no")

    client = httpx.AsyncClient
    monkeypatch.setattr(notify, "RETRY_S", (0.0, 0.0))
    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(answer), **kw))
    db = Database(f"sqlite:///{tmp_path / 'console.db'}")
    db.create_all()
    with db.session() as s:
        put_setting(s, "notifications", {"webhooks": [{"name": n, "url": f"https://hooks.example/{n}", "kind": "webhook"} for n in answers]})
    asyncio.run(notify.send(types.SimpleNamespace(db=db, settings=types.SimpleNamespace(base_url="")), "approval", {}))

    assert calls == ["flaky", "flaky", "gone", "down", "down", "down"]  # a 404 will not get better by asking again
    with db.session() as s:
        failed = {a.target: a.detail["error"] for a in s.execute(select(AuditEntry).where(AuditEntry.action == "notification.failed")).scalars()}
    assert failed == {"gone": "HTTP 404: no", "down": "HTTP 503: no"}
