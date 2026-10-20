---
name: pitr-restore
description: Point-in-time recovery into a SIDE CLONE (never over production) to recover a dropped, truncated or damaged table/database or to read data as of an earlier time - "relation \"ledger\" does not exist" (42P01), DROP TABLE / DROP DATABASE / TRUNCATE in the log, reconcile table_missing or lost > 0, good copies for corruption repair. Finds the destructive transaction's xid with pg_waldump, builds the clone with scripts/pitr_clone.sh, copies data back with pg_dump/pg_restore.
---

# pitr-restore - zero-loss recovery through a side clone

Never stop, overwrite or restore over the production cluster. Clones live only under /var/lib/raiden/work/.
Log every step in `$RAIDEN_INCIDENT_DIR/notes.md`.

## 1. When and who
```bash
jq -Rr 'fromjson? | select(.message|test("^statement: *(DROP|ALTER|TRUNCATE)";"i")) | "\(.timestamp) \(.user//"-")@\(.dbname//"-") pid=\(.pid) | \(.message|.[0:150])"' $(ls -1tr /var/log/postgresql/raiden-*.json)
jq -Rr 'fromjson? | select(.state_code=="42P01") | .timestamp' $(ls -1tr /var/log/postgresql/raiden-*.json) | head -1   # first app error = upper bound
```
log_statement='ddl' logs DROP/ALTER with user+time, NOT TRUNCATE/DELETE (class 'mod'): for those use the
first errors / row-count drop plus pg_waldump. The statement timestamp is when the statement STARTED.

## 2. Pick the recovery target (least data loss)
- **xid target (preferred):** stop right BEFORE the destructive transaction commits
  (`--target-xid X`, exclusive): every commit before it is kept.
- **time target:** `recovery_target_time` stops at the first commit with timestamp > T (inclusive) or
  >= T (`recovery_target_inclusive=false`). Using the DROP statement START time drops commits that landed
  between statement start and the DROP's commit - use only when no xid can be found, or for corruption
  (target = first error minus a margin).

Find the xid of the DROP/TRUNCATE commit:
```bash
psql -X -At -d postgres -c "SELECT pg_walfile_name(pg_switch_wal())"            # force the current segment into the archive
psql -X -At -d postgres -c "SELECT last_archived_wal, last_failed_wal FROM pg_stat_archiver"   # wait until >= that segment
psql -X -At -d postgres -c "SELECT oid FROM pg_database WHERE datname='ledger'"    # DBOID
sudo -n -u postgres find /var/lib/postgresql/wal_archive -maxdepth 1 -regex '.*/[0-9A-F]\{24\}' -newermt '2026-10-20 11:59:00 UTC' | sort | head -4   # DROP time minus 2 min; FIRST..LAST (file mtime = archiving time)
sudo -n -u postgres pg_waldump -p /var/lib/postgresql/wal_archive -r Transaction <FIRST> <LAST> 2>/dev/null | grep -E 'desc: COMMIT .*rels:' | grep "base/<DBOID>/"
```
Line: `tx: 81234, lsn: 0/5A0012C8, ... desc: COMMIT 2026-10-20 12:01:02.345 UTC; rels: base/16388/16390 base/16388/16393 ...`
The DROP's commit is the first such COMMIT at/after the statement time whose `rels:` lists files of the
ledger DB (table, TOAST table, TOAST index, PK index). Confirm, if needed, in a clone taken just before:
`SELECT pg_relation_filenode('public.ledger')` must appear in `rels:`. ("end of WAL"/"invalid record" errors at the end of pg_waldump output are normal.)

