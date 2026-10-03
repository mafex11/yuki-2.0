<#
.SYNOPSIS
  Build Yuki for Windows: PyInstaller onedir (packaging\dist\Yuki) and the Inno Setup installer
  (packaging\Output\YukiSetup-<version>.exe). No admin needed.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SkipInstaller
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Python .venv\Scripts\python.exe -Version 0.2.0

.NOTES
  Python: -Python, else .venv\Scripts\python.exe. PyInstaller must be importable there
  (dev: `uv pip install --python .venv\Scripts\python.exe pyinstaller`; CI: `uv sync --group build`).
  Never `uv sync`/`uv run` while the dev Yuki is running (locked exes in the venv).
  Inno Setup: -Iscc, else ISCC.exe on PATH, in %LOCALAPPDATA%\Programs\Inno Setup 6 / Inno, or Program Files.
#>
param(
    [string]$Python = "",
    [string]$Version = "",
    [string]$Iscc = "",
    [switch]$SkipInstaller,
    [switch]$SkipSelfCheck,
    [switch]$InstallerOnly,   # CI: sign dist\Yuki's executables between the two stages
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Pkg = Join-Path $Root "packaging"
$Dist = Join-Path $Pkg "dist"
$Work = Join-Path $Pkg "build"

if (-not $Python) { $Python = Join-Path $Root ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) { throw "Python not found: $Python" }
if (-not $Version) { $Version = (& $Python -c "import yuki; print(yuki.__version__)").Trim() }
$env:YUKI_VERSION = $Version
Write-Host "Yuki $Version  (python: $Python)"

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) { throw "PyInstaller is not installed for $Python (uv pip install --python $Python pyinstaller)" }

# the app icon: the logo picked in packaging/icon/icon.json ("mark"), drawn from its committed SVGs at every size;
# no pick yet keeps the current yuki.ico (make_icon.py)
& $Python (Join-Path $Pkg "make_icon.py")
if ($LASTEXITCODE -ne 0) { throw "make_icon.py failed ($LASTEXITCODE)" }
if ($Clean) { Remove-Item -Recurse -Force $Dist, $Work -ErrorAction SilentlyContinue }

$App = Join-Path $Dist "Yuki"
if (-not $InstallerOnly) {
# -- 1. PyInstaller onedir -------------------------------------------------------------------------
$sw = [Diagnostics.Stopwatch]::StartNew()
& $Python -m PyInstaller (Join-Path $Pkg "yuki.spec") --noconfirm --distpath $Dist --workpath $Work --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
$sw.Stop()
$bytes = (Get-ChildItem $App -Recurse -File | Measure-Object Length -Sum).Sum
$files = (Get-ChildItem $App -Recurse -File).Count
Write-Host ("onedir: {0}  {1:N0} MB in {2} files  (built in {3:N0} s)" -f $App, ($bytes / 1MB), $files, $sw.Elapsed.TotalSeconds)

# -- 2. self-check (offline: every native piece loads) --------------------------------------------
if (-not $SkipSelfCheck) {
    $cli = Join-Path $App "yuki-cli.exe"
    & $cli --version
    $t = [Diagnostics.Stopwatch]::StartNew()
    & $cli -m yuki.core.selfcheck
    $code = $LASTEXITCODE
    $t.Stop()
    Write-Host ("self-check: exit {0} in {1:N1} s" -f $code, $t.Elapsed.TotalSeconds)
    if ($code -ne 0) { throw "frozen self-check failed" }
}
}   # not InstallerOnly

# -- 3. Inno Setup installer ------------------------------------------------------------------------
if (-not $SkipInstaller) {
    if (-not $Iscc) {
        $found = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($found) { $Iscc = $found.Source }
    }
    if (-not $Iscc) {
        foreach ($c in @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno\ISCC.exe",
                         "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
            if (Test-Path $c) { $Iscc = $c; break }
        }
    }
    if (-not $Iscc) { throw "Inno Setup (ISCC.exe) not found; install it per-user: innosetup-6.x.exe /CURRENTUSER" }
    & $Iscc "/DAppVersion=$Version" "/DSourceDir=$App" (Join-Path $Pkg "installer.iss")
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
    $setup = Get-ChildItem (Join-Path $Pkg "Output") -Filter "YukiSetup-$Version.exe" | Select-Object -First 1
    Write-Host ("installer: {0}  {1:N0} MB" -f $setup.FullName, ($setup.Length / 1MB))
}
