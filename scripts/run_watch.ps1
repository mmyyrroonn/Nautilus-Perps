# Launch the read-only multi-venue spread watcher detached, logging to logs\.
# Windows PowerShell 5.1. Examples (times are UTC ISO):
#   .\scripts\run_watch.ps1 -Tag stocks -Symbols NVDA,TSLA,HOOD,SNDK,MU,SPCX,GOLD1 -Until 2026-09-08T20:05:00Z
#   .\scripts\run_watch.ps1 -Tag crypto -Symbols SOL,HYPE,ZEC,PONS,LIT,ASTER,DASH,PUMP,ARB -Minutes 480
# Stop early: Stop-Process -Id <pid>   (the pid is printed on launch)
param(
    [Parameter(Mandatory = $true)][string[]]$Symbols,
    [string]$Until = "",
    [double]$Minutes = 0,
    [string]$Venues = "HL,LIGHTER,ASTER",
    [string]$Tag = "run",
    [string]$Out = "reports\stage1"
)
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv python not found: $py" }
if (-not $Until -and $Minutes -le 0) { throw "give -Until <UTC ISO> or -Minutes <n>" }

$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "watch_${Tag}_$stamp.log"
$err = Join-Path $logDir "watch_${Tag}_$stamp.err.log"

$argList = @("src\spread_watch.py", "--symbols", ($Symbols -join ","), "--venues", $Venues, "--out", $Out)
if ($Until) { $argList += @("--until", $Until) } else { $argList += @("--minutes", $Minutes) }

$p = Start-Process -FilePath $py -ArgumentList $argList -WorkingDirectory $repo `
    -RedirectStandardOutput $log -RedirectStandardError $err -WindowStyle Hidden -PassThru
"pid=$($p.Id)"
"log=$log"
"args=$($argList -join ' ')"
