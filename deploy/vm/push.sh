#!/usr/bin/env bash
# Ship the working tree to the lab VM through Azure Run Command (no inbound ports) and run install.sh there.
#   deploy/vm/push.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../.."
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
COPYFILE_DISABLE=1 tar -c -z -f "$TMP/src.tgz" --no-xattrs --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache' \
  raiden arena .claude checks.d tests deploy/vm/install.sh deploy/vm/raiden.service deploy/vm/raiden.env.example
base64 < "$TMP/src.tgz" | tr -d '\n' > "$TMP/src.b64"
echo "payload: $(wc -c < "$TMP/src.b64") base64 characters"
split -b 45000 "$TMP/src.b64" "$TMP/chunk."
first=1
for c in "$TMP"/chunk.*; do
  { [ "$first" = 1 ] && echo ': > /tmp/raiden-src.b64'; printf "printf '%%s' '%s' >> /tmp/raiden-src.b64\n" "$(cat "$c")"; } > "$TMP/script.sh"
  "$HERE/run.sh" -f "$TMP/script.sh" > /dev/null
  first=0
done
"$HERE/run.sh" 'set -e; base64 -d /tmp/raiden-src.b64 > /tmp/raiden-src.tgz; rm -rf /tmp/raiden-src; mkdir -p /tmp/raiden-src; tar xzf /tmp/raiden-src.tgz -C /tmp/raiden-src; bash /tmp/raiden-src/deploy/vm/install.sh /tmp/raiden-src 2>&1 | tail -15'