## 3. Build the clone (copies the newest base backup older than the target, replays the archive)
```bash
/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_clone.sh --target-xid 81234 --before '<DROP time UTC>'
/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_clone.sh --target-time '2026-10-20 11:58:00 UTC'
#   options: [--inclusive] [--backup DIR] [--port 5433] [--timeout 540]
```
Prints `CLONE_DIR=`, `SOCKET_DIR=`, `PORT=`, the stop line, and exits 0 once promoted. Exit 3 = still
replaying after --timeout: poll `pg_isready -h <SOCKET_DIR> -p 5433` and `sudo -n -u postgres tail <CLONE_DIR>/clone.log`.
Expect `recovery stopping before commit of transaction 81234`, `archive recovery complete`. The clone has
no archiving, no preload libraries, unix socket only. Budget: copy ~ PGDATA size, replay usually 1-10 min.

## 4. Inspect, then copy back
```bash
S=<SOCKET_DIR>; psql -X -h "$S" -p 5433 -d ledger -c "SELECT count(*), max(created_at) FROM public.ledger" -c '\dp public.ledger' -c '\dt+ public.ledger'
psql -X -At -d ledger -c "SELECT to_regclass('public.ledger')"                    # does the name exist in production now?
```
A. Table missing in production:
```bash
pg_dump -h "$S" -p 5433 -d ledger -t public.ledger --no-owner -Fc -f /var/lib/raiden/work/ledger-pitr.dump
pg_restore -l /var/lib/raiden/work/ledger-pitr.dump                                 # TOC: TABLE, TABLE DATA, CONSTRAINT, INDEX, ACL
pg_restore -d ledger --no-owner -j1 --exit-on-error /var/lib/raiden/work/ledger-pitr.dump
psql -X -d ledger -c "ALTER TABLE public.ledger OWNER TO arena" -c '\dp public.ledger'   # ACL must match the clone's \dp
```
B. Name exists again (app recreated it, or TRUNCATE): never overwrite. In the CLONE:
`ALTER TABLE public.ledger RENAME TO ledger_pitr; ALTER INDEX public.ledger_pkey RENAME TO ledger_pitr_pkey;`
then dump/restore `-t public.ledger_pitr` as in A and merge in production:
`INSERT INTO public.ledger (worker, seq, account, amount, payload, created_at) SELECT worker, seq, account, amount, payload, created_at FROM public.ledger_pitr ON CONFLICT (worker, seq) DO NOTHING;`
Keep ledger_pitr (list it in Follow-ups for humans).
C. Whole database dropped: `pg_dump -h "$S" -p 5433 -d ledger -Fc -f /var/lib/raiden/work/ledger-db.dump`;
`psql -X -d postgres -c "CREATE DATABASE ledger OWNER arena"`; `pg_restore -d ledger --exit-on-error /var/lib/raiden/work/ledger-db.dump` (keeps owners).

## 5. Verify, clean up
- Row count in production >= clone count; `\d public.ledger` shows PK and indexes; owner arena; grants equal.
- reconcile (skill data-loss-verification): `table_missing` false, `lost` = 0, `ok` true.
- `/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_cleanup.sh <CLONE_DIR>` (fast stop + delete clone);
  keep the .dump until the report is written, then it may be deleted from /var/lib/raiden/work.

## Pitfalls
- `recovery ended before configured recovery target was reached` (FATAL, clone stops): target xid/time is
  not in the archived WAL - switch WAL and wait for archiving, check the archive for gaps
  (`ls` the segment sequence), or the chosen backup started AFTER the target (use an older --backup).
- Base backups are taken with `-X stream` and contain backup_label; a copy without backup_label is not a
  valid backup. A stale postmaster.pid in the copy is removed by the script.
- Debian keeps postgresql.conf/pg_hba.conf in /etc, not in PGDATA: the script writes minimal ones into the clone.
- postgresql.auto.conf from production is copied too (may contain bad settings); the script appends
  overrides at its end (last entry wins) including `shared_preload_libraries=''`.
- `recovery_target_timeline` default `latest` follows the newest timeline in the archive; normally right.
- Clone dir must be writable by OS user postgres (script makes it group postgres, mode 2770).
- Never restore by stopping production and replacing PGDATA - that is not allowed here; escalate if needed.
