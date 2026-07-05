$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$source = Join-Path $PSScriptRoot "dist\GroqInsertDictation.exe"
if (-not (Test-Path -LiteralPath $source)) {
    & ".\build-app.ps1"
}

$installDir = Join-Path $env:LOCALAPPDATA "Programs\GroqInsertDictation"
$target = Join-Path $installDir "GroqInsertDictation.exe"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item -LiteralPath $source -Destination $target -Force

$startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\GroqInsertDictation.cmd"
New-Item -ItemType Directory -Force -Path (Split-Path $startup) | Out-Null
Set-Content -LiteralPath $startup -Value "@echo off`r`nstart `"`" `"$target`"`r`n" -Encoding ASCII

Write-Host "Geinstalleerd: $target"
Write-Host "Autostart: $startup"
