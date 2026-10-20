---
name: disk-space-wal
description: 'Disk full, WAL growth and archiver failures - "No space left on device", SQLSTATE 53100, "PANIC: could not write to file pg_wal/...", "archive command failed with exit code", "archiving write-ahead log file ... failed too many times", pg_wal or the WAL archive growing, inactive replication slots retaining WAL, runaway temp files. Says what is safe to free and how, never touching pg_wal or PGDATA.'
---

# disk-space-wal - free space safely, fix WAL retention

Never delete or move anything in PGDATA (including pg_wal), the WAL archive (except via
pg_archivecleanup) or /var/backups/postgresql. Log every deletion/compression in notes.md.

## 1. Where is the space?
```bash
df -h / /var/lib/postgresql /var/lib/postgresql/wal_archive /var/backups/postgresql /var/log/postgresql /var/lib/raiden/work; df -i /var/lib/postgresql
sudo -n -u postgres du -sh /var/lib/postgresql/18/main/pg_wal /var/lib/postgresql/18/main/base /var/lib/postgresql/wal_archive /var/backups/postgresql/base
du -sh /var/log/postgresql /var/lib/raiden/work 2>/dev/null
sudo -n -u postgres du -sh /var/backups/postgresql/base/* | sort -k2
psql -X -At -d postgres -c "SELECT count(*), pg_size_pretty(sum(size)) FROM pg_ls_waldir()"
psql -X -At -d postgres -c "SELECT count(*) FILTER (WHERE name LIKE '%.ready') AS not_yet_archived FROM pg_ls_archive_statusdir()"
psql -X -At -d postgres -c "SELECT * FROM pg_stat_archiver"
psql -X -At -d postgres -c "SELECT slot_name, slot_type, active, wal_status, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained FROM pg_replication_slots"
psql -X -At -d postgres -c "SELECT name, setting, unit FROM pg_settings WHERE name IN ('max_wal_size','wal_keep_size','max_slot_wal_keep_size','archive_command','archive_library','temp_file_limit')"
psql -X -At -d postgres -c "SELECT datname, temp_files, pg_size_pretty(temp_bytes) FROM pg_stat_database WHERE temp_bytes > 0"
```
If the server is down (PANIC on WAL write), use only the OS commands, free space (step 3), then
`sudo -n systemctl start postgresql@18-main` and let crash recovery run.

## 2. Why is pg_wal growing? (WAL is kept until archived AND not needed by slots/wal_keep_size)
- **Archiver failing** (`failed_count` rising, `last_failed_time` > `last_archived_time`, many `.ready`):
  ```bash
  jq -Rr 'fromjson? | select(.message|test("archive command failed|failed too many times|archive_command")) | "\(.timestamp) \(.message) | \(.detail//"")"' $(ls -1tr /var/log/postgresql/raiden-*.json | tail -1) | tail -5
  sudo -n -u postgres test -w /var/lib/postgresql/wal_archive && echo archive-writable
  ```
  Typical causes: archive filesystem full, wrong permissions/owner on the archive dir, a changed
  archive_command (check `pg_settings.sourcefile`), a file already existing with different content.
  Fix the cause (free space, restore the archive_command from its source file backup + reload), then
  watch `last_archived_wal` advance; run `CHECKPOINT;` afterwards so old segments are recycled.
- **Inactive replication slot** (`active = f`, large `retained`): this lab has no replicas. Record
  `SELECT * FROM pg_replication_slots` in notes, then for an unknown inactive slot
  `SELECT pg_drop_replication_slot('<name>');` (a known consumer's slot: escalate instead; a softer cap is
  `ALTER SYSTEM SET max_slot_wal_keep_size = '20GB'; SELECT pg_reload_conf();`).
- **wal_keep_size / max_wal_size set huge**: find the source file in pg_settings, revert it (reload).
- **Temp files** (temp_bytes, `pgsql_tmp`): terminate the runaway query (connection-storm skill rules);
  consider `ALTER ROLE arena SET temp_file_limit = '5GB'`.

## 3. What may be freed (in this order)
1. The agent's own leftovers: old PITR clones
   `/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_cleanup.sh /var/lib/raiden/work/pitr-<stamp>`, old dumps in /var/lib/raiden/work.
2. Old jsonlog files (not today's, not the incident window): compress, don't delete:
   `sudo -n -u postgres gzip /var/log/postgresql/raiden-YYYYMMDD.json`
3. Archived WAL older than the start segment of the OLDEST base backup still present (nothing can
   restore from older WAL):
   ```bash
   OLDEST=$(sudo -n -u postgres ls -1 /var/backups/postgresql/base | sort | head -1)
   SEG=$(sudo -n -u postgres sed -n 's/^START WAL LOCATION: .*(file \([0-9A-F]\{24\}\)).*/\1/p' "/var/backups/postgresql/base/$OLDEST/backup_label"); echo "$OLDEST $SEG"
   sudo -n -u postgres pg_archivecleanup -n /var/lib/postgresql/wal_archive "$SEG" | wc -l    # dry run: files that would go
   sudo -n -u postgres pg_archivecleanup /var/lib/postgresql/wal_archive "$SEG"
   ```
4. Old base backups beyond the 2 newest are candidates, but deletion under /var/backups/postgresql
   is blocked for the agent: list them with sizes in the report/escalation for a human.
Never: rm in pg_wal or PGDATA, pg_resetwal, archive_command='true'/'/bin/true' (breaks PITR), lowering
durability settings.

## 4. Verify
- `df -h` back under 80% with headroom; `pg_ls_waldir()` shrinking after the archiver catches up and a
  checkpoint; `pg_stat_archiver.failed_count` stable and `last_archived_time` recent;
- no new 53100 / `No space left` lines in the log for 5 minutes;
- if writes failed or the server PANICked: run the reconcile oracle (skill data-loss-verification).

## Escalate when
Space cannot be freed without deleting backups or production data, the archive filesystem itself is
broken, or WAL keeps growing after the archiver and slots are healthy.
