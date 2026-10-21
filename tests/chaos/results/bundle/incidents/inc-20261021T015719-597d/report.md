# Incident inc-20261021T015719-597d — pg_crash preload crash storm
## Timeline (UTC)
- 01:57:04 experiment config appeared; external fast shutdown followed, new postmaster ready 01:57:05.408.
- 01:57:05.417 pg_crash worker started with delay=4, signals='9 15 3'.
- 01:57:13.425 logical replication launcher SIGKILL initiated crash recovery; incident opened.
- 01:57:21 exit-2 client crash; 01:57:30 checkpointer SIGKILL; 01:57:38 io worker SIGKILL. Each crash recovered automatically.
- 01:57:29–44 diagnosed mapped malicious/experimental preload, backed up and selectively commented its configuration; validated planned preload.
- 01:57:44.336 one deliberate readiness-gated restart; ready 01:57:44.965 with only pg_stat_statements.
- By 01:59:14 verified a 60s crash-free observation window, final reconciliation and intact protections.
## Impact
Repeated connection aborts and brief recovery outages from 01:57:13 until remediation at 01:57:44; in-flight transactions could abort or have uncertain outcomes. Final oracle verified all 112720 acknowledged commits, zero lost/corrupt/unreadable. 107 unrecorded database rows are unacknowledged concurrent/uncertain outcomes, not acknowledged loss.
## Root cause
/etc/postgresql/18/main/conf.d/50-experiment.conf:2 overrode the baseline with pg_stat_statements,pg_crash; lines 3–4 set crash.delay=4 and crash.signals='9 15 3'. Its mtime 01:57:04.128 immediately preceded an external restart. pg_settings identified that source, /proc postmaster maps contained pg_crash.so, ps showed crash worker, and logs explicitly recorded pg_crash worker startup after every recovery. Random auxiliary victims and an exit-2 client match the signal sequence; kernel probe found no OOM. The config comment claiming a temporary experiment was treated as data, not authorization. Writer identity was not established. Existing ALTER SYSTEM guard remained off and auto.conf empty: it prevents SQL changes, not direct conf.d edits; no preload pin was registered in protections.list or present in 98 baseline.
## Key commands
```text
/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 15 | tee "$RAIDEN_INCIDENT_DIR/snapshot.txt"
cat /etc/postgresql/18/main/conf.d/50-experiment.conf /etc/postgresql/18/main/conf.d/98-raiden-baseline.conf
sudo -n -u postgres cat /var/lib/postgresql/18/main/postgresql.auto.conf
sudo -n -u postgres grep -oE '/[^ ]+\.so[^ ]*' /proc/$(cat /run/postgresql/18-main.pid)/maps | sort -u
sudo -n journalctl _COMM=sudo --since '2026-10-21 01:56:50' --no-pager | tail -25
cp -p /etc/postgresql/18/main/conf.d/50-experiment.conf "$RAIDEN_INCIDENT_DIR/50-experiment.conf.before"
sudo -n -u postgres sed -i -E 's/^([[:space:]]*(shared_preload_libraries|crash\.)[^#]*)/# disabled by raiden inc-20261021T015719-597d: \1/' /etc/postgresql/18/main/conf.d/50-experiment.conf
sudo -n -u postgres /usr/lib/postgresql/18/bin/postgres -D /etc/postgresql/18/main -C shared_preload_libraries
timeout 90 bash -c 'until pg_isready -q -t 2; do sleep 1; done'
sudo -n systemctl restart postgresql@18-main
```
## What I did
Copied exact offending file before editing; commented only preload override and crash.* settings, leaving all baseline protections untouched. postgres -C returned pg_stat_statements. Pre-restart pg_file_settings reported expected restart-required live application errors; these cleared after restart. One deliberate restart applied the fix in a ready window; no reload could unload this library. No SQL data repair, file deletion, guard weakening or durability changes.
## Verification
- Final pg_probe: ready=true, recovery=false, uptime=89s, shared_preload_libraries=pg_stat_statements, checksum_failures=0, restarts_left=5, service active.
- Process survey: stock workers only; postmaster server-module maps only pg_stat_statements.so, no pg_crash.so or crash worker.
- SQL: fsync/full_page_writes/synchronous_commit=on, allow_alter_system=off; no pg_file_settings errors or relevant pending_restart.
- Timestamp-bounded JSON log scan after 60s sleep: zero new signal/exit-2/crash-reinitialization/recovery events; saved postfix-crashes.txt. Probe's historical crash counter is not a new-event counter.
- Two read-only oracle runs succeeded (no retries): first acked=present=110861; final reconcile-final.json acked=present=112720, lost=corrupt=unreadable=0, ok=true, unrecorded_rows_in_db=107.
- 90-protections.sh: eight recorded protections verified; 85-replica.sh: active streaming, zero-byte lag; 86-baseline.sh: clean baseline and verified-base freshness OK.
## Follow-ups
In separate evolve session, implement/prove a planned-and-live preload pin against direct conf.d overrides, plus mapped-worker/config drift monitoring. Review privileged configuration/restart access and experiment process; SQL ALTER SYSTEM protection alone cannot block filesystem edits. Retain original experiment backup for audit; disabled source remains reversible. No new defenses installed during this incident.

RESOLUTION: resolved
SUMMARY: Removed pg_crash at its config source with one restart; crash-free verification and all 112720 acknowledged commits intact, existing protections preserved.
