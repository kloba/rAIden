#!/usr/bin/env bash
# Exact reporting limits and database override/occupancy drift, inc-20261021T010712-42b2.
# On WARN/CRIT use learned-reporting-limits; read-only, bounded to 10 seconds.
set -euo pipefail
NAME=reporting-limits
export PGCONNECT_TIMEOUT=3
if ! r="$(timeout 10 psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "
WITH r AS (SELECT * FROM pg_roles WHERE rolname='reporting'),
cfg AS (SELECT split_part(v,'=',1) AS k, split_part(v,'=',2) AS v FROM r, unnest(rolconfig) v),
a AS (SELECT count(*) AS n,
 coalesce(max(extract(epoch FROM now()-state_change)) FILTER (WHERE state='idle'),0)::bigint AS idle_s,
 coalesce(max(extract(epoch FROM now()-state_change)) FILTER (WHERE state IN ('idle in transaction','idle in transaction (aborted)')),0)::bigint AS tx_s
 FROM pg_stat_activity WHERE usename='reporting' AND backend_type='client backend')
SELECT coalesce((SELECT NOT rolsuper AND rolcanlogin AND rolconnlimit=10 FROM r),false),
 coalesce((SELECT v::interval=interval '2min' FROM cfg WHERE k='idle_session_timeout'),false),
 coalesce((SELECT v::interval=interval '15s' FROM cfg WHERE k='idle_in_transaction_session_timeout'),false),
 (SELECT count(*) FROM pg_db_role_setting s, unnest(s.setconfig) v
 WHERE s.setdatabase<>0 AND (s.setrole=0 OR s.setrole=(SELECT oid FROM r))
 AND split_part(v,'=',1) IN ('idle_session_timeout','idle_in_transaction_session_timeout')),
 n,idle_s,tx_s FROM a;" 2>&1)"; then
  echo "WARN $NAME: database query failed (${r//$'\n'/ })"; exit 1
fi
IFS='|' read -r role_ok idle_ok tx_ok overrides n idle_s tx_s <<<"$r"
if [[ ! "$n|$overrides|$idle_s|$tx_s" =~ ^[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+$ ]]; then
  echo "WARN $NAME: unexpected query result"; exit 1
fi
msg="reporting $n/10 sessions, idle ${idle_s}s, idle transaction ${tx_s}s; database timeout overrides=$overrides"
if [[ "$role_ok|$idle_ok|$tx_ok" != 't|t|t' ]] || (( overrides > 0 )); then
  echo "CRIT $NAME: role missing/superuser/login/cap/timeout drift or unreviewed database override; $msg"; exit 2
fi
if (( n > 10 || idle_s > 125 || tx_s > 20 )); then
  echo "CRIT $NAME: existing sessions exceed role policy (defaults affect new logins); $msg"; exit 2
fi
if (( n >= 8 )); then echo "WARN $NAME: role approaching cap; $msg"; exit 1; fi
echo "OK $NAME: non-superuser cap=10, defaults=2min/15s; $msg"
exit 0
