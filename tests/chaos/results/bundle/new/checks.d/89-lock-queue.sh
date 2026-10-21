#!/usr/bin/env bash
# P5 drift and early lock-chain detector, inc-20261021T011631-7c80.
# On WARN/CRIT use learned-lock-queue. Read-only, timeout 10s; never kills sessions.
set -euo pipefail
NAME=lock-queue
export PGCONNECT_TIMEOUT=3
if ! r="$(timeout 10 psql -X -At -v ON_ERROR_STOP=1 -d ledger -c "
WITH roles AS (SELECT * FROM pg_roles WHERE rolname IN ('analyst','migrator')),
cfg AS (SELECT rolname,split_part(v,'=',1) k,split_part(v,'=',2) v FROM roles,unnest(rolconfig) v),
a AS (SELECT *,cardinality(pg_blocking_pids(pid)) AS blockers FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend')
SELECT
 (SELECT count(*)=2 AND bool_and(NOT rolsuper AND rolcanlogin) FROM roles),
 coalesce((SELECT v::interval=interval '3s' FROM cfg WHERE rolname='migrator' AND k='lock_timeout'),false),
 coalesce((SELECT v::interval=interval '15s' FROM cfg WHERE rolname='analyst' AND k='idle_in_transaction_session_timeout'),false),
 (SELECT count(*) FROM pg_db_role_setting s,unnest(s.setconfig) v WHERE s.setdatabase<>0 AND (s.setrole=0 OR s.setrole IN (SELECT oid FROM roles)) AND split_part(v,'=',1) IN ('lock_timeout','idle_in_transaction_session_timeout')),
 (SELECT count(*) FROM a WHERE usename='arena' AND blockers>0),
 (SELECT count(*) FROM a WHERE usename='migrator' AND wait_event_type='Lock'),
 coalesce((SELECT max(extract(epoch FROM now()-state_change))::int FROM a WHERE usename='analyst' AND state IN ('idle in transaction','idle in transaction (aborted)')),0),
 (SELECT count(*) FROM pg_locks l JOIN a ON a.pid=l.pid WHERE l.locktype='relation' AND l.mode='AccessExclusiveLock' AND NOT l.granted),
 coalesce((SELECT max(extract(epoch FROM now()-query_start))::int FROM a WHERE usename='migrator' AND wait_event_type='Lock'),0);" 2>&1)"; then
  echo "WARN $NAME: database query failed (${r//$'\n'/ })"; exit 1
fi
IFS='|' read -r role_ok lock_ok idle_ok overrides blocked waiting idle_s exclusive wait_s <<<"$r"
if [[ ! "$overrides|$blocked|$waiting|$idle_s|$exclusive|$wait_s" =~ ^[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+$ ]]; then
  echo "WARN $NAME: unexpected query result"; exit 1
fi
msg="arena blocked=$blocked; migrator waiters=$waiting/${wait_s}s; analyst idle-tx=${idle_s}s; exclusive relation waiters=$exclusive; database overrides=$overrides"
if [[ "$role_ok|$lock_ok|$idle_ok" != 't|t|t' ]] || (( overrides > 0 )); then
  echo "CRIT $NAME: missing/privileged role or 3s/15s default drift or unreviewed database override; $msg"; exit 2
fi
if (( blocked > 0 || wait_s > 4 || idle_s > 16 )); then
  echo "CRIT $NAME: live blocking or existing session exceeds P5 defaults; $msg"; exit 2
fi
if (( waiting > 0 || exclusive > 0 || idle_s >= 5 )); then
  echo "WARN $NAME: early exclusive wait/idle blocker; $msg"; exit 1
fi
echo "OK $NAME: migrator lock=3s, analyst idle-tx=15s; $msg"
exit 0
