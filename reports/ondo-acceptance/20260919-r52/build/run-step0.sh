#!/usr/bin/env bash
# Step 0: record frozen environment and source hashes.
set -u
OUT="E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/build"
FORK="E:/nautilus_trader/.worktrees/ondo-r52-cleanup"
APP="E:/Nautilus-Perps/.worktrees/ondo-r52-app"

# Build environment (per BUILD_WINDOWS.md + R5 runbook)
export UV_PROJECT_ENVIRONMENT="$FORK/.venv"
export VIRTUAL_ENV=
export CC=clang
export CXX=clang++
export PYTHONUTF8=1
export CARGO_TARGET_DIR="E:/nautilus_trader/.worktrees/task-ondo-r5/target"
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_PROMPT_MODIFIER CONDA_EXE CONDA_PYTHON_EXE

cd "$FORK" || exit 99

{
  echo "=== R5.2 build environment ==="
  echo "date_utc=$(date -u)"
  echo "host_os=$(uname -a)"
  echo "fork_worktree=$FORK"
  echo "app_worktree=$APP"
  echo "base_head=$(git rev-parse HEAD)"
  echo "base_branch=$(git branch --show-current)"
  echo
  echo "--- toolchain ---"
  echo "rustc: $(rustc --version)"
  echo "cargo: $(cargo --version)"
  echo "clang: $(clang --version | head -1)"
  echo "uv: $(uv --version)"
  echo "python(uv managed target): $("$FORK/.venv/Scripts/python.exe" --version 2>/dev/null || echo absent)"
  echo
  echo "--- CARGO_TARGET_DIR ---"
  echo "target=$CARGO_TARGET_DIR"
  echo
  echo "--- git status porcelain (fork) ---"
  git status --porcelain
  echo
  echo "--- tracked modified paths (fork) ---"
  git diff --name-only
  echo
  echo "--- untracked (fork) ---"
  git status --porcelain --untracked-files=all | grep '^??' || echo "(none)"
} > "$OUT/00-env.txt" 2>&1

# Frozen source hashes for the accepted six paths
{
  echo "=== Accepted native source/test SHA256 (must equal review/final-native-review.md) ==="
  for f in \
    crates/adapters/ondo/src/execution.rs \
    crates/adapters/ondo/src/reconciliation.rs \
    crates/adapters/ondo/src/websocket/private/stream.rs \
    crates/adapters/ondo/src/python/factories.rs \
    crates/adapters/ondo/tests/private_runtime.rs \
    crates/adapters/ondo/tests/reconciliation.rs; do
    sha256sum "$f"
  done
  echo
  echo "=== Whole tracked diff SHA256 (git diff, no working-tree newlines translation) ==="
  git diff | sha256sum
  echo
  echo "=== git diff --stat ==="
  git diff --stat
} > "$OUT/00-source-hashes.txt" 2>&1

echo "STEP0_EXIT=0" >> "$OUT/00-env.txt"
cat "$OUT/00-env.txt"
echo "-----"
cat "$OUT/00-source-hashes.txt"
