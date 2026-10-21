# Incident inc-20261021T005838-7552 — Dropped ledger table recovered without acknowledged data loss
## Timeline (UTC)
- 00:58:22.178: arena@ledger PID 36542 issued `DROP TABLE public.entries`; first missing-relation error followed at 00:58:22.218.
- 00:58:43: Triage found primary healthy; disk, archiver and checksum evidence ruled out an infrastructure failure.
- 00:59: approximately: Switched WAL, confirmed archive segment 59, identified destructive commit XID 88645 at 00:58:22.180105.
- 00:59:12–00:59:19: Built side clone from verified base backup 20261021T004705; recovery stopped before commit 88645.
- 00:59:28: Inspected and dumped 19,792 intact pre-drop rows, schema and ACL.
- By 00:59:36: Atomically restored only entries in production; application reads/writes resumed and reconciliation passed.
- 01:00: approximately: Repeat reconciliation, integrity checks and stability probes passed; disposable clone cleaned up.
## Impact
Application operations referencing entries failed for approximately 74 seconds. All 19,792 acknowledged rows were temporarily inaccessible, not permanently lost. Production stayed online; no primary restart or whole-cluster rewind occurred. Final oracle found all 20,734 acknowledged commits present, with zero lost, corrupt or unreadable rows.
## Root cause
A completed destructive DDL transaction by the application table-owner role arena removed public.entries. JSON log records arena@ledger, local psql, PID 36542 and the exact DROP statement. WAL records commit XID 88645, LSN 0/59479E38 at 00:58:22.180105 with six deleted relation files in database 16434. The pre-commit clone's entries filenode 84508 matched that deletion list. Production catalog confirmed the table absent. Arena was not a superuser, but owned the table and therefore could drop it. Existing extension allowlist did not cover DROP TABLE; it remained intact. No evidence identifies the human/process behind the local psql session beyond that attribution.
## Key commands
```text
/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 15
psql -X -At -d postgres -c "SELECT pg_walfile_name(pg_switch_wal()); SELECT oid FROM pg_database WHERE datname='ledger'; SELECT last_archived_wal,last_failed_wal FROM pg_stat_archiver;"
psql -X -d ledger -c "SELECT to_regclass('public.entries'); SELECT rolname,rolsuper,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname IN ('arena','raiden'); SELECT evtname,evtenabled FROM pg_event_trigger;"
jq -Rr 'fromjson? | select(.pid==36542) ' /var/log/postgresql/raiden-20261021.json
sudo -n -u postgres pg_waldump -p /var/lib/postgresql/wal_archive -r Transaction 000000010000000000000055 000000010000000000000059 2>/dev/null | grep -E 'desc: COMMIT .*rels:' | grep 'base/16434/'
/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_clone.sh --target-xid 88645 --before '2026-10-21 00:58:22.178 UTC'
pg_dump -h /var/lib/raiden/work/pitr-20261021T005912 -p 5433 -d ledger -t public.entries --no-owner -Fc -f /var/lib/raiden/work/inc-7552-entries.dump
pg_restore -l /var/lib/raiden/work/inc-7552-entries.dump
PGCONNECT_TIMEOUT=3 pg_restore -d ledger --single-transaction --exit-on-error /var/lib/raiden/work/inc-7552-entries.dump
/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_cleanup.sh /var/lib/raiden/work/pitr-20261021T005912
```
## What I did
Archived the current WAL segment and selected an exclusive XID target rather than guessing a time margin. Replayed the verified backup into an isolated side clone, preserving every pre-drop commit including rows created at 00:58:22.150347. Confirmed all payload hashes and inspected schema, ownership and grants. Saved a custom table-only dump and restored it in a single transaction with original arena ownership and analyst SELECT privileges; the primary key and both secondary indexes were restored. No existing table was overwritten, no durability setting weakened and no protection removed. Cleaned up the disposable clone, retaining the dump.
## Verification
- Clone and initial restored table: 19,792 rows, zero payload hash mismatches. Owner arena; ACL arena=arwdDxtm/arena and analyst=r/arena, matching clone. PK entries_pkey and account/created indexes present.
- `PGCONNECT_TIMEOUT=3 pg_amcheck -d ledger --parent-check --heapallindexed`: exit 0, no findings.
- `/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json`: baseline failed only because table was missing (acked=19792). Two post-repair runs passed; final after 30 seconds: acked=20734, present=20734, lost=0, corrupt=0, unreadable=0, unrecorded_rows_in_db=19, ok=true, all sample lists empty, seconds=0.6. Unrecorded rows are allowed concurrent/unknown client outcomes.
- Recent 40-second log query: no missing-relation or crash/recovery messages.
- Final pg_probe: ready=true, in_recovery=false, service active, checksum_failures=0, crash_lines_last_300kb=0; unchanged postmaster start 00:45:54, all six restart tokens retained.
- fsync/full_page_writes/synchronous_commit on; allow_alter_system off; pg_file_settings errors=0. Replica streaming with zero-byte sampled lag.
- 87-extension-guard.sh: OK, registry, trigger binding, function fingerprint and ACL intact.
## Follow-ups
- Separate evolve session: protect ledger application tables against destructive DDL and prove rejection using safe rollback tests. Review application-role ownership/DDL workflow and local access attribution. No new defenses built during this incident.
- Retained recovery artifact: /var/lib/raiden/work/inc-7552-entries.dump. Disposable PITR clone removed; production and standby untouched by cleanup.
- Keep verified recurring/off-host backups and WAL: a streaming replica replicates DROP and is not a backup.

RESOLUTION: resolved
SUMMARY: Restored dropped entries from an exclusive-XID PITR side clone; all 20,734 acknowledged commits verified present and intact, without a primary restart.
