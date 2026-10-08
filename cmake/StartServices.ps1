[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [Parameter(Mandatory = $true)][string]$NodeExecutable
)

$ErrorActionPreference = "Stop"
$backendPort = 8000
$frontendPort = 5173
$backendPidFile = Join-Path $ProjectRoot "backend\server.pid"
$frontendPidFile = Join-Path $ProjectRoot "frontend\server.pid"
$backendLog = Join-Path $ProjectRoot "backend\server.log"
$backendErrorLog = Join-Path $ProjectRoot "backend\server-error.log"
$frontendLog = Join-Path $ProjectRoot "frontend\vite.log"
$frontendErrorLog = Join-Path $ProjectRoot "frontend\vite-error.log"
$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$viteEntry = Join-Path $ProjectRoot "frontend\node_modules\vite\bin\vite.js"
$dataRoot = Join-Path $ProjectRoot "data\CSpider"

function Get-ListeningProcessId([int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) { return [int]$listener.OwningProcess }
    return $null
}

function Test-ApiHealth {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Start-DetachedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$StandardOutput,
        [Parameter(Mandatory = $true)][string]$StandardError
    )

    # CMake can supply both Path and PATH. Start-Process rejects that inherited
    # environment as duplicate keys, so launch cmd.exe with an explicitly
    # deduplicated environment. cmd redirects service output to persistent logs,
    # preventing the detached processes from keeping CMake's output pipe open.
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $env:ComSpec
    $info.WorkingDirectory = $WorkingDirectory
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    # cmd itself stays alive while its child service runs. Give it separate
    # pipes so it cannot inherit CMake's standard handles and keep a CMake
    # invocation open after this launcher returns. The child output is
    # redirected below to the log files.
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.Environment.Clear()
    $seenKeys = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($key in [Environment]::GetEnvironmentVariables("Process").Keys) {
        if ($seenKeys.Add([string]$key)) {
            $info.Environment[[string]$key] = [Environment]::GetEnvironmentVariable([string]$key, "Process")
        }
    }

    $argumentText = [string]::Join(" ", $Arguments)
    $info.Arguments = ('/d /s /c ""{0}" {1} 1>>"{2}" 2>>"{3}""' -f $FilePath, $argumentText, $StandardOutput, $StandardError)
    $process = [System.Diagnostics.Process]::Start($info)
    if ($null -eq $process) { throw "Unable to start $FilePath." }
    return $process
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf) -or
    -not (Test-Path -LiteralPath $viteEntry -PathType Leaf)) {
    throw "Environment is not configured. Run 'cmake --build build --target setup' first."
}
foreach ($name in @("development.json", "development_gold.sql", "validation.json", "validation_gold.sql", "test.json", "test_gold.sql", "tables.json")) {
    if (-not (Test-Path -LiteralPath (Join-Path $dataRoot $name) -PathType Leaf)) {
        throw "Missing $(Join-Path $dataRoot $name). Run 'cmake --build build --target setup'."
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $dataRoot "database") -PathType Container)) {
    throw "Missing $(Join-Path $dataRoot 'database'). Run 'cmake --build build --target setup'."
}

$startedBackend = $false
$startedFrontend = $false
try {
    $existingBackend = Get-ListeningProcessId $backendPort
    if ($existingBackend) {
        if (-not (Test-ApiHealth)) {
            throw "Port $backendPort is already in use by process $existingBackend, but it is not a healthy workbench backend."
        }
        Set-Content -LiteralPath $backendPidFile -Value $existingBackend -NoNewline -Encoding ascii
        Write-Host "Backend already running on http://127.0.0.1:$backendPort (PID $existingBackend)."
    }
    else {
        Set-Content -LiteralPath $backendLog -Value "" -Encoding utf8
        Set-Content -LiteralPath $backendErrorLog -Value "" -Encoding utf8
        $backend = Start-DetachedProcess -FilePath $venvPython `
            -Arguments @("-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "$backendPort") `
            -WorkingDirectory $ProjectRoot -StandardOutput $backendLog -StandardError $backendErrorLog
        Set-Content -LiteralPath $backendPidFile -Value $backend.Id -NoNewline -Encoding ascii
        $startedBackend = $true
    }

    $existingFrontend = Get-ListeningProcessId $frontendPort
    if ($existingFrontend) {
        Set-Content -LiteralPath $frontendPidFile -Value $existingFrontend -NoNewline -Encoding ascii
        Write-Host "Frontend already running on http://127.0.0.1:$frontendPort (PID $existingFrontend)."
    }
    else {
        Set-Content -LiteralPath $frontendLog -Value "" -Encoding utf8
        Set-Content -LiteralPath $frontendErrorLog -Value "" -Encoding utf8
        $frontend = Start-DetachedProcess -FilePath $NodeExecutable `
            -Arguments @("node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "$frontendPort", "--strictPort") `
            -WorkingDirectory (Join-Path $ProjectRoot "frontend") -StandardOutput $frontendLog -StandardError $frontendErrorLog
        Set-Content -LiteralPath $frontendPidFile -Value $frontend.Id -NoNewline -Encoding ascii
        $startedFrontend = $true
    }

    $deadline = (Get-Date).AddSeconds(30)
    do {
        $frontendReady = [bool](Get-ListeningProcessId $frontendPort)
        if ((Test-ApiHealth) -and $frontendReady) {
            Set-Content -LiteralPath $backendPidFile -Value (Get-ListeningProcessId $backendPort) -NoNewline -Encoding ascii
            Set-Content -LiteralPath $frontendPidFile -Value (Get-ListeningProcessId $frontendPort) -NoNewline -Encoding ascii
            Write-Host "Workbench is running: http://127.0.0.1:$frontendPort"
            Write-Host "Backend API docs:     http://127.0.0.1:$backendPort/docs"
            exit 0
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    throw "The services did not become ready within 30 seconds. Check backend/server-error.log and frontend/vite-error.log."
}
catch {
    if ($startedFrontend) {
        $processId = Get-ListeningProcessId $frontendPort
        if ($processId) { Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $frontendPidFile -Force -ErrorAction SilentlyContinue
    }
    if ($startedBackend) {
        $processId = Get-ListeningProcessId $backendPort
        if ($processId) { Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $backendPidFile -Force -ErrorAction SilentlyContinue
    }
    throw
}


