---
name: connection-storm
description: 'Handle connection exhaustion, session pile-ups and lock queues - "FATAL: sorry, too many clients already", "remaining connection slots are reserved for roles with the SUPERUSER attribute" (SQLSTATE 53300), many "idle in transaction" sessions, "process N still waiting for ...Lock on relation ..." (log_lock_waits), an ALTER TABLE / LOCK TABLE that freezes every writer behind it, runaway clients. Diagnose with pg_stat_activity and pg_blocking_pids, end only the culprit, then cap that role.'
---

# connection-storm - too many sessions / lock queue / idle-in-transaction pile-up

The agent is SUPERUSER and can use the superuser_reserved_connections slots (default 3). Do not waste
them: one short psql per command, no long-lived sessions. Log every termination in notes.md.
The application's role is `arena` and the oracle/agent run as `raiden`: they are VICTIMS here, never the offender.
The offender is another role (a report job, an analyst, a deploy tool): find it from the evidence, do not assume.

## 1. Size of the problem
```bash
jq -Rr --arg s "$(date -u -d '-15 min' '+%F %T')" 'fromjson? | select(.timestamp >= $s and .state_code=="53300") | "\(.timestamp[0:16]) \(.user//"-") \(.message|.[0:60])"' $(ls -1tr /var/log/postgresql/raiden-*.json | tail -2) | sort | uniq -c | tail
psql -X -At -d postgres -c "SELECT (SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend') AS used, current_setting('max_connections') AS max, current_setting('superuser_reserved_connections') AS su_reserved, current_setting('reserved_connections') AS reserved"
psql -X -d postgres -c "SELECT usename, application_name, client_addr, state, wait_event_type, count(*), max(now()-state_change) AS max_in_state, max(now()-xact_start) AS oldest_xact FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY 1,2,3,4,5 ORDER BY 6 DESC"
```
Patterns:
- one role (not `arena`, not `raiden`) holding most of the slots, usually `idle` => a leaking job / no pooling: the OFFENDER;
- many `idle in transaction` with old state_change => leaked transactions (client bug); they also hold
  locks and block vacuum;
- the application's role has few sessions and errors `53300` => it is the victim of somebody else's sessions.

## 1b. Lock queue (connections are fine, the application is frozen)
`still waiting for ...Lock on relation` in the log, writers `active` with wait_event_type `Lock`, no commits. A DDL statement
(`ALTER TABLE`, `LOCK TABLE`, `CREATE INDEX`) needs a strong lock; while it WAITS for an old transaction, every new reader and
writer queues behind it, although it holds nothing yet. So the head of the queue is often NOT the root cause.
```bash
psql -X -d postgres -c "SELECT a.pid, a.usename, a.state, a.wait_event_type, now()-a.xact_start AS xact_age, now()-a.state_change AS in_state, pg_blocking_pids(a.pid) AS blocked_by, left(a.query,70) AS query FROM pg_stat_activity a WHERE a.backend_type='client backend' AND (cardinality(pg_blocking_pids(a.pid)) > 0 OR a.pid IN (SELECT unnest(pg_blocking_pids(b.pid)) FROM pg_stat_activity b)) ORDER BY xact_age DESC NULLS LAST"
psql -X -d postgres -c "SELECT l.pid, l.mode, l.granted, c.relname FROM pg_locks l JOIN pg_class c ON c.oid = l.relation WHERE c.relname NOT LIKE 'pg\_%' ORDER BY l.granted, l.pid"
```
- ROOT blocker = a pid that blocks others and is not itself waiting (here often a forgotten `idle in transaction` session
  holding a plain read lock). The WAITING DDL is the next one in the chain; the application sessions are behind them.
- Fastest, least destructive release: `SELECT pg_cancel_backend(<waiting DDL pid>)` (only that statement fails; the queue drains in
  milliseconds), then end the root blocker (step 2) so the next DDL run does not queue again.
- If the root blocker is `idle in transaction` its work is uncommitted: ending it loses no acknowledged commit.

## 2. Terminate safely (order: idle in transaction -> lock blockers -> idle leaks of the offender)
Rules: only `backend_type = 'client backend'`; never `pg_backend_pid()`; never superuser sessions
(that includes the agent and the reconcile oracle); never the application role's own sessions (`arena`); never
autovacuum/aux processes; prefer `idle in transaction` (their work is uncommitted, so no acknowledged commit is lost).
```sql
-- preview first (same WHERE, without pg_terminate_backend), then terminate
SELECT pid, usename, application_name, state, now()-state_change AS in_state, pg_terminate_backend(pid, 5000)
FROM pg_stat_activity
WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
  AND usename = '<offender role>'
  AND (state IN ('idle in transaction', 'idle in transaction (aborted)') OR state = 'idle');
```
Lock blocker: terminate the ROOT blocker from step 1b after checking its query/age.
Terminating an `active` session mid-COMMIT gives an unknown outcome (the oracle tolerates `unrecorded_rows_in_db`).
If even superuser slots are exhausted: wait 10-30 s and retry (sessions churn); last resort is SIGTERM to a
specific backend whose ps title shows `idle in transaction`
(`ps -eo pid,etimes,cmd | grep '[i]dle in transaction'`, then `sudo -n -u postgres kill -TERM <pid>`).
Never SIGKILL, never the postmaster (`cat /run/postgresql/18-main.pid`).

## 3. Stop it from coming back (new sessions only - reload not needed)
Apply to the OFFENDER's role (never `arena`, never `raiden`). The ready-made, proven versions are P4 (connection and idle
limits) and P5 (lock_timeout for DDL roles, idle-in-transaction timeout for blockers) in skill preventive-protections:
```sql
ALTER ROLE <offender> CONNECTION LIMIT 10;                       -- choose well below max_connections - reserved
ALTER ROLE <offender> SET idle_session_timeout = '2min';
ALTER ROLE <offender> SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE <ddl role> SET lock_timeout = '3s';                   -- a migration waits at most 3 s instead of freezing the application
```
Check: `psql -X -d postgres -c "SELECT rolname, rolconnlimit, rolconfig FROM pg_roles WHERE rolname='<offender>'"`.
Existing sessions keep old settings: terminate offenders as in step 2.
Raising max_connections / superuser_reserved_connections needs a restart (budgeted) - only with clear
evidence that the legitimate load exceeds the limit; never lower superuser_reserved_connections.
Background on pool sizing, idle timeouts and what NOT to claim: skill postgres-best-practices, `references/postgresql-connection-management.md`.

## 4. Verify
- no new 53300 lines for 2 minutes (rerun the jq above); used < 80% of max_connections;
- the blocked list from 1b is empty and no `still waiting` line is newer than the fix;
- no `idle in transaction` older than the timeout;
- application still writing: `psql -X -At -d ledger -c "SELECT max(created_at) FROM public.entries"` is recent;
- if sessions were terminated during writes, run the reconcile oracle (skill data-loss-verification).

## Rollback
`ALTER ROLE <role> RESET idle_in_transaction_session_timeout;` (same for other settings),
`ALTER ROLE <role> CONNECTION LIMIT -1;`. Record the root cause (which role/client, what pattern) in the report.
