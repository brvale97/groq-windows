$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
$python = & ".\bootstrap.ps1" -Profile build | Select-Object -Last 1

Remove-Item -LiteralPath ".\dist\GroqInsertDictation.exe" -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath ".\dist\GroqInsertDictation.build.json" -Force -ErrorAction SilentlyContinue
& $python -m PyInstaller --noconsole --onefile --name GroqInsertDictation app.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is mislukt met exitcode $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath ".\dist\GroqInsertDictation.exe")) {
    throw "PyInstaller rapporteerde succes, maar de Windows-app ontbreekt."
}

$pythonDescription = (& $python -c "import sys; print(sys.version)").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Pythonversie uitlezen is mislukt met exitcode $LASTEXITCODE."
}
$appVersion = (& $python .\app.py --version).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "App-versie uitlezen is mislukt met exitcode $LASTEXITCODE."
}

$buildInfo = [ordered]@{
    app_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "app.py").Hash.ToLowerInvariant()
    core_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "dictation_core.py").Hash.ToLowerInvariant()
    requirements_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "requirements.txt").Hash.ToLowerInvariant()
    build_requirements_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "requirements-build.txt").Hash.ToLowerInvariant()
    bootstrap_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "bootstrap.ps1").Hash.ToLowerInvariant()
    build_script_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "build-app.ps1").Hash.ToLowerInvariant()
    python = $pythonDescription
    version = $appVersion
    exe_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath ".\dist\GroqInsertDictation.exe").Hash.ToLowerInvariant()
}
$buildInfo | ConvertTo-Json | Set-Content -LiteralPath ".\dist\GroqInsertDictation.build.json" -Encoding UTF8

Write-Host ""
Write-Host "Gebouwd: $PSScriptRoot\dist\GroqInsertDictation.exe"
