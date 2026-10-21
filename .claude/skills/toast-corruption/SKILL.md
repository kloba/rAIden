---
name: toast-corruption
description: Diagnose and repair data corruption in user tables - "missing chunk number 0 for toast value N in pg_toast_NNN", "unexpected chunk number", "invalid page in block", "could not read block", "page verification failed, calculated checksum", SQLSTATE XX001/XX002/XX000, checksum_failures > 0, reconcile corrupt/unreadable > 0. Finds every damaged row (amcheck + row scanner), restores good values from a PITR clone with keyed UPDATEs, never deletes rows.
---

# toast-corruption - find damaged rows, repair from a known-good copy

Rules: never DELETE damaged rows, never `zero_damaged_pages`/`ignore_checksum_failure`, no VACUUM FULL /
CLUSTER / pg_repack / REINDEX on the damaged table before the bad-row list is saved. Log actions in notes.md.

## 1. Evidence: what, where, since when
```bash
jq -Rr 'fromjson? | select(((.state_code//"")|test("^XX00")) or (.message|test("chunk number|invalid page|could not read block|page verification failed|checksum"))) | "\(.timestamp) \(.dbname) \(.state_code) \(.message) | \(.statement//""|.[0:80])"' $(ls -1tr /var/log/postgresql/raiden-*.json) | head -5
psql -X -At -d postgres -c "SELECT datname, checksum_failures, checksum_last_failure FROM pg_stat_database WHERE datname IS NOT NULL; SHOW data_checksums"
```
The FIRST error timestamp is an upper bound for the corruption time. Map the relation:
```bash
psql -X -At -d ledger -c "SELECT n.nspname||'.'||c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.reltoastrelid = 'pg_toast.pg_toast_NNN'::regclass"
psql -X -At -d ledger -c "SELECT pg_filenode_relation(0, <filenode>)::regclass"   # for 'block B of relation base/<dboid>/<filenode>'
```

## 2. Scope: list every damaged row (save the lists in the incident dir)
```bash
psql -X -At -d ledger -c "CREATE EXTENSION IF NOT EXISTS amcheck"
psql -X -At -d ledger -c "SELECT blkno, offnum, attnum, msg FROM verify_heapam('public.entries', check_toast => true) LIMIT 50"
psql -X -At -d ledger -c "SELECT id FROM public.entries WHERE ctid = ANY (ARRAY(SELECT format('(%s,%s)', blkno, offnum)::tid FROM verify_heapam('public.entries', check_toast => true)))"
pg_amcheck -d ledger --heapallindexed --install-missing --progress 2>&1 | tail -20     # whole DB incl. indexes and TOAST
psql -X -q -d ledger -v ON_ERROR_STOP=1 -f /opt/raiden/.claude/skills/toast-corruption/scripts/find_bad_rows.sql > "$RAIDEN_INCIDENT_DIR/bad_rows.csv"
```
find_bad_rows.sql fully detoasts every payload (md5) inside exception blocks, probing 200 heap blocks at a
time (`-v blocks=N`); output CSV `id,ctid,err`. A row with empty id = whole heap block
unreadable (keys must then come from the clone: same ctid block there, PITR is physical).

## 3. Source of good data: a PITR clone (skill pitr-restore, never production)
- Target = first error timestamp minus a margin (e.g. 2 min). Physical, non-WAL-logged damage (checksum
  failures, I/O errors) is NOT replayed from WAL, so even a later target yields good data; logical damage
  (rows deleted from pg_toast via SQL) IS replayed, so the target must precede it.
- `/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_clone.sh --target-time '<ts UTC>'` -> prints SOCKET_DIR.
- In the clone run the same scanner and confirm every bad key exists and is readable there:
  `psql -X -q -h "$SOCK" -p 5433 -d ledger -f .../find_bad_rows.sql` must return only the header line.
  Keys missing in the clone (inserted after the target) need a later target or path (c).

## 4. Repair in production (keyed UPDATE writes a new tuple + new TOAST chunks)
Export from the clone (`SOCK` = socket dir printed by pitr_clone.sh):
```bash
psql -X -h "$SOCK" -p 5433 -d ledger -v ON_ERROR_STOP=1 <<EOF
CREATE TEMP TABLE bad (id bigint, row_ctid tid, err text);
\copy bad FROM '$RAIDEN_INCIDENT_DIR/bad_rows.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT l.id, l.payload FROM public.entries l JOIN bad b USING (id)) TO '/var/lib/raiden/work/good_rows.csv' WITH (FORMAT csv)
EOF
wc -l /var/lib/raiden/work/good_rows.csv     # must equal the number of bad keys
```
Apply in ONE transaction, commit only if every key was updated:
```bash
psql -X -d ledger -v ON_ERROR_STOP=1 <<'EOF'
BEGIN;
CREATE TEMP TABLE good (id bigint, payload text) ON COMMIT DROP;
\copy good FROM '/var/lib/raiden/work/good_rows.csv' WITH (FORMAT csv)
WITH u AS (UPDATE public.entries l SET payload = g.payload FROM good g WHERE l.id = g.id RETURNING 1) SELECT count(*) AS updated FROM u \gset
SELECT :updated = (SELECT count(*) FROM good) AND :updated > 0 AS all_ok \gset
\echo updated :updated all_ok :all_ok
\if :all_ok
COMMIT;
\else
ROLLBACK;
\endif
EOF
```
Unreadable heap blocks: get the keys in that block from the clone
(`SELECT id FROM public.entries WHERE ctid >= '(B,0)' AND ctid < '(B+1,0)'`) and escalate - do not
rewrite pages yourself.

## 5. Verify
- scanner again in production: only the header line; `verify_heapam(..., check_toast => true)` returns 0 rows;
  `pg_amcheck -d ledger --heapallindexed` clean.
- reconcile oracle (skill data-loss-verification): `ok: true`, corrupt = 0, unreadable = 0.
- Stop and remove the clone with pitr_cleanup.sh; keep bad_rows.csv in the incident dir.

## 6. No good copy exists
Quarantine, do not drop: copy the readable columns and the error text to `public.entries_quarantine`
(`CREATE TABLE public.entries_quarantine AS SELECT id, account, amount, payload_md5, created_at, now() AS quarantined_at FROM public.entries WHERE id IN (...)`),
leave the original rows in place, write `/var/lib/raiden/escalations/$RAIDEN_INCIDENT_ID.md` with the key list and evidence.

## 7. Root cause (put in report)
- checksum_failures > 0, `page verification failed`, kernel I/O errors
  (`sudo -n journalctl -k | grep -iE 'i/o error|ext4-fs error'`) => physical/storage damage.
- `missing chunk` with data_checksums on and zero checksum failures => chunks removed logically
  (WAL-logged DML on a pg_toast table, or a bug); search the log for `pg_toast` statements and superuser sessions.
- recent crash cycles (crash-loop-rca) around the first error; when the victims were inserted (created_at).
