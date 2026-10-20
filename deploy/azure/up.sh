#!/usr/bin/env bash
# Provision the rAIden battle lab: resource group, Microsoft Foundry (Claude deployments),
# one Ubuntu VM with a system-assigned managed identity, and the role assignment that lets
# the VM call Claude with Entra ID (no API keys on disk).
#
#   RAIDEN_SUB=<subscription-id> ./deploy/azure/up.sh
#
# State (names only, no secrets) is written to deploy/azure/state.env for down.sh and the deploy scripts.
set -euo pipefail

SUB="${RAIDEN_SUB:?set RAIDEN_SUB to the subscription id}"
LOC="${RAIDEN_LOC:-swedencentral}"
STAMP="${RAIDEN_STAMP:-$(date +%Y%m%d)}"
RG="${RAIDEN_RG:-rg-raiden-pgconf-$STAMP}"
VM="${RAIDEN_VM:-vm-raiden}"
VM_SIZE="${RAIDEN_VM_SIZE:-Standard_D4s_v5}"
ADMIN="${RAIDEN_ADMIN:-azureuser}"
SSH_KEY="${RAIDEN_SSH_KEY:-$HOME/.ssh/raiden_lab}"
OPUS_VERSION="${RAIDEN_OPUS_VERSION:-2}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HERE/state.env"

azs() { az "$@" --subscription "$SUB"; }
log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }

[ -f "$STATE" ] && . "$STATE"
ACCT="${ACCT:-raiden-foundry-$((RANDOM % 9000 + 1000))$((RANDOM % 9000 + 1000))}"

MYIP="$(curl -s https://api.ipify.org)"
log "subscription=$SUB location=$LOC rg=$RG vm=$VM($VM_SIZE) foundry=$ACCT my_ip=$MYIP"

log "resource group"
azs group create -n "$RG" -l "$LOC" \
  --tags purpose=pgconf-lightning-talk app=raiden "delete-after=$(date -v+4d +%Y-%m-%d 2>/dev/null || date -d '+4 days' +%Y-%m-%d)" \
  -o none

log "Foundry account (AIServices) $ACCT"
if ! azs cognitiveservices account show -g "$RG" -n "$ACCT" -o none 2>/dev/null; then
  azs cognitiveservices account create -g "$RG" -n "$ACCT" -l "$LOC" \
    --kind AIServices --sku S0 --custom-domain "$ACCT" --yes -o none
fi
ACCT_ID="$(azs cognitiveservices account show -g "$RG" -n "$ACCT" --query id -o tsv)"

# Claude on Foundry requires a legal attestation (modelProviderData). Per the Azure docs it accepts the
# Anthropic commercial terms ON BEHALF OF the named organization, so the account owner must supply it:
#   MPD_ORG="<legal entity name>" MPD_COUNTRY=<ISO-3166 alpha-2> MPD_INDUSTRY=<e.g. technology>
# The property is not in the public ARM spec yet; it works with api-version 2026-05-01.
deploy_model() { # name version capacity
  local name="$1" ver="$2" cap="$3" body
  log "deployment $name (version $ver, capacity $cap)"
  body="$(python3 - "$name" "$ver" "$cap" <<'PY'
import json, os, sys
name, ver, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])
print(json.dumps({
  "sku": {"name": "GlobalStandard", "capacity": cap},
  "properties": {
    "model": {"format": "Anthropic", "name": name, "version": ver},
    "modelProviderData": {
      "organizationName": os.environ["MPD_ORG"],
      "countryCode": os.environ["MPD_COUNTRY"],
      "industry": os.environ["MPD_INDUSTRY"],
    },
  },
}))
PY
)"
  azs rest --method put \
    --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/$ACCT/deployments/$name?api-version=2026-05-01" \
    --body "$body" --query "{name:name, state:properties.provisioningState}" -o tsv \
    || log "WARN: deployment of $name failed (see error above)"
}
if [ -n "${MPD_ORG:-}" ] && [ -n "${MPD_COUNTRY:-}" ] && [ -n "${MPD_INDUSTRY:-}" ]; then
  deploy_model claude-opus-5-5 "$OPUS_VERSION" 40
  deploy_model claude-sonnet-5-5 2 40
  deploy_model claude-haiku-5-5 2 40
else
  log "SKIPPING Claude deployments: MPD_ORG / MPD_COUNTRY / MPD_INDUSTRY not set (legal attestation must come from the account owner)"
fi

log "VM $VM"
if ! azs vm show -g "$RG" -n "$VM" -o none 2>/dev/null; then
  azs vm create -g "$RG" -n "$VM" -l "$LOC" --image Ubuntu2404 --size "$VM_SIZE" \
    --admin-username "$ADMIN" --ssh-key-values "${SSH_KEY}.pub" \
    --assign-identity '[system]' --public-ip-sku Standard \
    --os-disk-size-gb 128 --storage-sku Premium_LRS --nsg-rule NONE -o none
fi

NSG="${VM}NSG"
log "NSG: ssh only from $MYIP"
azs network nsg rule create -g "$RG" --nsg-name "$NSG" -n ssh-from-operator --priority 100 \
  --access Allow --protocol Tcp --direction Inbound --source-address-prefixes "$MYIP/32" \
  --destination-port-ranges 22 -o none

PRINCIPAL="$(azs vm show -g "$RG" -n "$VM" --query identity.principalId -o tsv)"
log "role: Cognitive Services User for VM identity $PRINCIPAL on the Foundry account"
azs role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services User" --scope "$ACCT_ID" -o none || log "WARN: role assignment failed or exists"

IP="$(azs vm show -d -g "$RG" -n "$VM" --query publicIps -o tsv)"
cat > "$STATE" <<EOF
SUB=$SUB
LOC=$LOC
RG=$RG
VM=$VM
ACCT=$ACCT
ADMIN=$ADMIN
IP=$IP
SSH_KEY=$SSH_KEY
EOF
log "done. ssh -i $SSH_KEY $ADMIN@$IP   (state: $STATE)"
