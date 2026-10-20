2026-10-20T06:32:30Z pg-triage found PostgreSQL accepting connections; log shows arena@ledger DROP TABLE public.entries at 06:32:05.713 UTC followed by 42P01. Chosen skill pitr-restore because app table was dropped and data must be recovered from PITR clone.
2026-10-20T06:32:33Z running pg_switch_wal to force DROP WAL segment into archive because PITR requires archived WAL through destructive transaction.
2026-10-20T06:32:45Z identified DROP commit xid=236948 at 06:32:05.714529 UTC touching ledger relfilenodes base/16430/706021...; building side PITR clone stopping before xid.
2026-10-20T06:33:04Z restoring public.entries into production from PITR clone because production to_regclass is NULL and clone stopped before DROP has 23966 rows through 06:32:05.663940 UTC.
2026-10-20T06:34:08Z verification passed: reconcile ok=true lost=0 corrupt=0 unreadable=0 after restoring public.entries. Cleaning up PITR clone; keeping dump for incident evidence.
