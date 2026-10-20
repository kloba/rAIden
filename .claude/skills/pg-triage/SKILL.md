---
name: pg-triage
description: ENTRY POINT for every PostgreSQL incident - any ERROR/FATAL/PANIC in the log, server down or flapping, "terminated by signal", "the database system is in recovery mode", "not properly shut down", connection refused. First-2-minutes checks (connectivity, crash history, processes, settings, disk/WAL, OOM), then routes to the specialised skill.
---

# pg-triage - first 2 minutes of any incident

## Safety first (read before acting)
- Before EVERY state change (config edit, reload/restart, terminate, SQL write) append a line:
  `echo "$(date -u +%FT%TZ) <action> because <evidence>" >> "$RAIDEN_INCIDENT_DIR/notes.md"`
- Stay read-only until you have a hypothesis backed by log evidence. Restarts are budgeted (6/hour):
  never restart "to see if it helps"; prefer `sudo -n systemctl reload postgresql@18-main`.
- Shell variables do NOT persist between tool calls: make every command self-contained.
- Never touch PGDATA, pg_wal, /var/lib/postgresql/wal_archive or /var/backups/postgresql directly.

## 0. One-shot snapshot (read-only, ~10 s, safe while the DB is down)
`/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 30 | tee "$RAIDEN_INCIDENT_DIR/snapshot-$(date -u +%H%M%S).txt"`
(argument = minutes of log to summarise). Then drill down with the commands below only where needed.

## 1. Connectivity and recovery state
```bash
pg_isready -t 3; echo "rc=$?"     # 0 accepting, 1 rejecting (startup/recovery), 2 no response, 3 bad params
PGCONNECT_TIMEOUT=3 psql -X -At -d postgres -c "SELECT pg_is_in_recovery(), now()-pg_postmaster_start_time(), (SELECT now()-backend_start FROM pg_stat_activity WHERE backend_type='checkpointer')"
pg_lsclusters; sudo -n systemctl status postgresql@18-main --no-pager | head -15
```
- Checkpointer much younger than the postmaster => a crash-restart cycle happened after the last start
  (pg_postmaster_start_time() is NOT reset by crash recovery).
- FATAL `the database system is in recovery mode` / `is starting up` (57P03) => crash recovery in progress: wait, don't restart.
- Server not running: `tail -n 40 /var/log/postgresql/postgresql-18-main.log` and
  `sudo -n journalctl -u postgresql@18-main -n 40 --no-pager` (startup errors such as a missing
  shared_preload_libraries file are written there, before the jsonlog collector starts).

## 2. Log evidence (jsonlog, one JSON object per line; the SQL text key is `statement`)
Timestamps look like `2026-10-20 12:34:56.789 UTC`; string comparison works if `SHOW log_timezone` is UTC.
`jq -R 'fromjson? | ...'` skips a partially written last line.
```bash
# errors grouped by severity / SQLSTATE / normalised message, last 30 min
jq -Rr --arg s "$(date -u -d '-30 min' '+%F %T')" 'fromjson? | select(.timestamp >= $s and (.error_severity|IN("WARNING","ERROR","FATAL","PANIC"))) | [.error_severity, (.state_code//"-"), (.message|gsub("[0-9]+";"N")|.[0:110])] | @tsv' $(ls -1tr /var/log/postgresql/raiden-*.json | tail -2) | sort | uniq -c | sort -rn | head -25
# crash / recovery timeline
jq -Rr --arg s "$(date -u -d '-60 min' '+%F %T')" 'fromjson? | select(.timestamp >= $s and (.message|test("terminated by signal|exited with exit code|terminating any other active|all server processes terminated|not properly shut down|automatic recovery in progress|interrupted; last known up|redo done|ready to accept connections|received (fast|smart|immediate) shutdown"))) | "\(.timestamp) pid=\(.pid) \(.backend_type//"-") | \(.message)"' $(ls -1tr /var/log/postgresql/raiden-*.json | tail -2)
# config reloads and their errors
jq -Rr 'fromjson? | select(.message|test("SIGHUP|configuration file|cannot be changed without restarting|changed to")) | "\(.timestamp) \(.message)"' $(ls -1tr /var/log/postgresql/raiden-*.json | tail -1) | tail -20
```

