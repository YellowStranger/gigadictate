# Installs the setup silently for the current user, checks what it did, runs the end-to-end
# test against the installed exe, then uninstalls and checks that nothing is left behind.
param([Parameter(Mandatory)][string]$Setup)
$ErrorActionPreference = "Stop"
$dir = Join-Path $env:LOCALAPPDATA "Programs\GigaDictate"
$run = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$lnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\GigaDictate.lnk"
function Check($cond, $msg) { if (-not $cond) { throw "FAIL: $msg" }; Write-Host "ok: $msg" }

$p = Start-Process $Setup -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/TASKS="autostart"', '/LOG=install.log' -Wait -PassThru
Check ($p.ExitCode -eq 0) "setup exit code 0 (got $($p.ExitCode))"
Check (Test-Path "$dir\GigaDictate.exe") "exe installed to $dir"
Check (Test-Path "$dir\models\gigaam-v3-e2e-rnnt\v3_e2e_rnnt_encoder.int8.onnx") "model installed"
Check (Test-Path $lnk) "Start menu shortcut"
$val = (Get-ItemProperty $run -Name GigaDictate -ErrorAction SilentlyContinue).GigaDictate
Check ($val -eq "`"$dir\GigaDictate.exe`"") "autostart value matches the app's own format ($val)"
Check (-not (Get-Process GigaDictate -ErrorAction SilentlyContinue)) "silent install does not launch the app"

# installing again over the existing copy (update) must work too
$p = Start-Process $Setup -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/TASKS="autostart"' -Wait -PassThru
Check ($p.ExitCode -eq 0) "reinstall over existing copy"

$env:GIGADICTATE_EXE = "$dir\GigaDictate.exe"
$env:PYTHONPATH = "src"
python -m pytest tests/test_e2e_windows.py -v -s -rs
Check ($LASTEXITCODE -eq 0) "end-to-end test with the installed exe"

$p = Start-Process "$dir\unins000.exe" -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
Check ($p.ExitCode -eq 0) "uninstaller exit code 0"
$deadline = (Get-Date).AddSeconds(60)  # the uninstaller finishes in a temporary copy of itself
while ((Test-Path $dir) -and (Get-Date) -lt $deadline) { Start-Sleep 1 }
Check (-not (Test-Path $dir)) "install folder removed"
Check (-not (Test-Path $lnk)) "Start menu shortcut removed"
Check (-not (Get-ItemProperty $run -Name GigaDictate -ErrorAction SilentlyContinue)) "autostart value removed"
