#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ ! -x .venv/bin/python ]; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  fi
  uv sync --frozen
fi
echo "Datara Profile Generator：http://127.0.0.1:8765"
exec .venv/bin/python scripts/launch_mac.py
