#!/usr/bin/env bash
# Provision the rAIden battle lab on Azure: a resource group, an Azure OpenAI (Foundry) account with a model
# deployment (RAIDEN_MODEL, default gpt-5.5; the GPT-6 family, e.g. gpt-6.1-sol, is spoken to through the Responses API)
# and one Ubuntu 24.04 VM with a system-assigned managed identity that may call the model with Entra ID. No API keys
# anywhere and NO inbound ports: the VM is driven through Azure Run Command (deploy/vm/run.sh). The VM has a public IP
# only for outbound traffic (apt, GitHub, the model endpoint).
#
#   RAIDEN_SUB=<subscription-id> ./deploy/azure/up.sh
#
# State (names only, no secrets) is written to deploy/azure/state.env for down.sh and the deploy scripts.
set -euo pipefail

SUB="${RAIDEN_SUB:?set RAIDEN_SUB to the subscription id}"
LOC="${RAIDEN_LOC:-swedencentral}"
STAMP="${RAIDEN_STAMP:-$(date +%Y%m%d)}"
RG="${RAIDEN_RG:-rg-raiden-lab-$STAMP}"
VM="${RAIDEN_VM:-vm-raiden}"
VM_SIZE="${RAIDEN_VM_SIZE:-Standard_D4s_v5}"
ADMIN="${RAIDEN_ADMIN:-azureuser}"
MODEL="${RAIDEN_MODEL:-gpt-5.5}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HERE/state.env"

azs() { az "$@" --subscription "$SUB"; }
log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }

[ -f "$STATE" ] && . "$STATE"
FEATURE_REGISTERED_BY_UP="${FEATURE_REGISTERED_BY_UP:-0}"
write_state() {  # names only, no secrets; written early so that down.sh works even if a later step fails
  cat > "$STATE" <<EOF
SUB=$SUB
LOC=$LOC
RG=$RG
VM=$VM
ACCT=$ACCT
ADMIN=$ADMIN
FEATURE_REGISTERED_BY_UP=$FEATURE_REGISTERED_BY_UP
EOF
}

log "resource group"
azs group create -n "$RG" -l "$LOC" \
  --tags purpose=raiden-lab app=raiden "delete-after=$(date -v+4d +%Y-%m-%d 2>/dev/null || date -d '+4 days' +%Y-%m-%d)" \
  -o none
# a re-run reuses the account that is already in the resource group instead of creating another one
EXISTING="$(azs cognitiveservices account list -g "$RG" --query "[?kind=='AIServices'].name | [0]" -o tsv 2>/dev/null || true)"
ACCT="${ACCT:-${EXISTING:-raiden-foundry-$((RANDOM % 9000 + 1000))$((RANDOM % 9000 + 1000))}}"
log "subscription=$SUB location=$LOC rg=$RG vm=$VM($VM_SIZE) account=$ACCT model=$MODEL"
write_state

log "Azure OpenAI (AIServices) account $ACCT"
if ! azs cognitiveservices account show -g "$RG" -n "$ACCT" -o none 2>/dev/null; then
  azs cognitiveservices account create -g "$RG" -n "$ACCT" -l "$LOC" \
    --kind AIServices --sku S0 --custom-domain "$ACCT" --yes -o none
fi
ACCT_ID="$(azs cognitiveservices account show -g "$RG" -n "$ACCT" --query id -o tsv)"

log "deployment $MODEL (GlobalStandard)"
if azs cognitiveservices account deployment show -g "$RG" -n "$ACCT" --deployment-name "$MODEL" -o none 2>/dev/null; then
  log "deployment $MODEL already exists"
