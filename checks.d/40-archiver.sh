#!/usr/bin/env bash
# 40-archiver.sh - WAL archiving health (PITR depends on it).
#
# Usage: 40-archiver.sh            (no arguments, read-only except a small state file; < 2 s, bounded ~15 s)
# CRIT: archive_mode off; last_failed_time newer than last_archived_time (archiving is failing now);
#       archive dir missing or not writable by postgres.
# WARN: failed_count rose since the previous run (state in $RAIDEN_CHECK_STATE_DIR, default
#       /var/lib/raiden/work/check-state); > 20 segments waiting (.ready); database unreachable.
# Last line: "OK|WARN|CRIT archiver: <message>"; exit 0/1/2. On WARN/CRIT use skill: disk-space-wal.
set -euo pipefail
NAME=archiver
ARCHIVE=/var/lib/postgresql/wal_archive
STATE_DIR="${RAIDEN_CHECK_STATE_DIR:-/var/lib/raiden/work/check-state}"
export PATH="/usr/lib/postgresql/18/bin:$PATH" PGCONNECT_TIMEOUT=3

if ! r="$(timeout 8 psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "
    SELECT current_setting('archive_mode'), archived_count, failed_count,
           coalesce(last_archived_wal, '-'), coalesce(last_failed_wal, '-'),
           coalesce(extract(epoch FROM last_archived_time)::bigint, 0),
           coalesce(extract(epoch FROM last_failed_time)::bigint, 0),
           (SELECT count(*) FROM pg_ls_archive_statusdir() WHERE name LIKE '%.ready')
    FROM pg_stat_archiver" 2>&1)"; then
  echo "WARN $NAME: database unreachable, check skipped (${r//$'\n'/ })"; exit 1
fi
IFS='|' read -r mode archived failed last_ok_wal last_fail_wal last_ok_t last_fail_t ready <<<"$r"
now="$(date +%s)"
ok_age=$(( last_ok_t > 0 ? now - last_ok_t : -1 ))
info="archived=$archived failed=$failed ready=$ready last_ok=$last_ok_wal (${ok_age}s ago)"

if [ "$mode" = off ]; then echo "CRIT $NAME: archive_mode is off - no PITR possible ($info)"; exit 2; fi
if ! timeout 5 test -d "$ARCHIVE"; then echo "CRIT $NAME: archive directory $ARCHIVE missing or not accessible"; exit 2; fi
if ! timeout 5 sudo -n -u postgres test -w "$ARCHIVE" 2>/dev/null; then
  echo "CRIT $NAME: $ARCHIVE not writable by postgres (or sudo unavailable) ($info)"; exit 2
fi
if [ "$last_fail_t" -gt "$last_ok_t" ]; then
  echo "CRIT $NAME: archiving is failing - last failure $last_fail_wal at $(date -u -d "@$last_fail_t" '+%F %T') UTC is newer than last success ($info)"; exit 2
fi

prev=""
if mkdir -p "$STATE_DIR" 2>/dev/null; then
  prev="$(cat "$STATE_DIR/40-archiver.failed_count" 2>/dev/null || true)"
  echo "$failed" >"$STATE_DIR/40-archiver.failed_count" 2>/dev/null || true
fi
if [[ "$prev" =~ ^[0-9]+$ ]] && [ "$failed" -gt "$prev" ]; then
  echo "WARN $NAME: failed_count rose from $prev to $failed since last run (now recovering: last success is newer) ($info)"; exit 1
fi
if [ "$ready" -gt 20 ]; then echo "WARN $NAME: $ready WAL segments waiting to be archived ($info)"; exit 1; fi
echo "OK $NAME: $info"
exit 0
