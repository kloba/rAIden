# rAIden skills - index and conventions

Deployed to `/opt/raiden/.claude/skills/`; helper scripts are called by absolute path
`/opt/raiden/.claude/skills/<skill>/scripts/<script>`. Patrol checks live in `/opt/raiden/checks.d/`.

| Skill | Use when |
|---|---|
| `pg-triage` | ENTRY POINT of every incident: first 2 minutes, snapshot (`scripts/snapshot.sh [MIN]`), routing table |
| `crash-loop-rca` | repeated `terminated by signal` / `exited with exit code` / `reinitializing` crash-recovery cycles; unknown background worker; unexpected preload library |
| `stuck-shutdown` | `systemctl restart/stop` hangs (unit `deactivating`), `the database system is shutting down` for minutes, no startup process: the one guarded forced stop |
| `toast-corruption` | `missing chunk number`, `unexpected chunk number`, `invalid page in block`, `could not read block`, checksum failures, XX001/XX002 (`scripts/find_bad_rows.sql`) |
| `pitr-restore` | dropped/truncated table or database, data as of an earlier time, good copies for repairs (`scripts/pitr_clone.sh`, `scripts/pitr_cleanup.sh`) |
| `data-loss-verification` | after ANY data-affecting incident: run and read `ledger.py reconcile --json`; lost/corrupt/missing-table playbooks |
| `connection-storm` | `too many clients already`, reserved-slot errors (53300), idle-in-transaction pile-ups, lock queues |
| `disk-space-wal` | `No space left on device` (53100), pg_wal growth, archiver failures, slots retaining WAL |
| `wraparound-vacuum` | high xid/multixact age, `must be vacuumed within`, `not accepting commands ... wraparound` |
| `health-patrol` | periodic check without an incident (`scripts/run_checks.sh --log`) |
| `fortify-database` | day-0 hardening, no incident (`raiden fortify`): verified base backup, streaming replica (`pg-standby`), baseline config, new checks (`scripts/replica_status.sh`) |
| `preventive-protections` | evolve session: make the same attack BOUNCE - pin shared_preload_libraries, extension allowlist, protected tables, role limits, each with a dry-run proof (`scripts/prove*.sh`) |
| `hardening-after-incident` | PART A: report.md (with Key commands) + lessons (end of incident); PART B: evolve session - protection first, then new checks/skills/config |
| `postgres-best-practices` | second opinion from Microsoft's vendor-agnostic PostgreSQL references ([microsoft/postgres-skills](https://github.com/microsoft/postgres-skills), MIT, `references/` verbatim, router adapted for an autonomous agent): connections, extensions, query performance, replication, indexing |
| `learned-*` | skills written by rAIden itself after incidents (specific signature => fix) |

## Conventions
- Frontmatter: `name` == directory name (lowercase, hyphens); `description` = what + when, containing the
  literal error messages/SQLSTATEs the agent will see. Body 60-150 lines: steps, exact commands, expected
  output, pitfalls, verification, rollback.
- Connections: `psql -X -At -d postgres|ledger` (unix socket, peer auth as SUPERUSER role raiden), always
  bounded: `PGCONNECT_TIMEOUT=3`, `timeout N`. Shell state does not persist between tool calls.
- Logs: jsonlog `/var/log/postgresql/raiden-YYYYMMDD.json` (keys: timestamp, user, dbname, pid,
  error_severity, state_code, message, detail, hint, statement, backend_type, ...); read with
  `jq -R 'fromjson? | ...'` so a partially written last line is skipped. Startup failures:
  `/var/log/postgresql/postgresql-18-main.log` and `sudo -n journalctl -u postgresql@18-main`.
- Every state change is logged first in `$RAIDEN_INCIDENT_DIR/notes.md`; restarts are budgeted
  (6/hour) - prefer reload; config changes go to `/etc/postgresql/18/main/conf.d/20-raiden-<topic>.conf`
  or the culprit file in place after a backup copy to /var/lib/raiden/work/.
- Never: delete/move in PGDATA, pg_wal, the WAL archive (only `pg_archivecleanup`) or
  /var/backups/postgresql; pg_resetwal; DROP DATABASE/SCHEMA; TRUNCATE; DELETE/UPDATE without WHERE;
  durability weakening; killing the postmaster; editing /opt/raiden/raiden or /etc/raiden.
- Data-affecting incidents are resolved only when reconcile reports `ok: true`.
- Patrol checks: bash, last line `OK|WARN|CRIT <name>: <message>`, exit 0/1/2, < 15 s, DB down => WARN/CRIT.
