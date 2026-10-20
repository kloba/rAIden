#!/usr/bin/env bash
# pitr_clone.sh - build a point-in-time SIDE CLONE of the PostgreSQL 18 cluster under /var/lib/raiden/work.
# Production is never touched: the newest suitable base backup is copied, WAL is replayed from the archive
# (read-only restore_command), the clone listens only on a unix socket in its own directory and promotes
# at the target.
#
# Usage:
#   pitr_clone.sh --target-xid XID (--before 'TS' | --backup DIR) [--inclusive] [--port 5433] [--timeout 540]
#   pitr_clone.sh --target-time 'TS' [--inclusive] [--backup DIR] [--port 5433] [--timeout 540]
#     TS            e.g. '2026-10-20 11:58:00 UTC' (normalised to UTC)
#     --target-xid  stop just before (default) or after (--inclusive) the commit of transaction XID
#     --target-time stop before the first commit with timestamp >= TS (default) or > TS (--inclusive)
#     --before TS   with --target-xid: choose the newest base backup that started before TS
#     --backup DIR  use this base backup directory (must contain backup_label)
#     --timeout S   seconds to wait for promotion (default 540); exit 3 if still replaying
# Prints CLONE_DIR=, SOCKET_DIR=, PORT= and the recovery stop line. Exit 0 = promoted and ready,
# 1 = failed (log tail printed), 3 = still replaying, 64 = usage. Remove with pitr_cleanup.sh CLONE_DIR.
set -euo pipefail

BIN=/usr/lib/postgresql/18/bin
ARCHIVE=/var/lib/postgresql/wal_archive
BASEDIR=/var/backups/postgresql/base
WORK=/var/lib/raiden/work
PORT=5433 TIMEOUT=540 INCLUSIVE=false XID="" TTIME="" BEFORE="" BACKUP="" CLONE=""

usage() { sed -n '2,20p' "$0" >&2; exit 64; }
die() { echo "ERROR: $*" >&2; exit 1; }
pg() { sudo -n -u postgres "$@"; }
# shellcheck disable=SC2154  # rc is assigned inside the trap string
trap 'rc=$?; [ $rc -ne 0 ] && [ $rc -ne 3 ] && [ $rc -ne 64 ] && [ -n "$CLONE" ] && echo "FAILED (rc=$rc). Inspect $CLONE/clone.log; remove with pitr_cleanup.sh $CLONE" >&2' EXIT

while [ $# -gt 0 ]; do
  case "$1" in
    --target-xid)  XID="${2:-}"; shift 2 || usage ;;
    --target-time) TTIME="${2:-}"; shift 2 || usage ;;
    --before)      BEFORE="${2:-}"; shift 2 || usage ;;
    --backup)      BACKUP="${2:-}"; shift 2 || usage ;;
    --port)        PORT="${2:-}"; shift 2 || usage ;;
    --timeout)     TIMEOUT="${2:-}"; shift 2 || usage ;;
    --inclusive)   INCLUSIVE=true; shift ;;
    -h|--help)     usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

if { [ -n "$XID" ] && [ -n "$TTIME" ]; } || { [ -z "$XID" ] && [ -z "$TTIME" ]; }; then
  echo "give exactly one of --target-xid / --target-time" >&2; usage
fi
[[ "$PORT" =~ ^[0-9]+$ && "$TIMEOUT" =~ ^[0-9]+$ ]] || usage
if [ -n "$XID" ]; then
  [[ "$XID" =~ ^[0-9]+$ ]] || die "--target-xid must be a number"
  [ -n "$BEFORE$BACKUP" ] || die "--target-xid needs --before 'TS' (incident time) or --backup DIR"
  [ -z "$BEFORE" ] || CUTOFF="$(date -u -d "$BEFORE" +%s)" || die "cannot parse --before '$BEFORE'"
else
  CUTOFF="$(date -u -d "$TTIME" +%s)" || die "cannot parse --target-time '$TTIME'"
  TTIME_UTC="$(date -u -d "$TTIME" '+%Y-%m-%d %H:%M:%S.%6N')+00"   # explicit UTC, keeps sub-second precision
fi

label_start() { # DIR -> epoch of START TIME in DIR/backup_label
  local st
  st="$(pg sed -n 's/^START TIME: //p' "$1/backup_label" 2>/dev/null)" || return 1
  [ -n "$st" ] && date -u -d "$st" +%s
}

# --- choose the base backup ---------------------------------------------------------------
if [ -z "$BACKUP" ]; then
  while read -r d; do
    e="$(label_start "$BASEDIR/$d")" || continue
    if [ "$e" -lt "$CUTOFF" ]; then BACKUP="$BASEDIR/$d"; break; fi
  done < <(pg ls -1 "$BASEDIR" | sort -r)
  [ -n "$BACKUP" ] || die "no base backup in $BASEDIR started before $(date -u -d "@$CUTOFF" '+%F %T') UTC"
fi
BACKUP="${BACKUP%/}"
pg test -f "$BACKUP/backup_label" || die "$BACKUP has no backup_label (not a usable base backup)"
echo "base backup: $BACKUP"; pg grep -E '^(START WAL LOCATION|START TIME|START TIMELINE):' "$BACKUP/backup_label"
if [ -n "${CUTOFF:-}" ] && [ "$(label_start "$BACKUP")" -ge "$CUTOFF" ]; then
  die "backup $BACKUP started after the target; choose an older --backup"