else
  VERSION="$(azs cognitiveservices model list -l "$LOC" --query "[?model.name=='$MODEL' && model.format=='OpenAI'].model.version | [0]" -o tsv)"
  [ -n "$VERSION" ] || { log "no $MODEL model offered in $LOC for this subscription"; exit 1; }
  for cap in 150 100 50; do  # thousand tokens per minute; take the biggest the quota allows
    if azs cognitiveservices account deployment create -g "$RG" -n "$ACCT" --deployment-name "$MODEL" \
        --model-name "$MODEL" --model-version "$VERSION" --model-format OpenAI --sku-name GlobalStandard --sku-capacity "$cap" -o none 2>/tmp/raiden-deploy.err; then
      log "deployed $MODEL $VERSION with capacity $cap"; break
    fi
    log "capacity $cap refused: $(tail -c 200 /tmp/raiden-deploy.err)"
  done
fi

# This sandbox subscription refuses to create the VM's public IP (outbound only: apt, GitHub, the model endpoint)
# unless the Microsoft.Network feature below is registered. up.sh registers it when needed and records that in
# state.env, so down.sh can unregister it again and leave the subscription as it found it.
FEATURE=AllowBringYourOwnPublicIpAddress
FSTATE="$(azs feature show --namespace Microsoft.Network --name "$FEATURE" --query properties.state -o tsv 2>/dev/null || echo Unknown)"
if [ "$FSTATE" != "Registered" ]; then
  log "registering Microsoft.Network/$FEATURE (was: $FSTATE)"
  azs feature register --namespace Microsoft.Network --name "$FEATURE" -o none
  FEATURE_REGISTERED_BY_UP=1
  write_state
  for _ in $(seq 1 90); do
    FSTATE="$(azs feature show --namespace Microsoft.Network --name "$FEATURE" --query properties.state -o tsv)"
    [ "$FSTATE" = "Registered" ] && break
    sleep 10
  done
  [ "$FSTATE" = "Registered" ] || { log "feature still $FSTATE after 15 minutes"; exit 1; }
  azs provider register --namespace Microsoft.Network -o none
fi

log "VM $VM (managed identity, no inbound ports)"
if ! azs vm show -g "$RG" -n "$VM" -o none 2>/dev/null; then
  # az vm create insists on an SSH key; it is thrown away right after because nothing can ever connect to port 22
  KEYDIR="$(mktemp -d)"; trap 'rm -rf "$KEYDIR"' EXIT
  ssh-keygen -q -t ed25519 -N '' -C raiden-lab-unused -f "$KEYDIR/key"
  azs vm create -g "$RG" -n "$VM" -l "$LOC" --image Ubuntu2404 --size "$VM_SIZE" \
    --admin-username "$ADMIN" --ssh-key-values "$KEYDIR/key.pub" \
    --assign-identity '[system]' --public-ip-sku Standard \
    --os-disk-size-gb 128 --storage-sku Premium_LRS --nsg-rule NONE -o none
fi

PRINCIPAL="$(azs vm show -g "$RG" -n "$VM" --query identity.principalId -o tsv)"
log "role: Cognitive Services OpenAI User for the VM identity on the account"
azs role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" --scope "$ACCT_ID" -o none || log "WARN: role assignment failed or exists"

write_state

log "agent environment file on the VM"
ENVFILE="RAIDEN_AOAI_ENDPOINT=https://$ACCT.openai.azure.com/
RAIDEN_AOAI_DEPLOYMENT=$MODEL
RAIDEN_AOAI_API_VERSION=2025-04-01-preview
RAIDEN_AOAI_API=$( [[ "$MODEL" == gpt-6* ]] && echo responses || echo chat )
RAIDEN_REASONING_EFFORT=medium
RAIDEN_MAX_TURNS=80
RAIDEN_INCIDENT_TIMEOUT_S=1500
RAIDEN_MAX_WAIT_S=15
RAIDEN_QUIET_S=6"
azs vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts "install -d -m 755 /etc/raiden; printf '%s\n' '$(printf '%s' "$ENVFILE" | sed "s/'/'\\\\''/g")' > /etc/raiden/raiden.env; chmod 640 /etc/raiden/raiden.env; cat /etc/raiden/raiden.env" \
  --query "value[0].message" -o tsv | head -3 || true

log "done (state: $STATE). Next: deploy/vm/run.sh -f deploy/vm/bootstrap.sh && deploy/vm/push.sh"
