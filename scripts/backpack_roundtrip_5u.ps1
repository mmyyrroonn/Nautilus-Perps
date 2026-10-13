[CmdletBinding()]
param(
    [ValidateSet('both', 'maker', 'market')][string]$Scenario = 'both',
    [ValidateRange(5, 120)][int]$MakerWaitSeconds = 120,
    [ValidateRange(0, 120)][int]$MakerRepriceSeconds = 5,
    [ValidateRange(0, 23)][int]$MakerMaxReprices = 23,
    [switch]$CloseOnly,
    [string]$EntryClientOrderId,
    [string]$MaxCloseQuantity = '0.00005',
    [switch]$FreshAcceptance,
    [string]$StateDirectory,
    [string]$EnvFile,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
if (-not (($MakerRepriceSeconds -eq 0 -and $MakerMaxReprices -eq 0) -or
    ($MakerRepriceSeconds -ge 5 -and $MakerRepriceSeconds -le $MakerWaitSeconds -and $MakerMaxReprices -ge 1))) {
    throw 'Maker repricing requires 0/0 to disable, or a minimum 5-second interval within the side deadline and 1 to 23 replacements.'
}
$bpAppRoot = Split-Path -Parent $PSScriptRoot
$bpPython = Join-Path $bpAppRoot '.venv\Scripts\python.exe'
$bpState = Join-Path $bpAppRoot 'reports\backpack\operator-state'
$bpEnvFile = if ($EnvFile) { [System.IO.Path]::GetFullPath($EnvFile) } else { Join-Path $bpAppRoot '.env' }
$bpMode = if ($Scenario -eq 'both') { 'maker-market' } else { $Scenario }
if ($CloseOnly) {
    if ($Scenario -eq 'both' -or -not $EntryClientOrderId) {
        throw 'Close-only requires -Scenario maker or market and -EntryClientOrderId.'
    }
    $bpMode = 'close-market'
}
$bpRun = Join-Path $bpAppRoot ('reports\backpack\acceptance\run-' + $bpMode + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
if ($FreshAcceptance -and ($CloseOnly -or $StateDirectory)) {
    throw 'Fresh acceptance cannot be combined with close-only or an existing state directory.'
}
if ($StateDirectory) {
    if (-not [System.IO.Path]::IsPathRooted($StateDirectory) -or
        -not (Test-Path -LiteralPath $StateDirectory -PathType Container)) {
        throw 'StateDirectory must identify an existing absolute native state directory.'
    }
    $bpState = [System.IO.Path]::GetFullPath($StateDirectory)
}
if ($FreshAcceptance) { $bpState = Join-Path $bpRun 'state' }
if (-not (Test-Path -LiteralPath $bpPython -PathType Leaf)) {
    throw 'The application worktree Python environment is missing.'
}
if (-not $DryRun -and -not (Test-Path -LiteralPath $bpEnvFile -PathType Leaf)) {
    throw 'The configured credential environment file is missing.'
}

Push-Location -LiteralPath $bpAppRoot
$bpSessionLock = $null
try {
    New-Item -ItemType Directory -Path $bpRun -Force | Out-Null
    if (-not $DryRun) {
        $bpLockPath = Join-Path $bpAppRoot 'reports\backpack\acceptance\operator-session.lock'
        try {
            $bpSessionLock = [System.IO.File]::Open($bpLockPath, [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        } catch { throw 'Another acceptance launcher is running; no commands were executed.' }
    }
    if ($FreshAcceptance -and -not $DryRun) {
        Write-Host 'READ ONLY preflight: require no open orders and a flat account before creating a new session.'
        & $bpPython scripts\backpack_acceptance_session.py --env-file $bpEnvFile --output (Join-Path $bpState 'session-baseline.json')
        if ($LASTEXITCODE -ne 0) { throw "Flat preflight failed; no financial commands were executed. Report: $bpRun" }
    }
    if ($CloseOnly) {
        # Resolve the remaining owned quantity and fresh quote inside the native node.
        New-Item -ItemType Directory -Path $bpRun -Force | Out-Null
        $bpCommand = @{
            action = 'close-only'; symbol = 'BTC_USDC_PERP'; style = 'market'
            max_quantity = $MaxCloseQuantity; entry_client_order_id = $EntryClientOrderId
            wait_seconds = 10
        }
        ConvertTo-Json -InputObject @($bpCommand) -Depth 3 | Out-File -LiteralPath (Join-Path $bpRun ('BTC_USDC_PERP.' + $bpMode + '.commands.json')) -Encoding utf8
    } else {
        # Leave sizing headroom; native admission independently checks fresh quotes.
        & $bpPython scripts\backpack_acceptance_plan.py --symbol BTC_USDC_PERP --scenario $bpMode --max-leg-usdc 4.5 --maker-wait-secs $MakerWaitSeconds --maker-reprice-seconds $MakerRepriceSeconds --maker-max-reprices $MakerMaxReprices --output-dir $bpRun
        if ($LASTEXITCODE -ne 0) { throw 'Fresh sizing failed; no financial commands were executed.' }
    }
    $bpArguments = @(
        'scripts\backpack_operator.py', '--symbol', 'BTC_USDC_PERP',
        '--command-file', (Join-Path $bpRun ('BTC_USDC_PERP.' + $bpMode + '.commands.json')),
        '--limits', (Join-Path $bpAppRoot 'config\limits.toml'),
        '--max-leg-usd', '5', '--max-total-usd', '5',
        '--timeout-secs', '600', '--state-dir', $bpState
    )
    if ($DryRun) {
        Write-Host 'DRY RUN: no credentials or native trading node.'
    } else {
        if ($CloseOnly) {
            Write-Host "MAINNET: $bpMode; reduce-only Market sell against the confirmed original entry."
        } else {
            Write-Host "MAINNET: $bpMode; selected entry style -> reduce-only Market exit."
            Write-Host 'Sizing target: USD 4.5; USD 5 submission valuation cap. Market execution amount can vary.'
        }
        Write-Host "Maker entry only: reprice interval=${MakerRepriceSeconds}s; maximum replacements=$MakerMaxReprices; total entry deadline=${MakerWaitSeconds}s."
        Write-Host 'Every replacement waits for cancellation and fill reconciliation; incomplete groups stop later entries.'
        $bpArguments += @('--env-file', $bpEnvFile, '--execute', '--confirm-mainnet')
    }
    $bpLog = Join-Path $bpRun 'operator.log'
    $ErrorActionPreference = 'Continue'
    & $bpPython @bpArguments 2>&1 | ForEach-Object {
        $bpLine = $_.ToString()
        $bpLine | Out-File -LiteralPath $bpLog -Encoding utf8 -Append
        Write-Output $bpLine
    }
    $bpExitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    # Keep a standalone final result in addition to the streaming log.
    $bpResult = $null
    foreach ($bpSavedLine in (Get-Content -LiteralPath $bpLog -Encoding utf8)) {
        try {
            $bpDocument = ConvertFrom-Json -InputObject $bpSavedLine -ErrorAction Stop
            if ($null -ne $bpDocument.completed) { $bpResult = $bpDocument }
        } catch { }
    }
    if ($null -ne $bpResult) {
        $bpResult | ConvertTo-Json -Depth 30 | Out-File -LiteralPath (Join-Path $bpRun 'result.json') -Encoding utf8
        foreach ($bpTrip in $bpResult.roundtrips) {
            foreach ($bpLeg in $bpTrip.legs) {
                Write-Host ("LEG: {0} {1}; filled_qty={2}; USDC={3}; liquidity_verified={4}; order={5}" -f
                    $bpLeg.order_style, $bpLeg.side, $bpLeg.filled_quantity, $bpLeg.actual_notional_usdc,
                    $bpLeg.liquidity_verified, $bpLeg.venue_order_id)
            }
        }
        Write-Host ("Acceptance completed={0}; account_reconciled={1}; failure={2}" -f
            $bpResult.completed, $bpResult.account_reconciled, $bpResult.failure)
    }
    Write-Host "Log: $bpLog"
    Write-Host "Persistent state: $bpState"
    Write-Host 'Inspect the saved result before any repeat, especially residual quantities or unknown orders.'
    exit $bpExitCode
} finally {
    if ($null -ne $bpSessionLock) { $bpSessionLock.Dispose() }
    Pop-Location
}
