#!/usr/bin/env bash
# pitr_cleanup.sh - stop a PITR side clone created by pitr_clone.sh and delete its directory.
#
# Usage: pitr_cleanup.sh CLONE_DIR        CLONE_DIR must be /var/lib/raiden/work/pitr-<stamp>
#
# Refuses any other path and any postmaster.pid that does not belong to the clone; stops the clone with a
# fast shutdown (immediate as fallback), then removes the directory. Never touches production.
set -euo pipefail

BIN=/usr/lib/postgresql/18/bin
[ $# -eq 1 ] || { sed -n '2,7p' "$0" >&2; exit 64; }
DIR="$(realpath -m -- "$1")"
[[ "$DIR" =~ ^/var/lib/raiden/work/pitr-[A-Za-z0-9._-]+$ ]] || { echo "ERROR: refusing: $DIR is not /var/lib/raiden/work/pitr-*" >&2; exit 1; }
[ -d "$DIR" ] || { echo "nothing to do: $DIR does not exist"; exit 0; }
pg() { sudo -n -u postgres "$@"; }

if pg test -f "$DIR/data/postmaster.pid"; then
  pid_datadir="$(pg sed -n '2p' "$DIR/data/postmaster.pid")"
  if [ "$(realpath -m -- "$pid_datadir")" != "$DIR/data" ]; then
    echo "ERROR: $DIR/data/postmaster.pid names data dir '$pid_datadir' - refusing" >&2; exit 1
  fi
  if pg "$BIN/pg_ctl" -D "$DIR/data" status >/dev/null 2>&1; then
    echo "stopping clone $DIR ..."
    pg "$BIN/pg_ctl" -D "$DIR/data" -m fast -w -t 60 stop ||
      pg "$BIN/pg_ctl" -D "$DIR/data" -m immediate -w -t 30 stop
  fi
fi
if pg "$BIN/pg_ctl" -D "$DIR/data" status >/dev/null 2>&1; then
  echo "ERROR: clone in $DIR is still running - not deleting" >&2; exit 1
fi

[ -d "$DIR/data" ] && pg rm -rf -- "$DIR/data"
rm -rf -- "$DIR"
echo "removed $DIR"
df -h /var/lib/raiden/work | tail -1
