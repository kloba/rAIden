# Incident inc-20261020T063807-pat — Planned pg_crash preload left on disk after crash loop
## Timeline (UTC)
- 06:18:28–06:19:01 jsonlog shows repeated crash/recovery cycles: server processes terminated/reinitialized and automatic recovery started five times.
- 06:19:06 a deliberate fast shutdown/restart from the earlier incident completed; PostgreSQL became ready again.
- 06:38:07 patrol opened this incident: CRIT crash-recovery (recent crash history) and CRIT preload-workers (planned shared_preload_libraries='pg_stat_statements,pg_crash', expected 'pg_stat_statements').
- 06:38:11 triage snapshot: PostgreSQL accepting connections, not in recovery, live shared_preload_libraries='pg_stat_statements', but pg_file_settings still showed /etc/postgresql/18/main/conf.d/50-experiment.conf line 2 planned 'pg_stat_statements,pg_crash' plus crash.delay/crash.signals.
- 06:38:28 backed up /etc/postgresql/18/main/conf.d/50-experiment.conf to /var/lib/raiden/work/50-experiment.conf.20261020T063828Z.bak and replaced it with only shared_preload_libraries='pg_stat_statements'.
- 06:38:32 reloaded PostgreSQL configuration; jsonlog confirmed crash.signals and crash.delay were removed from configuration.
- 06:39:40 verification after >60 seconds stable: no new crash/recovery events; ledger reconcile returned ok=true.

## Impact
PostgreSQL was accepting connections when this incident opened. The direct risk was latent: the on-disk planned restart-only preload still contained pg_crash, so any future restart would have reloaded the crash experiment and likely resumed crash loops. The crash history in the prior 30 minutes may have interrupted in-flight clients, but durability settings were not weakened and the authoritative ledger oracle found no lost/corrupt/unreadable acknowledged rows.

## Root cause
A late conf.d file still contained the crash experiment:
- `/etc/postgresql/18/main/conf.d/50-experiment.conf` before remediation: `shared_preload_libraries = 'pg_stat_statements,pg_crash'`, `crash.delay = 4`, `crash.signals = '9 15 3'`.
- `pg_file_settings` showed line 2 `shared_preload_libraries|pg_stat_statements,pg_crash|applied=f|setting could not be applied`; `postgres -C shared_preload_libraries` showed the planned value for a fresh start would be `pg_stat_statements,pg_crash`.
- Kernel journal showed no OOM killer evidence, and the earlier crash timeline included random PostgreSQL process terminations/exit-code crashes consistent with the pg_crash preload experiment.
The live server was stable only because it had already been restarted with `pg_stat_statements`; the disk config drift was the remaining root cause and would have been reintroduced on the next restart.

## What I did
- Ran pg-triage snapshot and `pg_probe`: confirmed ready=true, in_recovery=false, live shared_preload_libraries=`pg_stat_statements`, no unknown background workers, and restart budget left=5.
- Confirmed planned config with SQL and `postgres -C`: pg_file_settings exposed `/etc/postgresql/18/main/conf.d/50-experiment.conf` as the source of planned `pg_stat_statements,pg_crash` and crash.* settings.
- Backed up the source file to `/var/lib/raiden/work/50-experiment.conf.20261020T063828Z.bak`.
- Replaced `/etc/postgresql/18/main/conf.d/50-experiment.conf` with:
  - `# pg_crash experiment removed by rAIden; keep only known-good preload`
  - `shared_preload_libraries = 'pg_stat_statements'`
- Validated parse with `postgres -C shared_preload_libraries` returning `pg_stat_statements`.
- Reloaded only (`systemctl reload postgresql@18-main`); no restart was needed because the live value was already correct and this removed the planned time bomb.

## Verification
- `pg_probe` after remediation: ready=true, in_recovery=false, service_state=active, shared_preload_libraries=`pg_stat_statements`, checksum_failures=0, crash_lines_last_300kb=0, restarts_left=5.
- SQL after reload: `SELECT pg_is_in_recovery()` returned `f`; `SHOW shared_preload_libraries` returned `pg_stat_statements`; no pending_restart rows were returned.
- `pg_file_settings` after reload showed 50-experiment.conf line 2 `shared_preload_libraries|pg_stat_statements|applied=t`; crash.delay/crash.signals no longer appeared.
- Jsonlog after reload: `parameter "crash.signals" removed from configuration file, reset to default` and `parameter "crash.delay" removed from configuration file, reset to default`; no crash/recovery messages in the 70-second verification window.
- Ledger oracle: `{ "acked": 34071, "present": 34071, "lost": 0, "corrupt": 0, "unreadable": 0, "ok": true, "unrecorded_rows_in_db": 31 }`.

## Follow-ups
- In the separate evolve phase, keep or strengthen patrol detection that compares planned restart-time `postgres -C shared_preload_libraries` with the allowed live baseline; this incident proves planned-on-disk drift can remain dangerous even when the live server looks healthy.
- Consider an allowlist check for nonstandard crash.* GUCs in `pg_file_settings`.
