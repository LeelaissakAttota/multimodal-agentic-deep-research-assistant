<#
.SYNOPSIS
  Sends ONE MDRA demo request file (research API :8000 or control API :8711) and prints the result.
.EXAMPLE
  .\send.ps1 requests\R06_research_pdf.json
.NOTES
  Control requests use $env:DEEP_RESEARCH_CONTROL_TOKEN as the Bearer token (never printed).
#>
param(
    [Parameter(Mandatory = $true)][string]$File,
    [string]$ResearchUrl = "http://127.0.0.1:8000",
    [string]$ControlUrl = "http://127.0.0.1:8711",
    [switch]$Quiet
)
$spec = Get-Content -Raw $File | ConvertFrom-Json
$base = if ($spec.api -eq "control") { $ControlUrl } else { $ResearchUrl }
$headers = @{ "Accept" = "application/json"; "X-Operator" = "demo-script" }
if ($spec.api -eq "control") {
    if ($spec.auth -is [string] -and $spec.auth -eq "wrong") { $headers["Authorization"] = "Bearer wrong-token-for-demo-only" }
    elseif ($spec.auth -is [bool] -and $spec.auth) {
        if (-not $env:DEEP_RESEARCH_CONTROL_TOKEN) { Write-Warning "DEEP_RESEARCH_CONTROL_TOKEN is not set in this terminal" }
        $headers["Authorization"] = "Bearer $($env:DEEP_RESEARCH_CONTROL_TOKEN)"
    }
}
$params = @{ Uri = $base.TrimEnd("/") + $spec.path; Method = $spec.method; Headers = $headers; UseBasicParsing = $true; TimeoutSec = 300 }
if ($null -ne $spec.body) {
    $params["ContentType"] = "application/json"
    $params["Body"] = [System.Text.Encoding]::UTF8.GetBytes(($spec.body | ConvertTo-Json -Depth 20 -Compress))
}
$status = 0; $content = ""
try {
    $resp = Invoke-WebRequest @params
    $status = [int]$resp.StatusCode; $content = $resp.Content
} catch {
    if ($_.Exception.Response) {
        $status = [int]$_.Exception.Response.StatusCode
        $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
        $content = $reader.ReadToEnd()
    } else { $content = "NO RESPONSE: $($_.Exception.Message)" }
}
$ok = ($status -eq [int]$spec.expect_status)
if ($ok -and $spec.expect_contains) { $ok = $content -like "*$($spec.expect_contains)*" }
if ($ok -and $content -like "*DEMO-NOT-A-SECRET-123*") { $ok = $false; $content = "LEAK: response echoed the submitted secret-like value" }
$result = [pscustomobject]@{ Name = [IO.Path]::GetFileNameWithoutExtension($File); Api = $spec.api; Status = $status; Expected = $spec.expect_status; Pass = $ok; Body = $content }
if (-not $Quiet) {
    "{0}  HTTP {1} (expected {2})  {3}" -f $result.Name, $status, $spec.expect_status, $(if ($ok) { "PASS" } else { "FAIL" })
    if ($spec.note) { "note: $($spec.note)" }
    try { $content | ConvertFrom-Json | ConvertTo-Json -Depth 6 } catch { $content }
}
return $result
