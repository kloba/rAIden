---
name: data-loss-verification
description: Run and interpret the authoritative application-data oracle (ledger.py reconcile --json) after ANY incident that may have touched data - crash recovery, PITR/restore, corruption repair, DROP/TRUNCATE, disk-full write failures. Explains acked/present/lost/corrupt/unreadable/unrecorded_rows_in_db/error/ok and the playbooks for lost > 0, corrupt/unreadable > 0 and 'table entries does not exist'.
---

# data-loss-verification - did we lose or damage any acknowledged commit?

The oracle compares the client-side ledger of ACKNOWLEDGED commits with the database. It is strictly
read-only and is the success criterion: an incident that may have touched data is not resolved until
`ok: true`.

## 1. Run it on a stable database
The oracle needs a stable server: wait for readiness, then 30 s without crash/recovery messages.
```bash
timeout 180 bash -c 'until pg_isready -q -t 2; do sleep 1; done' && sleep 30 && \
psql -X -At -d postgres -c "SELECT pg_is_in_recovery()"        # must be f
jq -Rr --arg s "$(date -u -d '-40 sec' '+%F %T')" 'fromjson? | select(.timestamp >= $s and (.message|test("terminated by signal|all server processes terminated|automatic recovery"))) | .message' $(ls -1tr /var/log/postgresql/raiden-*.json | tail -1) | wc -l   # must be 0
/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json > "$RAIDEN_INCIDENT_DIR/reconcile-$(date -u +%H%M%S).json"; echo rc=$?
jq . "$(ls -1t "$RAIDEN_INCIDENT_DIR"/reconcile-*.json | head -1)"
```
If it errors or times out because the DB flapped: wait until stable 30 s and retry ONCE. Still failing =>
cannot verify => final status at best `mitigated`, explain why.

## 2. Read the fields
| Field | Meaning | Good value |
|---|---|---|
| `acked` | commits the clients got a success ACK for | any |
| `present` | acked rows present in public.entries | = acked |
| `lost` | acked rows that are missing | **0** |
| `corrupt` | rows present but content differs from what was acked | **0** |
| `unreadable` | rows present but reading them raises an error (e.g. TOAST damage) | **0** |
| `unrecorded_rows_in_db` | rows never acked: commit outcome unknown to the client (connection died around COMMIT): present or absent are BOTH fine | any |
| `lost_sample` | sample of lost keys (id) | empty |
| `error` | `table entries does not exist`, `database unavailable: ...` or `query failed: ...` (no counts then) | absent |
| `ok` | true only if lost = corrupt = unreadable = 0 and there is no `error` | **true** |
Unlisted fields: report them verbatim. Rows present but never acked are fine (in-flight commits).

## 3. Playbooks
**error: table entries does not exist** -> skill pitr-restore (find the DROP, xid target, restore through a clone).

**corrupt > 0 or unreadable > 0** -> skill toast-corruption (bad-row list, repair from a PITR clone).

**lost > 0**
1. Localise in time: for some keys from `lost_sample`, take the ack time `t` (epoch) from the client ledger
   (`grep -F '"id":<ID>,' /var/lib/arena/ledger.jsonl`) and look at neighbours that exist:
   `psql -X -d ledger -c "SELECT id, created_at FROM public.entries WHERE created_at BETWEEN to_timestamp(<T>-5) AND to_timestamp(<T>+5) ORDER BY created_at"`
   The created_at of the rows around the gap gives the loss window.
2. Explain the window - typical causes:
   - a restore whose target was too early (time target taken from the DROP statement START): redo with
     `--target-xid` (exclusive) and copy only the missing keys;
   - rows written after the clone target but before the table vanished: same fix;
   - crash recovery in the window with durability intact should lose nothing acked: check
     `SELECT name, setting, sourcefile FROM pg_settings WHERE name IN ('fsync','synchronous_commit','full_page_writes','wal_sync_method')`
     - anything weakened is a critical finding (restore safe values only via the documented config path, escalate);
   - WAL archive gap (clone could not replay): check continuity
     ```bash
     sudo -n -u postgres ls /var/lib/postgresql/wal_archive | grep -E '^[0-9A-F]{24}$' | sort | while read -r f; do n=$(( 16#${f:8:8} * 256 + 16#${f:16:8} )); if [ -n "${p:-}" ] && [ "${f:0:8}" = "${p:0:8}" ] && [ "$n" -ne $((pn + 1)) ]; then echo "GAP: $p -> $f"; fi; p=$f; pn=$n; done
     ```
3. Recover the missing keys from a PITR clone (target just before the destructive event, or latest
   for physical damage): export them with `\copy (SELECT * FROM public.entries WHERE id IN (...)) TO '/var/lib/raiden/work/missing.csv' WITH (FORMAT csv)`,
   then in production inside one transaction:
   `CREATE TEMP TABLE m (LIKE public.entries); \copy m FROM '/var/lib/raiden/work/missing.csv' WITH (FORMAT csv)`
   and `INSERT INTO public.entries SELECT * FROM m ON CONFLICT (id) DO NOTHING;`
4. Re-run the oracle. If keys cannot be found in any clone: escalate with the key list and window
   (`/var/lib/raiden/escalations/$RAIDEN_INCIDENT_ID.md`).

## 4. Report
Put the final JSON (or its key numbers) in report.md `## Verification`, plus how many attempts were
needed. Never write `RESOLUTION: resolved` for a data-affecting incident without `ok: true`.
