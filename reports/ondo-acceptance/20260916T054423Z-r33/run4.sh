#!/usr/bin/env bash
# Batch 4: the same five counterproofs, with --no-fail-fast.
#
# Why this exists: batch 3 ran `cargo test --test reconciliation --test
# private_runtime` without --no-fail-fast, and cargo stops at the first test
# binary that fails. private_runtime runs before reconciliation, so in every one
# of the five runs the reconciliation target never executed. The failing names
# batch 3 recorded are therefore complete for private_runtime and *absent* for
# reconciliation - which is not the same thing as "reconciliation did not catch
# it", and must not be written up as either.
#
# With --no-fail-fast both targets run to completion, so each mutation gets a full
# specificity picture: which tests in each target go red, and what survives.

set -u

OUT="/e/Nautilus-Perps/reports/ondo-acceptance/$(cat /tmp/r33runid)"
SNAP=/tmp/r33-accepted
SRC=crates/adapters/ondo
FORK=/e/nautilus_trader

ACCEPTED=ce0803dc9604504f83e0baa47b88b1790ca75fb9719a71a1a7e0021e329c138e
EXECUTION=f6a1e2216e4df5e342b57ce083b833b658363232778a1b146bd4e2e578fc4552
RECONCILIATION=df982eb3014883146f20eed45a19191802517237f4c6e05120f21a5aa39fea64
STREAM=ca801839d5067395a34820ff8b66611ca027adafe177aaa8e0139e1af7c0c961

cd "$FORK" || exit 1

aggregate() { find $SRC/src $SRC/tests -name '*.rs' | sort | xargs sha256sum | sha256sum | cut -d' ' -f1; }
restore() {
  cp "$SNAP/reconciliation.rs" "$FORK/$SRC/src/reconciliation.rs"
  cp "$SNAP/execution.rs"      "$FORK/$SRC/src/execution.rs"
  cp "$SNAP/stream.rs"         "$FORK/$SRC/src/websocket/private/stream.rs"
}
verify() {
  local ok=0 a
  [ "$(sha256sum "$SRC/src/execution.rs" | cut -d' ' -f1)" = "$EXECUTION" ] || { echo "  execution.rs differs"; ok=1; }
  [ "$(sha256sum "$SRC/src/reconciliation.rs" | cut -d' ' -f1)" = "$RECONCILIATION" ] || { echo "  reconciliation.rs differs"; ok=1; }
  [ "$(sha256sum "$SRC/src/websocket/private/stream.rs" | cut -d' ' -f1)" = "$STREAM" ] || { echo "  stream.rs differs"; ok=1; }
  a=$(aggregate); [ "$a" = "$ACCEPTED" ] || { echo "  aggregate differs: $a"; ok=1; }
  return $ok
}

verify >/dev/null || { echo "ABORT: not the accepted bytes"; exit 1; }
python /tmp/r33-mut/mutate.py check >/dev/null || { echo "ABORT: anchors"; exit 1; }

for M in M9 M10 M11 M12 M13; do
  echo
  echo "=== $M (--no-fail-fast) ============================================"
  restore; verify >/dev/null || { echo "ABORT before $M"; exit 1; }
  python /tmp/r33-mut/mutate.py "$M" >/dev/null || { echo "ABORT applying $M"; exit 1; }

  cargo +1.98.0 test -p nautilus-ondo --locked --offline --no-fail-fast \
      --test private_runtime --test reconciliation \
      > "$OUT/r33_mutation_$M.nff.txt" 2>&1
  echo "  cargo exit=$?"
  # Per-target verdicts, in the order cargo ran them.
  python - "$OUT/r33_mutation_$M.nff.txt" <<'PY'
import re, sys
cur, n = None, 0
for ln in open(sys.argv[1], encoding='utf-8', errors='replace'):
    m = re.match(r'\s*Running (?:tests[/\\])?([^\s(]+)', ln)
    if m: cur = m.group(1); continue
    m = re.match(r'^test result: (\w+)\. (\d+) passed; (\d+) failed', ln)
    if m:
        n += 1
        verdict = "RED  " if m.group(1) == 'FAILED' else "green"
        print(f"    {verdict} {cur:<24} {m.group(2)} passed / {m.group(3)} failed")
    m = re.match(r'^test (\S+) \.\.\. FAILED', ln)
    if m: print(f"        caught: {m.group(1)}")
PY

  restore
  if verify >/dev/null; then echo "  restored byte-identical to the accepted content"; else echo "  RESTORE FAILED"; exit 1; fi
done

echo
if verify; then echo "  tree is the accepted content: $ACCEPTED"; else echo "  TREE IS DIRTY - do not commit"; exit 1; fi
