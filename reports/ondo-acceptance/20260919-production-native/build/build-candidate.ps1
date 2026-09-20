param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('stubs', 'release')]
    [string]$Stage,
    [string]$ForkPath = 'E:\nautilus_trader\.worktrees\ondo-production-native',
    [string]$ArtifactPrefix = '',
    [string]$DistDirectory = 'dist'
)

$ErrorActionPreference = 'Stop'
$forkPath = $ForkPath
$buildEnv = 'E:\nautilus_trader\.worktrees\ondo-r52-cleanup\.venv'
$buildPython = Join-Path $buildEnv 'Scripts\python.exe'
$reportPath = $PSScriptRoot
$distPath = Join-Path $reportPath $DistDirectory
$artifactName = "$ArtifactPrefix$Stage"
$env:PYTHONUTF8 = '1'
$env:CC = 'clang'
$env:CXX = 'clang++'
$env:VIRTUAL_ENV = $buildEnv
$env:PYO3_PYTHON = 'E:\nautilus_trader\.venv\Scripts\python.exe'
$env:UV_PROJECT_ENVIRONMENT = $buildEnv
# Existing Windows linker-message exception, scoped to this process only.
$env:CARGO_BUILD_WARNINGS = 'allow'
foreach ($envName in @('CONDA_PREFIX','CONDA_DEFAULT_ENV','CONDA_SHLVL',
    'CONDA_PROMPT_MODIFIER','CONDA_EXE','CONDA_PYTHON_EXE')) {
    [Environment]::SetEnvironmentVariable($envName, $null, 'Process')
}
$env:PATH = "$buildEnv\Scripts;C:\Users\myron\.cargo\bin;C:\Program Files\LLVM\bin;" + $env:PATH

$sourceRows = Get-ChildItem -LiteralPath (Join-Path $forkPath 'crates\adapters\ondo') -Recurse -File |
    Where-Object { $_.Extension -in @('.rs', '.toml') } |
    Sort-Object FullName | ForEach-Object {
        [ordered]@{ path=$_.FullName.Substring($forkPath.Length + 1);
            sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
    }
$sourceRows | ConvertTo-Json -Depth 3 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $reportPath "$artifactName-source.json")
$buildExit = 1
$started = [DateTime]::UtcNow
Push-Location (Join-Path $forkPath 'python')
try {
    # Cargo/maturin write progress on stderr; Windows PowerShell must not treat it
    # as a terminating script error. The native process exit code is the gate.
    $ErrorActionPreference = 'Continue'
    if ($Stage -eq 'stubs') {
        $env:CARGO_TARGET_DIR = 'E:\nautilus_trader\target'
        $env:NAUTILUS_STUB_PROFILE = 'nextest'
        & $buildPython generate_stubs.py *> (Join-Path $reportPath "${ArtifactPrefix}stubs.log")
    } else {
        $env:CARGO_TARGET_DIR = 'E:\nautilus_trader\.worktrees\task-ondo-r5\target'
        New-Item -ItemType Directory -Force -Path $distPath | Out-Null
        & $buildPython -m maturin build --release --out $distPath -i $buildPython *> (Join-Path $reportPath "${ArtifactPrefix}release.log")
    }
    $buildExit = $LASTEXITCODE
} finally {
    $ErrorActionPreference = 'Stop'
    Pop-Location
}
[ordered]@{ stage=$Stage; started_utc=$started.ToString('o');
    ended_utc=[DateTime]::UtcNow.ToString('o'); exit_code=$buildExit;
    source=$forkPath; build_python=$buildPython; cargo_target=$env:CARGO_TARGET_DIR;
    warning_exception='CARGO_BUILD_WARNINGS=allow (existing Windows linker messages)'
} | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $reportPath "$artifactName-status.json")
Write-Output "$Stage exit_code=$buildExit"
exit $buildExit
