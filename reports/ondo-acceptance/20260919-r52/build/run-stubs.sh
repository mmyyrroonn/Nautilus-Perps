#!/usr/bin/env bash
# Step 4: regenerate Python stubs against the release build, then remove the
# Windows generator-only CRLF noise so only semantic Ondo stub changes remain.
set -u
OUT="E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/build"
FORK="E:/nautilus_trader/.worktrees/ondo-r52-cleanup"

export UV_PROJECT_ENVIRONMENT="$FORK/.venv"
export VIRTUAL_ENV=
export CC=clang
export CXX=clang++
export PYTHONUTF8=1
# Coordinator-approved separate cache + canonical nextest stub profile. This keeps the
# release wheel cache (R5 target) untouched by the generator. Makefile: CARGO_CI_PROFILE ?= nextest.
export CARGO_TARGET_DIR="E:/nautilus_trader/target"
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_PROMPT_MODIFIER CONDA_EXE CONDA_PYTHON_EXE

cd "$FORK/python" || exit 99

# Baseline: what the accepted worktree already has under the stub tree.
git status --porcelain -- nautilus_trader > "$OUT/04-stubs-git-before.txt" 2>&1

echo "START=$(date -u)" > "$OUT/04-stubs.status"
START_EPOCH=$(date +%s)

# The strict build already recorded the documented nautilus-persistence-macros
# linker_messages denial; the allowance is for THIS invocation only.
NAUTILUS_STUB_PROFILE=nextest CARGO_BUILD_WARNINGS=allow \
  uv run --no-sync python generate_stubs.py > "$OUT/04-stubs.log" 2>&1
RC=$?

END_EPOCH=$(date +%s)
echo "END=$(date -u)" >> "$OUT/04-stubs.status"
echo "EXIT_CODE=$RC" >> "$OUT/04-stubs.status"
echo "WALL_SECONDS=$((END_EPOCH-START_EPOCH))" >> "$OUT/04-stubs.status"

if [ $RC -eq 0 ]; then
  git status --porcelain -- nautilus_trader > "$OUT/04-stubs-git-raw.txt" 2>&1
  # Remove CRLF-only generator noise; keep the semantic Ondo stub change.
  "$FORK/.venv/Scripts/python.exe" "$OUT/normalize-stubs.py" "$FORK/python/nautilus_trader" \
    > "$OUT/04-stubs-normalize.log" 2>&1
  NRC=$?
  git status --porcelain -- nautilus_trader > "$OUT/04-stubs-git-after.txt" 2>&1
  git diff --stat -- nautilus_trader > "$OUT/04-stubs-diff-stat.txt" 2>&1
  git diff -- nautilus_trader/adapters/ondo/__init__.pyi > "$OUT/04-ondo-stub.diff" 2>&1
  echo "normalize_exit=$NRC" >> "$OUT/04-stubs.status"
fi

git status --porcelain > "$OUT/04-fork-git-status-after-stubs.txt" 2>&1

echo "EXIT_CODE=$RC"
cat "$OUT/04-stubs.status"
echo "--- git status after (stub tree) ---"
cat "$OUT/04-stubs-git-after.txt" 2>/dev/null || true
exit $RC