## 3. Processes (spot odd workers)
```bash
ps -eo pid,ppid,lstart,cmd | grep '[p]ostgres:'          # children: "postgres: 18/main: <type>"
cat /run/postgresql/18-main.pid                          # postmaster PID
psql -X -At -d postgres -c "SELECT backend_type, count(*), min(backend_start) FROM pg_stat_activity GROUP BY 1 ORDER BY 1"
```
Expected backend_type on this PG18 lab: client backend, autovacuum launcher/worker, background writer,
checkpointer, walwriter, archiver, io worker (io_method=worker, default 3), logical replication launcher,
parallel worker (transient), walsender/walsummarizer only if configured. Anything else (a custom
background-worker name, or a `postgres:` process with no row in pg_stat_activity other than `logger`)
is suspicious => crash-loop-rca.

## 4. Settings, disk, OOM
```bash
psql -X -At -d postgres -c "SELECT name, setting, source, sourcefile, sourceline FROM pg_settings WHERE source NOT IN ('default','override','client','session') ORDER BY sourcefile NULLS FIRST, sourceline"
psql -X -At -d postgres -c "SELECT sourcefile, sourceline, name, setting, applied, error FROM pg_file_settings WHERE error IS NOT NULL OR NOT applied"
psql -X -At -d postgres -c "SELECT name, setting FROM pg_settings WHERE pending_restart"
ls -l --time-style=full-iso /etc/postgresql/18/main/ /etc/postgresql/18/main/conf.d/
df -h / /var/lib/postgresql /var/backups/postgresql /var/log/postgresql
psql -X -At -d postgres -c "SELECT count(*), pg_size_pretty(sum(size)) FROM pg_ls_waldir()"
psql -X -At -d postgres -c "SELECT failed_count, last_failed_wal, last_failed_time, last_archived_wal, last_archived_time FROM pg_stat_archiver"
sudo -n journalctl -k --since '-60min' --no-pager | grep -iE 'out of memory|oom-kill|killed process|i/o error|ext4-fs error' | tail
```
`dmesg` may be restricted; use journalctl -k. OOM kills show `Out of memory: Killed process <pid> (postgres)`.
A signal 9 in the PG log WITHOUT a matching OOM line was sent by someone/something else.

## 5. While the DB is flapping
- Wait with a bounded loop: `timeout 120 bash -c 'until pg_isready -q -t 2; do sleep 1; done'; echo rc=$?`
- One short probe per tool call; expect `server closed the connection unexpectedly` mid-command and retry once.
- Do not restart while crash recovery is running; restarting cannot fix a recurring external cause.
- Collect evidence (log, processes, settings) BETWEEN cycles; find the cause, then act once.

## 6. Route
| Evidence | Skill |
|---|---|
| repeated `terminated by signal` / `exited with exit code` / `all server processes terminated; reinitializing`; unknown backend_type; unexpected library in shared_preload_libraries | crash-loop-rca |
| `missing chunk number`, `unexpected chunk number`, `invalid page in block`, `could not read block`, `page verification failed`, XX001/XX002, checksum_failures > 0 | toast-corruption |
| `relation "..." does not exist` (42P01) for an app table, DROP/TRUNCATE in the log, need data as of earlier time | pitr-restore |
| any incident that may have touched data; is anything lost? | data-loss-verification |
| `too many clients already`, `remaining connection slots are reserved`, idle-in-transaction pile-up | connection-storm |
| `No space left on device`, 53100, `archive command failed`, pg_wal growing | disk-space-wal |
| `must be vacuumed within`, `not accepting commands ... wraparound`, 54000 xid limits | wraparound-vacuum |
| server won't start due to config | crash-loop-rca (remediation ladder: fix exact source file, restart once) |
| `systemctl restart/stop` never returns, unit `deactivating`, `FATAL: the database system is shutting down` for minutes, controldata `in crash recovery` | stuck-shutdown |
| incident stable | hardening-after-incident PART A (report + lessons) |

Record in notes.md: first error timestamp, symptom signature, chosen skill and why.
