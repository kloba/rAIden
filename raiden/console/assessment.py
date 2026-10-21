"""Server assessment: the review a senior DBA does when a database is onboarded, and again whenever asked.

Deterministic checks over facts gathered from the server (settings, extensions, roles, indexes, tables, slots, metrics) in six
areas - configuration, performance, reliability, security, observability, maintenance - plus PGTune-style sizing from the
host's memory, CPUs, storage and workload. Every finding says why it matters and how to fix it; fixes that rAIden can carry
out are typed actions and go through the same approvals as incident remediation. Managed services get provider guidance
instead of ALTER SYSTEM.
"""

from __future__ import annotations

import re
from typing import Any

from . import fmt

AREAS = ["configuration", "performance", "reliability", "security", "observability", "maintenance"]
MANAGED = {"azure_flexible", "aws_rds", "aws_aurora", "gcp_cloudsql"}
PROVIDER_HOW = {
    "azure_flexible": "Azure portal → Server parameters (or az postgres flexible-server parameter set)",
    "aws_rds": "the DB parameter group (aws rds modify-db-parameter-group)",
    "aws_aurora": "the DB cluster parameter group",
    "gcp_cloudsql": "database flags (gcloud sql instances patch --database-flags)",
    "kubernetes": "the cluster manifest (spec.postgresql.parameters)",
}
UNIT_BYTES = {"B": 1, "kB": 1024, "8kB": 8192, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def setting_bytes(s: dict[str, Any] | None) -> float | None:
    if not s or s.get("setting") in (None, "", "-1"):
        return None
    try:
        return float(s["setting"]) * UNIT_BYTES.get(s.get("unit") or "B", 1)
    except (TypeError, ValueError):
        return None


def _pretty(n: float) -> str:
    gb = n / 1024**3
    if gb >= 1 and abs(gb - round(gb)) < 0.05:
        return f"{round(gb)}GB"
    return f"{round(n / 1024**2)}MB"


def finding(fid: str, area: str, severity: str, title: str, detail: str, recommendation: str, evidence: str = "", fix: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": fid, "area": area, "severity": severity, "title": title, "detail": detail, "recommendation": recommendation, "evidence": evidence,
            "fix": fix or {}}


def _setting_fix(platform: str, name: str, value: str, restart: bool) -> dict[str, Any]:
    if platform in MANAGED or platform == "kubernetes":
        return {"kind": "provider", "how": f"Set {name} = {value} in {PROVIDER_HOW[platform]}" + (" (needs a restart)" if restart else ""), "restart": restart}
    if restart:
        return {"kind": "manual", "how": f"ALTER SYSTEM SET {name} = '{value}'; then restart in the maintenance window", "restart": True,
                "sql": f"ALTER SYSTEM SET {name} = '{value}';"}
    return {"kind": "action", "action": "alter_system_set", "args": {"name": name, "value": value}, "restart": False}


# ================================================================================================================ sizing
def tuning(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """PGTune-style recommendations: [{name, current, recommended, reason, restart}] for the settings that differ."""
    host = facts.get("host") or {}
    ram_gb, cpus = float(host.get("ram_gb") or 0), int(host.get("vcpus") or 0)
    storage, workload = host.get("storage") or "ssd", host.get("workload") or "oltp"
    st = facts.get("settings") or {}
    if ram_gb <= 0:
        return []
    ram = ram_gb * 1024**3
    max_conn = int((st.get("max_connections") or {}).get("setting") or 100)
    rec: list[tuple[str, float | str, str, bool]] = []
    sb = ram / 4
    rec.append(("shared_buffers", sb, "25% of RAM", True))
    rec.append(("effective_cache_size", ram * 3 / 4, "75% of RAM: what the OS page cache can hold", False))
    rec.append(("maintenance_work_mem", min(2 * 1024**3, ram / 16), "RAM/16, at most 2GB: faster vacuum and index builds", False))
    wm = max(4 * 1024**2, (ram - sb) / (max_conn * (2 if workload == "olap" else 3)) / (1 if workload == "olap" else 2))
    cur_wm = setting_bytes(st.get("work_mem")) or 4 * 1024**2
    if cur_wm < wm / 2 or cur_wm * max_conn * 2 > ram:  # raise it when far too small, lower it only when it can exhaust memory
        rec.append(("work_mem", min(wm, 512 * 1024**2), f"(RAM - shared_buffers) / ({max_conn} connections × sorts per query)", False))
    rec.append(("random_page_cost", "1.1" if storage in ("ssd", "nvme", "network") else "4", f"{storage.upper()} storage", False))
    rec.append(("effective_io_concurrency", "200" if storage in ("ssd", "nvme") else ("100" if storage == "network" else "2"), f"{storage.upper()} storage", False))
    rec.append(("checkpoint_completion_target", "0.9", "Spread checkpoint I/O", False))
    rec.append(("max_wal_size", 16 * 1024**3 if workload == "oltp" and ram_gb >= 32 else 4 * 1024**3, "Fewer forced checkpoints under write load", False))
    rec.append(("min_wal_size", 1 * 1024**3 if ram_gb >= 8 else 512 * 1024**2, "Keep recycled WAL", False))
    if cpus:
        rec.append(("max_worker_processes", str(max(8, cpus)), f"{cpus} vCPUs", True))
        rec.append(("max_parallel_workers", str(max(2, cpus)), f"{cpus} vCPUs", False))
        rec.append(("max_parallel_workers_per_gather", str(max(2, min(cpus // 2, 4 if workload == "oltp" else 8))), f"{workload.upper()} workload", False))
        rec.append(("autovacuum_max_workers", str(3 if cpus < 8 else 6), f"{cpus} vCPUs", True))
    rec.append(("autovacuum_vacuum_cost_limit", "2000" if storage in ("ssd", "nvme") else "400", "Let autovacuum keep up on fast storage", False))
    rec.append(("wal_compression", "lz4" if (facts.get("version_num") or 0) >= 150000 else "on", "Less WAL for full-page writes", False))
    if ram_gb >= 32 and facts.get("platform") not in MANAGED:
        rec.append(("huge_pages", "try", "Large shared_buffers: fewer TLB misses", True))
    if workload == "oltp":
        rec.append(("jit", "off", "JIT compile time hurts short OLTP queries", False))
    out = []
    for name, value, reason, restart in rec:
        cur = st.get(name)
        if cur is None:
            continue
        if not isinstance(value, str):  # a size in bytes
            cur_b = setting_bytes(cur)
            if cur_b is not None and 0.75 * value <= cur_b <= 1.33 * value:
                continue
            current = _pretty(cur_b) if cur_b is not None else str(cur.get("setting"))
            target = _pretty(float(value))
        else:
            if str(cur.get("setting")) == str(value):
                continue
            current, target = f"{cur.get('setting')}{cur.get('unit') or ''}" if cur.get("unit") in ("ms", "s") else str(cur.get("setting")), str(value)
        out.append({"name": name, "current": current, "recommended": target, "reason": reason,
                    "restart": restart or (cur.get("context") == "postmaster"), "fix": _setting_fix(facts.get("platform", ""), name, target, restart or cur.get("context") == "postmaster")})
    return out


# ================================================================================================================ checks
def run_checks(facts: dict[str, Any]) -> list[dict[str, Any]]:
    st: dict[str, dict[str, Any]] = facts.get("settings") or {}
    platform = facts.get("platform", "")
    managed = platform in MANAGED
    env = facts.get("environment", "production")
    m = facts.get("metrics") or {}
    exts = {e.split()[0] for e in facts.get("extensions") or []}
    val = lambda n: str((st.get(n) or {}).get("setting", ""))  # noqa: E731
    out: list[dict[str, Any]] = []

    # -- reliability -----------------------------------------------------------------------------------------
    for name in ("fsync", "full_page_writes"):
        if val(name) == "off":
            out.append(finding(f"durability-{name}", "reliability", "issue", f"{name} is off", "A crash can corrupt the database.",
                               f"Turn {name} back on now.", f"{name} = off", _setting_fix(platform, name, "on", False)))
    if val("synchronous_commit") in ("off", "local") and env == "production":
        out.append(finding("durability-synchronous_commit", "reliability", "suggestion", f"synchronous_commit = {val('synchronous_commit')}",
                           "Commits acknowledged to the application can be lost in a crash (up to wal_writer_delay × 3).", "Use on unless the loss is acceptable for this data.",
                           f"synchronous_commit = {val('synchronous_commit')}"))
    pend = [n for n, s in st.items() if s.get("pending_restart")]
    if pend:
        out.append(finding("pending-restart", "reliability", "issue", f"{fmt.plural(len(pend), 'setting')} {'changes' if len(pend) == 1 else 'change'} at the next restart",
                           "The configuration files differ from the running server. The next restart (a failover, a patch) activates whatever is in them.",
                           "Review the files (pg_file_settings) and either revert the change or plan the restart deliberately.", ", ".join(pend[:6]),
                           {"kind": "manual", "how": "SELECT name, setting, sourcefile, sourceline FROM pg_file_settings WHERE applied IS FALSE OR name IN (" +
                            ", ".join(f"'{n}'" for n in pend[:6]) + ");", "restart": False}))
    if val("data_checksums") == "off":
        out.append(finding("checksums", "reliability", "suggestion", "Data checksums are off", "Storage corruption goes unnoticed until it spreads into backups.",
                           "Enable checksums: pg_checksums --enable on a stopped server (or on the next rebuild); on managed services ask the provider.",
                           "data_checksums = off", {"kind": "manual", "how": "pg_checksums --enable -D $PGDATA (server stopped)", "restart": True}))
    if not managed and platform not in ("kubernetes",) and val("archive_mode") in ("off", ""):
        out.append(finding("archiving", "reliability", "issue", "WAL archiving is off", "Without archived WAL there is no point-in-time recovery: a DROP TABLE is restored from last night's dump at best.",
                           "archive_mode = on with a tested archive_command (pgBackRest, WAL-G, Barman).", "archive_mode = off", _setting_fix(platform, "archive_mode", "on", True)))
    if env == "production" and not facts.get("in_recovery") and facts.get("replicas") == 0 and not managed and platform != "kubernetes":
        out.append(finding("no-standby", "reliability", "suggestion", "No streaming standby", "A host failure means a restore from backup, not a failover.",
                           "Add a streaming standby (Patroni, repmgr, pg_auto_failover) and test the failover."))
    slots = facts.get("slots") or []
    for sl in slots:
        if not sl.get("active"):
            out.append(finding(f"slot-{sl['name']}", "reliability", "issue", f"Inactive replication slot {sl['name']}",
                               "It keeps WAL (and, for logical slots, catalog rows) until its consumer returns; the disk fills.",
                               "Find its consumer; drop the slot if it is gone (a person decides).", f"retains {fmt.size(sl.get('retained_bytes') or 0)}",
                               {"kind": "action", "action": "drop_replication_slot", "args": {"slot": sl["name"]}, "restart": False}))
    if slots and val("max_slot_wal_keep_size") in ("-1", ""):
        out.append(finding("slot-cap", "reliability", "suggestion", "Replication slots may retain unlimited WAL", "One abandoned slot can fill the disk.",
                           "Cap it: max_slot_wal_keep_size = 20GB (the slot is invalidated instead of the server stopping).", "max_slot_wal_keep_size = -1",
                           _setting_fix(platform, "max_slot_wal_keep_size", "20GB", False)))
    xid = m.get("maximum_used_transactionIDs") or max((d.get("xid_age") or 0 for d in facts.get("databases") or []), default=0)
    if xid and xid > 500_000_000:
        out.append(finding("xid-age", "reliability", "issue" if xid > 1_000_000_000 else "suggestion", f"Transaction ID age {fmt.big(xid)}",
                           "Autovacuum is not freezing fast enough; at about 2.1 billion PostgreSQL stops accepting writes.",
                           "Find what holds the xmin horizon (long transactions, prepared transactions, slots) and VACUUM (FREEZE) the oldest tables.", fmt.big(xid)))
    if facts.get("in_recovery") and val("hot_standby_feedback") == "off" and val("max_standby_streaming_delay") == "-1":
        out.append(finding("standby-delay", "reliability", "suggestion", "Replay can wait forever for standby queries",
                           "max_standby_streaming_delay = -1: one long report stops replay and the standby falls behind without limit.",
                           "Set max_standby_streaming_delay = 30s (and keep long reports on a dedicated replica).", "max_standby_streaming_delay = -1",
                           _setting_fix(platform, "max_standby_streaming_delay", "30s", False)))

    # -- security --------------------------------------------------------------------------------------------
    if val("password_encryption") == "md5":
        out.append(finding("password-md5", "security", "issue", "Passwords are hashed with MD5", "MD5 hashes are replayable and deprecated; PostgreSQL 18 warns on use.",
                           "password_encryption = scram-sha-256, then reset each login's password so it is re-hashed.", "password_encryption = md5",
                           _setting_fix(platform, "password_encryption", "scram-sha-256", False)))
    if val("ssl") == "off":
        out.append(finding("ssl-off", "security", "issue", "TLS is off", "Passwords and data cross the network in clear text.", "Enable ssl with a proper certificate; require it in pg_hba.conf (hostssl).",
                           "ssl = off", _setting_fix(platform, "ssl", "on", False)))
    ssl_pct = facts.get("ssl_pct")
    if ssl_pct is not None and ssl_pct < 100 and val("ssl") == "on":
        out.append(finding("ssl-partial", "security", "suggestion", f"{100 - ssl_pct:.0f}% of client connections do not use TLS", "TLS is available but not required.",
                           "Require TLS: hostssl lines in pg_hba.conf (or require_secure_transport on Azure).", f"{ssl_pct:.0f}% encrypted"))
    supers = [r for r in facts.get("superusers") or [] if r not in ("postgres", "rdsadmin", "azuresu", "azure_superuser", "cloudsqladmin", "cloudsqlsuperuser")]
    if len(supers) > 1:
        out.append(finding("superusers", "security", "suggestion", f"{len(supers)} login roles are superusers", "Every superuser can read any file the server can and run programs as the server.",
                           "Keep one break-glass superuser; give the others pg_monitor, pg_signal_backend or ownership instead.", ", ".join(supers[:8])))
    if facts.get("public_schema_create"):
        out.append(finding("public-create", "security", "issue", "Everybody may create objects in schema public",
                           "Any login can plant functions or tables that shadow application objects (CVE-2018-1058 pattern). Usually left over from an upgrade from before PostgreSQL 15.",
                           "REVOKE CREATE ON SCHEMA public FROM PUBLIC;", "PUBLIC has CREATE on public",
                           {"kind": "action", "action": "execute_sql", "args": {"sql": "REVOKE CREATE ON SCHEMA public FROM PUBLIC"}, "restart": False}))
    risky = sorted(exts & {"pg_surgery", "adminpack", "dblink", "plpython3u", "plperlu", "file_fdw", "lo"})
    if risky:
        out.append(finding("risky-extensions", "security", "suggestion", f"Extensions worth a review: {', '.join(risky)}", "They can bypass normal safety (pg_surgery edits heap tuples directly; untrusted languages run code as the server).",
                           "Drop what is not needed; restrict the rest to administrators.", ", ".join(risky)))
    if env == "production" and "pgaudit" not in exts:
        out.append(finding("no-audit", "security", "suggestion", "No audit logging (pgaudit)", "Who changed what cannot be answered after an incident.",
                           "Install pgaudit and log DDL and role changes (pgaudit.log = 'ddl, role').", "pgaudit not installed"))
    if val("log_connections") == "off":
        out.append(finding("log-connections", "security", "suggestion", "Connections are not logged", "Failed logins and unexpected clients are invisible.",
                           "log_connections = on (and log_disconnections = on).", "log_connections = off", _setting_fix(platform, "log_connections", "on", False)))
    out += azure_checks(facts.get("azure"), facts.get("azure_error"))

    # -- observability ---------------------------------------------------------------------------------------
    if "pg_stat_statements" not in exts:
        out.append(finding("pgss", "observability", "issue", "pg_stat_statements is not installed", "Without it nobody (and no AI) can tell which queries use the time.",
                           "Add pg_stat_statements to shared_preload_libraries (restart), then CREATE EXTENSION pg_stat_statements.", "",
                           {"kind": "manual", "how": "shared_preload_libraries += 'pg_stat_statements' (restart); CREATE EXTENSION pg_stat_statements;", "restart": True}))
    for name, good, why in (("track_io_timing", "on", "I/O time per query and table becomes visible"), ("log_lock_waits", "on", "lock waits longer than deadlock_timeout reach the log"),
                            ("log_autovacuum_min_duration", "10s", "slow autovacuum runs reach the log"), ("log_checkpoints", "on", "checkpoint pressure becomes visible")):
        cur = val(name)
        if cur and cur in ("off", "-1"):
            out.append(finding(f"log-{name}", "observability", "suggestion", f"{name} = {cur}", why[0].upper() + why[1:] + " when it is enabled.",
                               f"{name} = {good}", f"{name} = {cur}", _setting_fix(platform, name, good, False)))
    if val("log_min_duration_statement") == "-1":
        out.append(finding("slow-log", "observability", "suggestion", "Slow queries are not logged", "There is no record of a slow statement once it is gone.",
                           "log_min_duration_statement = 1000 (ms).", "log_min_duration_statement = -1", _setting_fix(platform, "log_min_duration_statement", "1000", False)))

    # -- configuration & performance ---------------------------------------------------------------------------
    for t in tuning(facts):
        sev = "suggestion"
        area = "performance" if t["name"] in ("work_mem", "random_page_cost", "effective_io_concurrency", "jit", "max_parallel_workers_per_gather", "effective_cache_size") else "configuration"
        out.append(finding(f"tune-{t['name']}", area, sev, f"{t['name']}: {t['current']} → {t['recommended']}", t["reason"][:1].upper() + t["reason"][1:] + ".",
                           f"Set {t['name']} = {t['recommended']}" + (" (needs a restart)" if t["restart"] else ""), f"current {t['current']}", t["fix"]))
    max_conn = int(val("max_connections") or 100)
    if max_conn > 500 and "pgbouncer" not in (facts.get("poolers") or []):
        out.append(finding("pooler", "performance", "suggestion", f"max_connections = {max_conn} without a pooler",
                           "Hundreds of backends cost memory and lock-manager contention; most of them are idle.",
                           "Put PgBouncer (transaction pooling) in front and lower max_connections.", f"max_connections = {max_conn}"))
    if (m.get("checkpoints_requested") or 0) > 0.5:
        out.append(finding("forced-checkpoints", "performance", "suggestion", "Checkpoints are forced by WAL volume", "Each forced checkpoint is an I/O spike and more full-page writes.",
                           "Raise max_wal_size until checkpoints happen by time.", f"{m.get('checkpoints_requested')} requested checkpoints per minute"))
    hit = m.get("cache_hit_ratio")
    if hit is not None and hit < 95:
        out.append(finding("cache-hit", "performance", "suggestion", f"Cache hit ratio {hit:.1f}%", "Many reads go to disk.", "Check shared_buffers sizing and the top queries by I/O.", f"{hit:.1f}%"))
    if val("autovacuum") == "off":
        out.append(finding("autovacuum-off", "configuration", "issue", "Autovacuum is off", "Dead rows pile up and transaction ID wraparound becomes a matter of time.",
                           "autovacuum = on", "autovacuum = off", _setting_fix(platform, "autovacuum", "on", False)))
    if val("autovacuum_vacuum_scale_factor") in ("0.2", "0.20") and any((t.get("live") or 0) > 50_000_000 for t in facts.get("largest_tables") or []):
        out.append(finding("av-scale", "configuration", "suggestion", "Autovacuum waits for 20% dead rows on very large tables",
                           "On a billion-row table that is 200 million dead rows before vacuum starts.", "autovacuum_vacuum_scale_factor = 0.05 (or per large table).",
                           "autovacuum_vacuum_scale_factor = 0.2", _setting_fix(platform, "autovacuum_vacuum_scale_factor", "0.05", False)))

    # -- maintenance -------------------------------------------------------------------------------------------
    for ix in facts.get("invalid_indexes") or []:
        out.append(finding(f"invalid-{ix}", "maintenance", "issue", f"Invalid index {ix}", "A failed CREATE INDEX CONCURRENTLY left it behind: writes maintain it, queries cannot use it.",
                           f"REINDEX INDEX CONCURRENTLY {ix} (or drop it).", ix, {"kind": "action", "action": "reindex_index_concurrently",
                                                                                    "args": {"database": facts.get("dbname", ""), "index": ix}, "restart": False}))
    for ix in (facts.get("unused_indexes") or [])[:5]:
        out.append(finding(f"unused-{ix['name']}", "maintenance", "suggestion", f"Unused index {ix['name']} ({fmt.size(ix.get('size_bytes') or 0)})",
                           "Never scanned since statistics were reset, yet every write maintains it.",
                           "Confirm it is unused on the replicas too, then DROP INDEX CONCURRENTLY.", f"{ix.get('idx_scan', 0)} scans, on {ix.get('table')}"))
    for grp in (facts.get("duplicate_indexes") or [])[:5]:
        out.append(finding(f"dup-{grp['indexes'][0]}", "maintenance", "suggestion", f"Duplicate indexes on {grp['table']}", "Same columns, same order: one of them is pure write overhead.",
                           f"Keep one of {', '.join(grp['indexes'])}.", ", ".join(grp["indexes"])))
    for fk in (facts.get("fk_without_index") or [])[:5]:
        out.append(finding(f"fk-{fk['table']}-{fk['columns']}", "performance", "suggestion", f"Foreign key without an index: {fk['table']}({fk['columns']})",
                           "Deletes and updates on the referenced table scan this one and take stronger locks.",
                           f"CREATE INDEX CONCURRENTLY ON {fk['table']} ({fk['columns']})", fk.get("constraint", ""),
                           {"kind": "action", "action": "create_index_concurrently", "args": {"database": facts.get("dbname", ""), "table": fk["table"], "columns": fk["columns"].split(",")}, "restart": False}))
    nopk = facts.get("tables_without_pk") or []
    if nopk:
        out.append(finding("no-pk", "maintenance", "suggestion", f"{fmt.plural(len(nopk), 'table')} without a primary key", "Logical replication cannot replicate their UPDATE/DELETE, and duplicates creep in.",
                           "Add a primary key (or REPLICA IDENTITY FULL as a stopgap).", ", ".join(nopk[:6])))
    for t in facts.get("largest_tables") or []:
        if (t.get("dead_pct") or 0) > 20 and (t.get("live") or 0) > 100_000:
            out.append(finding(f"bloat-{t['name']}", "maintenance", "suggestion", f"{t['name']}: {t['dead_pct']}% dead rows", "Autovacuum is not keeping up with this table.",
                               f"ALTER TABLE {t['name']} SET (autovacuum_vacuum_scale_factor = 0.02); VACUUM it once now.", f"{fmt.big(t.get('dead') or 0)} dead rows",
                               {"kind": "action", "action": "vacuum_table", "args": {"database": facts.get("dbname", ""), "table": t["name"]}, "restart": False}))
    return out


def azure_checks(az: dict[str, Any] | None, error: str | None = None) -> list[dict[str, Any]]:
    """The four PostgreSQL security assessments of Microsoft Fabric's Database Hub, from the flexible server's Azure
    resource (drivers/azure_monitor.posture): same names, same issue/suggestion split."""
    if error:
        return [finding("azure-unreadable", "observability", "suggestion", "The server's Azure resource could not be read",
                        "Without it the Azure security assessments (Entra authentication, encryption key, public access) and the platform metrics are missing.",
                        "Give the console's identity the Monitoring Reader role on the server; it covers both.", error[:200])]
    if not az:
        return []
    out = []

    def portal(how: str) -> dict[str, Any]:
        return {"kind": "provider", "how": how, "restart": False}
    if not az.get("entra_auth"):
        out.append(finding("azure-entra-auth", "security", "issue", "Microsoft Entra authentication is not enabled",
                           "Logins are PostgreSQL passwords only: no central identity, no conditional access, no managed identities for applications.",
                           "Enable Microsoft Entra authentication and add an Entra administrator.", "authConfig.activeDirectoryAuth = Disabled",
                           portal("Azure portal → the server → Security → Authentication: PostgreSQL and Microsoft Entra authentication")))
    elif az.get("password_auth"):
        out.append(finding("azure-entra-enforced", "security", "suggestion", "Microsoft Entra authentication is not enforced",
                           "Password logins are still accepted next to Entra ID.",
                           "Once every application and person signs in with Entra ID, allow Microsoft Entra authentication only.",
                           "authConfig.passwordAuth = Enabled", portal("Azure portal → the server → Security → Authentication: Microsoft Entra authentication only")))
    if not az.get("cmk"):
        out.append(finding("azure-cmk", "security", "suggestion", "Data at rest uses a service-managed key",
                           "The data is encrypted, but with a key Microsoft manages; some policies require a customer-managed key.",
                           "If your policy requires it, use a customer-managed key from Azure Key Vault. It is chosen when a server is created: restore or "
                           "replicate into a new server with the key.", "dataEncryption.type = SystemManaged",
                           portal("Create the new server with Data encryption → customer-managed key, then move the workload (point-in-time restore or a replica)")))
    if az.get("public_network"):
        out.append(finding("azure-public-network", "security", "suggestion", "Public network access is enabled",
                           "The server accepts connections from the internet, limited only by its firewall rules.",
                           "Reach it through a private endpoint (or VNet integration) and disable public network access.",
                           "network.publicNetworkAccess = Enabled", portal("Azure portal → the server → Networking: add a private endpoint, then Public access → Disabled")))
    return out


def scores(findings: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
    per = {}
    for area in AREAS:
        fs = [f for f in findings if f["area"] == area]
        per[area] = max(0, 100 - 22 * sum(f["severity"] == "issue" for f in fs) - 7 * sum(f["severity"] == "suggestion" for f in fs))
    weights = {"reliability": 3, "security": 3, "configuration": 2, "performance": 2, "observability": 1, "maintenance": 1}
    overall = round(sum(per[a] * weights[a] for a in AREAS) / sum(weights.values()))
    return overall, per


def summary_prompt(server: str, findings: list[dict[str, Any]], score: int) -> str:
    lines = [f"ASSESSMENT SUMMARY for {server}: overall score {score}/100.", "Findings (area | severity | title | evidence):"]
    lines += [f"- {f['area']} | {f['severity']} | {f['title']} | {f.get('evidence', '')}" for f in findings]
    lines.append("Write 4-7 sentences for the database owner: what state the server is in, the three most important things to do first "
                 "(why each matters for this server), and what can wait. Plain Markdown, no headings, no tables.")
    return "\n".join(lines)


def demo_summary(prompt: str) -> str:
    score = re.search(r"overall score (\d+)", prompt)
    issues = re.findall(r"- (\w+) \| issue \| ([^|]+) \|", prompt)
    sugg = re.findall(r"- (\w+) \| suggestion \| ([^|]+) \|", prompt)
    s = int(score.group(1)) if score else 0
    state = "in good shape" if s >= 85 else "solid but with gaps" if s >= 70 else "at risk in a few places"
    text = f"This server is **{state}** (score {s}/100)."
    if issues:
        text += " Fix first: " + "; ".join(f"**{t.strip()}** ({a})" for a, t in issues[:3]) + "."
    if sugg:
        text += f" After that, {len(sugg)} suggestions mostly about " + ", ".join(sorted({a for a, _ in sugg})[:3]) + " can go into the next maintenance window."
    text += " Every fix rAIden can apply goes through approval; settings that need a restart are marked."
    return text
