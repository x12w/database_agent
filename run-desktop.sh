#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [ ! -x venv/bin/python ]; then
  echo '请先创建 Python 3.13 环境并安装项目依赖：uv venv --python 3.13 venv && uv pip install --python venv/bin/python -e .'
  exit 1
fi
if [ ! -d web/node_modules ]; then npm --prefix web ci --no-audit --no-fund; fi
npm --prefix web run build
if [ ! -d desktop/node_modules ]; then
  if [ -d /nix/store ]; then
    ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm --prefix desktop ci --no-audit --no-fund
  else
    npm --prefix desktop ci --no-audit --no-fund
  fi
fi
exec node desktop/scripts/launch.cjs
