---
name: crash-loop-rca
description: Root-cause and stop repeated PostgreSQL crashes and crash-recovery cycles - "server process / client backend (PID N) was terminated by signal 9/6/11", "exited with exit code 2", "terminating connection because of unexpected SIGQUIT signal", "all server processes terminated; reinitializing", "database system was not properly shut down; automatic recovery in progress". Finds the signal sender (OOM killer, external process, unexpected preloaded library or unknown background worker), removes it with one budgeted restart, and verifies.
---

# crash-loop-rca - who is killing PostgreSQL processes?

Write every state change to `$RAIDEN_INCIDENT_DIR/notes.md` first. Restart budget: 6/hour - plan for ONE restart.

## 1. Read the pattern (jsonlog, last 60 min)
```bash
jq -Rr --arg s "$(date -u -d '-60 min' '+%F %T')" 'fromjson? | select(.timestamp >= $s and (.message|test("terminated by signal|exited with exit code|unexpected SIGQUIT|crash of another server process|all server processes terminated|not properly shut down|ready to accept connections"))) | "\(.timestamp) \(.backend_type//"-") | \(.message) | \(.detail//"" | .[0:120])"' $(ls -1tr /var/log/postgresql/raiden-*.json | tail -2)
```
Postmaster lines name the victim, e.g. `client backend (PID 4242) was terminated by signal 9: Killed`,
`checkpointer process (PID ..) ...`, `background worker "x" (PID ..) exited with exit code 1`; for backends the
DETAIL is `Failed process was running: <query>`.

| Signal / exit | Meaning | Next check |
|---|---|---|
| 9 Killed + kernel `Out of memory: Killed process <pid>` | OOM killer | memory: `free -m`, work_mem x connections, shared_buffers vs RAM |
| 9 with NO OOM line | someone/something sent SIGKILL | sender hunt (step 2) |
| exit code 2 + victim logged `terminating connection because of unexpected SIGQUIT signal` | SIGQUIT (3) not from the postmaster | sender hunt |
| 15 to a backend: FATAL 57P01 `terminating connection due to administrator command` (no crash cycle) | pg_terminate_backend or kill -TERM | sender hunt if repeated |
| 11 Segfault / 6 Aborted, same `Failed process was running` query every time | bug in core/extension code path | escalate with query + detail; disable the extension if one is involved |

Victims that are random backends AND auxiliary processes (checkpointer, walwriter, background writer,
io worker, autovacuum launcher) at regular intervals point to a deliberate signal sender (an external
process or code loaded into the server), not to a query bug.
OOM check: `sudo -n journalctl -k --since '-60min' --no-pager | grep -iE 'out of memory|oom-kill|killed process'`

## 2. Find the sender
In-server suspects (code loaded into the postmaster survives restarts until the config changes):
```bash
psql -X -At -d postgres -c "SELECT name, setting, source, sourcefile, sourceline FROM pg_settings WHERE name ~ 'preload_libraries'"
psql -X -At -d postgres -c "SELECT sourcefile, sourceline, name, setting, applied FROM pg_file_settings WHERE name ~ 'preload_libraries' OR name LIKE '%.%' ORDER BY sourcefile, sourceline"
psql -X -At -d postgres -c "SELECT name, setting, source, sourcefile FROM pg_settings WHERE name ~ '^[a-z_]+\.[a-z_.]+$' AND source <> 'default'"
psql -X -At -d postgres -c "SELECT pid, backend_type, backend_start, application_name FROM pg_stat_activity WHERE backend_type NOT IN ('client backend','autovacuum launcher','autovacuum worker','background writer','checkpointer','walwriter','archiver','io worker','logical replication launcher','parallel worker','walsender','walsummarizer','startup')"
```
Baseline: shared_preload_libraries = 'pg_stat_statements' only. pg_file_settings re-reads the files at
query time and shows what a restart WOULD load (the `applied = t` row wins); pg_settings shows what runs now.
For an unknown worker PID:
```bash
cat /proc/<pid>/status | grep -E '^(Name|State|PPid)'; tr '\0' ' ' < /proc/<pid>/cmdline; echo
sudo -n -u postgres grep -oE '/[^ ]+\.so[^ ]*' /proc/$(cat /run/postgresql/18-main.pid)/maps | sort -u   # libraries mapped in the postmaster
ls -l --time-style=full-iso /usr/lib/postgresql/18/lib/*.so | sort -k6,7 | tail -5                        # newest server modules
for f in /usr/lib/postgresql/18/lib/*.so; do dpkg -S "$f" >/dev/null 2>&1 || echo "NOT FROM A PACKAGE: $f"; done
```
Who changed the config and when:
```bash
ls -lt --time-style=full-iso /etc/postgresql/18/main/ /etc/postgresql/18/main/conf.d/
sudo -n -u postgres cat /var/lib/postgresql/18/main/postgresql.auto.conf
B=$(sudo -n -u postgres sh -c 'ls -1d /var/backups/postgresql/base/*/ | tail -1'); sudo -n -u postgres diff "${B}postgresql.auto.conf" /var/lib/postgresql/18/main/postgresql.auto.conf
sudo -n journalctl _COMM=sudo --since '-24h' --no-pager | grep -iE 'postgresql|conf' | tail
jq -Rr 'fromjson? | select(.message|test("changed to|SIGHUP|ALTER SYSTEM")) | "\(.timestamp) \(.user//"-") \(.message)"' $(ls -1tr /var/log/postgresql/raiden-*.json | tail -2) | tail
```
External suspects (if nothing in-server): `ps -eo pid,ppid,user,lstart,cmd --sort=lstart | grep -v '[p]ostgres:' | tail -20`,
`sudo -n -u postgres crontab -l`, `ls -l /etc/cron.d`. Do not kill anything you cannot identify - escalate with pid/user/cmdline/parent.

