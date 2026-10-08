$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

cmake -S . -B build -G Ninja
if ($LASTEXITCODE -ne 0) { throw "CMake configuration failed." }

cmake --build build --target start
if ($LASTEXITCODE -ne 0) { throw "CMake target 'start' failed." }
