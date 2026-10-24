# Starts the MDRA control API (127.0.0.1:8711) and the Streamlit dashboard (127.0.0.1:8502).
# A fresh control token is generated for this session if none is set. It is never printed.
param([int]$ApiPort = 8711, [int]$DashPort = 8502)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
if (-not $env:DEEP_RESEARCH_CONTROL_TOKEN -or $env:DEEP_RESEARCH_CONTROL_TOKEN.Length -lt 16) {
  $env:DEEP_RESEARCH_CONTROL_TOKEN = & $Py -c "import secrets;print(secrets.token_urlsafe(32))"
}
$env:DEEP_RESEARCH_CONTROL_URL = "http://127.0.0.1:$ApiPort"
$env:PYTHONPATH = Join-Path $Root "src"
$logs = Join-Path $Root "logs"; New-Item -ItemType Directory -Force $logs | Out-Null
$busy = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue
if (-not $busy) {
  Start-Process -FilePath $Py -ArgumentList "-m","deep_research.control.server","--port",$ApiPort `
    -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput "$logs\control.out.log" -RedirectStandardError "$logs\control.err.log"
} else { Write-Host "Control API already listening on $ApiPort (it must use the same token)." }
Start-Process -FilePath $Py -ArgumentList "-m","streamlit","run","src\deep_research\dashboard\app.py",`
  "--server.address","127.0.0.1","--server.port",$DashPort,"--server.headless","true","--theme.base","dark" `
  -WorkingDirectory $Root -WindowStyle Hidden `
  -RedirectStandardOutput "$logs\dashboard.out.log" -RedirectStandardError "$logs\dashboard.err.log"
Start-Sleep -Seconds 4
Write-Host "Dashboard: http://127.0.0.1:$DashPort  (control API on :$ApiPort)"
