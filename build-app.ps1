param(
    # Also build the single-file bridge for updaters up to 0.1.27 (release builds).
    [switch]$Bridge
)
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
$python = & ".\bootstrap.ps1" -Profile build | Select-Object -Last 1

function Assert-NativeSuccess([string]$Context) {
    if ($LASTEXITCODE -ne 0) { throw "$Context is mislukt met exitcode $LASTEXITCODE." }
}

function Get-Sha256([string]$Path) {
    (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

$folder = ".\dist\GroqInsertDictation"
$zip = ".\dist\GroqInsertDictation-win64.zip"
$single = ".\dist\GroqInsertDictation.exe"
$manifest = ".\dist\GroqInsertDictation.build.json"
foreach ($path in @($folder, $zip, $single, $manifest)) {
    Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path ".\build" | Out-Null
& $python .\branding.py ".\build\GroqInsertDictation.ico"
Assert-NativeSuccess "App-icoon genereren"

$env:GROQ_BUILD_MODE = "onedir"
& $python -m PyInstaller --noconfirm --clean --distpath ".\dist" --workpath ".\build\onedir" ".\packaging\GroqInsertDictation.spec"
Assert-NativeSuccess "PyInstaller (mapbuild)"
if (-not (Test-Path -LiteralPath "$folder\GroqInsertDictation.exe") -or -not (Test-Path -LiteralPath "$folder\_internal")) {
    throw "PyInstaller rapporteerde succes, maar de mapbuild is onvolledig."
}

# License texts for Python, Qt for Python (LGPLv3) and the other bundled packages.
$licenses = Join-Path $folder "licenses"
New-Item -ItemType Directory -Force -Path $licenses | Out-Null
Copy-Item -LiteralPath ".\THIRD_PARTY_NOTICES.md" -Destination $licenses
Copy-Item -Path ".\packaging\licenses\*.txt" -Destination $licenses
& $python .\packaging\collect_licenses.py $licenses
Assert-NativeSuccess "Licentieteksten verzamelen"

if ($Bridge) {
    $env:GROQ_BUILD_MODE = "onefile"
    & $python -m PyInstaller --noconfirm --clean --distpath ".\dist" --workpath ".\build\onefile" ".\packaging\GroqInsertDictation.spec"
    Assert-NativeSuccess "PyInstaller (single-file bridge)"
    if (-not (Test-Path -LiteralPath $single)) { throw "De single-file bridge ontbreekt." }
}
Remove-Item Env:\GROQ_BUILD_MODE -ErrorAction SilentlyContinue

# A flat zip: GroqInsertDictation.exe, _internal and licenses at the top level.
& $python .\packaging\make_zip.py $folder $zip
Assert-NativeSuccess "Updatepakket maken"

$pythonDescription = (& $python -c "import sys; print(sys.version)").Trim()
Assert-NativeSuccess "Pythonversie uitlezen"
$pysideVersion = (& $python -c "import PySide6; print(PySide6.__version__)").Trim()
Assert-NativeSuccess "PySide6-versie uitlezen"
$appVersion = (& $python .\app.py --version).Trim()
Assert-NativeSuccess "App-versie uitlezen"

$sources = [ordered]@{}
foreach ($file in @(
    "app.py", "audio_player.py", "branding.py", "config_store.py", "dictation_core.py", "engine.py", "history.py",
    "hotkeys.py", "microphone_test.py", "settings_ui.py", "ui_theme.py", "updater.py", "windows_services.py",
    "requirements.txt", "requirements-build.txt", "bootstrap.ps1", "build-app.ps1",
    "packaging\GroqInsertDictation.spec", "packaging\collect_licenses.py", "packaging\make_zip.py", "THIRD_PARTY_NOTICES.md",
    "install-app.ps1"
)) {
    $sources[$file] = Get-Sha256 $file
}
$folderBytes = (Get-ChildItem -LiteralPath $folder -Recurse -File | Measure-Object -Property Length -Sum).Sum
$buildInfo = [ordered]@{
    version = $appVersion
    python = $pythonDescription
    pyside6 = $pysideVersion
    layout = "onedir"
    sources = $sources
    exe_sha256 = Get-Sha256 "$folder\GroqInsertDictation.exe"
    folder_bytes = $folderBytes
    zip_sha256 = Get-Sha256 $zip
    zip_bytes = (Get-Item -LiteralPath $zip).Length
}
if ($Bridge) {
    $buildInfo["onefile_sha256"] = Get-Sha256 $single
    $buildInfo["onefile_bytes"] = (Get-Item -LiteralPath $single).Length
}
# Without a byte order mark, so every JSON reader (and the updater) accepts it.
[IO.File]::WriteAllText((Join-Path $PSScriptRoot $manifest), ($buildInfo | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
Copy-Item -LiteralPath $manifest -Destination (Join-Path $folder "GroqInsertDictation.build.json")

Write-Host ""
Write-Host "Gebouwd: $PSScriptRoot\dist\GroqInsertDictation\GroqInsertDictation.exe"
Write-Host "Updatepakket: $PSScriptRoot\dist\GroqInsertDictation-win64.zip"
if ($Bridge) { Write-Host "Bridge: $PSScriptRoot\dist\GroqInsertDictation.exe" }
