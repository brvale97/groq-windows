$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
$python = & ".\bootstrap.ps1" -Profile runtime | Select-Object -Last 1
& $python .\app.py
exit $LASTEXITCODE
