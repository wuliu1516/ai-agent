[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ProjectRoot)

$ErrorActionPreference = "Stop"
function Get-ListeningProcessId([int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) { return [int]$listener.OwningProcess }
    return $null
}

$services = @(
    @{ Name = "frontend"; Port = 5173; PidFile = Join-Path $ProjectRoot "frontend\server.pid" },
    @{ Name = "backend"; Port = 8000; PidFile = Join-Path $ProjectRoot "backend\server.pid" }
)
foreach ($service in $services) {
    $processId = Get-ListeningProcessId $service.Port
    if ($processId) {
        Stop-Process -Id $processId -Force
        Write-Host "$($service.Name): stopped listener PID $processId on port $($service.Port)."
    }
    else {
        Write-Host "$($service.Name): no listener on port $($service.Port)."
    }
    Remove-Item -LiteralPath $service.PidFile -Force -ErrorAction SilentlyContinue
}

