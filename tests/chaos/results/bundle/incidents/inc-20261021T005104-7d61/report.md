# Incident inc-20261021T005104-7d61 — Four damaged TOAST payloads recovered
## Timeline (UTC)
- 00:50:54.428 postgres created pg_surgery; 00:50:54.534 removed it.
- 00:50:54.578 first unexpected chunk number 2 (expected 1) error in pg_toast_84508.
- 00:51:10 triage confirmed stable primary, no crashes, checksum failures or storage errors.
- 00:51–00:52 scanner and amcheck isolated four rows; pre-repair oracle unreadable=4, lost=0.
- 00:51:36–00:51:40 built side PITR clone from verified base, stopping before 00:50:54.401345 commit.
- 00:52 repair committed exactly four keyed payload updates from hash-validated clone values.
- 00:52–00:53 full scanning and amcheck clean; final oracle ok=true.
- 00:53 side clone stopped and removed; primary and streaming replica healthy.
## Impact
Four acknowledged entries were present but unreadable, causing queries touching their payloads to fail. No primary downtime or acknowledged row loss. Other application writes continued. Damage observed from 00:50:54 until repair at approximately 00:52; all four payloads restored without changing account, amount, hash, key or timestamp.
## Root cause
Superuser TOAST tuple surgery is strongly supported by the evidence: postgres created pg_surgery immediately before corruption and removed it immediately afterwards. WAL between those DDL commits contains four full-page images on TOAST filenode 84518 (blocks 9516, 9621, 9779, 9795), not ordinary chunk DELETE records. Four entries then lacked readable chunk 1 (values 91601, 91678, 91808, 91817). Checksums were enabled with zero failures, and kernel/primary logs showed no I/O errors or crashes. The exact surgery SELECT and actor identity beyond database role postgres are not recorded with log_statement=ddl. The dangerous extension was already removed, with no active culprit session; repair removed all resulting live-row corruption. No new preventive defenses were built during this session.
## Key commands
```text
/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 30
PGCONNECT_TIMEOUT=3 psql -X -d ledger -c "SELECT c.oid::regclass FROM pg_class c WHERE c.reltoastrelid='pg_toast.pg_toast_84508'::regclass; ..."
jq -Rr 'fromjson? | select(.timestamp >= "2026-10-21 00:50:50" and .timestamp < "2026-10-21 00:50:55") | ...' /var/log/postgresql/raiden-20261021.json
PGCONNECT_TIMEOUT=3 psql -X -q -d ledger -v ON_ERROR_STOP=1 -f /opt/raiden/.claude/skills/toast-corruption/scripts/find_bad_rows.sql > "$RAIDEN_INCIDENT_DIR/bad_rows.csv"
/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_clone.sh --target-time '2026-10-21 00:50:54.400 UTC' | tee "$RAIDEN_INCIDENT_DIR/clone-build.txt"
PGCONNECT_TIMEOUT=3 psql -X -h /var/lib/raiden/work/pitr-20261021T005136 -p 5433 -d ledger -f "$RAIDEN_INCIDENT_DIR/export-good.sql"
PGCONNECT_TIMEOUT=3 psql -X -d ledger -f "$RAIDEN_INCIDENT_DIR/repair.sql" | tee "$RAIDEN_INCIDENT_DIR/repair-result.txt"
/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_cleanup.sh /var/lib/raiden/work/pitr-20261021T005136
```
## What I did
Mapped the TOAST table, preserved the four damaged keys/ctids/errors, and checked the entire database. Recovered into a side clone from base 20261021T004705, preserving production throughout. Clone scan was clean and each victim payload matched its stored MD5. Exported four values, then updated only matching keys/hashes in one transaction, with lock/statement timeouts and rollback unless all four were updated. Commit returned updated=4, all_ok=t. Removed only the side clone after successful verification. No primary restart/reload, deletes, durability changes or existing protection changes.
## Verification
- Pre-repair oracle: acked=8790, present=8786, lost=0, corrupt=0, unreadable=4, ok=false.
- Post-repair scanner: header only; verify_heapam(check_toast=>true): 0 errors.
- pg_amcheck -d ledger --heapallindexed --progress: all 277 relations/13790 pages checked, exit 0, no errors.
- Final reconcile (first post-repair attempt): acked=11208, present=11208, lost=0, corrupt=0, unreadable=0, unrecorded_rows_in_db=8, ok=true, seconds=0.3. No lost/corrupt/unreadable samples.
- Final pg_probe: ready=true, recovery=false, service active, checksum_failures=0, crash lines=0, restarts_left=6.
- fsync, full_page_writes, synchronous_commit all on; pg_surgery absent.
- Replica streaming asynchronously, lag_bytes=0. No corruption/crash messages in final observation since 00:52:15.
## Follow-ups
In separate evolve session, implement/test an extension allowlist to reject pg_surgery installation and monitor it; review superuser access and audit coverage. Replica reproduces WAL-logged damage and is not a historical backup. Keep recurring verified backups plus off-host copies. Clone removed; recovered CSV /var/lib/raiden/work/inc-7d61-good_rows.csv retained for audit, alongside bad-key lists, scripts and evidence in incident directory. No permanent staging tables left behind.

RESOLUTION: resolved
SUMMARY: Recovered four unreadable ledger payloads from a verified PITR side clone; all acknowledged commits verified intact, no primary restart or durability weakening.
