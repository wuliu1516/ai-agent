[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ProjectRoot)

$ErrorActionPreference = "Stop"
function Get-ListeningProcessId([int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) { return [int]$listener.OwningProcess }
    return $null
}

$backendId = Get-ListeningProcessId 8000
$frontendId = Get-ListeningProcessId 5173
$apiHealthy = $false
if ($backendId) {
    try {
        $apiHealthy = (Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200
    }
    catch { $apiHealthy = $false }
}

if ($backendId -and $apiHealthy) {
    Write-Host "Backend:  running (PID $backendId, http://127.0.0.1:8000)"
}
else {
    Write-Error "Backend:  unavailable"
}
if ($frontendId) {
    Write-Host "Frontend: running (PID $frontendId, http://127.0.0.1:5173)"
}
else {
    Write-Error "Frontend: unavailable"
}

if (-not ($backendId -and $apiHealthy -and $frontendId)) { exit 1 }

