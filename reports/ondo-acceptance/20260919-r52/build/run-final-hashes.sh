#!/usr/bin/env bash
set -u
OUT="E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/build"
cd "E:/nautilus_trader/.worktrees/ondo-r52-cleanup" || exit 99
{
  echo "final_diff_sha256=$(git diff | sha256sum | awk '{print $1}')"
  echo "ondo_stub_diff_sha256=$(git diff -- python/nautilus_trader/adapters/ondo/__init__.pyi | sha256sum | awk '{print $1}')"
  echo "base_head=$(git rev-parse HEAD)"
  echo "final_status_lines=$(git status --porcelain | wc -l)"
} > "$OUT/00-final-hashes.txt" 2>&1
cat "$OUT/00-final-hashes.txt"
