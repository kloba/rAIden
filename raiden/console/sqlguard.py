"""SQL guard for the console's AI tools.

Read-only SQL runs in a READ ONLY transaction with a statement timeout; on top of that, functions that act even inside a
read-only transaction (pg_terminate_backend, pg_reload_conf, dblink ...) are refused lexically. Statements that change
something are never free text: they are typed actions (drivers/base.py ACTIONS) or `execute_sql`, which always needs a
person and is checked against the same rules as the host agent's guardrails.
"""

from __future__ import annotations

import re

from ..guardrails import DENY_ANY, SAFE_TABLE

READ_START = re.compile(r"^\s*(select|with|show|explain|values|table)\b", re.I)
SIDE_EFFECT_FUNCS = re.compile(
    r"\b(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_promote|pg_wal_replay_(pause|resume)|"
    r"pg_create_restore_point|pg_switch_wal|pg_drop_replication_slot|pg_create_(physical|logical)_replication_slot|"
    r"pg_replication_slot_advance|pg_logical_emit_message|pg_stat_reset\w*|pg_advisory\w*|pg_notify|set_config|setval|nextval|"
    r"dblink\w*|lo_import|lo_export|lo_unlink|pg_read_file|pg_read_binary_file|pg_ls_dir|pg_file_write|pg_sleep\w*|"
    r"pg_import_system_collations|pg_log_backend_memory_contexts|query_to_xml|query_to_json\w*|txid_current|pg_current_xact_id)\s*\(",
    re.I,
)
SELECT_INTO = re.compile(r"\bselect\b[^;]*?\binto\s+(?!strict\b)\w", re.I | re.S)
ANALYZE_DML = re.compile(r"^\s*explain\b.*\banalyze\b.*\b(insert|update|delete|merge)\b", re.I | re.S)


def _strip(sql: str) -> str:
    """Comments and string literals out, so keywords inside them neither trigger nor hide a rule."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"\$(\w*)\$.*?\$\1\$", "''", sql, flags=re.S)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def check_readonly(sql: str) -> str | None:
    """None when the statement may run as read-only; otherwise the reason it may not."""
    text = _strip(sql).strip().rstrip(";").strip()
    if not text:
        return "empty statement"
    if ";" in text:
        return "one statement at a time"
    if not READ_START.match(text):
        return "only SELECT, WITH, SHOW, EXPLAIN, VALUES or TABLE can run as read-only; changes are typed actions that go through approval"
    m = SIDE_EFFECT_FUNCS.search(text)
    if m:
        return f"{m.group(1)}() has side effects even in a read-only transaction; use the matching action instead"
    if SELECT_INTO.search(text):
        return "SELECT ... INTO creates a table"
    if ANALYZE_DML.search(text):
        return "EXPLAIN ANALYZE executes the statement"
    return None


def check_statement(sql: str) -> str | None:
    """Rules for execute_sql (a person approves it anyway). None = allowed."""
    raw = _strip(sql)
    for rule, pattern, reason in DENY_ANY:
        if rule in ("no-secrets", "no-psql-shell"):
            continue
        if pattern.search(raw):
            return f"{rule}: {reason}"
    for part in raw.split(";"):
        stmt = " ".join(part.split()).lower()
        if not stmt:
            continue
        m = re.search(r"\bdrop\s+(database|schema|tablespace|role|user|owned)\b", stmt)
        if m:
            return f"DROP {m.group(1).upper()} is not allowed"
        m = re.search(r"\bdrop\s+table\s+(.*)$", stmt)
        if m and not SAFE_TABLE.search(m.group(1)):
            return "DROP TABLE is only allowed for scratch tables (tmp_/temp_/scratch_/restore_/quarantine_/pitr_ prefixes)"
        m = re.search(r"\btruncate\s+(?:table\s+)?(?:only\s+)?(.*)$", stmt)
        if m and not SAFE_TABLE.search(m.group(1)):
            return "TRUNCATE is not allowed on application tables"
        m = re.search(r"^(delete\s+from|update)\s+([\w.\"]+)", stmt)
        if m and " where " not in f" {stmt} " and not SAFE_TABLE.search(m.group(2)):
            return "DELETE/UPDATE without WHERE is not allowed"
        if "pg_terminate_backend" in stmt and " where " not in f" {stmt} ":
            return "pg_terminate_backend over all sessions is not allowed; filter by role, state or age"
        if re.search(r"\b(pg_resetwal|pg_resetxlog)\b", stmt):
            return "pg_resetwal destroys WAL history"
    return None
