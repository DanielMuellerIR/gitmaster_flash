#!/bin/sh
# Prüft die Laufzeit und startet den gemeinsamen Installer.
set -eu
if ! command -v python3 >/dev/null 2>&1 ||
    ! python3 -I -c 'import curses, sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.9+ with curses required / Python 3.9+ mit curses erforderlich.' >&2
    if [ "$(uname -s)" = Linux ]; then
        printf '%s\n' 'CachyOS: sudo pacman -S --needed python git' \
            'Linux Mint: sudo apt install python3 git' >&2
    fi
    exit 1
fi
exec python3 -IB -c '
import runpy
import sys
from pathlib import Path

sys.argv.pop(0)
installer = Path(sys.argv[0]).resolve().parent / "tools/install.py"
runpy.run_path(str(installer), run_name="__main__")
' "$0" "$@"
