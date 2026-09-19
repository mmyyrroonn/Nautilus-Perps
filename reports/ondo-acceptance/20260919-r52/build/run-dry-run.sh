#!/usr/bin/env bash
# Step 7: offline public dry-run from the candidate wheel.
# No client, credentials or network: prints the resolved plan and exits.
set -u
OUT="E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/build"
APP="E:/Nautilus-Perps/.worktrees/ondo-r52-app"
CANDPY="$APP/.venv/Scripts/python.exe"

export PYTHON_DOTENV_DISABLED=1
export PYTHONUTF8=1
# Remove venue credential variables by name (never print values).
for v in $(env | grep -i '^ONDO_' | cut -d= -f1); do unset "$v"; done

cd "$APP" || exit 99

echo "START=$(date -u)" > "$OUT/07-dry-run.status"
"$CANDPY" src/ondo_probe.py --mode public --symbols NVDA --dry-run \
  > "$OUT/07-dry-run.json" 2> "$OUT/07-dry-run.stderr.txt"
RC=$?
echo "END=$(date -u)" >> "$OUT/07-dry-run.status"
echo "EXIT_CODE=$RC" >> "$OUT/07-dry-run.status"
echo "EXIT_CODE=$RC"
cat "$OUT/07-dry-run.status"
echo "--- dry-run json ---"
cat "$OUT/07-dry-run.json"
echo "--- stderr (tail) ---"
tail -n 5 "$OUT/07-dry-run.stderr.txt" || true
exit $RC
