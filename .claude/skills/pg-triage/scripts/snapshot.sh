#!/usr/bin/env bash
# snapshot.sh - read-only first-look triage snapshot for the PostgreSQL 18 cluster 18/main (rAIden).
#
# Usage: snapshot.sh [MINUTES]      MINUTES of jsonlog/kernel log to summarise (default 30)
#
# Safe while the server is down or flapping: every probe is bounded by a timeout and a failed probe
# is reported inline, never fatal. Changes nothing. Typical runtime < 15 s.
set -euo pipefail

MIN="${1:-30}"
[[ "$MIN" =~ ^[0-9]+$ ]] || { echo "usage: $0 [MINUTES]" >&2; exit 64; }
export PGCONNECT_TIMEOUT=3 PGDATABASE=postgres
SINCE="$(date -u -d "-${MIN} min" '+%F %T')"
mapfile -t LOGS < <(ls -1tr /var/log/postgresql/raiden-*.json 2>/dev/null | tail -2)

sec() { printf '\n=== %s ===\n' "$*"; }
q() { timeout 10 psql -X -At -F ' | ' -v ON_ERROR_STOP=1 -c "$1" 2>&1 || echo "(query failed rc=$?)"; }
jl() { # jl JQ_FILTER : run a filter over the recent jsonlog files, skipping partial/unparsable lines
  if ((${#LOGS[@]})); then
    timeout 10 jq -Rr --arg s "$SINCE" "fromjson? | select(.timestamp >= \$s) | $1" "${LOGS[@]}" 2>&1 || true
  else
    echo "(no /var/log/postgresql/raiden-*.json files)"
  fi
}

sec "host $(hostname) at $(date -u '+%F %T') UTC, log window since $SINCE"
uptime
rc=0; pg_isready -t 3 || rc=$?
echo "pg_isready rc=$rc (0 accepting, 1 rejecting/starting, 2 no response)"
pg_lsclusters 2>&1 || true
timeout 10 sudo -n systemctl status postgresql@18-main --no-pager 2>&1 | head -8 || true

if [ "$rc" -eq 0 ]; then
  sec "server state (in_recovery | postmaster uptime | since last (re)init = checkpointer age)"
  q "SELECT pg_is_in_recovery(), date_trunc('second', now()-pg_postmaster_start_time()), (SELECT date_trunc('second', now()-min(backend_start)) FROM pg_stat_activity WHERE backend_type='checkpointer')"
  sec "backend types (UNKNOWN = not a stock PG18 process type)"
  q "SELECT backend_type, count(*), CASE WHEN backend_type IN ('client backend','autovacuum launcher','autovacuum worker','background writer','checkpointer','walwriter','archiver','io worker','logical replication launcher','logical replication apply worker','logical replication parallel worker','logical replication tablesync worker','parallel worker','walsender','walreceiver','walsummarizer','startup','slotsync worker') THEN '' ELSE '<-- UNKNOWN' END FROM pg_stat_activity GROUP BY 1 ORDER BY 1"
  sec "preload libraries"
  q "SELECT name, setting, coalesce(sourcefile,'-'), coalesce(sourceline::text,'-') FROM pg_settings WHERE name LIKE '%preload_libraries'"
  sec "settings from config files / ALTER SYSTEM / command line"
  q "SELECT name, setting, source, coalesce(sourcefile,'-'), coalesce(sourceline::text,'-') FROM pg_settings WHERE source IN ('configuration file','command line','environment variable','global') ORDER BY sourcefile NULLS FIRST, sourceline"
  sec "config file errors / not applied / pending restart"
  q "SELECT sourcefile, sourceline, name, setting, applied, coalesce(error,'') FROM pg_file_settings WHERE error IS NOT NULL OR NOT applied"
  q "SELECT 'pending_restart', name, setting FROM pg_settings WHERE pending_restart"
  sec "sessions | xid age | checksum failures | archiver | WAL dir"
  q "SELECT state, count(*), max(date_trunc('second', now()-xact_start)) AS oldest_xact FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY 1"
  q "SELECT 'max_connections', setting FROM pg_settings WHERE name='max_connections'"
  q "SELECT datname, age(datfrozenxid), mxid_age(datminmxid), checksum_failures FROM pg_database d LEFT JOIN pg_stat_database s USING (datname) ORDER BY 2 DESC"
  q "SELECT archived_count, last_archived_wal, last_archived_time, failed_count, last_failed_wal, last_failed_time FROM pg_stat_archiver"
  q "SELECT count(*) AS wal_files, pg_size_pretty(sum(size)) FROM pg_ls_waldir()"
else
  sec "server not accepting connections: startup log and service journal"
  tail -n 25 /var/log/postgresql/postgresql-18-main.log 2>&1 || true
  timeout 10 sudo -n journalctl -u postgresql@18-main -n 25 --no-pager 2>&1 || true
fi

sec "postgres processes (ps)"
ps -eo pid,ppid,lstart,cmd | grep -E '[p]ostgres(:| -D)' || echo "(no postgres processes)"

sec "log: errors grouped (count severity sqlstate message)"
jl 'select(.error_severity|IN("WARNING","ERROR","FATAL","PANIC")) | [.error_severity, (.state_code//"-"), (.message|gsub("[0-9]+";"N")|.[0:110])] | @tsv' | sort | uniq -c | sort -rn | head -25

sec "log: crash / recovery / reload timeline (last 30 events)"
jl 'select(.message|test("terminated by signal|exited with exit code|terminating any other active|all server processes terminated|not properly shut down|automatic recovery in progress|interrupted; last known up|ready to accept connections|shutdown request|received SIGHUP|configuration file")) | "\(.timestamp) pid=\(.pid) \(.backend_type//"-") | \(.message)"' | tail -30

sec "log: DDL statements"
jl 'select(.message|test("^statement: *(DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE)";"i")) | "\(.timestamp) \(.user//"-")@\(.dbname//"-") pid=\(.pid) | \(.message|.[0:160])"' | tail -15

sec "disk"
df -h / /var/lib/postgresql /var/lib/postgresql/wal_archive /var/backups/postgresql /var/log/postgresql 2>&1 | awk '!seen[$0]++'

sec "kernel: OOM / I/O errors"
timeout 10 sudo -n journalctl -k --since "-${MIN}min" --no-pager 2>&1 | grep -iE 'out of memory|oom-kill|killed process|i/o error|ext4-fs error|blk_update_request' | tail -10 || echo "(none)"

sec "config files (newest first)"
ls -lt --time-style=full-iso /etc/postgresql/18/main/ /etc/postgresql/18/main/conf.d/ 2>&1 | head -25
