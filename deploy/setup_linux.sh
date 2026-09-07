#!/usr/bin/env bash
# Idempotent remote setup for the read-only spread watcher. Run from the repo
# root ON the target Linux box:
#   bash deploy/setup_linux.sh [path/to/nautilus_trader-*.whl]
#
# Installs uv + Python 3.12 + a project venv + python-dotenv (+ the nautilus
# wheel if given) and pm2 (via nodejs/npm). No keys are touched here.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "== uv =="
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv --version

echo "== python 3.12 =="
uv python install 3.12

echo "== venv =="
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.12 .venv
fi

echo "== deps =="
uv pip install --python .venv/bin/python python-dotenv

WHEEL="${1:-}"
if [ -n "$WHEEL" ]; then
  echo "== nautilus_trader wheel: $WHEEL =="
  uv pip install --python .venv/bin/python "$WHEEL"
else
  echo "== no nautilus_trader wheel given yet (pass one as \$1 to install it) =="
fi

echo "== pm2 =="
if ! command -v pm2 >/dev/null 2>&1; then
  if ! command -v npm >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y nodejs npm
  fi
  npm install -g pm2
fi
pm2 --version

echo "== verification =="
.venv/bin/python --version
if .venv/bin/python -c "import nautilus_trader" 2>/dev/null; then
  echo "nautilus_trader: import OK"
else
  echo "nautilus_trader: import FAILED (expected until the wheel is installed)"
fi
