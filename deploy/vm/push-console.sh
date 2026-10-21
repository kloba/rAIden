#!/usr/bin/env bash
# Ship the console's code to the lab VM through Azure Run Command (no inbound ports) and install it into the lab's
# virtual environment (deploy/vm/console-lab.sh creates it). The web UI is not shipped: the live checks use the API.
#   deploy/vm/push-console.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../.."
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
COPYFILE_DISABLE=1 tar -c -z -f "$TMP/src.tgz" --no-xattrs --exclude '__pycache__' --exclude '*.pyc' --exclude 'raiden/console/static' \
  raiden tests/live pyproject.toml README.md LICENSE
base64 < "$TMP/src.tgz" | tr -d '\n' > "$TMP/src.b64"
echo "payload: $(wc -c < "$TMP/src.b64") base64 characters"
split -b 45000 "$TMP/src.b64" "$TMP/chunk."
first=1
for c in "$TMP"/chunk.*; do
  { [ "$first" = 1 ] && echo ': > /tmp/raiden-console-src.b64'; printf "printf '%%s' '%s' >> /tmp/raiden-console-src.b64\n" "$(cat "$c")"; } > "$TMP/script.sh"
  "$HERE/run.sh" -f "$TMP/script.sh" > /dev/null
  first=0
done
"$HERE/run.sh" "set -e; rm -rf /tmp/raiden-console-src; mkdir -p /tmp/raiden-console-src; base64 -d /tmp/raiden-console-src.b64 | tar xz -C /tmp/raiden-console-src;
  /opt/raiden-lab/venv/bin/pip install -q '/tmp/raiden-console-src[console]' 2>&1 | tail -5; /opt/raiden-lab/venv/bin/raiden --help | head -3"
