#!/usr/bin/env bash
# Run a shell script on the lab VM as root through the Azure VM agent (no inbound ports needed).
#   deploy/vm/run.sh 'systemctl status raiden --no-pager'
#   deploy/vm/run.sh -f path/to/script.sh
# Output is limited to ~4 KB by the platform: tail big logs, do not cat them.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/../azure/state.env"
if [ "${1:-}" = "-f" ]; then scripts="@$2"; else scripts="$1"; fi
az vm run-command invoke -g "$RG" -n "$VM" --subscription "$SUB" --command-id RunShellScript \
  --scripts "$scripts" --query "value[0].message" -o tsv
