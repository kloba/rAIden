2026-10-20T06:24:32Z first error 2026-10-20T06:24:12.548Z: XX001 unexpected chunk number in pg_toast_706021; PostgreSQL accepting connections; chose toast-corruption skill
2026-10-20T06:24:49Z wrote table-specific scanner for public.entries because toast relation maps to entries not public.ledger
2026-10-20T06:25:07Z building PITR side clone target 2026-10-20 06:24:10 UTC to get known-good copies before first corruption error
2026-10-20T06:25:31Z applying keyed UPDATE for 4 corrupted public.entries payload values from PITR clone; will commit only if all 4 ids update
2026-10-20T06:26:20Z verification succeeded: scanner and verify_heapam clean, pg_amcheck clean, reconcile ok true
