#!/usr/bin/env bash
# Step 8: one bounded 2-minute public-only probe on NVDA/TSLA with the candidate.
# Hard external timeout of 180 s. No credentials, no .env, no private/sandbox mode.
set -u
OUT="E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/build"
APP="E:/Nautilus-Perps/.worktrees/ondo-r52-app"
CANDPY="$APP/.venv/Scripts/python.exe"
PROBE_OUT="$OUT/public-probe"

export PYTHON_DOTENV_DISABLED=1
export PYTHONUTF8=1
# Remove venue credential variables by name (never print values).
for v in $(env | grep -i '^ONDO_' | cut -d= -f1); do unset "$v"; done

rm -rf "$PROBE_OUT"
cd "$APP" || exit 99

echo "START=$(date -u)" > "$OUT/08-public-probe.status"
START_EPOCH=$(date +%s)

timeout -k 10 180 "$CANDPY" src/ondo_probe.py \
  --mode public --symbols NVDA,TSLA --minutes 2 --out "$PROBE_OUT" \
  > "$OUT/08-public-probe.stdout.log" 2> "$OUT/08-public-probe.stderr.log"
RC=$?

END_EPOCH=$(date +%s)
{
  echo "END=$(date -u)"
  echo "EXIT_CODE=$RC"
  echo "WALL_SECONDS=$((END_EPOCH-START_EPOCH))"
  echo "timeout_exit_124_means_hard_external_timeout=$([ $RC -eq 124 ] && echo yes || echo no)"
} >> "$OUT/08-public-probe.status"
echo "EXIT_CODE=$RC"
cat "$OUT/08-public-probe.status"
echo "--- stdout tail ---"
tail -n 20 "$OUT/08-public-probe.stdout.log" || true
exit $RC
