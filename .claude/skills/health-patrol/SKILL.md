---
name: health-patrol
description: 'Periodic 24/7 health patrol when there is no incident - runs every check in /opt/raiden/checks.d (each prints "OK|WARN|CRIT <name>: <message>" last and exits 0/1/2), interprets WARN/CRIT, picks the matching skill, logs the worst status to /var/lib/raiden/memory/patrol.log and reports a one-paragraph status. Use for "patrol", "health check", "routine check".'
---

# health-patrol - cheap periodic check (< 2 minutes, < ~15 tool calls)
Patrol runs every 30 minutes and also immediately after a configuration reload shows up in the PostgreSQL log.

## 1. Run all checks (parallel, each bounded to 20 s) and log the result
```bash
/opt/raiden/.claude/skills/health-patrol/scripts/run_checks.sh --log
```
Output: one line per check `rc=<0|1|2|3> <file>: <verdict line>`, then `WORST: OK|WARN|CRIT`.
rc 3 = check timed out, crashed or printed no verdict (treated as WARN; fix or report the check).
`--log` appends `<UTC ts> worst=<status> ok=N warn=N crit=N [<names of non-OK checks>]`
to /var/lib/raiden/memory/patrol.log. Without the script:
`for c in /opt/raiden/checks.d/*; do [ -f "$c" ] && [ -x "$c" ] || continue; o=$(timeout 20 "$c" 2>&1); echo "rc=$? $(basename "$c"): $(tail -n1 <<<"$o")"; done`

## 2. Compare with recent history (spot flapping / trends)
`tail -n 12 /var/lib/raiden/memory/patrol.log` and, if it exists, `tail -n 20 /var/lib/raiden/memory/lessons.md`
(known signatures and fixes from earlier incidents).

## 3. Interpret WARN / CRIT
| Check | Skill / action |
|---|---|
| 10-liveness | CRIT = server down, not accepting connections or in recovery: incident -> pg-triage |
| 20-wraparound | wraparound-vacuum (find the xmin blocker first) |
| 30-disk | disk-space-wal |
| 40-archiver | disk-space-wal, section "Archiver failing" (PITR depends on it) |
| 50-backup-freshness | see below |
| 60-sessions | connection-storm |
| 70-integrity | checksum failures: toast-corruption (CRIT); invalid index: `REINDEX INDEX CONCURRENTLY <idx>` if no `pg_stat_progress_create_index` row is building it |
| 80-crash-recovery | crash-loop-rca |
| other (learned, 85-/90-...) | read the check's header comment: it names its skill |

Backup freshness: if the newest base backup is too old and `df` shows free space >= 2x the database size
(`psql -X -At -d postgres -c "SELECT pg_size_pretty(sum(pg_database_size(oid))) FROM pg_database"`),
take one: `sudo -n -u postgres pg_basebackup -D /var/backups/postgresql/base/$(date -u +%Y%m%dT%H%M%S) -X stream -c fast -P`
(new directory only, never touch existing backups); otherwise report it.

Patrol fixes are limited to single-step, reversible actions covered by a skill (e.g. terminate one
idle-in-transaction session, reload a reverted setting, REINDEX CONCURRENTLY). Anything that needs a
restart, a restore, a repair or more than a few steps: do not start it inside the patrol - state
clearly that an incident is required and which skill applies. Never restart during a patrol.

## 4. Output (final answer)
One paragraph: worst status, each non-OK check with its message, what you did (or why nothing),
and the recommended next step; e.g.
`Patrol 2026-10-20T12:00Z: WARN. 60-sessions: 3 idle-in-transaction > 5 min (oldest 7m, app arena) -
terminated pid 4242 per connection-storm; others OK (xid age 12M, disk 41%, archiver ok, backup 5h old).`
If a check itself is broken (rc 3 or no verdict), say so - a silent check is worse than a red one.
