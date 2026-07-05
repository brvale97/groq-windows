$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

$stamp = ".venv\.requirements-installed"
$needsInstall = -not (Test-Path $stamp)

if (-not $needsInstall) {
    $needsInstall = (Get-Item "requirements.txt").LastWriteTime -gt (Get-Item $stamp).LastWriteTime
}

if ($needsInstall) {
    & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
    & ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
    New-Item -ItemType File -Force $stamp | Out-Null
}

& ".\.venv\Scripts\python.exe" .\app.py
