#!/usr/bin/env bash
# Install rAIden on the lab VM from an unpacked source tree. Run as root:  sudo bash install.sh /tmp/raiden-src
# Idempotent. Code is root-owned (the agent cannot edit its own guardrails); skills and checks.d are agent-writable.
set -euo pipefail
SRC="${1:-/tmp/raiden-src}"
[ -d "$SRC/raiden" ] || { echo "no raiden/ package under $SRC"; exit 1; }

install -d -o root -g root -m 755 /opt/raiden /opt/arena /etc/raiden
install -d -o root -g root -m 700 /var/lib/arena-private /var/lib/arena-private/scenarios
install -d -o arena -g arena -m 750 /var/lib/arena

rsync -a --delete "$SRC/raiden/" /opt/raiden/raiden/
find /opt/raiden/raiden -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
chown -R root:root /opt/raiden/raiden && chmod -R go-w /opt/raiden/raiden

# pristine copy: the arena resets skills/checks from here between dry runs
install -d -o root -g root -m 755 /opt/raiden-src
rsync -a --delete "$SRC/.claude/" /opt/raiden-src/.claude/
rsync -a --delete "$SRC/checks.d/" /opt/raiden-src/checks.d/
rsync -a "$SRC/.claude/" /opt/raiden/.claude/
rsync -a "$SRC/checks.d/" /opt/raiden/checks.d/
chown -R raiden:raiden /opt/raiden/.claude /opt/raiden/checks.d
chmod +x /opt/raiden/checks.d/*.sh /opt/raiden/.claude/skills/*/scripts/* 2>/dev/null || true

# arena: the oracle is visible to the agent, the referee and the attacks are not
install -m 755 -o root -g root "$SRC/tests/chaos/ledger.py" /opt/arena/ledger.py
install -m 700 -o root -g root "$SRC/tests/chaos/battle.py" /var/lib/arena-private/battle.py
rsync -a --delete "$SRC/tests/chaos/scenarios/" /var/lib/arena-private/scenarios/
chown -R root:root /var/lib/arena-private
chmod -R go-rwx /var/lib/arena-private

install -m 644 "$SRC/deploy/vm/raiden.service" /etc/systemd/system/raiden.service
install -m 644 "$SRC/deploy/vm/raiden-fortify.service" /etc/systemd/system/raiden-fortify.service
install -m 644 "$SRC/deploy/vm/pg-standby.service" /etc/systemd/system/pg-standby.service
if [ ! -f /etc/raiden/raiden.env ]; then
  echo "NOTE: /etc/raiden/raiden.env is missing - create it from deploy/vm/raiden.env.example"
fi
[ -f /etc/raiden/raiden.env ] && chown root:root /etc/raiden/raiden.env && chmod 640 /etc/raiden/raiden.env
systemctl daemon-reload
if [ -f /etc/raiden/raiden.env ]; then systemctl enable raiden >/dev/null 2>&1 || true; fi
echo "INSTALL_DONE $(date -Is)"
