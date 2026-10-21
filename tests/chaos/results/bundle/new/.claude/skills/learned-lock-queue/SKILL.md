---
name: learned-lock-queue
description: "still waiting for AccessExclusiveLock on relation, idle analyst blocking migrator LOCK TABLE, or CRIT lock-queue - confirm lock chain and P5 drift, cancel only culprit and verify bounded role defaults."
---
# learned-lock-queue
## Signature
- `process N still waiting for AccessExclusiveLock on relation ...` followed by arena reader/writer lock waits.
- Analyst idle read transaction -> migrator exclusive waiter -> arena sessions in pg_blocking_pids.
- WARN/CRIT lock-queue: early idle blocker/exclusive waiter or analyst/migrator role policy drift.
## Confirm
```bash
bash /opt/raiden/checks.d/89-lock-queue.sh
PGCONNECT_TIMEOUT=3 psql -X -d ledger -c "SELECT pid,usename,state,now()-state_change AS state_age,wait_event_type,pg_blocking_pids(pid),left(query,300) FROM pg_stat_activity WHERE datname='ledger' ORDER BY xact_start NULLS LAST;"
PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "SELECT rolname,rolsuper,rolcanlogin,rolconfig FROM pg_roles WHERE rolname IN ('analyst','migrator'); SELECT * FROM pg_db_role_setting WHERE setdatabase<>0;"
```
Expected non-superuser login roles: migrator lock_timeout=3s; analyst idle_in_transaction_session_timeout=15s; no database overrides. Resolve actual PID chain, never assume query text alone means culprit.
## Fix
1. Load connection-storm and read protections.list. Write evidence/action to incident notes first.
2. Cancel only the verified waiting migrator, replacing 0 below with its observed PID:
```sql
SELECT pid,pg_cancel_backend(pid) FROM pg_stat_activity
WHERE pid=0 AND usename='migrator' AND backend_type='client backend'
AND state='active' AND wait_event_type='Lock';
```
3. Recheck chain. If verified analyst still holds abandoned uncommitted read transaction, end only that PID:
```sql
SELECT pid,pg_terminate_backend(pid,5000) FROM pg_stat_activity
WHERE pid=0 AND usename='analyst' AND backend_type='client backend'
AND state IN ('idle in transaction','idle in transaction (aborted)');
```
4. Save role defaults with psql to incident role-settings-before.txt. If lost, restore P5:
```sql
ALTER ROLE migrator SET lock_timeout='3s';
ALTER ROLE analyst SET idle_in_transaction_session_timeout='15s';
```
Defaults apply at fresh login; existing offender pool must reconnect through its owner. Do not kill arena/oracle sessions. No reload or restart needed. Escalate unexpected database overrides or legitimate work blocked by policy; never weaken registered protections.
## Verify
- Rerun detector and activity query: zero blocked arena, no old idle analyst, no lingering migrator queue; observe ongoing writes for >30s.
- pg_probe ready, unchanged postmaster, durability settings on, no config errors.
- Run `/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json` if transactions/data were affected; require ok=true.
- Calm evolve proof: use the explicit disposable fresh-login/expiry invocation in Pitfalls below. Do not run the catalogue lock-timeout helper on primary; a scratch table alone does not isolate timeout alerts.
- Require both PROOF OK lines and CLEANUP OK; stop the independently initialized disposable cluster afterwards. Primary settings need only read-only confirmation if intact.
## Pitfalls
- Scratch P5 timeouts can wake the watcher: load learned-scratch-proof-alert and correlate trusted audit/PID/statement before intervention. The fresh-login helper now refuses production before writes; run future deliberate timeout proofs only on an independently initialized disposable cluster, never primary/replica/PITR clone. Catalogue helpers are not primary-refusal guarded; do not rerun them on primary merely to classify an alert.
- SET ROLE does not apply ALTER ROLE login defaults. Peer-login failure is not timeout failure; never change authentication to prove defaults.
- P5 bounds queue lifetime, not zero waiting: an exclusive waiter can still stall clients for up to 3s. A live default can be overridden with SET; database/role overrides and old pools matter.
- Active long transactions or DDL already holding its lock are not bounded by these timeouts. Find the true blocker before canceling anything.
- Scratch proof catches IdleInTransactionSessionTimeout (SQLSTATE 25P03), not merely OperationalError; finally cleanup must run on failed assertions.
- After inc-20261021T014327-184f, also run `bash /opt/raiden/checks.d/89-lock-wait-history.sh`: transient production queues can drain between live samples. WARN for correlated 55P03 proves bounded waiting, not zero impact; CRIT for stale unmatched log waits is not proof of ongoing blocking. Run the live Confirm queries before intervention; if no culprit survives and defaults are intact, do not cancel/reconfigure/restart anything.
- Correlate session_id/PID/statement/timestamp. Analyst EOF with an open transaction is client disconnect, not evidence the 15s idle limit fired. Application_name alone never establishes scratch provenance. History samples two newest logs, 2MiB each/10min, exact unquoted migrator LOCK only; other SQL/roles and high-volume history can be missed.
- Reprove P5 only with explicit independently initialized disposable target: `RAIDEN_PROOF_HOST=<private-socket> RAIDEN_PROOF_PORT=<port> RAIDEN_PROOF_DBNAME=ledger timeout 45 /opt/raiden/venv/bin/python /var/lib/raiden/work/prove_lock_queue_defaults.py`. Copy only observed role defaults, not production data; prove different system identifier and preserve primary refusal. No need to reapply intact settings. Inc-184f reproof canceled equivalent fresh-login LOCK in 3.000s and expired idle analyst at 15s, with cleanup verified.
## Origin
Incident inc-20261021T011631-7c80, 2026-10-21; cancellation released five application waiters in ~1ms; no restart or acknowledged data loss.
