# Incident inc-20261021T010712-42b2 — Reporting connections exhausted application slots
## Timeline (UTC)
- 01:07:01: reporting opened 92 local connections, all observed idle.
- 01:07:02.080: first FATAL 53300; arena rejected at 01:07:05.795, 01:07:15.937 and 01:07:26.087.
- 01:07:18–29: triage found healthy primary but 98/100 client sessions; reporting unlimited, no role timeouts, no lock queue.
- 01:07:45.211: applied reporting connection cap and idle timeouts; disconnected its 92 idle backends, all successful.
- 01:07:59: only five application sessions remained; ledger writes current.
- 01:10:04–11: sustained recovery verified, oracle clean, primary ready with unchanged start time.
## Impact
Application connection attempts failed intermittently for approximately 43 seconds from first error until containment. PostgreSQL remained online. No application or superuser sessions were terminated; only idle reporting clients were disconnected. No acknowledged commits lost: final oracle acked=present=35595, lost=corrupt=unreadable=0.
## Root cause
A non-superuser reporting role consumed 92 of 100 connection slots with idle local sessions opened in the same second. Its rolconnlimit was -1 and rolconfig empty. Five arena sessions and the diagnostic connection brought occupancy to 98. Logs show reporting first hitting the reserved-slot boundary followed by arena failures. This is unbounded reporting client fan-out, not legitimate application load, lock contention, memory pressure or a crash. Exact client executable was not established; application_name was empty. Database-side cause was corrected at the offending role, without raising global capacity.
## Key commands
```text
/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 30 | tee "$RAIDEN_INCIDENT_DIR/snapshot.txt"
PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "SELECT usename, application_name, client_addr, state, wait_event_type, count(*), min(backend_start), max(now()-state_change) AS max_in_state FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY 1,2,3,4,5 ORDER BY 6 DESC; ..."
SELECT rolname, rolsuper, rolconnlimit, rolconfig FROM pg_roles WHERE rolname IN ('reporting','arena','raiden');
SELECT pid,usename,application_name,state,left(query,100) FROM pg_stat_activity WHERE usename='reporting' ORDER BY pid LIMIT 5;
SELECT count(*) AS blocked FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid))>0;
jq -Rr 'fromjson? | select(.timestamp >= "2026-10-21 01:06:30" and (.state_code=="53300" or (.message|test("reporting|ALTER ROLE")))) | [.timestamp,.user,.message] | @tsv' /var/log/postgresql/raiden-20261021.json | tail -20
PGCONNECT_TIMEOUT=3 psql -X -At -d postgres -c "SELECT rolname,rolsuper,rolconnlimit,rolconfig FROM pg_roles WHERE rolname='reporting'" > "$RAIDEN_INCIDENT_DIR/reporting-role.before.txt"
PGCONNECT_TIMEOUT=3 psql -X -v ON_ERROR_STOP=1 -d postgres -c "ALTER ROLE reporting CONNECTION LIMIT 10; ALTER ROLE reporting SET idle_session_timeout='2min'; ALTER ROLE reporting SET idle_in_transaction_session_timeout='15s';"
PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "SELECT pid,usename,state,pg_terminate_backend(pid,5000) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid() AND usename='reporting' AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname=usename) AND state IN ('idle','idle in transaction','idle in transaction (aborted)') ORDER BY pid;" | tee "$RAIDEN_INCIDENT_DIR/terminations.txt"
```
## What I did
Saved original role configuration before changing it. Set reporting CONNECTION LIMIT 10, idle_session_timeout=2min and idle_in_transaction_session_timeout=15s. Terminated only idle non-superuser reporting client sessions, PIDs 42168–42259; all 92 returned true, individually recorded in terminations.txt. Settings affect new sessions, so disconnecting the existing idle pool was necessary. No active transaction terminated, no table edits, config edits, reload or restart. Existing backups, replica, baseline and SQL guards preserved. Used pg-triage, connection-storm and data-loss-verification; loaded postgres-best-practices as second-opinion router, no reference-specific advice required.
## Verification
- More than two minutes after containment, post-fix-errors.txt empty: no new 53300, lock-wait or crash/recovery messages since 01:07:45.
- Final pg_probe: ready=true, in_recovery=false, client_sessions=6/max_connections=100, oldest_xact_s=0, checksum_failures=0, crash count=0; original postmaster start 00:45:54.446158, restarts_left=6.
- Role remains capped at 10 with 2min/15s timeouts. No reporting sessions reappeared during observation. Blocked sessions=0, idle transactions older than 15s=0.
- `SELECT count(*),max(created_at) FROM public.entries`: 32274 rows at 01:07:59, 35591 at 01:10:04; newest timestamp 01:10:04.451422 proves continued writes.
- Replica streaming, asynchronous, replay lag=0 bytes. pg_file_settings errors=0. fsync/full_page_writes/synchronous_commit all on; allow_alter_system off.
- `/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json`: first attempt ok=true, acked=present=35595, lost=corrupt=unreadable=0, unrecorded_rows_in_db=32 (concurrent/in-flight rows are permitted), samples empty, seconds=1.1. Saved reconcile-final.json.
## Follow-ups
In separate evolve session, register/monitor reporting role limits and validate connection-cap rejection using a safe test; no new patrol checks or defense artifacts built during this incident. Reporting client owner should bound its connection pool, provide application_name and back off on connection failures. Exact reporting client process was not identified. Limits are database-side containment of fan-out, not a fix to client pooling code. Do not lift limits blindly or raise max_connections; review legitimate workload requirements first.

RESOLUTION: resolved
SUMMARY: Capped unlimited reporting fan-out and disconnected 92 idle reporting clients; application writes recovered without restart, all 35,595 acknowledged commits verified intact.
