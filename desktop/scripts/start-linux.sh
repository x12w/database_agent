#!/usr/bin/env bash
set -euo pipefail
DBCHAT_APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [ -d /nix/store ]; then
  DBCHAT_ENGINE="${DBCHAT_ELECTRON:-$(command -v electron || true)}"
  if [ -z "$DBCHAT_ENGINE" ]; then
    DBCHAT_ENGINE="$(find /nix/store -maxdepth 3 -path '*/bin/electron' -print 2>/dev/null | sort -V | tail -1)"
  fi
  if [ -z "$DBCHAT_ENGINE" ]; then
    echo 'NixOS 上请先安装 electron，然后重新启动应用。' >&2
    exit 1
  fi
  export DBCHAT_RESOURCES_DIR="$DBCHAT_APP_DIR/resources"
  exec "$DBCHAT_ENGINE" "$DBCHAT_APP_DIR/resources/app.asar"
fi
exec "$DBCHAT_APP_DIR/dbchat"
