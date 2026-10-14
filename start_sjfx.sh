#!/bin/sh
set -eu
sjfx_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$sjfx_root"
export PYTHONUTF8=1
export PYTHONDONTWRITEBYTECODE=1
if [ -x "$sjfx_root/runtime/bin/python3" ]; then
  sjfx_python="$sjfx_root/runtime/bin/python3"
elif [ -x "$sjfx_root/.venv/bin/python" ]; then
  sjfx_python="$sjfx_root/.venv/bin/python"
else
  sjfx_python=python3
fi
exec "$sjfx_python" -B -X utf8 "$sjfx_root/sjfxctl.py" start "$@"
