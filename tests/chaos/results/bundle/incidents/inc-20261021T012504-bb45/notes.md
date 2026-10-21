# inc-20261021T012504-bb45 notes
- 2026-10-21T01:25:12Z triage: ready, no recovery/crashes, six restarts left; no immediate containment needed.
- First signature 01:22:12.700Z AccessExclusiveLock wait, followed at 01:22:14.700Z by expected 3s lock timeout; repeated 01:22:40.191Z/01:22:42.191Z.
- Evidence: both waiter PIDs 49135/49258 are scratch_p5_migrator, application_name raiden-p5-scratch-proof, statement LOCK TABLE public.scratch_p5_queue_probe IN ACCESS EXCLUSIVE MODE. These are isolated P5 proof runs from prior incident evolve session, not public.entries or production migrator.
- Read proof script and evolution record: scratch analyst holds read lock, scratch migrator times out after 3s, analyst expires after 15s, finally drops scratch table/roles. First proof was rerun after correcting exception handling; this explains two event pairs.
- 01:25:34Z live catalog/activity and 89-lock-queue.sh: scratch table and both roles absent, no blocked arena or exclusive waiters, analyst/migrator defaults intact, no database overrides. No SQL/config/process changes warranted; preserve all protections and avoid rerunning alert-producing proof.
- Verification underway: read-only ledger oracle, durability/config checks and bounded live write/lock observation. No database state changes planned.
