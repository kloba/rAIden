#!/usr/bin/env bash
# prove_lock_timeout.sh <role> - dry-run proof of protection P5 (lock_timeout): a DDL statement must give up fast instead of
# freezing every session queued behind it. Reads the role's lock_timeout from pg_roles.rolconfig, then on a SCRATCH table
# one session holds a read lock while a second runs ALTER TABLE with exactly that lock_timeout. Real tables are never touched.
# Prints PROOF OK or PROOF FAILED (exit 0 / 1). Takes about 15 s.
set -uo pipefail
export PGCONNECT_TIMEOUT=3
DB=ledger
role="${1:-}"
[ -n "$role" ] || { echo "usage: prove_lock_timeout.sh <ddl role>" >&2; exit 64; }
p() { timeout 40 psql -X -d "$DB" "$@" 2>&1; }
holder=""
cleanup() { [ -n "$holder" ] && wait "$holder" 2>/dev/null; p -c "DROP TABLE IF EXISTS public.scratch_lock_probe" >/dev/null; }
fail() { echo "PROOF FAILED: $*"; cleanup; exit 1; }

lt="$(p -At -c "select coalesce((select split_part(c, '=', 2) from pg_roles r, unnest(r.rolconfig) c where r.rolname = '$role' and c like 'lock_timeout=%' limit 1), '')")"
[ -n "$lt" ] || fail "role $role has no lock_timeout in pg_roles.rolconfig (ALTER ROLE $role SET lock_timeout = '3s' first)"
p -v ON_ERROR_STOP=1 -c "DROP TABLE IF EXISTS public.scratch_lock_probe" -c "CREATE TABLE public.scratch_lock_probe (id int)" >/dev/null || fail "could not create the scratch table"

# session A: an old transaction holding a plain read lock for 12 s (the forgotten-transaction half of the incident)
timeout 30 psql -X -d "$DB" -c "BEGIN" -c "SELECT count(*) FROM public.scratch_lock_probe" -c "SELECT pg_sleep(12)" -c "ROLLBACK" >/dev/null 2>&1 &
holder=$!
sleep 1.5
# session B: the DDL, with the role's own lock_timeout
start=$(date +%s)
out="$(p -c "SET lock_timeout = '$lt'" -c "ALTER TABLE public.scratch_lock_probe ADD COLUMN probe int")"
secs=$(( $(date +%s) - start ))
echo "$out" | sed 's/^/  /'
cols="$(p -At -c "select count(*) from information_schema.columns where table_name = 'scratch_lock_probe' and column_name = 'probe'")"
cleanup
[ "$cols" = "0" ] || fail "the ALTER TABLE went through although a lock was held"
[ "$secs" -le 9 ] 2>/dev/null || fail "the ALTER TABLE waited ${secs}s (lock_timeout $lt)"
case "$out" in *"canceling statement due to lock timeout"*) echo "PROOF OK: ALTER TABLE gave up after ${secs}s (lock_timeout $lt) instead of queueing everybody behind it" ;; *) fail "no lock timeout error in the output above" ;; esac
