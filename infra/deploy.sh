#!/usr/bin/env bash
# Deploy the abian-invoice-app infrastructure.
#
# Usage:
#   ./infra/deploy.sh <resource-group> [<params-file>]
#
# The resource group must already exist. Example:
#   az group create -n rg-abian-invoice -l westeurope
#   ./infra/deploy.sh rg-abian-invoice
#
# If no params file is given, defaults to infra/main.parameters.json.

set -euo pipefail

RG="${1:?resource group name required}"
PARAMS="${2:-infra/main.parameters.json}"

if [[ ! -f "$PARAMS" ]]; then
  echo "Parameters file not found: $PARAMS"
  echo "Copy infra/main.parameters.example.json → $PARAMS and fill in values."
  exit 1
fi

echo "Deploying to resource group: $RG"
echo "Parameters file: $PARAMS"

az deployment group create \
  --resource-group "$RG" \
  --template-file infra/main.bicep \
  --parameters "@$PARAMS" \
  --output table

echo
echo "--- Outputs ---"
az deployment group show \
  --resource-group "$RG" \
  --name "$(basename "${PARAMS%.*}")" \
  --query properties.outputs \
  --output json 2>/dev/null || \
  az deployment group list --resource-group "$RG" \
    --query "[?properties.provisioningState=='Succeeded'] | [0].properties.outputs" \
    --output json
