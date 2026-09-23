param(
    [ValidateSet('buy', 'sell')]
    [string]$Side,
    [switch]$Execute,
    [switch]$SupersedeClaim,
    [switch]$OperatorConfirmedPolicy
)

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskScript = Join-Path $taskRoot 'src\ondo_btc_operator.py'
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
    throw 'The isolated candidate interpreter is missing. Do not use the root venv.'
}
if (-not $Side) {
    $Side = Read-Host 'Entry direction (buy or sell; no default)'
}
if ($Side -cnotin @('buy', 'sell')) {
    throw 'Choose exactly buy or sell. Nothing was started.'
}
$taskMode = '--prepare-only'
if ($Execute) { $taskMode = '--execute' }
$extra = @()
if ($SupersedeClaim) { $extra += '--supersede-claim' }
if ($OperatorConfirmedPolicy) { $extra += '--operator-confirmed-policy' }
& $taskPython -I $taskScript --side $Side $taskMode @extra
exit $LASTEXITCODE