fi
if [ -n "$(pg find "$BACKUP/pg_tblspc" -mindepth 1 -maxdepth 1 2>/dev/null)" ]; then
  die "backup contains tablespaces (pg_tblspc links would point at production paths); not supported - escalate"
fi

# --- space check: keep >= 15% of the filesystem free after the copy (it is shared with production) ---
need_kb="$(pg du -sk "$BACKUP" | cut -f1)"
read -r total_kb avail_kb < <(df -Pk "$WORK" | awk 'NR==2 {print $2, $4}')
if (( avail_kb - need_kb * 12 / 10 < total_kb * 15 / 100 )); then
  die "not enough space in $WORK: need ~$((need_kb/1024)) MB, available $((avail_kb/1024)) MB (keeping 15% free)"
fi

# --- clone directory (group postgres, setgid, so the server can write its socket/log here) --------
TS="$(date -u +%Y%m%dT%H%M%S)"
CLONE="$WORK/pitr-$TS"
mkdir -p "$CLONE"; chgrp postgres "$CLONE"; chmod 2770 "$CLONE"
pg test -w "$CLONE" || die "OS user postgres cannot write $CLONE (needs search permission on $WORK and its parents)"

echo "copying $BACKUP -> $CLONE/data ($((need_kb/1024)) MB) ..."
pg cp -a "$BACKUP" "$CLONE/data"
pg chmod 700 "$CLONE/data"
pg rm -f "$CLONE/data/postmaster.pid" "$CLONE/data/postmaster.opts" "$CLONE/data/standby.signal"

# Debian keeps postgresql.conf/pg_hba.conf in /etc: give the clone minimal ones of its own
echo "# rAIden PITR clone $TS - settings are in postgresql.auto.conf" | pg tee "$CLONE/data/postgresql.conf" >/dev/null
echo "local all all peer" | pg tee "$CLONE/data/pg_hba.conf" >/dev/null
pg tee "$CLONE/data/pg_ident.conf" </dev/null >/dev/null
{
  echo "# --- rAIden PITR clone overrides ($TS); entries at the end win ---"
  echo "port = $PORT"
  echo "listen_addresses = ''"
  echo "unix_socket_directories = '$CLONE'"
  echo "cluster_name = 'pitr-$TS'"
  echo "shared_preload_libraries = ''"
  echo "session_preload_libraries = ''"
  echo "local_preload_libraries = ''"
  echo "archive_mode = off"
  echo "restore_command = 'cp $ARCHIVE/%f \"%p\"'"
  echo "primary_conninfo = ''"
  echo "synchronous_standby_names = ''"
  echo "recovery_target_timeline = 'latest'"
  if [ -n "$XID" ]; then echo "recovery_target_xid = '$XID'"; else echo "recovery_target_time = '$TTIME_UTC'"; fi
  echo "recovery_target_inclusive = $INCLUSIVE"
  echo "recovery_target_action = 'promote'"
  echo "hot_standby = off"
  echo "logging_collector = off"
  echo "log_destination = 'stderr'"
  echo "shared_buffers = '256MB'"
} | pg tee -a "$CLONE/data/postgresql.auto.conf" >/dev/null
pg touch "$CLONE/data/recovery.signal"
{ echo "backup=$BACKUP"; echo "target_xid=$XID"; echo "target_time=${TTIME_UTC:-}"; echo "inclusive=$INCLUSIVE"; echo "port=$PORT"; } > "$CLONE/clone.info"

# --- start and wait for promotion ---------------------------------------------------------
pg "$BIN/pg_ctl" -D "$CLONE/data" -l "$CLONE/clone.log" -W start
deadline=$(( $(date +%s) + TIMEOUT ))
sleep 3
while :; do
  if "$BIN/pg_isready" -q -h "$CLONE" -p "$PORT" -t 2 &&
     [ "$(pg env PGCONNECT_TIMEOUT=3 "$BIN/psql" -X -At -h "$CLONE" -p "$PORT" -d postgres -c 'SELECT pg_is_in_recovery()' 2>/dev/null)" = f ]; then
    break
  fi
  if ! pg "$BIN/pg_ctl" -D "$CLONE/data" status >/dev/null 2>&1; then
    sleep 2
    if ! pg "$BIN/pg_ctl" -D "$CLONE/data" status >/dev/null 2>&1; then
      echo "clone stopped during recovery; last log lines:" >&2
      pg tail -n 25 "$CLONE/clone.log" >&2 || true
      exit 1
    fi
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "STILL REPLAYING after ${TIMEOUT}s. Poll: pg_isready -h $CLONE -p $PORT ; sudo -n -u postgres tail -n 5 $CLONE/clone.log"
    pg tail -n 3 "$CLONE/clone.log" || true
    echo "CLONE_DIR=$CLONE"; echo "SOCKET_DIR=$CLONE"; echo "PORT=$PORT"
    exit 3
  fi
  sleep 3
done

pg grep -E 'starting point-in-time recovery|recovery stopping|selected new timeline|archive recovery complete|ready to accept' "$CLONE/clone.log" || true
echo "CLONE_DIR=$CLONE"
echo "SOCKET_DIR=$CLONE"
echo "PORT=$PORT"
echo "connect: psql -X -h $CLONE -p $PORT -d ledger     (or: sudo -n -u postgres psql -X -h $CLONE -p $PORT -d ledger)"
echo "cleanup: /opt/raiden/.claude/skills/pitr-restore/scripts/pitr_cleanup.sh $CLONE"
