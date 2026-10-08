$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$datasetDir = Join-Path $projectRoot "data\CSpider"
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
$npmCommand = Get-Command npm -ErrorAction SilentlyContinue
$viteEntry = Join-Path $projectRoot "frontend\node_modules\vite\bin\vite.js"

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf) -or -not (Test-Path -LiteralPath $viteEntry -PathType Leaf)) {
    throw "Environment is not configured. Run .\setup.ps1 from the repository root first."
}
foreach ($name in @(
    "development.json",
    "development_gold.sql",
    "validation.json",
    "validation_gold.sql",
    "test.json",
    "test_gold.sql",
    "tables.json"
)) {
    if (-not (Test-Path -LiteralPath (Join-Path $datasetDir $name) -PathType Leaf)) {
        throw "Missing $datasetDir\$name. Check D:\dataset\CSpider and rerun .\setup.ps1."
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $datasetDir "database") -PathType Container)) {
    throw "Missing $datasetDir\database. Rerun .\setup.ps1."
}
if (-not $nodeCommand -or -not $npmCommand) {
    throw "Node.js 24.19.0 and npm 11.17.0 were not found on PATH."
}
$nodeVersion = (& node --version).Trim()
$npmVersion = (& npm --version).Trim()
if ($nodeVersion -ne "v24.19.0" -or $npmVersion -ne "11.17.0") {
    throw "Expected Node.js v24.19.0 and npm 11.17.0; found $nodeVersion and $npmVersion."
}

$processes = @()
try {
    $backend = Start-Process -FilePath $pythonExe `
        -ArgumentList @("-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $projectRoot -NoNewWindow -PassThru
    $processes += $backend

    $frontendDir = Join-Path $projectRoot "frontend"
    $frontend = Start-Process -FilePath $nodeCommand.Source `
        -ArgumentList @("node_modules/vite/bin/vite.js", "--host", "127.0.0.1") `
        -WorkingDirectory $frontendDir -NoNewWindow -PassThru
    $processes += $frontend

    Write-Host "Workbench: http://127.0.0.1:5173"
    Write-Host "Backend API docs: http://127.0.0.1:8000/docs"
    Write-Host "Press Ctrl+C to stop both services."

    while ($true) {
        Start-Sleep -Seconds 1
        foreach ($process in $processes) {
            $process.Refresh()
            if ($process.HasExited) {
                throw "A service process exited with code $($process.ExitCode)."
            }
        }
    }
}
finally {
    foreach ($process in $processes) {
        $process.Refresh()
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
