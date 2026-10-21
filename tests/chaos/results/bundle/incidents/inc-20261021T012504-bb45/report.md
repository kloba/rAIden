# Incident inc-20261021T012504-bb45 — Expected scratch-proof lock timeouts
## Timeline (UTC)
- 01:22:12.700: scratch backend 49135 logged a one-second exclusive-lock wait; 01:22:14.700: its three-second lock timeout canceled the statement.
- 01:22:28: first proof cleaned up its scratch table and roles.
- 01:22:40.191: rerun backend 49258 logged the same wait; 01:22:42.191: expected timeout canceled it.
- 01:22:54.191: scratch analyst expired under the 15-second idle-transaction policy; 01:22:56: second proof cleaned up.
- 01:25:12–01:25:34: triage and log/catalog comparison identified both alerts as prior incident's isolated protection proofs; production lock detector was OK.
- 01:25:43–01:26:18: ledger grew by 925 rows during 35-second observation; no new lock-wait/lock-timeout events since 01:23:00.
- 01:26:24: final probe healthy with unchanged postmaster and restart budget.
## Impact
No evidence of application blocking or downtime from these events. The logged locks targeted only an empty scratch table, not public.entries. Two proof statements waited three seconds and were intentionally canceled. Both scratch roles/table are absent. Oracle verified all 59,676 acknowledged commits present, with zero loss, corruption or unreadability. The 48 unrecorded database rows reflect concurrent traffic, not lost acknowledgments.
## Root cause
False-positive operational alert from deliberate P5 protection verification in the previous incident's evolve session. Both exact alert PIDs were logged as user scratch_p5_migrator, application_name raiden-p5-scratch-proof, statement LOCK TABLE public.scratch_p5_queue_probe IN ACCESS EXCLUSIVE MODE. Relation OIDs 158535 and 159264 corresponded to successive scratch-table creations. The script /var/lib/raiden/work/prove_lock_queue_defaults.py deliberately holds a scratch analyst read transaction and tests a fresh equivalent migrator's lock_timeout=3s; the expected SQLSTATE is 55P03. Evolution records explain the rerun after fixing handling of the analyst's expected idle-timeout exception. This is successful protection behavior, not recurrence of the production analyst/migrator queue. Live production defaults are intact and no database overrides exist.
## Key commands
```text
load_skill(name="pg-triage")
pg_probe()
load_skill(name="learned-lock-queue")
/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 10 | tee "$RAIDEN_INCIDENT_DIR/snapshot.txt"
read_file(path="/var/lib/raiden/memory/evolution.md", offset=1, limit=220)
PGCONNECT_TIMEOUT=3 psql -X -d ledger -c "SELECT pid,usename,application_name,state,... FROM pg_stat_activity ...; SELECT to_regclass('public.scratch_p5_queue_probe') AS scratch_table; ..."
jq -Rr 'fromjson? | select(.timestamp >= "2026-10-21 01:22:10" and .timestamp <= "2026-10-21 01:23:00" and (.pid==49135 or .pid==49258 or .pid==49134 or .pid==49257)) | {timestamp,pid,user,dbname,application_name,message,detail,statement,state_code}' /var/log/postgresql/raiden-20261021.json | tee "$RAIDEN_INCIDENT_DIR/probe-log-evidence.txt"
read_file(path="/var/lib/raiden/work/prove_lock_queue_defaults.py", offset=1, limit=220)
```
No database-changing fix, reload, restart or termination was needed or performed.
## What I did
Collected read-only triage snapshot, inspected protection memory, resolved the alert PIDs to scratch statements, and checked the proof implementation against its recorded run. Confirmed cleanup and live policy. Preserved all protections; did not rerun the alert-producing proof or alter watcher settings. Wrote incident evidence and report only.
## Verification
- `bash /opt/raiden/checks.d/89-lock-queue.sh`: OK twice; blocked arena=0, migrator waiters=0, exclusive relation waiters=0, analyst idle transaction age=0, database overrides=0; production defaults migrator 3s / analyst 15s.
- `/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json`: ok=true, acked=present=59676, lost=corrupt=unreadable=0; saved in reconcile.json.
- `SELECT clock_timestamp(),count(*),max(id) FROM public.entries` before/after 35 seconds: count 59728 -> 60653, confirming ongoing writes.
- JSON log query for lock waits/timeouts since 01:23:00 returned no rows.
- pg_settings: fsync/full_page_writes/synchronous_commit on; allow_alter_system off. pg_file_settings errors=0.
- Final pg_probe: ready=true, recovery=false, active service, no crashes/checksum failures, oldest transaction age=0, start unchanged at 00:45:54.446158 UTC, restarts_left=6.
## Follow-ups
In a separate evolve session, document expected scratch-proof signatures for triage. Any alert classification refinement must match trusted proof provenance and exact scratch relation/user/application combination, not suppress real production locks or raise thresholds. Watcher implementation is outside this session's editable scope. No outstanding production remediation or scratch cleanup.

RESOLUTION: resolved
SUMMARY: Both lock alerts were expected isolated P5 proof timeouts; production healthy, scratch objects cleaned up, 59,676 acknowledged commits verified intact, no database changes or restart.
