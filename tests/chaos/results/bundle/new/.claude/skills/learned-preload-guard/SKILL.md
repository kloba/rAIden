---
name: learned-preload-guard
description: "CRIT preload-guard, pg_crash worker started, or repeated terminated by signal with no OOM - confirm direct config preload drift and preserve the P1 pin."
---
# learned-preload-guard
## Signature
- `pg_crash worker started`, repeated auxiliary signal 9/client exit 2 and recovery.
- `CRIT preload-guard`: planned/live mismatch, masked experiment, pin fingerprint,
  file-setting error, unknown worker or mapped module. WARN means inspect failed.
## Confirm (read-only)
1. `bash /opt/raiden/checks.d/89-preload-guard.sh` (healthy: OK, rc=0).
2. `PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "SELECT name,setting,sourcefile,sourceline,pending_restart FROM pg_settings WHERE name IN ('shared_preload_libraries','allow_alter_system'); SELECT sourcefile,sourceline,name,setting,applied,error FROM pg_file_settings WHERE name='shared_preload_libraries' OR name LIKE 'crash.%' OR error IS NOT NULL; SELECT backend_type,count(*) FROM pg_stat_activity GROUP BY 1;"`
3. `sudo -n -u postgres /usr/lib/postgresql/18/bin/postgres -D /etc/postgresql/18/main -C shared_preload_libraries`
Expected live and planned `pg_stat_statements`; ALTER SYSTEM off; 99 pin last;
no active crash.* values or errors, stock workers. Also use pg_probe and
crash-loop-rca's maps/log/OOM commands: unknown worker alone is not attribution.
## Fix
- Record hypothesis in incident notes before changes; copy exact source first.
- If only earlier 50-experiment drift is masked, preserve 99 pin. Back up
  offending file to incident directory; comment only evidenced preload/crash
  assignments via postgres OS user. Do not disable whole file or other guards.
- Typical evidenced source cleanup (only if that file is the culprit):
  `cp -p /etc/postgresql/18/main/conf.d/50-experiment.conf "$RAIDEN_INCIDENT_DIR/50-experiment.before"`
  `sudo -n -u postgres sed -i -E 's/^([[:space:]]*(shared_preload_libraries|crash\.)[^#]*)/# disabled by raiden: \1/' /etc/postgresql/18/main/conf.d/50-experiment.conf`
- Validate `postgres -C`, then `sudo -n systemctl reload postgresql@18-main`.
- If malicious library is already mapped/live, reload cannot unload it. Follow
  crash-loop-rca: readiness-gated ONE budgeted restart in incident only.
- Never restart during evolve; never remove/change P1 or disable ALTER SYSTEM.
  Later filename, command-line override or legitimate library blocked: escalate.
## Verify
- Detector OK; pg_file_settings no errors; pg_probe ready, no recovery.
- Check fresh timestamp-bounded log window for no new crash/recovery events.
- After loaded-module incident run authoritative ledger reconcile --json:
  `/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json`
- `bash /opt/raiden/checks.d/90-protections.sh`; unchanged durability on.
## Pitfalls / scope
- Planned safe does not imply loaded safe; use maps/live settings before restart.
- Pin beats earlier files, not a privileged later filename, edited pin, auto.conf
  filesystem write, or command-line setting. ALTER SYSTEM guard only covers SQL.
- Detector allows enumerated stock workers and postmaster PG-lib modules only;
  legitimate additions need review, not suppression. It cannot identify writer.
- Proof uses retained config copy with attack and positive control; never load
  pg_crash on a real server. Use /var/lib/raiden/work/prove_pin_preload_597d.sh.
## Origin
Incident inc-20261021T015719-597d, 2026-10-21; direct conf.d override bypassed
SQL-only protection. Exact retained-copy attack/control proof passed; no restart.
