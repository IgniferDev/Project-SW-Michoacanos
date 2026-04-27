$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = if ($env:AGM_PYTHON) { $env:AGM_PYTHON } else { "python" }
$pythonPath = (Get-Command $python).Source
$logsDir = Join-Path $root ".logs"
$pidFile = Join-Path $root ".agm-pids.json"

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

$services = @(
  @{ Name = "ms-auth"; Port = 8011 },
  @{ Name = "ms-periods"; Port = 8012 },
  @{ Name = "ms-academics"; Port = 8013 },
  @{ Name = "ms-grades"; Port = 8014 },
  @{ Name = "ms-attendance"; Port = 8015 },
  @{ Name = "ms-notifications"; Port = 8016 },
  @{ Name = "ms-reports"; Port = 8017 }
)

$pids = @()
foreach ($service in $services) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = "powershell.exe"
  $psi.WorkingDirectory = $root
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.Arguments = "-NoProfile -Command `"& { `$env:PYTHONPATH = '$root;$root\proto_generated'; & '$pythonPath' -m uvicorn app.main:app --host 127.0.0.1 --port $($service.Port) --app-dir $($service.Name) }`""

  $process = New-Object System.Diagnostics.Process
  $process.StartInfo = $psi
  $null = $process.Start()
  $pids += [pscustomobject]@{ name = $service.Name; pid = $process.Id; port = $service.Port }
}

$pids | ConvertTo-Json | Set-Content -Path $pidFile -Encoding UTF8
Write-Output "Servicios iniciados. Archivo PID: $pidFile"
