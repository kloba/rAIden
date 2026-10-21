---
name: wraparound-vacuum
description: 'Transaction-ID / multixact wraparound risk and blocked vacuum - high age(datfrozenxid) or mxid_age(datminmxid), "WARNING: database ... must be vacuumed within N transactions", "database is not accepting commands that assign new transaction IDs (or MultiXactIds) to avoid wraparound data loss" (54000), anti-wraparound autovacuum not finishing or being canceled, freezing held back by long transactions, prepared transactions or replication slots.'
---

# wraparound-vacuum - get datfrozenxid moving again

Limits: autovacuum_freeze_max_age default 200M (anti-wraparound autovacuum starts), vacuum_failsafe_age
1.6e9 (vacuum skips index cleanup), new XIDs refused ~3M before 2^31 (~2.147e9). Patrol: WARN 1.2e9, CRIT 1.7e9.
Read-only transactions keep working even at the stop limit; plain VACUUM works in multi-user mode.

## 1. How bad, where
```bash
psql -X -At -d postgres -c "SELECT datname, age(datfrozenxid), mxid_age(datminmxid), datallowconn FROM pg_database ORDER BY 2 DESC"
psql -X -d ledger -c "SELECT c.oid::regclass, c.relkind, age(c.relfrozenxid) AS xid_age, mxid_age(c.relminmxid) AS mxid_age, pg_size_pretty(pg_total_relation_size(c.oid)) FROM pg_class c WHERE c.relkind IN ('r','m','t') ORDER BY age(c.relfrozenxid) DESC LIMIT 10"
psql -X -d postgres -c "SELECT pid, datname, relid, phase, heap_blks_total, heap_blks_scanned, index_vacuum_count FROM pg_stat_progress_vacuum"
psql -X -At -d postgres -c "SELECT pid, now()-xact_start, left(query,90) FROM pg_stat_activity WHERE backend_type='autovacuum worker'"   # '(to prevent wraparound)'
jq -Rr 'fromjson? | select(.message|test("must be vacuumed within|not accepting commands|canceling autovacuum task|lock not available|failsafe")) | "\(.timestamp) \(.dbname//"-") \(.message|.[0:120]) | \(.context//""|.[0:80])"' $(ls -1tr /var/log/postgresql/raiden-*.json | tail -2) | tail -15
```
Run the pg_class query in every database with high age (pg_class is per database; relid::regclass only resolves there).

## 2. What holds the xmin horizon back (freezing cannot pass it)
```bash
psql -X -d postgres -c "SELECT pid, datname, usename, state, age(backend_xmin) AS xmin_age, now()-xact_start AS xact_age, left(query,60) FROM pg_stat_activity WHERE backend_xmin IS NOT NULL ORDER BY age(backend_xmin) DESC LIMIT 5"
psql -X -d postgres -c "SELECT gid, database, owner, prepared, age(transaction) FROM pg_prepared_xacts ORDER BY prepared"
psql -X -d postgres -c "SELECT slot_name, active, age(xmin) AS xmin_age, age(catalog_xmin) AS catalog_xmin_age FROM pg_replication_slots"
psql -X -d postgres -c "SELECT pid, application_name, backend_xmin FROM pg_stat_replication"    # hot_standby_feedback from standbys
psql -X -At -d postgres -c "SELECT name, setting, source, sourcefile FROM pg_settings WHERE name ~ '^(autovacuum|vacuum_)' AND source <> 'default'"
psql -X -At -d ledger -c "SELECT oid::regclass, reloptions FROM pg_class WHERE reloptions::text ~ 'autovacuum'"
```
Fix the blocker first (log each action in notes.md):
- long transaction / idle in transaction: `SELECT pg_terminate_backend(<pid>)` (client backends only,
  connection-storm rules); its uncommitted work was never acknowledged.
- orphaned prepared transaction (old, unknown gid/owner): connect to its database and
  `ROLLBACK PREPARED '<gid>';` - if the owner is the application role, escalate instead of guessing.
- inactive unknown replication slot with old xmin: `SELECT pg_drop_replication_slot('<name>');`
  (record its row first); a known consumer's slot: escalate.
- tampered settings (`autovacuum = off`, huge `autovacuum_freeze_max_age`, per-table
  `autovacuum_enabled=false`): revert in the source file / `ALTER TABLE ... RESET (autovacuum_enabled)`,
  then `SELECT pg_reload_conf();` (anti-wraparound autovacuum runs even with autovacuum=off).
- autovacuum repeatedly `canceling autovacuum task`: find the conflicting lock holder
  (`pg_locks` / `pg_blocking_pids`) and stop the DDL/LOCK loop.

## 3. Freeze
Manual VACUUM is unthrottled by default (vacuum_cost_delay = 0). Oldest tables first, in the database
that owns them; run long ones in the background and watch pg_stat_progress_vacuum:
```bash
nohup psql -X -d ledger -c "SET statement_timeout = 0" -c "VACUUM (FREEZE, VERBOSE) public.entries" > /var/lib/raiden/work/vacuum-ledger.log 2>&1 &
# near/after the stop limit, faster: VACUUM (FREEZE, VERBOSE, INDEX_CLEANUP OFF) <table>
```
Then advance datfrozenxid for the whole database: `psql -X -d <db> -c "SET statement_timeout = 0" -c "VACUUM (FREEZE)"`
(or `vacuumdb -d <db> --freeze --jobs=2`). Speed up autovacuum (reload, no restart):
```sql
ALTER SYSTEM SET autovacuum_vacuum_cost_limit = 2000;   -- default -1 => vacuum_cost_limit 200
ALTER SYSTEM SET autovacuum_vacuum_cost_delay = '1ms';   -- default 2ms
SELECT pg_reload_conf();
```
Revert both (`ALTER SYSTEM RESET ...; SELECT pg_reload_conf();`) after the age is back below 200M.

## 4. Verify
`age(datfrozenxid)` per database drops (typically to < vacuum_freeze_table_age, 150M); no new
`must be vacuumed within` / 54000 lines; a write works:
`psql -X -At -d ledger -c "SELECT max(created_at) FROM public.entries"` keeps advancing.

## Escalation only
Single-user mode (`postgres --single`) requires stopping the server and is NOT allowed for the agent.
If VACUUM cannot advance datfrozenxid (corruption errors during vacuum, blocker you may not remove),
write the escalation with ages, blockers and the vacuum log.
