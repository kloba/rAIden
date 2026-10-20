# Incident inc-20261020T064521-82fd — pg_crash preload caused repeated crash recovery
## Timeline (UTC)
- 06:45:06 first crash cycle in the log: background writer exited with exit code 2, postmaster terminated other processes, and automatic crash recovery started.
- 06:45:10 and 06:45:14 further crash cycles occurred from client backends exiting with code 2; another background writer crash followed at 06:45:22.
- 06:45:24 triage snapshot showed PostgreSQL accepting connections but flapping, postmaster/checkpointer ages near zero, live `shared_preload_libraries = pg_stat_statements, pg_crash` from `/var/lib/postgresql/18/main/postgresql.auto.conf:3`, and a `postgres: 18/main: crash worker` process.
- 06:45:35 backed up `/var/lib/postgresql/18/main/postgresql.auto.conf` to `/var/lib/raiden/work/postgresql.auto.conf.*.inc-20261020T064521-82fd.bak` and used separate `ALTER SYSTEM` commands to set `shared_preload_libraries = 'pg_stat_statements'` and reset `crash.delay`/`crash.signals`.
- 06:45:46 pre-restart validation showed planned `postgres -C shared_preload_libraries` = `pg_stat_statements`; restarted `postgresql@18-main` once because preloads require postmaster start.
- 06:45:47 service returned ready; post-restart checks showed live preload `pg_stat_statements`, no unknown backend types, and no crash worker.
- 06:47:01 after a 60 second observation window there were zero new crash/recovery messages; reconcile returned `ok: true`.
## Impact
PostgreSQL repeatedly killed and restarted all active server processes for roughly 41 seconds (06:45:06 to the deliberate clean restart at 06:45:46), causing transient connection failures and aborting in-flight transactions. Acknowledged data was not lost: the application oracle found 45,431 acknowledged rows present, lost 0, corrupt 0, unreadable 0, `ok: true`.
## Root cause
A local `ALTER SYSTEM` at 06:44:57 set `shared_preload_libraries = 'pg_stat_statements', 'pg_crash'` plus `crash.delay = 4` and `crash.signals = '9 15 3'` in `/var/lib/postgresql/18/main/postgresql.auto.conf`. The next restart loaded `pg_crash`, visible as `shared_preload_libraries = pg_stat_statements, pg_crash` and as a nonstandard `postgres: 18/main: crash worker`. That worker caused regular random process exits with code 2 (background writer and client backends), forcing postmaster crash recovery cycles. Kernel logs showed no OOM or I/O errors, and disk space was healthy.
## What I did
- Ran pg-triage snapshot and pg_probe to capture liveness, crash timeline, process list, config sources, disk and kernel evidence.
- Loaded crash-loop-rca because the evidence matched repeated crash recovery with an unexpected preload/background worker.
- Copied `/var/lib/postgresql/18/main/postgresql.auto.conf` to `/var/lib/raiden/work/postgresql.auto.conf.<timestamp>.inc-20261020T064521-82fd.bak` before changing it.
- Ran `ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements'` and `ALTER SYSTEM RESET crash.delay` / `ALTER SYSTEM RESET crash.signals` as separate commands.
- Validated planned config with `postgres -C shared_preload_libraries` before restart; it returned only `pg_stat_statements`.
- Restarted `postgresql@18-main` once after a ready/calm check, then verified the server was accepting connections.
## Verification
- Post-restart `SHOW shared_preload_libraries` returned `pg_stat_statements`.
- `pg_stat_activity` query for nonstandard backend types returned no rows, and `ps` showed only stock PostgreSQL processes; the `crash worker` was gone.
- `pg_file_settings` showed the applied `postgresql.auto.conf:3` value was `pg_stat_statements` with no error.
- 60 second stability window from 06:46:01: crash/recovery message count was 0; `pg_is_in_recovery()` was false and postmaster uptime was over 1 minute.
- Final pg_probe: ready true, in_recovery false, service active, shared_preload_libraries `pg_stat_statements`, checksum_failures 0.
- Reconcile JSON: `{"acked":45431,"present":45431,"lost":0,"corrupt":0,"unreadable":0,"unrecorded_rows_in_db":38,"ok":true,"seconds":1.4}`.
## Follow-ups
- In the evolve session, add or strengthen a patrol check that compares planned and live `shared_preload_libraries` against an allowlist and flags unknown background workers, including values introduced through `postgresql.auto.conf`.
- Consider a config-drift baseline for `postgresql.auto.conf` as well as `/etc/postgresql/18/main/conf.d` so accidental `ALTER SYSTEM` preload changes are caught before restart.
- Consider alerting on repeated crash-recovery cycles with a shorter time window because this incident recurred within seconds.
