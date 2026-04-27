$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root ".agm-pids.json"

if (-not (Test-Path $pidFile)) {
  Write-Output "No se encontro archivo PID."
  exit 0
}

$pids = Get-Content $pidFile -Raw | ConvertFrom-Json
foreach ($entry in $pids) {
  $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
  if ($process) {
    Stop-Process -Id $entry.pid -Force
  }
}

Remove-Item $pidFile -Force
Write-Output "Servicios detenidos."
