param([switch]$DumpConfig,[int]$Port=3080)
$ErrorActionPreference = 'Stop'
$projectRoot=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$env:DSH_HOME=Join-Path $projectRoot 'runtime\dsh-home'
$env:DSH_AGENTS_HOME=Join-Path $env:DSH_HOME 'agents'
$env:DSH_TELEMETRY_DISABLED='1'
$env:DSH_TOOLS_MODE='native'
$env:NO_PROXY='127.0.0.1,localhost,::1'
$env:TEMP=Join-Path $projectRoot 'runtime\dsh-tmp'
$env:TMP=$env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP,(Join-Path $projectRoot 'runtime\workbench-v1') | Out-Null
$node=(Get-Command node -ErrorAction Stop).Source
if(-not $env:MODAB_PYTHON){$env:MODAB_PYTHON=(Get-Command python -ErrorAction Stop).Source}
$patch=& $node (Join-Path $PSScriptRoot 'configure.mjs')
if($LASTEXITCODE -ne 0){throw 'Unable to generate local ModAb configuration'}
$dshBin=Join-Path $projectRoot 'runtime\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js'
if(-not (Test-Path -LiteralPath $dshBin)){throw 'Install the declared DSH version in runtime/dsh first; see README.md'}
$cliArgs=@($dshBin,'--profile','web','--patch',([string]$patch))
if($DumpConfig){$cliArgs+='--dump-config'}else{$cliArgs+=@('--host','127.0.0.1','--port',([string]$Port),'--no-open')}
& $node @cliArgs
exit $LASTEXITCODE
