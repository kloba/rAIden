# Incident inc-20261020T063221-0cee — public.entries dropped and restored
## Timeline (UTC)
- 06:32:05.713 log recorded `arena@ledger` executing `DROP TABLE public.entries` from local `psql` (pid 186810).
- 06:32:05.727 first application `ERROR 42P01 relation "entries" does not exist`; errors continued until 06:33:16 while the table was missing/restoring.
- 06:32:25 triage snapshot showed PostgreSQL online, not in recovery, no checksum failures, and `shared_preload_libraries=pg_stat_statements`.
- 06:32:34 forced WAL switch archived segment `000000010000000100000046` so the destructive transaction was available for PITR.
- 06:32:35 `pg_waldump` identified the DROP commit as transaction xid `236948` at `2026-10-20 06:32:05.714529 UTC`, touching ledger relfilenodes under `base/16430/`.
- 06:32:46-06:32:56 built a side PITR clone from base backup `20261020T061759`, stopping before xid `236948`; clone recovery reported `recovery stopping before commit of transaction 236948` and became ready.
- 06:33:00 clone inspection found `public.entries` present with 23,966 rows and max `created_at=2026-10-20 06:32:05.66394+00`; production `to_regclass('public.entries')` was NULL.
- 06:33:15 restored `public.entries` to production with `pg_dump`/`pg_restore` from the clone and reset table/index ownership to `arena`.
- 06:33:59 reconciliation completed successfully with `ok=true`.
- 06:34:00 cleaned up the side PITR clone; retained `/var/lib/raiden/work/entries-pitr-inc-20261020T063221.dump` as incident evidence.

## Impact
The application table `public.entries` was absent for about 70 seconds, causing many `42P01 relation "entries" does not exist` errors. PostgreSQL itself stayed online; no restart was needed. Data was at risk because an application ledger table was dropped. Authoritative reconciliation after restore reported 24,828 acknowledged rows present, with lost=0, corrupt=0, unreadable=0, and ok=true. There are 23 unrecorded rows in the database, which the oracle treats as acceptable in-flight/non-acked rows.

## Root cause
A destructive DDL statement was executed in the production `ledger` database: JSON log line at `2026-10-20 06:32:05.713 UTC` shows `user=arena`, `dbname=ledger`, `application_name=psql`, `remote_host=[local]`, `statement: DROP TABLE public.entries`. The first missing-relation error followed 14 ms later. WAL evidence confirmed the commit: `pg_waldump` found transaction `236948` committing at `06:32:05.714529 UTC` with dropped relfilenodes in database OID `16430` (ledger). No crash, storage, or corruption evidence was present during this incident.

## What I did
- Ran pg-triage snapshot and `pg_probe`; confirmed the server was accepting connections, not in recovery, and had checksum_failures=0.
- Switched WAL with `SELECT pg_walfile_name(pg_switch_wal())`, then waited until archiver reported `000000010000000100000046` archived.
- Found the destructive transaction using `pg_waldump -p /var/lib/postgresql/wal_archive -r Transaction ... | grep base/16430/`; selected xid `236948` as the exclusive recovery target.
- Built a side clone only under `/var/lib/raiden/work` using `pitr_clone.sh --target-xid 236948 --before '2026-10-20 06:32:05 UTC'`.
- Inspected clone schema/counts and verified production table was missing.
- Dumped only `public.entries` from the clone to `/var/lib/raiden/work/entries-pitr-inc-20261020T063221.dump` and restored it to production with `pg_restore --exit-on-error`.
- Set table and index ownership back to `arena`.
- Ran the application reconciliation oracle and removed the side clone with `pitr_cleanup.sh`.

## Verification
- `pg_probe`: ready=true, in_recovery=false, service_state=active, checksum_failures=0.
- Production table after restore: `count(*)=24021`, `max(created_at)=2026-10-20 06:33:26.04603+00`, owner `arena`, primary key `entries_pkey`, indexes `entries_account_idx` and `entries_created_idx` present.
- No new `42P01 relation "entries" does not exist` log lines in the last 40 seconds at 06:34 UTC.
- Reconcile JSON from `/var/lib/raiden/incidents/inc-20261020T063221-0cee/reconcile-063359.json`: `acked=24828`, `present=24828`, `lost=0`, `corrupt=0`, `unreadable=0`, `unrecorded_rows_in_db=23`, `ok=true`, `seconds=0.8`.

## Follow-ups
- In a separate evolve session, consider a guardrail/detector for destructive DDL by application role `arena` against protected tables, or a learned skill for fast table-drop PITR recovery.
- Review why local `psql` as `arena` was able to execute `DROP TABLE public.entries`; revoke unnecessary DDL privileges or split runtime and migration roles if compatible with the application.
- Keep the dump `/var/lib/raiden/work/entries-pitr-inc-20261020T063221.dump` until humans confirm it is no longer needed.
