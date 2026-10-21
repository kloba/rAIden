# Fortify fort-20261021T004657 - day-0 hardening
## Findings before
Healthy PostgreSQL 18.6 primary with normal application traffic, no crash evidence or checksum failures. Zero base backups, one archived WAL file, no replication slots, and pg-standby inactive. WAL archiving on and wal_level=replica; ALTER SYSTEM enabled. Existing conf.d files were 10-raiden-baseline.conf and 20-archive-atomic.conf; primary preload was pg_stat_statements. Memory protections/evolution/lessons files did not yet exist.

## Key commands
```text
sudo -n -u postgres ls -1 /var/backups/postgresql/base/; sudo -n -u postgres ls /var/lib/postgresql/wal_archive | wc -l
sudo -n -u postgres pg_basebackup -D /var/backups/postgresql/base/20261021T004705 -Fp -X stream -c fast
sudo -n -u postgres pg_verifybackup /var/backups/postgresql/base/20261021T004705
pg_basebackup -h /var/run/postgresql -U raiden -D /var/lib/raiden/work/replica -Fp -X stream -c fast -R -C -S raiden_replica
cp /var/lib/raiden/work/replica/postgresql.auto.conf /var/lib/raiden/work/replica-auto.conf.before-fortify
sudo -n systemctl start pg-standby
printf '%s\n' ... 'allow_alter_system = off' "max_slot_wal_keep_size = '4GB'" "idle_in_transaction_session_timeout = '5min'" | sudo -n -u postgres tee /etc/postgresql/18/main/conf.d/98-raiden-baseline.conf
sudo -n systemctl reload postgresql@18-main
psql -X -d postgres -c "alter system set work_mem = '4MB'"
chmod 755 /opt/raiden/checks.d/85-replica.sh /opt/raiden/checks.d/86-baseline.sh
/opt/raiden/.claude/skills/health-patrol/scripts/run_checks.sh
/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json
```
New files and skill were written with write_file; standby auto.conf overrides were appended with edit_file after saving a copy. Five standby capacity settings explicitly match primary requirements (100, 8, 10, 0, 64). No existing primary configuration file was overwritten.

## Verification
- Backup: `backup successfully verified`; one complete backup at `/var/backups/postgresql/base/20261021T004705`. Archive patrol: archived=79, failed=0, ready=0.
- Replica: unit active, slot raiden_replica active, state=streaming, sync_state=async. Dedicated SQL sampling obtained `ZERO_LAG_PROOF: t|streaming|0`; both standalone and full-patrol checks also observed zero-byte lag. Small transient lag during writes is normal, not a synchronous durability guarantee.
- Standby: recovery=t and public.entries readable (2975 rows at initial verification).
- Baseline live values: allow_alter_system=off, max_slot_wal_keep_size=4096 MB, idle_in_transaction_session_timeout=300000 ms; all sourced from 98-raiden-baseline.conf. pg_file_settings errors=0. Harmless ALTER SYSTEM SET work_mem rejected: `ALTER SYSTEM is not allowed in this environment`.
- Checks: bash syntax passed. Healthy runs: replica rc=0 in 0.013s, baseline rc=0 in 0.038s. Missing-socket tests promptly returned WARN for each. Learned skill frontmatter and required sections validated.
- Full patrol: all 11 checks rc=0, `WORST: OK`, including:
  - `OK replica: slot active, streaming, replay lag=0 bytes`
  - `OK baseline: ALTER SYSTEM off, 4GB slot cap, 5min idle timeout, config clean, all 1 backups younger than 7 days`
  - `OK protections: 1 protection(s) verified (baseline-config)`
- Application read-only oracle: ok=true, acked=5299, present=5299, lost=0, corrupt=0, unreadable=0, unrecorded_rows_in_db=5 during live traffic.
- Primary final probe ready=true; postmaster start unchanged at 2026-10-21T00:45:54.446158Z, restart budget unchanged at 6. fsync/full_page_writes/synchronous_commit remain on; shared_preload_libraries remains pg_stat_statements. No primary restart or application data writes.

## New artifacts
- `/opt/raiden/checks.d/85-replica.sh` — active slot/streaming/64MB lag check, startup grace and bounded connection failures.
- `/opt/raiden/checks.d/86-baseline.sh` — live and applied baseline, config errors, every backup timestamp younger than seven days.
- `/opt/raiden/.claude/skills/learned-replica-ops/SKILL.md` — signatures, confirmation, standby-only restart/rebuild, verification, PITR reminder.
- `/var/lib/raiden/memory/protections.list` — baseline-config, replica and backup recorded.
- `/var/lib/raiden/memory/evolution.md` — implementation and validation evidence.

## Follow-ups
Move the standby to another host/failure domain; this same-host asynchronous replica is neither backup nor host-loss protection. Schedule recurring verified base backups and a side-clone PITR restore drill. Copy base backups and continuous WAL off-site with monitored retention. Define retention separately: the requested check warns if ANY base backup exceeds seven days; no backups were deleted. The 4GB slot cap is checkpoint-enforced and can invalidate an abandoned replica slot, requiring a rebuild; it is not an instantaneous total disk quota.

RESOLUTION: resolved
SUMMARY: Verified PITR base, zero-lag streaming replica, reloadable baseline and tested patrol/runbook protections installed without primary restart or application writes.
