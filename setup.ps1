[CmdletBinding()]
param(
    [switch]$SkipDataSplit
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

cmake -S . -B build -G Ninja
if ($LASTEXITCODE -ne 0) { throw "CMake configuration failed." }

$target = if ($SkipDataSplit) { "setup-deps" } else { "setup" }
cmake --build build --target $target
if ($LASTEXITCODE -ne 0) { throw "CMake target '$target' failed." }
