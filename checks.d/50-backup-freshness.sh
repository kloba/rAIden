#!/usr/bin/env bash
# 50-backup-freshness.sh - age of the newest complete base backup in /var/backups/postgresql/base.
#
# Usage: 50-backup-freshness.sh    (no arguments, read-only, does not need the database; < 2 s)
# A backup counts only if its directory contains backup_label; age = now - START TIME from backup_label
# (fallback: the <YYYYmmddTHHMMSS> directory name, read as UTC).
# Thresholds: > 24 h WARN, > 72 h CRIT, no complete backup CRIT.
# Last line: "OK|WARN|CRIT backup-freshness: <message>"; exit 0/1/2.
# On WARN/CRIT see skill health-patrol (take a new base backup if space allows) / disk-space-wal.
set -euo pipefail
NAME=backup-freshness
BASEDIR=/var/backups/postgresql/base
WARN_H=24
CRIT_H=72

as_pg() { timeout 5 sudo -n -u postgres "$@" 2>/dev/null; }
if ! list="$(timeout 5 ls -1 "$BASEDIR" 2>/dev/null)"; then
  list="$(as_pg ls -1 "$BASEDIR")" || { echo "CRIT $NAME: cannot list $BASEDIR"; exit 2; }
fi

newest="" newest_t=0 incomplete=0 total=0
while read -r d; do
  [ -n "$d" ] || continue
  total=$((total + 1))
  label="$BASEDIR/$d/backup_label"
  if ! st="$(sed -n 's/^START TIME: //p' "$label" 2>/dev/null)" || [ -z "$st" ]; then
    st="$(as_pg sed -n 's/^START TIME: //p' "$label")" || st=""
  fi
  if [ -z "$st" ]; then
    if [ -e "$label" ] || as_pg test -e "$label"; then
      st="${d:0:8} ${d:9:2}:${d:11:2}:${d:13:2} UTC"
    else
      incomplete=$((incomplete + 1)); continue
    fi
  fi
  t="$(date -u -d "$st" +%s 2>/dev/null)" || { incomplete=$((incomplete + 1)); continue; }
  if [ "$t" -gt "$newest_t" ]; then newest_t=$t; newest=$d; fi
done <<<"$list"

if [ -z "$newest" ]; then
  echo "CRIT $NAME: no complete base backup (with backup_label) in $BASEDIR ($total dirs, $incomplete incomplete)"; exit 2
fi
age_h=$(( ($(date +%s) - newest_t) / 3600 ))
msg="newest $newest started $(date -u -d "@$newest_t" '+%F %T') UTC (${age_h} h ago), $total backups, $incomplete incomplete"
if [ "$age_h" -ge "$CRIT_H" ]; then echo "CRIT $NAME: $msg; older than ${CRIT_H} h"; exit 2; fi
if [ "$age_h" -ge "$WARN_H" ]; then echo "WARN $NAME: $msg; older than ${WARN_H} h"; exit 1; fi
echo "OK $NAME: $msg"
exit 0