## 3. Remediation ladder (in-server cause)
1. Back up the exact source file named by sourcefile/sourceline:
   `cp -p /etc/postgresql/18/main/conf.d/<file> /var/lib/raiden/work/<file>.$(date -u +%Y%m%dT%H%M%SZ).bak`
2. Remove only the offending library, keep pg_stat_statements. Comment out the line(s) and the
   library's custom `prefix.*` settings in place, e.g.
   `sudo -n -u postgres sed -i -E 's/^([[:space:]]*(shared_preload_libraries|<prefix>\.)[^#]*)/# disabled by raiden '"$RAIDEN_INCIDENT_ID"': \1/' /etc/postgresql/18/main/conf.d/<file>`
   If the source is postgresql.auto.conf use SQL: `ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';`
3. Validate BEFORE restarting (must print only the expected list, no errors):
   `sudo -n -u postgres /usr/lib/postgresql/18/bin/postgres -D /etc/postgresql/18/main -C shared_preload_libraries`
   and `psql -X -At -d postgres -c "SELECT sourcefile, sourceline, setting FROM pg_file_settings WHERE name='shared_preload_libraries' AND applied"`
   If pg_stat_statements is no longer in the list, set it explicitly in a new file
   `/etc/postgresql/18/main/conf.d/20-raiden-preload.conf` (`shared_preload_libraries = 'pg_stat_statements'`,
   via `sudo -n -u postgres tee`) and make sure no later file / postgresql.auto.conf overrides it; re-run `-C`.
4. Restart ONCE (preload libraries need a restart; reload is not enough):
   Wait for a calm window first - a fast shutdown that lands in the middle of crash recovery can hang the postmaster (skill stuck-shutdown):
   `timeout 90 bash -c 'until pg_isready -q -t 2; do sleep 1; done'; sleep 1; sudo -n systemctl restart postgresql@18-main && timeout 180 bash -c 'until pg_isready -q -t 2; do sleep 2; done'; echo rc=$?`
5. Verify: `SHOW shared_preload_libraries` = expected; backend_type survey has no unknown rows; no unknown
   `postgres:` process in ps; then no new crash for 60 s:
   ```bash
   T0="$(date -u '+%F %T')"; sleep 60; jq -Rr --arg s "$T0" 'fromjson? | select(.timestamp >= $s and (.message|test("terminated by signal|exited with exit code [^01]|all server processes terminated"))) | .message' $(ls -1tr /var/log/postgresql/raiden-*.json | tail -1) | wc -l   # expect 0
   ```
6. Crash recovery means in-flight transactions were aborted: run the reconcile oracle
   (skill data-loss-verification). Do not declare success unless `ok: true`.

OOM cause: lower memory use (e.g. `ALTER ROLE arena SET work_mem = '16MB'`, connection limits), reload,
never touch durability settings. Bug cause (11/6): escalate with the query and timestamps.

## Rollback
If the server does not come back after the edit: restore the backup copy
(`sudo -n -u postgres cp /var/lib/raiden/work/<file>.<ts>.bak /etc/postgresql/18/main/conf.d/<file>`), read
`tail -n 40 /var/log/postgresql/postgresql-18-main.log`, fix the syntax, restart once more. If two restarts fail: escalate.

## Prevention (record in report Follow-ups; implemented later by hardening-after-incident PART B)
- patrol check: shared_preload_libraries allowlist + unknown backend_type detector;
- config drift detection (checksum of /etc/postgresql/18/main/** and postgresql.auto.conf vs a stored baseline);
- pre-restart validation with `postgres -C` (ExecStartPre-style) before any restart;
- crash-recovery counter from the jsonlog (checks.d/80-crash-recovery.sh) with a lower threshold.
