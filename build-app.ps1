$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m PyInstaller --noconsole --onefile --name GroqInsertDictation app.py

Write-Host ""
Write-Host "Gebouwd: $PSScriptRoot\dist\GroqInsertDictation.exe"
