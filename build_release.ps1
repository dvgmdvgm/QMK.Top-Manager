<#!
.SYNOPSIS
Build a clean, public one-file Windows EXE for QMK.Top Manager for SK75 TMR.

.DESCRIPTION
The application writes user state only to LocalAppData at runtime.  This
script does not package profiles_config.json, logs, virtual environments or
any user configuration from this checkout.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Create .venv first, then install requirements.txt and requirements-build.txt."
}

Push-Location $projectRoot
try {
    & $python -m PyInstaller --noconfirm --clean "QMK.Top Manager.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller exited with code $LASTEXITCODE."
    }
    $releaseExe = Join-Path $projectRoot "dist\QMK.Top Manager for SK75 TMR.exe"
    $checksumPath = "$releaseExe.sha256"
    $checksum = (Get-FileHash -LiteralPath $releaseExe -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $checksumPath -Value "$checksum  QMK.Top Manager for SK75 TMR.exe" -Encoding ascii -NoNewline
}
finally {
    Pop-Location
}

Write-Host "Built: $(Join-Path $projectRoot 'dist\QMK.Top Manager for SK75 TMR.exe')"
Write-Host "Checksum: $(Join-Path $projectRoot 'dist\QMK.Top Manager for SK75 TMR.exe.sha256')"
