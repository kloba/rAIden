#!/usr/bin/env bash
# 30-disk.sh - space and inode usage of the filesystems holding /, PGDATA, the WAL archive, backups and logs.
#
# Usage: 30-disk.sh                (no arguments, read-only, does not need the database; < 1 s)
# Thresholds (blocks or inodes, worst filesystem): >= 80% WARN, >= 90% CRIT.
# Last line: "OK|WARN|CRIT disk: <message>"; exit 0/1/2. On WARN/CRIT use skill: disk-space-wal.
set -euo pipefail
NAME=disk
WARN_PCT=80
CRIT_PCT=90
PATHS=(/ /var/lib/postgresql/18/main /var/lib/postgresql/wal_archive /var/backups/postgresql /var/log/postgresql)

worst=0 report="" missing=""
declare -A seen=()
for p in "${PATHS[@]}"; do
  if ! line="$(timeout 5 df -P "$p" 2>/dev/null | awk 'NR==2 {print $6, $5}')" || [ -z "$line" ]; then
    missing+=" $p"; continue
  fi
  ipct="$(timeout 5 df -Pi "$p" 2>/dev/null | awk 'NR==2 {print $5}')" || ipct="-"
  read -r mnt pct <<<"$line"
  [ -n "${seen[$mnt]:-}" ] && continue
  seen[$mnt]=1
  pct="${pct%\%}"; ipct="${ipct%\%}"
  [[ "$ipct" =~ ^[0-9]+$ ]] || ipct=0
  use=$(( pct > ipct ? pct : ipct ))
  report+=" $mnt=${pct}%(inodes ${ipct}%)"
  if [ "$use" -ge "$CRIT_PCT" ]; then sev=2; elif [ "$use" -ge "$WARN_PCT" ]; then sev=1; else sev=0; fi
  if [ "$sev" -gt "$worst" ]; then worst=$sev; fi
done

[ -n "$missing" ] && report+=" (not found/unreadable:$missing)"
if [ -z "${seen[*]:-}" ]; then echo "CRIT $NAME: df failed for all paths:$missing"; exit 2; fi
case "$worst" in
  2) echo "CRIT $NAME:$report (>= ${CRIT_PCT}%)"; exit 2 ;;
  1) echo "WARN $NAME:$report (>= ${WARN_PCT}%)"; exit 1 ;;
esac
if [ -n "$missing" ]; then echo "WARN $NAME:$report"; exit 1; fi
echo "OK $NAME:$report"
exit 0
