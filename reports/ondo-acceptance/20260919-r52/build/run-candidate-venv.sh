#!/usr/bin/env bash
# Step 5a: create the dedicated candidate venv in the app worktree and install
# the new dist-r52 wheel plus the existing app/test dependency set.
# Every uv pip command pins --python to the exact candidate interpreter.
set -u
OUT="E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/build"
APP="E:/Nautilus-Perps/.worktrees/ondo-r52-app"
CAND="$APP/.venv"
CANDPY="$CAND/Scripts/python.exe"
DIST="E:/nautilus_trader/dist-r52"

export PYTHONUTF8=1
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_PROMPT_MODIFIER CONDA_EXE CONDA_PYTHON_EXE

WHEEL=$(ls "$DIST"/nautilus_trader-*.whl 2>/dev/null | head -1)
if [ -z "$WHEEL" ]; then
  echo "NO_WHEEL_IN_DIST_R52" | tee "$OUT/05-candidate-venv.status"
  exit 1
fi

echo "wheel=$WHEEL" > "$OUT/05-candidate-venv.status"
echo "wheel_sha256=$(sha256sum "$WHEEL" | awk '{print $1}')" >> "$OUT/05-candidate-venv.status"

# Fresh dedicated venv; do not touch the canonical E:/Nautilus-Perps/.venv.
rm -rf "$CAND"
uv venv --python 3.12 --managed-python "$CAND" >> "$OUT/05-candidate-venv.log" 2>&1
RC=$?
if [ $RC -ne 0 ]; then
  echo "EXIT_CODE=$RC" >> "$OUT/05-candidate-venv.status"
  exit $RC
fi

uv pip install --python "$CANDPY" -r "$OUT/candidate-requirements.txt" \
  >> "$OUT/05-candidate-venv.log" 2>&1
RC1=$?

uv pip install --python "$CANDPY" "$WHEEL" >> "$OUT/05-candidate-venv.log" 2>&1
RC2=$?

{
  echo "uv_venv_exit=$RC"
  echo "deps_install_exit=$RC1"
  echo "wheel_install_exit=$RC2"
  echo "candidate_python=$("$CANDPY" --version 2>&1)"
} >> "$OUT/05-candidate-venv.status"

uv pip freeze --python "$CANDPY" > "$OUT/05-candidate-freeze.txt" 2>&1 || true

RC=$(( RC != 0 ? RC : (RC1 != 0 ? RC1 : RC2) ))
echo "EXIT_CODE=$RC" >> "$OUT/05-candidate-venv.status"
echo "EXIT_CODE=$RC"
cat "$OUT/05-candidate-venv.status"
exit $RC
