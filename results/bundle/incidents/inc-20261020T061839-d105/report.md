# Incident inc-20261020T061839-d105 — crash loop from pg_crash preload experiment
## Timeline (UTC)
- 06:18:15 `/etc/postgresql/18/main/conf.d/50-experiment.conf` was created/modified with `shared_preload_libraries = 'pg_stat_statements,pg_crash'` and `crash.*` settings.
- 06:18:24 first crash: postmaster logged `client backend (PID 180019) was terminated by signal 9: Killed`, followed by `terminating any other active server processes` and crash recovery.
- 06:18:28 second crash: `archiver process (PID 180066) exited with exit code 2` followed by reinitialization and crash recovery.
- 06:18:33 third crash: `background writer process (PID 180072) was terminated by signal 9: Killed` followed by reinitialization and crash recovery.
- 06:18:43 triage snapshot found PostgreSQL accepting connections but recently reinitialized; live `shared_preload_libraries` was `pg_stat_statements,pg_crash` from `conf.d/50-experiment.conf:2` and a nonstandard `crash worker` process was present.
- 06:18:53 chose `crash-loop-rca`; kernel OOM log check found no OOM killer evidence.
- 06:18:59 backed up `50-experiment.conf` to `/var/lib/raiden/work/50-experiment.conf.20261020T061859Z.bak` and edited the file to keep only `pg_stat_statements`, commenting out `crash.delay` and `crash.signals`.
- 06:19:05 performed one deliberate restart because `shared_preload_libraries` is restart-only; service returned ready (`rc=0`).
- 06:19:17-06:20:17 monitored for 60 seconds; crash/recovery event count after monitor start was 0.
- 06:20:47 data oracle ran successfully and reported `ok: true`.
## Impact
PostgreSQL crash-recovered multiple times between 06:18:24 and 06:19:02, aborting in-flight work and causing transient connection errors/recovery-mode responses. The deliberate restart at 06:19:05 caused a short planned interruption. No acknowledged application commits were lost or corrupted: reconcile reported 6084 acknowledged rows present, lost 0, corrupt 0, unreadable 0, ok true.
## Root cause
A temporary experiment config loaded `pg_crash` into the server: `shared_preload_libraries = 'pg_stat_statements,pg_crash'` in `/etc/postgresql/18/main/conf.d/50-experiment.conf:2`, plus `crash.delay = 4` and `crash.signals = '9 15 3'`. Evidence chain: the file mtime was 06:18:15, immediately before crash loops; pg_probe showed live `shared_preload_libraries` included `pg_crash`; process listing showed a nonstandard `postgres: 18/main: crash worker`; crash victims were random PostgreSQL processes (client backend, archiver, background writer/io worker); and kernel logs had no OOM killer entries. Removing `pg_crash` and restarting stopped the crash loop.
## What I did
- Ran pg-triage snapshot and pg_probe: server was accepting connections but had recent crash-recovery cycles and `pg_crash` loaded.
- Confirmed via SQL that `shared_preload_libraries` came from `/etc/postgresql/18/main/conf.d/50-experiment.conf:2` and inspected the file contents.
- Checked kernel journal for OOM killer evidence; none was found.
- Copied the experiment config to `/var/lib/raiden/work/50-experiment.conf.20261020T061859Z.bak`.
- Edited `/etc/postgresql/18/main/conf.d/50-experiment.conf` to set `shared_preload_libraries = 'pg_stat_statements'` and comment the `crash.*` settings.
- Validated planned setting with `postgres -C shared_preload_libraries`, which returned `pg_stat_statements`.
- Restarted `postgresql@18-main` once in a ready window; it returned ready with restart budget reduced from 6 to 5.
## Verification
- After restart, pg_probe showed `ready: true`, `in_recovery: false`, `shared_preload_libraries: pg_stat_statements`, and no `crash worker` in backend types.
- Process listing after restart showed only expected PostgreSQL processes (logger, io workers, checkpointer, background writer, walwriter, autovacuum launcher, archiver, logical replication launcher, and client backends).
- 60-second stability probe starting 06:19:17 found 0 new `terminated by signal`, non-benign `exited with exit code`, `all server processes terminated`, or recovery messages.
- Archiver status after the fix showed `failed_count=0` and a recent archived WAL `000000010000000100000036` at 06:19:37 UTC.
- Checksum counters for `postgres` and `ledger` were 0.
- Reconcile JSON: `{"acked": 6084, "present": 6084, "lost": 0, "corrupt": 0, "unreadable": 0, "unrecorded_rows_in_db": 4, "ok": true}`.
## Follow-ups
- In a separate evolve session, add a patrol check for planned and running `shared_preload_libraries` allowlist and unexpected background workers so an experiment preload is caught before/during restart.
- Add config drift detection or review for new late-order files in `/etc/postgresql/18/main/conf.d/`.
- Keep the backup `/var/lib/raiden/work/50-experiment.conf.20261020T061859Z.bak` until reviewed; current production config intentionally leaves the experiment file present but neutered.