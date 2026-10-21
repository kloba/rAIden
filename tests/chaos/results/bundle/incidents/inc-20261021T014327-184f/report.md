# Incident inc-20261021T014327-184f — Production lock queue bounded by existing protection
## Timeline (UTC)
- 01:43:16 approximately: migrator PID 59223 requests ACCESS EXCLUSIVE on public.entries behind lock holder PID 59211.
- 01:43:17.744–17.884: exclusive waiter and five arena reader/writer waits logged; watcher opens incident.
- 01:43:19.744: migrator receives 55P03 lock timeout; all five arena sessions acquire locks at the same timestamp.
- 01:43:20.640: analyst PID 59211 disconnects with an open transaction (unexpected EOF); root blocker removed.
- 01:43:33–47: agent triage confirms healthy primary, empty queue, intact P5 defaults and ongoing writes. No intervention needed.
- 01:45:42–50: repeat checks and final oracle succeed; no recurring lock waits for over two minutes.
## Impact
Five logged application sessions (one reader, four writers) waited 2.861–2.910 seconds. The migration lock request failed after its configured 3s limit. PostgreSQL remained available; no restart or crash occurred. No acknowledged data lost or damaged: oracle acked=present=91540, lost=corrupt=unreadable=0, ok=true.
## Root cause
A production analyst connection PID 59211 held a transaction lock on entries (catalog OID 104379). Migrator PID 59223 requested `LOCK TABLE public.entries IN ACCESS EXCLUSIVE MODE`, creating a queue behind this holder and delaying compatible application locks. The log explicitly names holder 59211 and the six-member wait queue. Its subsequent same-PID EOF record identifies analyst and idle-in-transaction state. The original analyst SQL and client intent were not logged; no assumption about intentional activity is needed.

Existing protection worked: migrator role has lock_timeout=3s, analyst has idle_in_transaction_session_timeout=15s; both non-superuser login roles, no database overrides. Migrator cancellation and five application lock acquisitions share timestamp 01:43:19.744. Analyst disconnected at 01:43:20.640 before the 15s idle limit; its departure was client EOF, not timeout enforcement. This was not an isolated scratch proof. No persistent role-policy drift or surviving offender existed when inspected.
## Key commands
```text
/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 10 | tee "$RAIDEN_INCIDENT_DIR/snapshot.txt"
PGCONNECT_TIMEOUT=3 psql -X -d ledger -c "SELECT pid,usename,application_name,state,now()-state_change AS state_age,wait_event_type,pg_blocking_pids(pid),left(query,300) FROM pg_stat_activity WHERE datname='ledger' ORDER BY xact_start NULLS LAST; ..."
bash /opt/raiden/checks.d/89-lock-queue.sh
jq -Rr 'fromjson? | select(.timestamp >= "2026-10-21 01:43:00" and .timestamp < "2026-10-21 01:43:33" and (.pid==59223 or (.message|test("lock|transaction timeout"))))' /var/log/postgresql/raiden-20261021.json | tee "$RAIDEN_INCIDENT_DIR/lock-log.json"
jq -Rr 'fromjson? | select(.timestamp >= "2026-10-21 01:43:00" and .timestamp < "2026-10-21 01:43:40" and (.pid==59211 or .pid==59223 or (.message|test("Lock|lock timeout|idle-in-transaction"))))' /var/log/postgresql/raiden-20261021.json | tee "$RAIDEN_INCIDENT_DIR/chain-log.json"
```
Only five diagnostic commands mattered; no database mutation, fix command, reload or restart was run. Existing automatic lock cancellation and client disconnect had already cleared the cause.
## What I did
1. Loaded triage and lock-queue runbooks; saved snapshot and correlated PID/session log evidence.
2. Confirmed entries identity, no blocked sessions, absence of analyst/migrator connections and exact protected role settings.
3. Preserved all defenses without changes. Did not cancel nonexistent culprits or replay noisy scratch proofs against production.
4. Observed recovery and write progress, ran read-only protection checks and authoritative reconciliation once.
## Verification
- At 01:43:47 entries count=88480, latest created_at=01:43:47.401170; at 01:45:42 count=91536, latest=01:45:42.291536: +3056 rows during observation.
- Repeat lock-queue check OK: blocked arena=0, exclusive/migrator waiters=0, old analyst idle transactions=0, database overrides=0.
- No new still-waiting, lock-timeout, crash or recovery messages after 01:43:20.640 through 01:45:42 (over two minutes).
- Protection check OK, seven registered applicable protection records verified.
- Final pg_probe ready=true, in_recovery=false, clients=6, oldest_xact_s=0, checksum_failures=0, crash lines=0; start unchanged at 00:45:54.446158, restarts_left=6.
- fsync/full_page_writes/synchronous_commit=on; allow_alter_system=off; config errors=0.
- Oracle first attempt: acked=91540, present=91540, lost=0, corrupt=0, unreadable=0, unrecorded_rows_in_db=81, ok=true, seconds=3.0. Unrecorded rows are concurrent/unacknowledged outcomes, not acknowledged loss. JSON saved in reconcile-final.json.
## Follow-ups
Client owners should close read transactions promptly and use bounded migration retry/backoff. P5 bounds waiting rather than preventing every brief queue; role defaults remain user-overridable. No new defenses built during this incident. Retain production alerts rather than treating all 55P03 events as scratch noise. Any further prevention belongs to a separate evolve session; do not weaken existing limits to accommodate migration failures.

RESOLUTION: resolved
SUMMARY: Existing 3s migrator timeout drained five production waiters; analyst client then disconnected. No intervention or restart needed; all 91540 acknowledged commits verified intact.
