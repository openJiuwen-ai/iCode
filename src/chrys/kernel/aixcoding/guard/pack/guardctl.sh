#!/bin/sh
# [guard] Run guardctl.py (the guard switch manager) without uv.
#
# This is the POSIX counterpart of guardctl.cmd: it finds a usable python at
# run time and hands over to guardctl.py next to this script.
#   1. PATH scan for python3 / python;
#   2. the interpreter the packaged iCode unpacks on first run
#      (~/.local/share/pyapp/data/chrys/<hash>/<version>/python/bin/python3).
# Usage:  ./guardctl.sh status | list | show IN-07 | disable IN-07 | on | off
#         ./guardctl.sh workspace restrict | workspace allow | reset
# Keep this file ASCII-only.
set -e

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
script="$here/guardctl.py"
if [ ! -f "$script" ]; then
  echo "[guard] guardctl.py is missing next to this script." >&2
  exit 1
fi

for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    exec "$candidate" "$script" "$@"
  fi
done

for root in \
  "${XDG_DATA_HOME:-$HOME/.local/share}/pyapp/data/chrys" \
  "$HOME/Library/Application Support/pyapp/data/chrys"; do
  [ -d "$root" ] || continue
  found=$(find "$root" -type f -name 'python3*' -path '*/bin/*' 2>/dev/null | head -n 1)
  if [ -n "$found" ]; then
    exec "$found" "$script" "$@"
  fi
done

echo "[guard] no usable python interpreter found (PATH or packaged runtime)" >&2
exit 1
