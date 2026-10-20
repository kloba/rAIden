---
name: learned-pg-crash-preload
description: "repeated terminated by signal 9 plus crash worker and shared_preload_libraries contains pg_crash - confirm the preload experiment, remove it safely, restart once, and verify stability."
---
# learned-pg-crash-preload

## Signature
- PostgreSQL jsonlog repeats `terminated by signal 9: Killed`, `exited with exit code 2`, `terminating any other active server processes`, or `automatic recovery in progress` across different process types.
- `pg_probe` or `SHOW shared_preload_libraries` includes `pg_crash`; after incident inc-20261020T064521-82fd also check for `pg_crash` introduced through `ALTER SYSTEM` in `/var/lib/postgresql/18/main/postgresql.auto.conf`.
- `pg_stat_activity` or `ps` shows a nonstandard `crash worker` background worker.
- Kernel journal has no matching OOM-killer evidence.

## Confirm
1. Check live and planned preload sources:
   ```bash
   PGCONNECT_TIMEOUT=3 psql -X -At -d postgres -c "SELECT name, setting, sourcefile, sourceline FROM pg_settings WHERE name='shared_preload_libraries'; SELECT sourcefile, sourceline, name, setting, applied, error FROM pg_file_settings WHERE name='shared_preload_libraries' OR name LIKE 'crash.%' ORDER BY sourcefile,sourceline;"
   sudo -n -u postgres /usr/lib/postgresql/18/bin/postgres -D /var/lib/postgresql/18/main -c config_file=/etc/postgresql/18/main/postgresql.conf -C shared_preload_libraries
   ```
   Expected bad output: `pg_crash` appears, either from a late `/etc/postgresql/18/main/conf.d/*.conf` file or from `/var/lib/postgresql/18/main/postgresql.auto.conf` written by `ALTER SYSTEM`.
2. Check process evidence:
   ```bash
   PGCONNECT_TIMEOUT=3 psql -X -At -d postgres -c "SELECT DISTINCT backend_type FROM pg_stat_activity ORDER BY 1;"
   ps -eo pid,ppid,lstart,cmd | grep '[p]ostgres:'
   ```
   Expected bad output: `crash worker` appears.
3. Rule out OOM before blaming the preload:
   ```bash
   sudo -n journalctl -k --since '-60min' --no-pager | grep -iE 'out of memory|oom-kill|killed process' | tail -20; echo rc=$?
   ```

## Fix
1. Pick the exact source file from `pg_settings.sourcefile` or `pg_file_settings`, then copy it before editing. If the source is `postgresql.auto.conf`, prefer `ALTER SYSTEM SET/RESET` as separate commands after the backup (a combined multi-command `ALTER SYSTEM` runs inside a transaction and fails):
   ```bash
   ts=$(date -u +%Y%m%dT%H%M%SZ)
   sudo -n -u postgres cp -a /var/lib/postgresql/18/main/postgresql.auto.conf /var/lib/raiden/work/postgresql.auto.conf.$ts.bak
   PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements'"
   PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "ALTER SYSTEM RESET crash.delay"
   PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "ALTER SYSTEM RESET crash.signals"
   ```
   For a conf.d file, back up that file and edit it instead:
   ```bash
   sudo -n -u postgres cp -a /etc/postgresql/18/main/conf.d/50-experiment.conf /var/lib/raiden/work/50-experiment.conf.$ts.bak
   ```
2. Ensure `shared_preload_libraries` contains the known-good libraries (normally `pg_stat_statements`) and remove/comment/reset `crash.*` settings. Do not touch durability settings.
3. Validate the planned post-restart setting and config parse before restarting:
   ```bash
   sudo -n -u postgres /usr/lib/postgresql/18/bin/postgres -D /var/lib/postgresql/18/main -c config_file=/etc/postgresql/18/main/postgresql.conf -C shared_preload_libraries
   PGCONNECT_TIMEOUT=3 psql -X -At -d postgres -c "SELECT sourcefile, sourceline, name, setting, applied, error FROM pg_file_settings WHERE error IS NOT NULL OR name='shared_preload_libraries' ORDER BY sourcefile,sourceline;"
   ```
4. If the live server still has `pg_crash` loaded, `shared_preload_libraries` is restart-only: wait for `pg_isready` to be OK, check `pg_probe.restarts_left`, then perform one deliberate restart:
   ```bash
   until pg_isready -q -t 2; do sleep 1; done
   sudo -n systemctl restart postgresql@18-main
   until pg_isready -q -t 2; do sleep 1; done
   ```
5. If the live server is already clean but `pg_file_settings`/`postgres -C` still shows planned `pg_crash`, do **not** spend a restart. After backing up and editing the source file, reload to clear stale custom `crash.*` settings and verify the planned value:
   ```bash
   sudo -n systemctl reload postgresql@18-main
   sudo -n -u postgres /usr/lib/postgresql/18/bin/postgres -D /var/lib/postgresql/18/main -c config_file=/etc/postgresql/18/main/postgresql.conf -C shared_preload_libraries
   PGCONNECT_TIMEOUT=3 psql -X -At -d postgres -c "SELECT sourcefile,sourceline,name,setting,applied,error FROM pg_file_settings WHERE name='shared_preload_libraries' OR name LIKE 'crash.%' ORDER BY sourcefile,sourceline,name;"
   ```

## Verify
- `pg_probe` shows `ready: true`, `in_recovery: false`, and no `pg_crash` in `shared_preload_libraries`.
- SQL and process list show no `crash worker`:
  ```bash
  PGCONNECT_TIMEOUT=3 psql -X -At -d postgres -c "SHOW shared_preload_libraries; SELECT DISTINCT backend_type FROM pg_stat_activity ORDER BY 1;"
  ps -eo pid,ppid,lstart,cmd | grep '[p]ostgres:'
  ```
- Monitor jsonlog for at least 60 seconds with zero new crash/recovery events.
- Run the application oracle after crash recovery: `/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json` must return `ok: true`.

## Origin
incident inc-20261020T061839-d105, 2026-10-20; sharpened after incident inc-20261020T064521-82fd to cover `ALTER SYSTEM`/`postgresql.auto.conf` reintroduction.
