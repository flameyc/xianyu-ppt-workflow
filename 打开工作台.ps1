param([switch]$NoOpen)
$ErrorActionPreference = 'Stop'
$runtimeConfig = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config/runtime.json') -Raw -Encoding UTF8 | ConvertFrom-Json
& $runtimeConfig.python -X utf8 (Join-Path $PSScriptRoot 'workflow.py') dashboard
if ($LASTEXITCODE -ne 0) { throw '工作台生成失败，请查看上方错误' }
if (-not $NoOpen) { Invoke-Item -LiteralPath (Join-Path $PSScriptRoot '工作台.html') }
