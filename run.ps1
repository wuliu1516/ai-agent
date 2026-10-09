$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

make start
if ($LASTEXITCODE -ne 0) { throw "make start failed." }
