---
name: learned-replica-ops
description: "pg-standby failed, raiden_replica slot inactive, lag growing, or requested WAL segment has already been removed: confirm and safely reconnect or rebuild the local streaming replica."
---
# learned-replica-ops
## Signature
Patrol 85-replica WARN/CRIT; pg-standby failed; slot inactive; lag growing;
`requested WAL segment has already been removed` in standby logs.
A replica replays DROP TABLE and corruption: PITR from the base backups is the way back, not the replica.
## Confirm
Set `export PGCONNECT_TIMEOUT=3`; never restart the primary for a replica failure.
```bash
systemctl status pg-standby --no-pager
sudo -n journalctl -u pg-standby -n 40 --no-pager
psql -X -d postgres -c "select slot_name,active,wal_status from pg_replication_slots"
psql -X -d postgres -c "select state,pg_wal_lsn_diff(pg_current_wal_lsn(),replay_lsn) as lag_bytes from pg_stat_replication"
```
An inactive slot during the first 90 seconds of primary uptime may just be reconnecting.
## Fix
For transient failure, `sudo -n systemctl restart pg-standby`; wait and verify.
If WAL is gone, load fortify-database and rebuild its step 2, not production.
Confirm `/var/lib/raiden/work/replica` is the standby, and preserve its config before edits.
Stop `sudo -n systemctl stop pg-standby`; confirm slot inactive using SQL above.
Move the stopped directory to a unique timestamped path under /var/lib/raiden/work (never overwrite an existing path).
Drop ONLY its inactive slot: `psql -X -d postgres -c "select pg_drop_replication_slot('raiden_replica')"`.
```bash
pg_basebackup -h /var/run/postgresql -U raiden -D /var/lib/raiden/work/replica -Fp -X stream -c fast -R -C -S raiden_replica
psql -X -At -d postgres -c "select name||'='||setting from pg_settings where name in ('max_connections','max_worker_processes','max_wal_senders','max_prepared_transactions','max_locks_per_transaction')"
```
Use write_file: postgresql.conf = one-line comment, pg_hba.conf = `local all all peer`, pg_ident.conf = one-line comment.
Preserve the generated postgresql.auto.conf before appending these overrides using edit_file:
```conf
port = 5434
listen_addresses = ''
unix_socket_directories = '/tmp'
hot_standby = on
shared_preload_libraries = ''
archive_mode = off
logging_collector = off
log_destination = 'stderr'
shared_buffers = '256MB'
cluster_name = 'raiden-replica'
```
Append the five capacity settings with values >= those read from primary (defaults at origin: 100,8,10,0,64).
Do not change primary preload, durability, or any existing primary protection.
`sudo -n systemctl start pg-standby`
`timeout 90 bash -c 'until pg_isready -q -h /tmp -p 5434 -t 2; do sleep 1; done'`
Escalate if permissions, disk space, authentication or protections block safe repair.
## Verify
```bash
/opt/raiden/.claude/skills/fortify-database/scripts/replica_status.sh
psql -X -At -h /tmp -p 5434 -d ledger -c "select pg_is_in_recovery(), count(*) from public.entries"
/opt/raiden/checks.d/85-replica.sh
/opt/raiden/checks.d/86-baseline.sh
```
Require active slot, state=streaming, near-zero lag and recovery=t with readable rows.
If production data may have been affected, run `/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json`.
## Origin
Fortify fort-20261021T004657, 2026-10-21. Standby is same-host asynchronous redundancy, not HA or backup.
