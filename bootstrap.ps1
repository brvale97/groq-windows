param(
    [ValidateSet("runtime", "build")]
    [string]$Profile = "runtime"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Assert-NativeSuccess([string]$Context) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Context is mislukt met exitcode $LASTEXITCODE."
    }
}

$requirementsFile = if ($Profile -eq "build") { "requirements-build.txt" } else { "requirements.txt" }
$python = ".\.venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.13 -m venv .venv
        Assert-NativeSuccess "Python 3.13 virtual environment maken"
    } else {
        & python -m venv .venv
        Assert-NativeSuccess "Virtual environment maken"
    }
}

$pythonVersion = (& $python -c "import sys; print(sys.version)").Trim()
Assert-NativeSuccess "Pythonversie uitlezen"
if (-not $pythonVersion.StartsWith("3.13.")) {
    throw "Deze build is vastgezet op Python 3.13; gevonden: $pythonVersion"
}
$requirementsContent = @(
    $pythonVersion
    (Get-Content -LiteralPath "requirements.txt" -Raw)
    if ($Profile -eq "build") { Get-Content -LiteralPath "requirements-build.txt" -Raw }
) -join "`n"
$hasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $hashBytes = $hasher.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($requirementsContent))
} finally {
    $hasher.Dispose()
}
$requirementsHash = [System.BitConverter]::ToString($hashBytes).Replace("-", "").ToLowerInvariant()
$stamp = ".venv\.requirements-$Profile.sha256"
$installedHash = if (Test-Path -LiteralPath $stamp) { (Get-Content -LiteralPath $stamp -Raw).Trim() } else { "" }

$needsInstall = $installedHash -ne $requirementsHash
if (-not $needsInstall) {
    & $python -m pip check
    $needsInstall = $LASTEXITCODE -ne 0
}

if ($needsInstall) {
    & $python -m pip install --upgrade pip
    Assert-NativeSuccess "pip bijwerken"
    & $python -m pip install -r $requirementsFile
    Assert-NativeSuccess "Dependencies installeren"
    & $python -m pip check
    Assert-NativeSuccess "Dependencies controleren"
    Set-Content -LiteralPath $stamp -Value $requirementsHash -Encoding ASCII
}

Write-Output $python
