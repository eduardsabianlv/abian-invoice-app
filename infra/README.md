# Infrastructure

Bicep + scripts to deploy `abian-invoice-app` to Azure.

## What gets created

| Resource | Purpose |
|---|---|
| Storage Account (`<base>stg<hash>`) | Table Storage for secrets, company mappings, manual invoice links |
| Log Analytics workspace (`<base>-logs`) | Log backend for App Insights |
| Application Insights (`<base>-ai`) | Functions logs, metrics, live metrics |
| Static Web App (`<base>-swa`, Standard SKU) | Hosts SPA + Python Azure Functions |

Three tables are pre-created: `secrets`, `companyMappings`, `manualLinks`.

## Prerequisites

- Azure CLI (`az --version`) with an active subscription (`az login`)
- A resource group. Create one:
  ```
  az group create -n rg-abian-invoice -l westeurope
  ```
- Bicep is included with modern `az`; verify with `az bicep version`.

## First-time deploy

1. Copy the parameters file:
   ```
   cp infra/main.parameters.example.json infra/main.parameters.json
   ```
   Edit `baseName` and `repositoryUrl` at minimum. `baseName` must be 3-14 lowercase chars.

2. Deploy:
   ```
   ./infra/deploy.sh rg-abian-invoice
   ```
   This runs `az deployment group create` and prints outputs on success.

3. Get the Static Web App deploy token (needed once for GitHub Actions):
   ```
   az staticwebapp secrets list \
     -n <swa-name-from-output> \
     --query properties.apiKey -o tsv
   ```
   Save it as a repo secret named `AZURE_STATIC_WEB_APPS_API_TOKEN` (matches the existing workflow).

4. Push to `main` — the GitHub Action in `.github/workflows/azure-static-web-apps.yml` builds and deploys.

5. Open the site at `https://<swa-hostname>`. Go to **Iestatījumi** and enter PAX8 + Moneo credentials. They're written to the `secrets` table — never to source control.

## Re-deploy

Re-run `./infra/deploy.sh rg-abian-invoice` at any time. It's idempotent — Bicep computes the diff and updates only what changed.

## Where credentials live

- **PAX8 / Moneo API keys** — written by the **Iestatījumi** UI into the `secrets` table. Not in Bicep, not in GitHub secrets.
- **Azure Storage connection string** — auto-injected as `AzureWebJobsStorage` app setting by this Bicep; the backend's `storage.py` reads it and switches from JSON files to Tables.
- **SWA deploy token** — GitHub secret `AZURE_STATIC_WEB_APPS_API_TOKEN`.

## Local development

Nothing about this Bicep affects local dev. `swa start app` still runs against JSON files in `api/` unless you set `AZURE_TABLES_CONNECTION_STRING` in `api/local.settings.json` pointing at Azurite or a real storage account.

To use a real storage account locally:
```json
{
  "Values": {
    "AzureWebJobsStorage": "DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net"
  }
}
```

To use Azurite (emulator):
```
npm i -g azurite
azurite --silent --location ./.azurite --debug ./.azurite/debug.log
```
Then in `local.settings.json`:
```json
{
  "Values": {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true"
  }
}
```
