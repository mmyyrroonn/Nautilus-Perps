#!/usr/bin/env bash
# R3.3 acceptance, run on frozen bytes.
#
# The lesson this run encodes: a green suite is only evidence for the *bytes it
# compiled*, so the hash of every ondo source file is written into the same
# record, before and after each command. If a writer moves the tree under the
# run, the two hashes differ and the run is void.

set -u

OUT="/e/Nautilus-Perps/reports/ondo-acceptance/$(cat /tmp/r33runid)"
SRC=crates/adapters/ondo

hashes() {
  find $SRC/src $SRC/tests -name '*.rs' | sort | xargs sha256sum
}

cd /e/nautilus_trader || exit 1

{
  echo "# R3.3 acceptance environment"
  echo
  echo "date -u: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host shell: $(uname -s) $(uname -r)"
  echo
  echo "## FORK E:\\nautilus_trader"
  echo "branch: $(git branch --show-current)"
  echo "HEAD:   $(git rev-parse HEAD)"
  echo "HEAD message: $(git log -1 --format=%s)"
  echo
  echo "## APP E:\\Nautilus-Perps"
  echo "branch: $(git -C /e/Nautilus-Perps branch --show-current)"
  echo "HEAD:   $(git -C /e/Nautilus-Perps rev-parse HEAD)"
  echo
  echo "## ondo sources and tests, sha256"
  hashes
  echo
  echo "## git status --porcelain, ondo only, non-.pyi"
  git status --porcelain -- $SRC | grep -v '\.pyi$' || true
} > "$OUT/mine_environment.txt"

for step in "fmt:cargo +1.98.0 fmt -p nautilus-ondo -- --check" \
            "suite:cargo +1.98.0 test -p nautilus-ondo --locked --offline" \
            "python_check:cargo +1.98.0 check -p nautilus-ondo --features python --locked --offline" \
            "python_test:cargo +1.98.0 test -p nautilus-ondo --features python --locked --offline --test python"; do
  name=${step%%:*}
  cmd=${step#*:}

  before=$(hashes | sha256sum | cut -d' ' -f1)
  echo "=== $name: $cmd"
  $cmd > "$OUT/mine_$name.txt" 2>&1
  code=$?
  after=$(hashes | sha256sum | cut -d' ' -f1)

  echo "  exit=$code  tree-stable=$([ "$before" = "$after" ] && echo yes || echo NO)"
  grep -E "^(test result|error)" "$OUT/mine_$name.txt" | tail -12
  echo "  before=$before"
  echo "  after =$after"
done
