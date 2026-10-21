# Incident inc-20261021T011631-7c80 — Ledger lock queue
## Timeline (UTC)
- 01:16:22 analyst PID 46719 began a read transaction and remained idle in transaction.
- 01:16:23 migrator PID 46731 requested ACCESS EXCLUSIVE on public.entries; application reads and inserts queued behind it.
- 01:16:24.035 first logged lock wait; watcher opened incident.
- 01:16:37 activity probe established analyst -> migrator -> five arena sessions blocking chain.
- 01:16:49.305 canceled migrator statement; all five application locks acquired at 01:16:49.305–306 after ~26 seconds waiting.
- 01:16:49.306 terminated the analyst's uncommitted idle transaction.
- Before 01:17:10 saved prior role defaults and applied analyst 15s idle-transaction timeout and migrator 3s lock timeout.
- 01:19:20 verification showed ongoing writes and no blocked sessions; oracle succeeded. Final probe remained healthy.
## Impact
Five observed ledger reader/writer sessions stalled for approximately 26 seconds; PostgreSQL continued accepting connections. No primary downtime or restart. No application or oracle sessions were terminated. All 49,552 acknowledged commits verified present, readable and correct; zero loss or corruption.
## Root cause
An analyst read transaction retained a relation read lock while idle. Its last query was SELECT count(*) FROM public.entries WHERE id < 0. Migrator's LOCK TABLE public.entries IN ACCESS EXCLUSIVE MODE waited behind that lock, and PostgreSQL lock queue ordering placed subsequent application readers and writers behind the incompatible exclusive waiter. pg_blocking_pids showed 46719 blocking 46731 and 46731 blocking the five arena sessions. JSON log detail independently names holder 46719 and the full queue on relation 104379/database 16434.
Both offender roles were non-superusers with no role or database-specific defaults. There was no role-specific idle-transaction bound for analyst or lock-wait bound for migrator. The source-role defaults are now 15s and 3s respectively; application client transaction/migration design remains a follow-up, not an inferred malicious intent.
## Key commands
```text
PGCONNECT_TIMEOUT=3 psql -X -d ledger -c "SELECT pid,usename,application_name,state,xact_start,wait_event_type,wait_event,pg_blocking_pids(pid) AS blockers,left(query,400) AS query FROM pg_stat_activity WHERE datname='ledger' ORDER BY xact_start NULLS LAST;"
/usr/bin/cat /var/lib/raiden/memory/protections.list /var/lib/raiden/memory/evolution.md
SELECT pid,usename,pg_cancel_backend(pid) FROM pg_stat_activity WHERE pid=46731 AND usename='migrator' AND backend_type='client backend' AND state='active' AND wait_event_type='Lock' AND query='LOCK TABLE public.entries IN ACCESS EXCLUSIVE MODE';
SELECT pid,usename,state,pg_terminate_backend(pid,5000) FROM pg_stat_activity WHERE pid=46719 AND usename='analyst' AND backend_type='client backend' AND state='idle in transaction';
SELECT rolname,rolsuper,rolconnlimit,rolconfig FROM pg_roles WHERE rolname IN ('analyst','migrator'); SELECT setdatabase,setrole,setconfig FROM pg_db_role_setting WHERE setrole IN (SELECT oid FROM pg_roles WHERE rolname IN ('analyst','migrator'));
/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 10 | /usr/bin/tee "$RAIDEN_INCIDENT_DIR/snapshot.txt"
PGCONNECT_TIMEOUT=3 psql -X -At -d postgres -c "SELECT rolname,rolconfig FROM pg_roles WHERE rolname IN ('analyst','migrator'); SELECT setdatabase,setrole,setconfig FROM pg_db_role_setting WHERE setrole IN (SELECT oid FROM pg_roles WHERE rolname IN ('analyst','migrator'));" > "$RAIDEN_INCIDENT_DIR/role-settings-before.txt"
ALTER ROLE analyst SET idle_in_transaction_session_timeout='15s'; ALTER ROLE migrator SET lock_timeout='3s';
```
SQL lines above were executed through psql in this order. Role defaults require fresh connections, not reload/restart.
## What I did
1. Loaded pg-triage and connection-storm; read existing protections and diagnosed exact blocking chain.
2. Canceled only verified waiting migrator statement (true), releasing application queue immediately.
3. Terminated only verified idle analyst root blocker (true); its transaction was uncommitted.
4. Preserved catalog role defaults in role-settings-before.txt; applied source-role timeouts and confirmed catalog entries without database overrides. No existing offender sessions remained afterward.
5. Observed recovery for over two minutes and reconciled application data. No table writes, config file edits, reloads, restarts or protection weakening.
## Verification
- Log evidence: arena locks acquired within 1ms of cancellation; corrected post-fix JSON filter found zero new lock waits, crash/recovery messages or 53300 events after 01:16:49.307. Initial filter had a jq precedence error and was rerun successfully; output saved in post-fix-log-events.txt.
- Rows increased from 46,095 at 01:17:10 to 49,547 at 01:19:20; latest created_at 01:19:20.160557 was <0.1s old. Zero blocked sessions or idle transactions older than 15s.
- Catalog: analyst idle_in_transaction_session_timeout=15s, migrator lock_timeout=3s, no database overrides. Fresh login SHOW attempts failed peer authentication; actual login/runtime expiration behavior was not proven and no authentication changes were made.
- Final pg_probe: ready=true, in_recovery=false, six clients/100 maximum, oldest transaction 0s, checksum_failures=0, crash lines=0, unchanged postmaster start 00:45:54.446158, restarts_left=6.
- fsync/full_page_writes/synchronous_commit on; allow_alter_system off; configuration errors zero.
- First oracle attempt: ok=true, acked=present=49552, lost=corrupt=unreadable=0, unrecorded_rows_in_db=43; empty damage samples. Full output in reconcile-final.json. Concurrent unrecorded rows are not lost acknowledged commits.
## Follow-ups
- Separate evolve session: validate timeout behavior using harmless isolated contention and appropriate role-login proof; register policy and add independent drift/lock-chain patrol and learned runbook. No new defenses built during this incident.
- Analyst client owner: end read transactions promptly; migrator owner: short transactions, bounded lock acquisition and retry/backoff outside transactions. Defaults are new-login-only and user-overridable, not immutable barriers.
- Direct offender login proof requires approved authentication; do not weaken peer authentication or existing SQL guards to test it.

RESOLUTION: resolved
SUMMARY: Released ledger lock queue, removed idle analyst blocker, bounded offender timeouts without restart; all 49,552 acknowledged commits verified intact.
