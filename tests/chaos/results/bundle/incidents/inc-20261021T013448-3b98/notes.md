# Incident notes
- 2026-10-21T01:34:41.975Z: arena backend 55221 logged DROP TABLE public.entries.
- 2026-10-21T01:34:56Z: pg-triage snapshot: primary ready, no recovery/crashes, disk 7% used, zero archive failures; routed to learned-table-drop-guard.
- 2026-10-21T01:35Z: exact PID log shows ERROR 42501 at 01:34:41.978 UTC from block_protected_drop(); destructive attempt rejected in 3ms. entries exists, registry and triggers intact; all ledger sessions unblocked. 88-table-guard and 90-protections checks OK. No database intervention justified; do not restore/restart or repeat real-table DROP.
- Verification plan: read-only acknowledged-commit oracle, final readiness/durability probe. No new defenses during incident.
- 2026-10-21T01:35:18Z: oracle first attempt ok=true, acked=present=74948, lost/corrupt/unreadable=0, unrecorded=64; durability on and config errors=0. Final primary probe healthy, unchanged postmaster, six restarts left.
- Close-out: report written; lessons.md copied to lessons.before.md before appending one factual PID-correlation lesson. No database changes.
