#!/usr/bin/env bash
# R3.3 mutation counterproofs, third attempt.
#
# What attempt 2 got wrong: its aggregate used absolute paths while the accepted
# value had been computed with repo-root-relative paths. sha256sum hashes the file
# name too, so those two can never be equal even on identical bytes, and the run
# aborted before doing anything. Its per-file check was broken the same way (the
# snapshot's names carried no directory prefix). Two independent-looking checks
# were in fact the same broken check.
#
# What attempt 1 got wrong: it never proved the snapshot compiled, so all five
# mutations died on a pre-existing error in a half-written tree and none of the
# five results was evidence.
#
# This version: (a) aggregates the way accept.sh did, (b) anchors on three
# hard-coded digests that come from the acceptance record rather than from the
# tree it is checking, and (c) splits the compile verdict out as its own phase, so
# "the build refused the mutation" can never be mistaken for "a test caught it".

set -u

OUT="/e/Nautilus-Perps/reports/ondo-acceptance/$(cat /tmp/r33runid)"
SNAP=/tmp/r33-accepted
SRC=crates/adapters/ondo
FORK=/e/nautilus_trader

# From the R3.3 acceptance record (the values printed by accept.sh before and
# after every command), not from the tree under test.
ACCEPTED=ce0803dc9604504f83e0baa47b88b1790ca75fb9719a71a1a7e0021e329c138e
EXECUTION=f6a1e2216e4df5e342b57ce083b833b658363232778a1b146bd4e2e578fc4552
RECONCILIATION=df982eb3014883146f20eed45a19191802517237f4c6e05120f21a5aa39fea64
STREAM=ca801839d5067395a34820ff8b66611ca027adafe177aaa8e0139e1af7c0c961

cd "$FORK" || exit 1

aggregate() {
  find $SRC/src $SRC/tests -name '*.rs' | sort | xargs sha256sum | sha256sum | cut -d' ' -f1
}

restore() {
  cp "$SNAP/reconciliation.rs" "$FORK/$SRC/src/reconciliation.rs"
  cp "$SNAP/execution.rs"      "$FORK/$SRC/src/execution.rs"
  cp "$SNAP/stream.rs"         "$FORK/$SRC/src/websocket/private/stream.rs"
}

verify() {
  local ok=0
  local e r s
  e=$(sha256sum "$SRC/src/execution.rs" | cut -d' ' -f1)
  r=$(sha256sum "$SRC/src/reconciliation.rs" | cut -d' ' -f1)
  s=$(sha256sum "$SRC/src/websocket/private/stream.rs" | cut -d' ' -f1)
  [ "$e" = "$EXECUTION" ]      || { echo "  execution.rs differs: $e"; ok=1; }
  [ "$r" = "$RECONCILIATION" ] || { echo "  reconciliation.rs differs: $r"; ok=1; }
  [ "$s" = "$STREAM" ]         || { echo "  stream.rs differs: $s"; ok=1; }
  local a; a=$(aggregate)
  [ "$a" = "$ACCEPTED" ] || { echo "  aggregate differs: $a"; ok=1; }
  return $ok
}

echo "=== pre-flight: the tree is the accepted bytes ==="
if verify; then echo "  three digests + aggregate ok"; else echo "  ABORT"; exit 1; fi

echo "=== pre-flight: the accepted bytes compile ==="
if cargo +1.98.0 check -p nautilus-ondo --locked --offline > /tmp/r33-mut/preflight_check.txt 2>&1; then
  echo "  compiles clean"
else
  echo "  ABORT: the frozen bytes do not compile"
  tail -5 /tmp/r33-mut/preflight_check.txt
  exit 1
fi

echo "=== pre-flight: mutation anchors intact ==="
python /tmp/r33-mut/mutate.py check || { echo "ABORT"; exit 1; }

echo
echo "############ PHASE A: does each mutation compile? ############"
for M in M9 M10 M11 M12 M13; do
  restore; verify >/dev/null || { echo "ABORT before $M"; exit 1; }
  python /tmp/r33-mut/mutate.py "$M" >/dev/null || { echo "ABORT applying $M"; exit 1; }
  if cargo +1.98.0 check -p nautilus-ondo --locked --offline > "/tmp/r33-mut/check_$M.txt" 2>&1; then
    echo "  $M compiles  <- valid counterproof candidate"
  else
    echo "  $M DOES NOT COMPILE  <- void, must be re-crafted:"
    grep -m3 '^error' "/tmp/r33-mut/check_$M.txt" | sed 's/^/      /'
  fi
  restore; verify >/dev/null || { echo "ABORT restoring after $M"; exit 1; }
done

echo
echo "############ PHASE B: what does each compiling mutation do to the tests? ############"
for M in M9 M10 M11 M12 M13; do
  echo
  echo "=== $M =============================================================="
  if grep -q '^error' "/tmp/r33-mut/check_$M.txt"; then
    echo "  skipped: did not compile, so it is not evidence either way"
    continue
  fi
  restore; verify >/dev/null || { echo "ABORT before $M"; exit 1; }
  python /tmp/r33-mut/mutate.py "$M" >/dev/null || { echo "ABORT applying $M"; exit 1; }

  cargo +1.98.0 test -p nautilus-ondo --locked --offline \
      --test reconciliation --test private_runtime \
      > "$OUT/r33_mutation_$M.txt" 2>&1
  code=$?
  echo "  cargo exit=$code"
  grep -E '^test result' "$OUT/r33_mutation_$M.txt" | sed 's/^/  /'
  echo "  failed:"
  grep -E '^test .* FAILED' "$OUT/r33_mutation_$M.txt" | sed 's/^/    /' || echo "    (none)"
  echo "  caught-by count: $(grep -cE '^test .* FAILED' "$OUT/r33_mutation_$M.txt")"

  restore
  if verify >/dev/null; then echo "  restored byte-identical to the accepted content"; else echo "  RESTORE FAILED"; exit 1; fi
done

echo
echo "############ final integrity ############"
if verify; then echo "  tree is the accepted content: $ACCEPTED"; else echo "  TREE IS DIRTY - do not commit"; exit 1; fi
