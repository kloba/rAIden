#!/usr/bin/env bash
# run_checks.sh - run every executable in /opt/raiden/checks.d in parallel and summarise (health patrol).
#
# Usage: run_checks.sh [--log] [--dir DIR] [--timeout SEC]
#   --log      append "<UTC ts> worst=<S> ok=N warn=N crit=N [non-OK checks]" to /var/lib/raiden/memory/patrol.log
#   --dir      checks directory (default /opt/raiden/checks.d)
#   --timeout  per-check timeout in seconds (default 20)
# Contract for checks: print "OK|WARN|CRIT <name>: <message>" as the LAST line, exit 0/1/2.
# A check that times out, exits with another code or prints no verdict is shown with rc=3 (counted as WARN).
# Exit code: 0 all OK, 1 worst is WARN, 2 worst is CRIT.
set -euo pipefail

DIR=/opt/raiden/checks.d TIMEOUT=20 LOG=0
while [ $# -gt 0 ]; do
  case "$1" in
    --log)     LOG=1; shift ;;
    --dir)     DIR="${2:?--dir needs a value}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
    *) sed -n '2,11p' "$0" >&2; exit 64 ;;
  esac
done

TMP="$(mktemp -d /tmp/raiden-patrol.XXXXXX)"
trap 'rm -rf -- "$TMP"' EXIT

checks=()
for c in "$DIR"/*; do
  if [ -f "$c" ] && [ -x "$c" ]; then checks+=("$c"); fi
done
if [ "${#checks[@]}" -eq 0 ]; then echo "WORST: CRIT (no executable checks in $DIR)"; exit 2; fi

for c in "${checks[@]}"; do
  n="$(basename "$c")"
  ( rc=0; timeout -k 2 "$TIMEOUT" "$c" >"$TMP/$n.out" 2>&1 </dev/null || rc=$?; echo "$rc" >"$TMP/$n.rc" ) &
done
wait

names=(OK WARN CRIT)
worst=0 ok=0 warn=0 crit=0
bad=()
for c in "${checks[@]}"; do
  n="$(basename "$c")"
  rc="$(cat "$TMP/$n.rc" 2>/dev/null || echo 3)"
  verdict="$(grep -v '^[[:space:]]*$' "$TMP/$n.out" 2>/dev/null | tail -n 1 || true)"
  case "$verdict" in
    "OK "*) vsev=0 ;; "WARN "*) vsev=1 ;; "CRIT "*) vsev=2 ;; *) vsev=-1 ;;
  esac
  if [[ ! "$rc" =~ ^[012]$ ]] || [ "$vsev" -lt 0 ]; then
    [ "$rc" = 124 ] && verdict="timed out after ${TIMEOUT}s; last output: ${verdict:-none}"
    sev=1; shown=3
  else
    sev=$(( rc > vsev ? rc : vsev )); shown=$sev
  fi
  case "$sev" in 0) ok=$((ok + 1)) ;; 1) warn=$((warn + 1)); bad+=("$n") ;; 2) crit=$((crit + 1)); bad+=("$n") ;; esac
  if [ "$sev" -gt "$worst" ]; then worst=$sev; fi
  echo "rc=$shown $n: ${verdict:-<no output>}"
done

echo "WORST: ${names[$worst]}"
if [ "$LOG" = 1 ]; then
  mkdir -p /var/lib/raiden/memory
  echo "$(date -u +%FT%TZ) worst=${names[$worst]} ok=$ok warn=$warn crit=$crit [${bad[*]:-}]" >>/var/lib/raiden/memory/patrol.log
fi
exit "$worst"
