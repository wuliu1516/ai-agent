[CmdletBinding()]
param(
    [switch]$SkipDataSplit
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$target = if ($SkipDataSplit) { "setup-deps" } else { "setup" }
make $target
if ($LASTEXITCODE -ne 0) { throw "make $target failed." }
