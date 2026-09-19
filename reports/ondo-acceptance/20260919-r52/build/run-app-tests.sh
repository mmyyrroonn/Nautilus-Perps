#!/usr/bin/env bash
# Step 6: full app test suite from the app worktree with the candidate interpreter.
set -u
OUT="E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/build"
APP="E:/Nautilus-Perps/.worktrees/ondo-r52-app"
CAND="$APP/.venv"
CANDPY="$CAND/Scripts/python.exe"

export PYTHON_DOTENV_DISABLED=1
export PYTHONUTF8=1
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_PROMPT_MODIFIER CONDA_EXE CONDA_PYTHON_EXE

cd "$APP" || exit 99

echo "START=$(date -u)" > "$OUT/06-app-tests.status"
START_EPOCH=$(date +%s)
"$CANDPY" -m pytest tests -q -p no:cacheprovider > "$OUT/06-app-tests.log" 2>&1
RC=$?
END_EPOCH=$(date +%s)
{
  echo "END=$(date -u)"
  echo "EXIT_CODE=$RC"
  echo "WALL_SECONDS=$((END_EPOCH-START_EPOCH))"
  echo "interpreter=$("$CANDPY" -c 'import sys;print(sys.executable)' 2>&1)"
  echo "nautilus_file=$("$CANDPY" -c 'import nautilus_trader;print(nautilus_trader.__file__)' 2>&1)"
} >> "$OUT/06-app-tests.status"
echo "EXIT_CODE=$RC" >> "$OUT/06-app-tests.log"
cat "$OUT/06-app-tests.status"
tail -n 5 "$OUT/06-app-tests.log"
exit $RC
