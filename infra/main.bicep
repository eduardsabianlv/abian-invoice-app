// abian-invoice-app infrastructure
// Resources:
//   - Storage Account (Tables: secrets, companyMappings, manualLinks)
//   - Application Insights (logs, metrics)
//   - Static Web App (hosts SPA + Python Azure Functions)
//
// Deploy from the repo root:
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file infra/main.bicep \
//     --parameters @infra/main.parameters.json

@description('Base name used as a prefix for all resources. Lowercase, 3-14 chars.')
@minLength(3)
@maxLength(14)
param baseName string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Static Web App region. Must be one of the SWA-supported regions.')
@allowed([
  'westeurope'
  'northeurope'
  'eastus2'
  'westus2'
  'centralus'
  'eastasia'
])
param swaLocation string = 'westeurope'

@description('Static Web App SKU. Standard is required for linked Azure Functions.')
@allowed([
  'Free'
  'Standard'
])
param swaSku string = 'Standard'

@description('GitHub repository URL for SWA deployment source.')
param repositoryUrl string = ''

@description('GitHub branch to deploy from.')
param branch string = 'main'

// ----------------------------------------------------------------------------
// Storage Account
// ----------------------------------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: toLower('${baseName}stg${uniqueString(resourceGroup().id)}')
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource secretsTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'secrets'
}

resource mappingsTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'companyMappings'
}

resource linksTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'manualLinks'
}

// ----------------------------------------------------------------------------
// Application Insights
// ----------------------------------------------------------------------------

resource workspace 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${baseName}-logs'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${baseName}-ai'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    IngestionMode: 'LogAnalytics'
  }
}

// ----------------------------------------------------------------------------
// Static Web App (hosts SPA + linked Python Functions)
// ----------------------------------------------------------------------------

resource swa 'Microsoft.Web/staticSites@2023-12-01' = {
  name: '${baseName}-swa'
  location: swaLocation
  sku: {
    name: swaSku
    tier: swaSku
  }
  properties: {
    repositoryUrl: repositoryUrl
    branch: branch
    buildProperties: {
      appLocation: 'app'
      apiLocation: 'api'
      outputLocation: ''
    }
    stagingEnvironmentPolicy: 'Enabled'
  }
}

// App settings for the linked Functions runtime.
// AzureWebJobsStorage → used by our storage.py to detect Azure Tables.
// APPLICATIONINSIGHTS_CONNECTION_STRING → auto-wires Functions logs into AI.
//
// PAX8/Moneo secrets are NOT set here — they are managed through the app's own
// Iestatījumi UI, which writes to the "secrets" table. This keeps deploy
// recipes free of production credentials.
resource swaSettings 'Microsoft.Web/staticSites/config@2023-12-01' = {
  parent: swa
  name: 'appsettings'
  properties: {
    AzureWebJobsStorage: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storage.listKeys().keys[0].value}'
    APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.properties.ConnectionString
    FUNCTIONS_WORKER_RUNTIME: 'python'
  }
}

// ----------------------------------------------------------------------------
// Outputs
// ----------------------------------------------------------------------------

output staticWebAppName string = swa.name
output staticWebAppHostname string = swa.properties.defaultHostname
output storageAccountName string = storage.name
output appInsightsName string = appInsights.name

@description('The key needed by the GitHub Action to deploy this SWA. Get it with: az staticwebapp secrets list -n <name> --query properties.apiKey -o tsv')
output swaDeployTokenHint string = 'Run: az staticwebapp secrets list -n ${swa.name} --query properties.apiKey -o tsv'
