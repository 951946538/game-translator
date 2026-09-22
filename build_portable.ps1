# build_portable.ps1 - one-shot portable build (GPU edition)
# Usage: powershell -ExecutionPolicy Bypass -File build_portable.ps1
# Output: dist\GameTranslator\ (zip the folder to distribute, run GameTranslator.bat)
#
# Why portable instead of PyInstaller: paddle's dynamic imports break static
# analysis (missing-module whack-a-mole). A full runtime + all deps is final.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$dist = Join-Path $root "dist\GameTranslator"

# Read base python location from venv config
$cfg = Get-Content (Join-Path $root ".venv\pyvenv.cfg") -Encoding UTF8
$basePy = ($cfg | Where-Object { $_ -match '^home\s*=' }) -replace '^home\s*=\s*', ''
if (-not (Test-Path (Join-Path $basePy "pythonw.exe"))) { throw "Base python not found: $basePy" }
$venvSite = Join-Path $root ".venv\Lib\site-packages"
Write-Output "Base python: $basePy"

# Clean & recreate output dir
if (Test-Path $dist) { [System.IO.Directory]::Delete($dist, $true) }
New-Item -ItemType Directory -Force -Path $dist | Out-Null

# 1. Program sources
Copy-Item (Join-Path $root "main.py") $dist
Copy-Item (Join-Path $root "app") (Join-Path $dist "app") -Recurse

# 2. Full python runtime (stdlib + tcl/tk)
Write-Output "Copying python runtime..."
robocopy $basePy (Join-Path $dist "runtime") /E /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy python failed" }

# 3. All dependencies (full copy - no more missing modules, ever)
Write-Output "Copying dependencies (~4GB, takes a few minutes)..."
$dstSite = Join-Path $dist "runtime\Lib\site-packages"
robocopy $venvSite $dstSite /E /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy deps failed" }

# Remove packaging-only tools (friends don't need them)
Get-ChildItem $dstSite -Directory | Where-Object {
    $_.Name -match '^(pyinstaller|altgraph|pefile)(-[0-9].*)?$'
} | ForEach-Object { [System.IO.Directory]::Delete($_.FullName, $true) }

# 4. Launcher & readme from packaging templates
# (wildcard copy: PS1 read as ANSI garbles non-ASCII literals, so no hardcoded CJK names)
Get-ChildItem (Join-Path $root "packaging") -File | Copy-Item -Destination $dist

$size = [math]::Round((Get-ChildItem $dist -Recurse | Measure-Object Length -Sum).Sum / 1GB, 2)
Write-Output "Build OK: $dist ($size GB)"
Write-Output "Selftest: dist\GameTranslator\runtime\python.exe dist\GameTranslator\main.py --selftest"
