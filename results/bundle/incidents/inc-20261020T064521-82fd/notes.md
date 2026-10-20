2026-10-20T06:45:35Z first error 2026-10-20T06:45:06Z; symptom repeated exit code 2 crash recovery; chose crash-loop-rca because shared_preload_libraries includes pg_crash from postgresql.auto.conf and crash worker is running; no OOM evidence.
2026-10-20T06:45:35Z will back up and alter postgresql.auto.conf via ALTER SYSTEM to remove pg_crash/crash.* because pg_crash crash worker is causing repeated exit code 2 crashes.
2026-10-20T06:45:38Z ALTER SYSTEM combined command failed before changes (cannot run inside transaction); retrying as separate ALTER SYSTEM commands.
2026-10-20T06:45:46Z restarting once because shared_preload_libraries is postmaster-start only; planned config validates as pg_stat_statements only and pg_probe restarts_left=5.
