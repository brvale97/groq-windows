$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

function Get-Sha256([string]$Path) {
    (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

$folder = Join-Path $PSScriptRoot "dist\GroqInsertDictation"
$buildInfoPath = Join-Path $PSScriptRoot "dist\GroqInsertDictation.build.json"
$needsBuild = -not (Test-Path -LiteralPath "$folder\GroqInsertDictation.exe") -or -not (Test-Path -LiteralPath $buildInfoPath)
if (-not $needsBuild) {
    try {
        $actualBuild = Get-Content -LiteralPath $buildInfoPath -Raw | ConvertFrom-Json
        foreach ($property in $actualBuild.sources.PSObject.Properties) {
            if (-not (Test-Path -LiteralPath $property.Name) -or (Get-Sha256 $property.Name) -ne $property.Value) {
                $needsBuild = $true
                break
            }
        }
        if (-not $needsBuild -and $actualBuild.exe_sha256 -ne (Get-Sha256 "$folder\GroqInsertDictation.exe")) {
            $needsBuild = $true
        }
    } catch {
        $needsBuild = $true
    }
}
if ($needsBuild) {
    & ".\build-app.ps1"
}

$installDir = Join-Path $env:LOCALAPPDATA "Programs\GroqInsertDictation"
$target = Join-Path $installDir "GroqInsertDictation.exe"
$running = @(Get-CimInstance Win32_Process -Filter "Name = 'GroqInsertDictation.exe'" | Where-Object { $_.ExecutablePath -eq $target })
if ($running.Count -gt 0) {
    throw "Groq Insert Dictation draait nog. Sluit de app af via het systeemvak (Afsluiten) en start dit script opnieuw."
}

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
# Keep the previous version (single exe or folder build) until the new one is in place.
$backup = Join-Path $installDir ("backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
foreach ($name in @("GroqInsertDictation.exe", "_internal", "licenses", "GroqInsertDictation.build.json")) {
    $path = Join-Path $installDir $name
    if (Test-Path -LiteralPath $path) {
        New-Item -ItemType Directory -Force -Path $backup | Out-Null
        Move-Item -LiteralPath $path -Destination (Join-Path $backup $name)
    }
}
try {
    Copy-Item -Path "$folder\*" -Destination $installDir -Recurse -Force
} catch {
    foreach ($name in @("GroqInsertDictation.exe", "_internal", "licenses", "GroqInsertDictation.build.json")) {
        Remove-Item -LiteralPath (Join-Path $installDir $name) -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath (Join-Path $backup $name)) {
            Move-Item -LiteralPath (Join-Path $backup $name) -Destination (Join-Path $installDir $name)
        }
    }
    throw
}

$startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\GroqInsertDictation.cmd"
New-Item -ItemType Directory -Force -Path (Split-Path $startup) | Out-Null
Set-Content -LiteralPath $startup -Value "@echo off`r`nstart `"`" `"%LOCALAPPDATA%\Programs\GroqInsertDictation\GroqInsertDictation.exe`"`r`n" -Encoding ASCII

Write-Host "Geinstalleerd: $target"
if (Test-Path -LiteralPath $backup) { Write-Host "Vorige versie bewaard in: $backup" }
Write-Host "Autostart: $startup"
