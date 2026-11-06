# Build the PorxPy Windows installer (v0.145.0).
#
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# Produces dist\PorxPy-Setup.exe from the current checkout (a fixed name
# since v0.147.2, so it is a permanent release download -- see porxpy.iss):
#   1. PyInstaller freezes main.py and its packages into dist\PorxPy\
#      (packaging\porxpy.spec says what ships beside the code).
#   2. Inno Setup wraps that folder into one installer
#      (packaging\porxpy.iss).
#
# Needs, once per build machine:
#   pip install -r packaging\requirements-build.txt   (in the app's venv)
#   winget install JRSoftware.InnoSetup
#
# The version is read from porxpy\__init__.py, the one place it lives.
# Run from anywhere; paths are taken relative to this script.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# The Python that runs the app: the project's venv if there is one.
$python = @("$root\.venv1\Scripts\python.exe", "$root\.venv\Scripts\python.exe") |
          Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) { $python = 'python' }

$init = Get-Content "$root\porxpy\__init__.py" -Raw
if ($init -notmatch 'VERSION\s*=\s*"([^"]+)"') { throw 'No VERSION in porxpy\__init__.py' }
$version = $Matches[1]
Write-Host "PorxPy $version - building with $python"

& $python -m PyInstaller "$PSScriptRoot\porxpy.spec" --noconfirm `
    --distpath "$root\dist" --workpath "$root\build"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw 'Inno Setup 6 not found - winget install JRSoftware.InnoSetup' }

& $iscc "/DAppVersion=$version" "$PSScriptRoot\porxpy.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed ($LASTEXITCODE)" }

Write-Host ""
Write-Host "Installer: $root\dist\PorxPy-Setup.exe (v$version)"
