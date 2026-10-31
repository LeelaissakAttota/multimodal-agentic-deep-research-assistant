<#
.SYNOPSIS
  Runs all MDRA demo requests: R* research API, C* control API, E* errors (422/404/401).
.EXAMPLE
  .\run-all.ps1
  .\run-all.ps1 -Filter "R*"      # research API only (control API not needed)
#>
param([string]$Filter = "*", [string]$ResearchUrl = "http://127.0.0.1:8000", [string]$ControlUrl = "http://127.0.0.1:8711")
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$results = foreach ($f in Get-ChildItem (Join-Path $here "requests") -Filter "$Filter.json" | Sort-Object Name) {
    & (Join-Path $here "send.ps1") -File $f.FullName -ResearchUrl $ResearchUrl -ControlUrl $ControlUrl -Quiet
}
$results | Select-Object Name, Api, Status, Expected, @{ n = "Result"; e = { if ($_.Pass) { "PASS" } else { "FAIL" } } } | Format-Table -AutoSize
"PASS: $(@($results | Where-Object Pass).Count) / $(@($results).Count)"
