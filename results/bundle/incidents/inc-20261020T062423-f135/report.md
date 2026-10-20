# Incident inc-20261020T062423-f135 — TOAST corruption in public.entries repaired from PITR clone
## Timeline (UTC)
- 06:24:12.233-06:24:12.357 postgres@ledger queried `pg_toast.pg_toast_706021` for chunk_seq=1 CTIDs for chunk_ids 756443, 756618, 756647 and 756679.
- 06:24:12.404 postgres@ledger ran `CREATE EXTENSION IF NOT EXISTS pg_surgery`.
- 06:24:12.452 postgres@ledger ran `SELECT heap_force_kill('pg_toast.pg_toast_706021'::regclass, ARRAY['(14406,2)'::tid,'(14637,4)'::tid,'(14679,2)'::tid,'(14720,4)'::tid])`.
- 06:24:12.499 postgres@ledger ran `DROP EXTENSION pg_surgery`.
- 06:24:12.548 first error: `XX001 unexpected chunk number 2 (expected 1) for toast value 756443 in pg_toast_706021`; later reads reported the same signature for toast values 756618, 756647 and 756679.
- 06:24:26 triage snapshot showed PostgreSQL accepting connections, not in recovery, checksum_failures=0, no kernel OOM/I/O errors, and `pg_toast_706021` mapped to `public.entries`.
- 06:24:xx `verify_heapam('public.entries', check_toast => true)` and `pg_amcheck -d ledger --heapallindexed` identified four damaged heap rows at CTIDs `(140,59)`, `(142,28)`, `(144,35)`, `(144,43)`.
- 06:24:xx table-specific row scanner saved the four bad `public.entries` ids in `bad_entries.csv`: 2336093886121471391, 2394953312923791622, 3406649998999408864, 3469806427781173689.
- 06:25:07 built a side PITR clone targeting `2026-10-20 06:24:10 UTC`, before the destructive transaction; clone reached consistency and accepted connections on port 5433.
- 06:25:xx scanner on the clone found 0 bad rows; the four ids existed there and `md5(payload)=payload_md5` was true.
- 06:25:xx exported good payload values from the clone to `/var/lib/raiden/work/good_entries_inc-20261020T062423-f135.csv`.
- 06:25:xx applied one production transaction updating `public.entries.payload` by primary key for all four ids; committed only after `updated 4 all_ok t`.
- 06:25:xx post-repair scanner found 0 bad rows; `verify_heapam` returned 0 rows; `pg_amcheck` completed cleanly.
- 06:26:09 application reconcile completed with `ok: true`.
- 06:26:xx PITR clone was stopped and removed.

## Impact
Four `public.entries` rows had unreadable TOASTed `payload` values for roughly 1-2 minutes. Reads of those rows failed with SQLSTATE XX001 until the repair transaction committed. PostgreSQL stayed up; no restart or downtime was needed. Data was at risk but the final application oracle reported acked=14499, present=14499, lost=0, corrupt=0, unreadable=0, ok=true.

## Root cause
The corruption was logical damage to `public.entries`' TOAST table, not storage failure. Evidence:
- `public.entries` maps to toast relation `pg_toast.pg_toast_706021`.
- Logs immediately before the first error show superuser activity: `CREATE EXTENSION IF NOT EXISTS pg_surgery`, `heap_force_kill('pg_toast.pg_toast_706021'::regclass, ARRAY[...])`, then `DROP EXTENSION pg_surgery`.
- The forced-kill CTIDs corresponded to chunk_seq=1 rows for toast values later reported as `unexpected chunk number 2 (expected 1)`.
- Data checksums were enabled and `pg_stat_database.checksum_failures` stayed 0 for all databases; kernel log showed no OOM or I/O errors.
- A PITR clone stopped before 06:24:10 contained readable, md5-valid copies of the affected rows.

## What I did
- Ran pg-triage snapshot and confirmed the server was accepting connections and not in recovery.
- Loaded toast-corruption runbook and mapped `pg_toast.pg_toast_706021` to `public.entries`.
- Installed/used `amcheck`; `verify_heapam` and `pg_amcheck` scoped damage to exactly four `public.entries` rows.
- Wrote and ran a table-specific scanner (`find_bad_entries.sql`) to detoast every `public.entries.payload` and save the exact bad ids in `bad_entries.csv`.
- Built a PITR side clone at `2026-10-20 06:24:10 UTC`; verified the clone had no bad rows and had readable values for the four ids.
- Exported good `(id,payload)` rows from the clone and applied a single keyed `UPDATE public.entries SET payload = ...` transaction in production. It committed only after all four ids updated.
- Verified repair with scanner, `verify_heapam`, `pg_amcheck`, row-level md5 checks, and the application reconcile oracle.
- Cleaned up the PITR clone; kept incident artifacts and exported good row CSV under the incident/work paths.

## Verification
- `find_bad_entries.sql` after repair: `bad rows found: 0` and CSV contained only `id,row_tid,err` header.
- `SELECT count(*) FROM verify_heapam('public.entries', check_toast => true)`: `0`.
- `pg_amcheck -d ledger --heapallindexed --install-missing`: scanned 276/276 relations with no corruption output after repair.
- Checksums: all databases had `checksum_failures=0`.
- Stable server probe: ready=true, in_recovery=false, service active, `shared_preload_libraries=pg_stat_statements`.
- Reconcile JSON (`reconcile-062609.json`): `acked=14499`, `present=14499`, `lost=0`, `corrupt=0`, `unreadable=0`, `unrecorded_rows_in_db=12`, `ok=true`.
- Final 30-second corruption-log check returned 0 new matching events.

## Follow-ups
- Add a patrol/evolution item to detect/log unexpected `pg_surgery` extension creation or direct superuser modification of `pg_toast` relations.
- Consider an incident-specific learned skill for the signature `pg_surgery heap_force_kill` followed by `unexpected chunk number ... in pg_toast_*`.
- Review why a postgres@ledger session was able to run `heap_force_kill` on an application TOAST table; no further destructive action was taken during this incident.
