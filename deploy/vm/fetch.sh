#!/usr/bin/env bash
# Download a (compressed) file from the lab VM through Run Command, which returns at most ~4 KB per call:
# gzip+base64 it on the VM, then page through it.   deploy/vm/fetch.sh /remote/file /local/file
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REMOTE="$1"; LOCAL="$2"; CH=3600
out() { "$HERE/run.sh" "$1" | awk '/^\[stdout\]/{f=1;next} /^\[stderr\]/{f=0} f'; }
meta="$(out "gzip -9 -c '$REMOTE' | base64 -w0 > /tmp/fetch.b64; wc -c < /tmp/fetch.b64; sha256sum '$REMOTE' | cut -c1-64")"
size="$(printf '%s\n' "$meta" | sed -n 1p | tr -d '[:space:]')"; sum="$(printf '%s\n' "$meta" | sed -n 2p | tr -d '[:space:]')"
echo "fetching $REMOTE: $size base64 chars in $(( (size + CH - 1) / CH )) pages"
: > "$LOCAL.b64"
off=1
while [ "$off" -le "$size" ]; do
  want=$(( size - off + 1 )); [ "$want" -gt "$CH" ] && want=$CH
  for try in 1 2 3; do  # Run Command sometimes returns an empty or short page: ask again instead of failing at the checksum
    page="$(out "cut -c${off}-$((off + CH - 1)) /tmp/fetch.b64" | tr -d '\n ')" || page=""
    [ "${#page}" -eq "$want" ] && break
    echo "page at $off: got ${#page}/$want chars (try $try)" >&2
  done
  printf '%s' "$page" >> "$LOCAL.b64"
  off=$((off + CH))
done
base64 -d < "$LOCAL.b64" | gunzip > "$LOCAL"
got="$(shasum -a 256 "$LOCAL" | cut -c1-64)"
if [ "$got" = "$sum" ]; then echo "ok: $LOCAL ($(wc -c < "$LOCAL") bytes, sha256 verified)"; else echo "CHECKSUM MISMATCH ($got vs $sum)"; exit 1; fi
rm -f "$LOCAL.b64"
