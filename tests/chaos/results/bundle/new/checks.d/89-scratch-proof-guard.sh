#!/usr/bin/env bash
# Primary proof refusal and leaked proof objects. Use learned-scratch-proof-alert.
# Added after inc-20261021T012504-bb45; read-only, no alert suppression.
set -euo pipefail
NAME=scratch-proof-guard
export PGCONNECT_TIMEOUT=3
if ! /usr/bin/sha256sum --status -c /var/lib/raiden/memory/scratch-proof-guard.baseline; then
  echo "CRIT $NAME: proof script fingerprint drift or missing baseline; review primary guard"; exit 2
fi
if ! v="$(/usr/bin/timeout 8 psql -X -At -v ON_ERROR_STOP=1 -d ledger -c "SELECT (SELECT count(*) FROM pg_roles WHERE rolname IN ('scratch_p5_analyst','scratch_p5_migrator')) + (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='scratch_p5_queue_probe') + (SELECT count(*) FROM pg_stat_activity WHERE application_name='raiden-p5-scratch-proof');" 2>/dev/null)"; then
  echo "WARN $NAME: database unreachable, scratch residue not verified"; exit 1
fi
if [[ "$v" != 0 ]]; then
  echo "CRIT $NAME: scratch proof roles/table/sessions present ($v); inspect provenance and locks"; exit 2
fi
echo "OK $NAME: primary refusal fingerprint intact; no known scratch proof residue"
