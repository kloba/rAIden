---
name: connection-storm
description: 'Handle connection exhaustion and session pile-ups - "FATAL: sorry, too many clients already", "remaining connection slots are reserved for roles with the SUPERUSER attribute" (SQLSTATE 53300), many "idle in transaction" sessions, lock queues, runaway clients. Diagnose with pg_stat_activity, terminate only safe targets, set per-role timeouts.'
---

# connection-storm - too many sessions / idle-in-transaction pile-up

The agent is SUPERUSER and can use the superuser_reserved_connections slots (default 3). Do not waste
them: one short psql per command, no long-lived sessions. Log every termination in notes.md.

## 1. Size of the problem
```bash
jq -Rr --arg s "$(date -u -d '-15 min' '+%F %T')" 'fromjson? | select(.timestamp >= $s and .state_code=="53300") | "\(.timestamp[0:16]) \(.user//"-") \(.message|.[0:60])"' $(ls -1tr /var/log/postgresql/raiden-*.json | tail -2) | sort | uniq -c | tail
psql -X -At -d postgres -c "SELECT (SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend') AS used, current_setting('max_connections') AS max, current_setting('superuser_reserved_connections') AS su_reserved, current_setting('reserved_connections') AS reserved"
psql -X -d postgres -c "SELECT usename, application_name, client_addr, state, wait_event_type, count(*), max(now()-state_change) AS max_in_state, max(now()-xact_start) AS oldest_xact FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY 1,2,3,4,5 ORDER BY 6 DESC"
psql -X -d postgres -c "SELECT pid, pg_blocking_pids(pid) AS blocked_by, wait_event, now()-xact_start AS xact_age, left(query,60) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0 ORDER BY xact_age DESC LIMIT 20"
```
Patterns:
- many `idle in transaction` with old state_change => leaked transactions (client bug); they also hold
  locks and block vacuum;
- many `active` waiting on `Lock` => one blocker at the head of the queue (`blocked_by`);
- one application_name/client_addr with hundreds of `idle` sessions => connection leak / no pooling.

## 2. Terminate safely (order: idle in transaction -> lock blockers -> idle leaks)
Rules: only `backend_type = 'client backend'`; never `pg_backend_pid()`; never superuser sessions
(that includes the agent and the reconcile oracle); never autovacuum/aux processes; prefer
`idle in transaction` (their work is uncommitted, so no acknowledged commit is lost).
```sql
-- preview first (same WHERE, without pg_terminate_backend), then terminate
SELECT pid, usename, application_name, state, now()-state_change AS in_state, pg_terminate_backend(pid, 5000)
FROM pg_stat_activity
WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
  AND usename = 'arena'
  AND state IN ('idle in transaction', 'idle in transaction (aborted)')
  AND now() - state_change > interval '2 minutes';
```
Lock blocker: terminate the single head-of-queue pid shown in `blocked_by` after checking its query/age.
Terminating an `active` session mid-COMMIT gives an unknown outcome (the oracle tolerates `unknown_*`).
If even superuser slots are exhausted: wait 10-30 s and retry (sessions churn); last resort is SIGTERM to a
specific backend whose ps title shows `idle in transaction`
(`ps -eo pid,etimes,cmd | grep '[i]dle in transaction'`, then `sudo -n -u postgres kill -TERM <pid>`).
Never SIGKILL, never the postmaster (`cat /run/postgresql/18-main.pid`).

## 3. Stop it from coming back (new sessions only - reload not needed)
```sql
ALTER ROLE arena SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE arena SET statement_timeout = '60s';          -- app does small inserts; adjust if it runs long reports
ALTER ROLE arena SET lock_timeout = '10s';
-- optional, for a runaway client: cap the role, leaving room for others
ALTER ROLE arena CONNECTION LIMIT 80;                     -- choose < max_connections - reserved
```
Check: `psql -X -d postgres -c "SELECT rolname, rolconnlimit, rolconfig FROM pg_roles WHERE rolname='arena'"`.
Existing sessions keep old settings: terminate offenders as in step 2.
Raising max_connections / superuser_reserved_connections needs a restart (budgeted) - only with clear
evidence that the legitimate load exceeds the limit; never lower superuser_reserved_connections.

## 4. Verify
- no new 53300 lines for 2 minutes (rerun the jq above); used < 80% of max_connections;
- no `idle in transaction` older than the timeout; blocked list empty;
- application still writing: `psql -X -At -d ledger -c "SELECT max(created_at) FROM public.ledger"` is recent;
- if sessions were terminated during writes, run the reconcile oracle (skill data-loss-verification).

## Rollback
`ALTER ROLE arena RESET idle_in_transaction_session_timeout;` (same for other settings),
`ALTER ROLE arena CONNECTION LIMIT -1;`. Record the root cause (which client/app, what pattern) in the report.
