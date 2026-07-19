$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
$python = & ".\bootstrap.ps1" -Profile runtime | Select-Object -Last 1
& $python -c "import sounddevice as sd; print(sd.query_devices()); print('default', sd.default.device)"
exit $LASTEXITCODE
