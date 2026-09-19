#!/usr/bin/env bash
# Step 5: final release wheel bundling the normalized generated stubs.
set -u
OUT="E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/build"
FORK="E:/nautilus_trader/.worktrees/ondo-r52-cleanup"
DIST="E:/nautilus_trader/dist-r52"

export UV_PROJECT_ENVIRONMENT="$FORK/.venv"
export VIRTUAL_ENV=
export CC=clang
export CXX=clang++
export PYTHONUTF8=1
export CARGO_TARGET_DIR="E:/nautilus_trader/.worktrees/task-ondo-r5/target"
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_PROMPT_MODIFIER CONDA_EXE CONDA_PYTHON_EXE

cd "$FORK/python" || exit 99

# Remove the intermediate wheel so the final artifact identity is unambiguous.
rm -f "$DIST"/nautilus_trader-*.whl

echo "START=$(date -u)" > "$OUT/05-final-wheel.status"
START_EPOCH=$(date +%s)

CARGO_BUILD_WARNINGS=allow uv run --no-sync maturin build --release --out "$DIST" \
  -i "$FORK/.venv/Scripts/python.exe" > "$OUT/05-final-wheel.log" 2>&1
RC=$?

END_EPOCH=$(date +%s)
{
  echo "END=$(date -u)"
  echo "EXIT_CODE=$RC"
  echo "WALL_SECONDS=$((END_EPOCH-START_EPOCH))"
} >> "$OUT/05-final-wheel.status"

if [ $RC -eq 0 ]; then
  WHEEL=$(ls "$DIST"/nautilus_trader-*.whl | head -1)
  {
    echo "wheel=$WHEEL"
    echo "wheel_bytes=$(stat -c %s "$WHEEL")"
    echo "wheel_sha256=$(sha256sum "$WHEEL" | awk '{print $1}')"
  } >> "$OUT/05-final-wheel.status"

  # Confirm the bundled Ondo stub carries the native getter property.
  "$FORK/.venv/Scripts/python.exe" - "$WHEEL" > "$OUT/05-final-wheel-stub-check.txt" 2>&1 <<'PY'
import sys, zipfile
wheel = sys.argv[1]
name = "nautilus_trader/adapters/ondo/__init__.pyi"
with zipfile.ZipFile(wheel) as z:
    text = z.read(name).decode("utf-8")
has_class = "class OndoExecutionClientFactory" in text
has_prop = "def supports_ordered_shutdown(self) -> bool: ..." in text
print(f"stub_member={name}")
print(f"has_factory_class={has_class}")
print(f"has_supports_ordered_shutdown={has_prop}")
sys.exit(0 if (has_class and has_prop) else 1)
PY
  echo "stub_check_exit=$?" >> "$OUT/05-final-wheel.status"
fi

echo "EXIT_CODE=$RC"
cat "$OUT/05-final-wheel.status"
exit $RC
