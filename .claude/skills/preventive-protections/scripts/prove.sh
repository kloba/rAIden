#!/usr/bin/env bash
# prove.sh <extension-allowlist|protected-tables> - dry-run proof of an event-trigger protection in database ledger.
# Uses only scratch objects (and a transaction it rolls back); real data is never touched.
# Prints PROOF OK or PROOF FAILED (exit 0 / 1).
set -uo pipefail
export PGCONNECT_TIMEOUT=3
DB=ledger
fail() { echo "PROOF FAILED: $*"; exit 1; }
p() { timeout 30 psql -X -d "$DB" "$@" 2>&1; }

case "${1:-}" in
  extension-allowlist)
    ev="$(p -At -c "select evtenabled from pg_event_trigger where evtname = 'raiden_extension_allowlist'")"
    [ "$ev" = "O" ] || fail "event trigger raiden_extension_allowlist is missing or disabled (evtenabled='$ev')"
    out="$(p -c "BEGIN" -c "CREATE EXTENSION pg_surgery" -c "ROLLBACK")"
    echo "$out" | sed 's/^/  /'
    left="$(p -At -c "select count(*) from pg_extension where extname = 'pg_surgery'")"
    [ "$left" = "0" ] || { p -c "DROP EXTENSION pg_surgery" >/dev/null; fail "pg_surgery got created in $DB despite the trigger"; }
    case "$out" in *"is not on the allowlist"*) echo "PROOF OK: CREATE EXTENSION pg_surgery bounces and nothing was created" ;; *) fail "no allowlist error in the output above" ;; esac
    ;;
  protected-tables)
    ev="$(p -At -c "select evtenabled from pg_event_trigger where evtname = 'raiden_protect_tables'")"
    [ "$ev" = "O" ] || fail "event trigger raiden_protect_tables is missing or disabled (evtenabled='$ev')"
    real="$(p -At -c "select count(*) from raiden.protected_tables where table_name !~ '^scratch_'")"
    [ "${real:-0}" -ge 1 ] 2>/dev/null || fail "raiden.protected_tables has no real application table registered"
    p -v ON_ERROR_STOP=1 -c "CREATE TABLE public.scratch_protect_probe (id int)" -c "INSERT INTO raiden.protected_tables (schema_name, table_name) VALUES ('public', 'scratch_protect_probe')" >/dev/null || fail "could not create the scratch probe"
    out="$(p -c "DROP TABLE public.scratch_protect_probe")"
    echo "$out" | sed 's/^/  /'
    p -c "DELETE FROM raiden.protected_tables WHERE table_name = 'scratch_protect_probe'" -c "DROP TABLE IF EXISTS public.scratch_protect_probe" >/dev/null
    left="$(p -At -c "select count(*) from pg_class where relname = 'scratch_protect_probe'")"
    [ "$left" = "0" ] || fail "scratch probe table is still there"
    case "$out" in *"is protected and cannot be dropped"*) echo "PROOF OK: DROP TABLE of a registered table bounces ($real real tables registered)" ;; *) fail "no protection error in the output above" ;; esac
    ;;
  *) echo "usage: prove.sh extension-allowlist|protected-tables" >&2; exit 64 ;;
esac
