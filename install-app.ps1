$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$source = Join-Path $PSScriptRoot "dist\GroqInsertDictation.exe"
$buildInfoPath = Join-Path $PSScriptRoot "dist\GroqInsertDictation.build.json"
$expectedBuild = [ordered]@{
    app_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "app.py").Hash.ToLowerInvariant()
    core_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "dictation_core.py").Hash.ToLowerInvariant()
    requirements_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "requirements.txt").Hash.ToLowerInvariant()
    build_requirements_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "requirements-build.txt").Hash.ToLowerInvariant()
    bootstrap_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "bootstrap.ps1").Hash.ToLowerInvariant()
    build_script_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "build-app.ps1").Hash.ToLowerInvariant()
}
$needsBuild = -not (Test-Path -LiteralPath $source) -or -not (Test-Path -LiteralPath $buildInfoPath)
if (-not $needsBuild) {
    try {
        $actualBuild = Get-Content -LiteralPath $buildInfoPath -Raw | ConvertFrom-Json
        foreach ($property in $expectedBuild.Keys) {
            if ($actualBuild.$property -ne $expectedBuild[$property]) {
                $needsBuild = $true
                break
            }
        }
        if (-not $needsBuild) {
            $currentExeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash.ToLowerInvariant()
            if ($actualBuild.exe_sha256 -ne $currentExeHash) {
                $needsBuild = $true
            }
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
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item -LiteralPath $source -Destination $target -Force

$startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\GroqInsertDictation.cmd"
New-Item -ItemType Directory -Force -Path (Split-Path $startup) | Out-Null
Set-Content -LiteralPath $startup -Value "@echo off`r`nstart `"`" `"$target`"`r`n" -Encoding ASCII

Write-Host "Geinstalleerd: $target"
Write-Host "Autostart: $startup"
