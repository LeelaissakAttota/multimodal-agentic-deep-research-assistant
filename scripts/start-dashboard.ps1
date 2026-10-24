# Starts the Multimodal Agentic Deep Research Assistant control API (127.0.0.1:8711) and dashboard (127.0.0.1:8502).
# A fresh control token is generated for this session if none is set. It is never printed.
param([int]$ApiPort = 8711, [int]$DashPort = 8502)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
# Token: env var, else a per-user token file (reused so other local tools can connect), else a new one.
$TokenDir = Join-Path $env:APPDATA "deep-research"
$TokenFile = Join-Path $TokenDir "control.token"
if (-not $env:DEEP_RESEARCH_CONTROL_TOKEN -or $env:DEEP_RESEARCH_CONTROL_TOKEN.Length -lt 16) {
  if (Test-Path $TokenFile) { $env:DEEP_RESEARCH_CONTROL_TOKEN = (Get-Content $TokenFile -Raw).Trim() }
  if (-not $env:DEEP_RESEARCH_CONTROL_TOKEN -or $env:DEEP_RESEARCH_CONTROL_TOKEN.Length -lt 16) {
    $env:DEEP_RESEARCH_CONTROL_TOKEN = (& $Py -c "import secrets;print(secrets.token_urlsafe(32))").Trim()
    New-Item -ItemType Directory -Force $TokenDir | Out-Null
    Set-Content -Path $TokenFile -Value $env:DEEP_RESEARCH_CONTROL_TOKEN -NoNewline -Encoding ascii
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    icacls $TokenFile /inheritance:r /grant:r "*$($sid):F" | Out-Null
  }
}
$env:DEEP_RESEARCH_CONTROL_URL = "http://127.0.0.1:$ApiPort"
$env:PYTHONPATH = Join-Path $Root "src"
$logs = Join-Path $Root "logs"; New-Item -ItemType Directory -Force $logs | Out-Null
function Test-Port([int]$Port) {
  $c = New-Object Net.Sockets.TcpClient
  try { $c.Connect("127.0.0.1", $Port); return $true } catch { return $false } finally { $c.Close() }
}
$busy = Test-Port $ApiPort
if (-not $busy) {
  Start-Process -FilePath $Py -ArgumentList "-m","deep_research.control.server","--port",$ApiPort `
    -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput "$logs\control.out.log" -RedirectStandardError "$logs\control.err.log"
} else { Write-Host "Control API already listening on $ApiPort (it must use the same token)." }
if (Test-Port $DashPort) { Write-Host "Dashboard already running on $DashPort."; exit 0 }
Start-Process -FilePath $Py -ArgumentList "-m","streamlit","run","src\deep_research\dashboard\app.py",`
  "--server.address","127.0.0.1","--server.port",$DashPort,"--server.headless","true","--theme.base","dark" `
  -WorkingDirectory $Root -WindowStyle Hidden `
  -RedirectStandardOutput "$logs\dashboard.out.log" -RedirectStandardError "$logs\dashboard.err.log"
Start-Sleep -Seconds 4
Write-Host "Dashboard: http://127.0.0.1:$DashPort  (control API on :$ApiPort)"
