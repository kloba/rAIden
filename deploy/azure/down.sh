#!/usr/bin/env bash
# Tear the lab down completely: delete the resource group, purge the soft-deleted Foundry account,
# and verify that nothing is left behind.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/state.env"
azs() { az "$@" --subscription "$SUB"; }
log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }

log "deleting resource group $RG (this also removes the VM, disks, NIC, public IP, NSG and role assignments)"
azs group delete -n "$RG" --yes

log "purging the soft-deleted Foundry account $ACCT"
azs cognitiveservices account purge --location "$LOC" --resource-group "$RG" --name "$ACCT" -o none 2>&1 || true

log "verification"
echo -n "resource group exists: "; azs group exists -n "$RG"
echo -n "soft-deleted Foundry accounts named $ACCT: "
azs cognitiveservices account list-deleted --query "[?name=='$ACCT'] | length(@)" -o tsv
echo -n "leftover resources tagged app=raiden: "
azs resource list --tag app=raiden --query "length(@)" -o tsv

if [ -f "${SSH_KEY:-}" ]; then
  log "removing the throwaway SSH key and known_hosts entry"
  ssh-keygen -R "$IP" >/dev/null 2>&1 || true
  rm -f "$SSH_KEY" "${SSH_KEY}.pub"
fi
log "teardown complete"
