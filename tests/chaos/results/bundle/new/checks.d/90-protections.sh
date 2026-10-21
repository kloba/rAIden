#!/usr/bin/env bash
# 90-protections.sh - are the protections rAIden installed still in place?
#
# Reads /var/lib/raiden/memory/protections.list (one protection per line: name|UTC time|description) and re-verifies every
# protection it knows how to verify: baseline-config, pin-preload, extension-allowlist, protected-tables, role-limits,
# lock-timeout (the last two are verified for the role named at the start of the description: "role <name>: ...").
# CRIT = a recorded protection was removed or disabled; WARN = it could not be verified. No list yet = OK.
# Last line: "OK|WARN|CRIT protections: <message>"; exit 0/1/2. Read-only, < 5 s. On CRIT use skill: preventive-protections.
set -uo pipefail
NAME=protections
LIST=/var/lib/raiden/memory/protections.list
CONF=/etc/postgresql/18/main
BIN=/usr/lib/postgresql/18/bin
export PGCONNECT_TIMEOUT=3
q() { timeout 8 psql -X -At -d "${2:-postgres}" -c "$1" 2>/dev/null; }

if [ ! -s "$LIST" ]; then echo "OK $NAME: no protections recorded yet"; exit 0; fi
if ! pg_isready -q -t 3; then echo "WARN $NAME: database not accepting connections, protections not verified"; exit 1; fi

verified=(); broken=(); unsure=()
while IFS='|' read -r pname _ pdesc; do
  [ -z "$pname" ] && continue
  case "$pname" in
    baseline-config)
      if [ "$(q "show allow_alter_system")" = "off" ] && [ -f "$CONF/conf.d/98-raiden-baseline.conf" ]; then verified+=("$pname")
      else broken+=("$pname: allow_alter_system is not off or 98-raiden-baseline.conf is gone"); fi ;;
    pin-preload)
      pin="$CONF/conf.d/99-raiden-pin-preload.conf"
      if [ ! -f "$pin" ]; then broken+=("$pname: $pin is gone"); continue; fi
      pinned="$(sed -n "s/^shared_preload_libraries[[:space:]]*=[[:space:]]*'\(.*\)'.*/\1/p" "$pin" | tail -1)"
      planned="$(timeout 8 sudo -n -u postgres "$BIN/postgres" -D "$CONF" -C shared_preload_libraries 2>/dev/null | tail -1)"
      if [ -z "$planned" ]; then unsure+=("$pname: could not ask postgres -C")
      elif [ "$planned" != "$pinned" ]; then broken+=("$pname: next restart would load '$planned', pinned '$pinned'")
      else verified+=("$pname"); fi ;;
    extension-allowlist|protected-tables)
      trg=raiden_extension_allowlist; [ "$pname" = protected-tables ] && trg=raiden_protect_tables
      ev="$(q "select evtenabled from pg_event_trigger where evtname = '$trg'" ledger)"
      if [ "$ev" = "O" ]; then verified+=("$pname")
      elif [ -z "$ev" ]; then broken+=("$pname: event trigger $trg is missing in ledger")
      else broken+=("$pname: event trigger $trg is disabled (evtenabled=$ev)"); fi ;;
    role-limits|lock-timeout)
      role="$(printf '%s' "$pdesc" | sed -n 's/^role \([A-Za-z_][A-Za-z0-9_]*\).*/\1/p')"
      if [ -z "$role" ]; then unsure+=("$pname: no role name at the start of the description"); continue; fi
      if [ "$pname" = role-limits ]; then
        lim="$(q "select rolconnlimit from pg_roles where rolname = '$role'")"
        if [ -n "$lim" ] && [ "$lim" -gt 0 ] 2>/dev/null; then verified+=("$pname")
        else broken+=("$pname: role $role has no connection limit any more"); fi
      else
        cfg="$(q "select coalesce(array_to_string(rolconfig, ','), '') from pg_roles where rolname = '$role'")"
        case "$cfg" in *lock_timeout=*) verified+=("$pname") ;; *) broken+=("$pname: role $role has no lock_timeout any more") ;; esac
      fi ;;
    scratch-proof-primary-refusal)
      if /usr/bin/sha256sum --status -c /var/lib/raiden/memory/scratch-proof-guard.baseline; then verified+=("$pname")
      else broken+=("$pname: primary-refusal proof script fingerprint drift"); fi ;;
    *) ;;  # replica, backup ...: other checks or no machine verification
  esac
done < "$LIST"

if [ "${#broken[@]}" -gt 0 ]; then echo "CRIT $NAME: ${broken[*]}"; exit 2; fi
if [ "${#unsure[@]}" -gt 0 ]; then echo "WARN $NAME: ${unsure[*]}"; exit 1; fi
echo "OK $NAME: ${#verified[@]} protection(s) verified (${verified[*]:-none})"
exit 0
