#!/usr/bin/env bash
# prove_pin_preload.sh - dry-run proof that the shared_preload_libraries pin beats a config-file experiment, and that
# ALTER SYSTEM is refused. Production is not touched and nothing is restarted.
# Usage: prove_pin_preload.sh [LIBRARY]     (LIBRARY = the attacker's library, default pg_crash)
# How: copy the config tree to /tmp/raiden-prove-*/, add simulated attacker files that sort BEFORE the pin, and ask
# `postgres -C shared_preload_libraries` which list the NEXT restart would load.
# Prints PROOF OK or PROOF FAILED (exit 0 / 1).
set -uo pipefail
LIB="${1:-pg_crash}"
CONF=/etc/postgresql/18/main
PIN="$CONF/conf.d/99-raiden-pin-preload.conf"
BIN=/usr/lib/postgresql/18/bin
fail() { echo "PROOF FAILED: $*"; exit 1; }

[ -f "$PIN" ] || fail "$PIN does not exist"
WORK="$(mktemp -d /tmp/raiden-prove-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
cp -r "$CONF" "$WORK/main" || fail "cannot copy $CONF"
chmod -R a+rX "$WORK"

planned() { sudo -n -u postgres "$BIN/postgres" -D "$1" -C shared_preload_libraries 2>&1 | tail -1; }

real="$(planned "$CONF")"
echo "next restart would load (real config):      $real"
case ",$real," in *",$LIB,"*) fail "the real config already loads $LIB at the next restart" ;; esac

printf '%s\n' "# simulated attacker experiment" "shared_preload_libraries = 'pg_stat_statements,$LIB'" > "$WORK/main/conf.d/50-experiment.conf"
printf '%s\n' "# simulated second attacker file" "shared_preload_libraries = '$LIB,pg_stat_statements'" > "$WORK/main/conf.d/90-attacker.conf"
sim="$(planned "$WORK/main")"
echo "next restart would load (with attacker files): $sim"
case ",$sim," in *",$LIB,"*) fail "an attacker file in conf.d still gets $LIB loaded: the pin does not sort last or is wrong" ;; esac

# the same experiment WITHOUT the pin must load the library, otherwise the test proves nothing
rm -f "$WORK/main/conf.d/99-raiden-pin-preload.conf"
control="$(planned "$WORK/main")"
echo "control, pin removed from the copy:          $control"
case ",$control," in *",$LIB,"*) ;; *) fail "control run did not load $LIB, the dry run is not valid" ;; esac

out="$(timeout 20 psql -X -d postgres -c "alter system set work_mem = '4MB'" 2>&1)"
echo "ALTER SYSTEM attempt: $out"
case "$out" in
  *"not allowed"*) ;;
  *) timeout 20 psql -X -d postgres -c "alter system reset work_mem" >/dev/null 2>&1; fail "ALTER SYSTEM was accepted: set allow_alter_system = off (fortify baseline) first" ;;
esac
echo "PROOF OK: attacker files lose against the pin ($sim) and ALTER SYSTEM is refused"
