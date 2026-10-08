[CmdletBinding()]
param(
    [switch]$SkipDataSplit
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw ("Command failed ({0}): {1} {2}" -f $LASTEXITCODE, $FilePath, ($Arguments -join " "))
    }
}

function Assert-Version {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Actual,
        [Parameter(Mandatory = $true)][string]$Expected
    )

    if ($Actual -ne $Expected) {
        throw ("{0} version mismatch: found {1}, expected {2}." -f $Name, $Actual, $Expected)
    }
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
$npmCommand = Get-Command npm -ErrorAction SilentlyContinue
if (-not $pythonCommand -or -not $nodeCommand -or -not $npmCommand) {
    throw "Install Python 3.12.4, Node.js 24.19.0, and npm 11.17.0, then make sure they are on PATH."
}

$pythonVersion = (& python -c 'import platform; print(platform.python_version())').Trim()
$nodeVersion = (& node --version).Trim()
$npmVersion = (& npm --version).Trim()
Assert-Version -Name "Python" -Actual $pythonVersion -Expected "3.12.4"
Assert-Version -Name "Node.js" -Actual $nodeVersion -Expected "v24.19.0"
Assert-Version -Name "npm" -Actual $npmVersion -Expected "11.17.0"

if (-not $SkipDataSplit) {
    $sourceDir = "D:\dataset\CSpider"
    $requiredFiles = @(
        "train.json",
        "train_gold.sql",
        "dev.json",
        "dev_gold.sql",
        "tables.json",
        "char_emb.txt",
        "README.txt"
    )
    if (-not (Test-Path -LiteralPath $sourceDir -PathType Container)) {
        throw "CSpider source directory not found: $sourceDir"
    }
    foreach ($name in $requiredFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $sourceDir $name) -PathType Leaf)) {
            throw "CSpider source data is incomplete. Missing: $sourceDir\$name"
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $sourceDir "database") -PathType Container)) {
        throw "CSpider source data is incomplete. Missing: $sourceDir\database"
    }
}

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Invoke-Checked -FilePath python -Arguments @("-m", "venv", ".venv")
}
$venvPythonVersion = (& $venvPython -c 'import platform; print(platform.python_version())').Trim()
Assert-Version -Name "Virtual environment Python" -Actual $venvPythonVersion -Expected "3.12.4"

Invoke-Checked -FilePath $venvPython -Arguments @(
    "-m", "pip", "install", "--disable-pip-version-check", "-r", "backend/requirements.txt"
)
Invoke-Checked -FilePath $venvPython -Arguments @("-m", "pip", "check")

Push-Location (Join-Path $projectRoot "frontend")
try {
    Invoke-Checked -FilePath npm -Arguments @("ci")
}
finally {
    Pop-Location
}

if (-not $SkipDataSplit) {
    Invoke-Checked -FilePath $venvPython -Arguments @("split_cspider.py")
}

Write-Host "Environment setup complete. Run .\run.ps1 to start the workbench."
