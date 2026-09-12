param([Parameter(ValueFromRemainingArguments=$true)][string[]]$WorkflowArgs)
$ErrorActionPreference = 'Stop'
$runtimeConfig = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config/runtime.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$env:PYTHONUTF8 = '1'
& $runtimeConfig.python -X utf8 (Join-Path $PSScriptRoot 'workflow.py') @WorkflowArgs
exit $LASTEXITCODE

