# Incident inc-20261021T013448-3b98 — Protected table DROP rejected
## Timeline (UTC)
- 01:34:41.975: arena backend PID 55221 logged `DROP TABLE public.entries`.
- 01:34:41.978: same PID received ERROR 42501: `raiden protection: table public.entries is protected and cannot be dropped`.
- 01:34:56: triage found healthy primary, no crash/recovery, ample disk and zero archiver failures.
- ~01:35:00: confirmed entries present, registered, guard fingerprint/ownership/ACL intact and no blocked ledger sessions.
- 01:35:18: reconciliation succeeded on first attempt; final probe confirmed healthy service.
## Impact
The destructive statement failed after 3ms; table removal did not commit. No observed downtime or persistent lock queue. All 74,948 acknowledged commits remain present and correct. The oracle also observed 64 unrecorded rows during concurrent traffic, which are not evidence of lost acknowledged commits.
## Root cause
The watcher alerted on a logged statement start, not a successful DROP. An arena session attempted to remove the protected application table. Exact PID attribution links the LOG to the ERROR 3ms later, with PL/pgSQL context `block_protected_drop() line 6 at RAISE`. The existing sql_drop event trigger rejected the operation. Independent catalog/fingerprint checks corroborate an intact guard and table. The initiating client intent is unknown; there is no evidence of a committed destructive operation or protection drift.
## Key commands
```text
/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 5
PGCONNECT_TIMEOUT=3 psql -X -d ledger -c "SELECT to_regclass('public.entries'); SELECT * FROM raiden.protected_tables; SELECT evtname,evtevent,evtenabled,evtfoid::regprocedure FROM pg_event_trigger; SELECT pid,usename,state,pg_blocking_pids(pid) FROM pg_stat_activity WHERE datname='ledger';"
jq -Rr 'fromjson? | select(.pid==55221) | [.timestamp,.user,.dbname,.pid,.error_severity,.state_code,.message,.statement,.context] | @tsv' /var/log/postgresql/raiden-20261021.json
bash /opt/raiden/checks.d/88-table-guard.sh
bash /opt/raiden/checks.d/90-protections.sh
```
Only five diagnostic command lines mattered; no SQL write, configuration edit, cancellation, reload or restart was performed.
## What I did
Loaded pg-triage, learned-table-drop-guard, data-loss-verification and close-out runbooks. Correlated statement and rejection by backend PID; checked the table registry, triggers, sessions and independent protections. Left the effective protection unchanged. Did not restore data or retest DROP against the live table. Saved reconciliation evidence and backed up lessons.md before appending a lesson.
## Verification
- `88-table-guard.sh`: OK; entries exists and registered, DROP guard fingerprint, owners and arena ACL intact.
- `90-protections.sh`: OK; six applicable protections verified.
- Read-only pg_settings check: fsync/full_page_writes/synchronous_commit on; allow_alter_system off; configuration errors=0.
- No crash/recovery messages in recent 40-second window; primary was already stable for more than 30 seconds before oracle.
- `/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json`, first attempt, 2.4 seconds: acked=present=74948, lost=corrupt=unreadable=0, ok=true; unrecorded_rows_in_db=64. Full JSON in reconcile.json.
- Final pg_probe: ready=true, in_recovery=false, active service, six clients, oldest transaction=0 seconds, checksum_failures=0, crash_lines_last_300kb=0. Postmaster start unchanged at 00:45:54.446158 UTC; restart budget remains six.
## Follow-ups
No recovery or protection change needed. Client owner may investigate why arena issued DROP. In a separate evolve session, consider richer alert correlation distinguishing attempted DDL from rejection, without suppressing attempts or weakening guards. Legitimate table-removal requests require human review; do not disable this protection.

RESOLUTION: resolved
SUMMARY: Existing guard rejected arena DROP in 3ms; table and protections intact, all 74,948 acknowledged commits verified, no database changes or restart.
