#!/usr/bin/env bash
# Detect extension/allowlist/trigger drift after inc-20261021T005104-7d61.
# On CRIT use skill: learned-extension-guard. Read-only; ledger is the guarded DB.
set -euo pipefail
NAME=extension-guard
export PGCONNECT_TIMEOUT=3
BASE=/var/lib/raiden/memory/extension-guard.baseline
if [[ ! -s "$BASE" ]]; then echo "CRIT $NAME: independent baseline missing"; exit 2; fi
expected="$(cat "$BASE")"
if ! actual="$(timeout 10 psql -X -At -v ON_ERROR_STOP=1 -d ledger -c "
SET statement_timeout='5s';
SELECT
 coalesce((SELECT string_agg(extname::text,',' ORDER BY extname) FROM pg_extension),'') || '|' ||
 coalesce((SELECT string_agg(extname::text,',' ORDER BY extname) FROM raiden.allowed_extensions),'') || '|' ||
 (SELECT count(*) FROM pg_event_trigger WHERE evtname='raiden_extension_allowlist' AND evtenabled IN ('O','A') AND evtevent='ddl_command_end' AND evttags=ARRAY['CREATE EXTENSION']::text[] AND evtfoid='raiden.block_unlisted_extension()'::regprocedure) || '|' ||
 md5(pg_get_functiondef('raiden.block_unlisted_extension()'::regprocedure)) || '|' ||
 (SELECT count(*) FROM pg_class c, LATERAL aclexplode(c.relacl) a WHERE c.oid='raiden.allowed_extensions'::regclass AND a.grantee=0 AND a.privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE','TRIGGER'));
" 2>/dev/null)"; then
 echo "WARN $NAME: ledger unreachable or guard query failed; inspect guard catalogs"; exit 1
fi
# psql prints SET unless quiet; compare only the result row.
actual="${actual##*$'\n'}"
if [[ "$actual" != "$expected" ]]; then
 echo "CRIT $NAME: extension/registry/trigger/function/ACL drift: '$actual', expected '$expected'"; exit 2
fi
echo "OK $NAME: amcheck/plpgsql only; registry, trigger binding, function fingerprint and public write ACL intact"
