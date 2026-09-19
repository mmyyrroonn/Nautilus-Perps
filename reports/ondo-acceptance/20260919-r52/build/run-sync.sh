#!/usr/bin/env bash
# Step 1: uv sync the fork worktree build venv.
set -u
OUT="E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/build"
FORK="E:/nautilus_trader/.worktrees/ondo-r52-cleanup"

export UV_PROJECT_ENVIRONMENT="$FORK/.venv"
export VIRTUAL_ENV=
export CC=clang
export CXX=clang++
export PYTHONUTF8=1
export CARGO_TARGET_DIR="E:/nautilus_trader/.worktrees/task-ondo-r5/target"
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_PROMPT_MODIFIER CONDA_EXE CONDA_PYTHON_EXE

cd "$FORK/python" || exit 99

# Remove any empty/partial venv from a prior interrupted run
if [ -d "$FORK/.venv" ] && [ ! -f "$FORK/.venv/Scripts/python.exe" ]; then
  rm -rf "$FORK/.venv"
fi

echo "START=$(date -u)" > "$OUT/01-uv-sync.status"
uv sync --all-groups --all-extras --no-install-package nautilus-trader \
        --inexact --managed-python --python 3.12 > "$OUT/01-uv-sync.log" 2>&1
RC=$?
echo "END=$(date -u)" >> "$OUT/01-uv-sync.status"
echo "EXIT_CODE=$RC" >> "$OUT/01-uv-sync.status"
if [ -f "$FORK/.venv/Scripts/python.exe" ]; then
  "$FORK/.venv/Scripts/python.exe" --version >> "$OUT/01-uv-sync.status" 2>&1
fi
echo "EXIT_CODE=$RC"
exit $RC
