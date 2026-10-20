---
name: learned-pg-surgery-toast
description: "pg_surgery heap_force_kill on pg_toast followed by XX001 unexpected chunk number - logical TOAST chunk removal - confirm, repair from PITR clone, and verify. Use when logs show heap_force_kill, pg_surgery, direct pg_toast surgery, or unexpected chunk number in pg_toast."
---
# learned-pg-surgery-toast

## Signature
- PostgreSQL JSON log shows `CREATE EXTENSION IF NOT EXISTS pg_surgery`, then `heap_force_kill('pg_toast.pg_toast_NNN'::regclass, ARRAY[...])`, optionally `DROP EXTENSION pg_surgery`.
- User reads fail with SQLSTATE `XX001` and messages like `unexpected chunk number 2 (expected 1) for toast value N in pg_toast_NNN`.
- Checksums may remain clean (`pg_stat_database.checksum_failures = 0`) because this is WAL-logged logical damage, not storage failure.

## Confirm
1. Start with `pg-triage`, then load `toast-corruption` and `pitr-restore`.
2. Find the destructive statements and first error:
   ```bash
   jq -Rr 'fromjson? | select(((.message//"")|test("pg_surgery|heap_force_kill|pg_toast|unexpected chunk number";"i"))) | "\(.timestamp) \(.user//"?")@\(.dbname//"?") \(.state_code//"") \(.message)"' /var/log/postgresql/raiden-*.json | tail -50
   ```
3. Map the TOAST table and scope damage:
   ```bash
   psql -X -At -d ledger -c "SELECT n.nspname||'.'||c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.reltoastrelid = 'pg_toast.pg_toast_NNN'::regclass"
   psql -X -At -d postgres -c "SELECT datname, checksum_failures FROM pg_stat_database WHERE datname IS NOT NULL"
   psql -X -At -d ledger -c "CREATE EXTENSION IF NOT EXISTS amcheck; SELECT * FROM verify_heapam('public.entries', check_toast => true) LIMIT 50"
   ```

## Fix
- Do not DELETE rows, do not vacuum/reindex/repack the damaged table first, and do not use `zero_damaged_pages`, `ignore_checksum_failure`, or more `pg_surgery`.
- Save a bad-row key list with a table-specific scanner that detoasts the affected payload column inside exception blocks. Preserve the CSV in the incident directory.
- Build a side PITR clone targeting before the `heap_force_kill` timestamp, because logical pg_toast damage is replayed if the target is too late.
- In the clone, rerun the scanner; it must be clean. Export only the good values for the bad primary keys.
- In production, apply one keyed `UPDATE` transaction that writes new payload values and commits only when the updated count equals the exported count.

## Verify
- Rerun the scanner in production: only the CSV header/no bad rows.
- `verify_heapam('<mapped table>', check_toast => true)` returns zero rows and `pg_amcheck -d ledger --heapallindexed --install-missing` is clean.
- Run `/opt/arena/ledger.py reconcile --json`; require `ok: true`, `lost=0`, `corrupt=0`, and `unreadable=0`.
- Confirm `pg_surgery` is not installed unless deliberately needed: `SELECT count(*) FROM pg_extension WHERE extname='pg_surgery'` returns `0`.

## Origin
incident inc-20261020T062423-f135, 2026-10-20
