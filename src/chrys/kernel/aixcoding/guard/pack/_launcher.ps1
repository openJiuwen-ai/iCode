# [guard] launcher: run the hook script with a real python found at runtime.
#
# iCode picks the interpreter for `type: script` hooks itself (uv -> python3 ->
# python) and does not filter the Microsoft Store stubs in
# %LOCALAPPDATA%\Microsoft\WindowsApps: a python3 stub prints the Store banner
# and exits 9009, so every `on_error: block` hook blocks everything.  This
# launcher is invoked instead (type: command + powershell -File), and finds a
# real interpreter each run:
#   1. PATH scan for python.exe, skipping 0-byte stubs and reparse-point
#      aliases (the Store stubs are reparse points, whatever their length);
#   2. the interpreter the packaged iCode unpacks on first run under
#      %LOCALAPPDATA%\pyapp\data\chrys\<hash>\<version>\python\python.exe.
# The script to run is passed as GUARD_SCRIPT (relative to the hooks
# directory, e.g. `guard\_prompt_guard.py` or `scripts\session.py`); any
# extra argv elements are forwarded to it.  Keep this file ASCII-only:
# Windows PowerShell 5.1 parses BOM-less files as ANSI.
$ErrorActionPreference = 'SilentlyContinue'
$hooksRoot = Split-Path -Parent $PSScriptRoot
$script = Join-Path $hooksRoot $env:GUARD_SCRIPT
$py = ''
foreach ($dir in ($env:PATH -split ';')) {
  if (-not $dir) { continue }
  $candidate = Join-Path $dir 'python.exe'
  if (Test-Path -LiteralPath $candidate -PathType Leaf) {
    $item = Get-Item -LiteralPath $candidate
    if ($item.Length -gt 0 -and (([int]$item.Attributes -band 0x400) -eq 0)) {
      $py = $item.FullName
      break
    }
  }
}
if (-not $py) {
  $root = Join-Path $env:LOCALAPPDATA 'pyapp\data\chrys'
  $hit = Get-ChildItem -LiteralPath $root -Recurse -Filter python.exe -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Length -gt 0 -and (([int]$_.Attributes -band 0x400) -eq 0) } |
    Select-Object -First 1
  if ($hit) { $py = $hit.FullName }
}
if (-not $py) {
  [Console]::Error.WriteLine('[guard] no usable python interpreter found (PATH or packaged runtime)')
  exit 1
}
& $py $script @args
exit $LASTEXITCODE
