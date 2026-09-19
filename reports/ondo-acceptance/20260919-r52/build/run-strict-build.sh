#!/usr/bin/env bash
# Step 2: STRICT release maturin build. Reuses R5 dependency cache.
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
mkdir -p E:/nautilus_trader/dist-r52

echo "START=$(date -u)" > "$OUT/02-strict-build.status"
START_EPOCH=$(date +%s)

uv run --no-sync maturin build --release --out E:/nautilus_trader/dist-r52 \
  -i "$FORK/.venv/Scripts/python.exe" > "$OUT/02-strict-build.log" 2>&1
RC=$?

END_EPOCH=$(date +%s)
{
  echo "END=$(date -u)"
  echo "EXIT_CODE=$RC"
  echo "WALL_SECONDS=$((END_EPOCH-START_EPOCH))"
  wc=$(grep -c 'warning:' "$OUT/02-strict-build.log" 2>/dev/null || true)
  echo "warning_count=$wc"
} >> "$OUT/02-strict-build.status"
echo "EXIT_CODE=$RC" >> "$OUT/02-strict-build.log"
exit $RC
