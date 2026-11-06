#!/bin/sh
# Linux: Laufzeit und Selbsttests prüfen, dann den passenden Shell-Wrapper einrichten.
set -eu
repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.9+ required / Python 3.9+ erforderlich.' \
        'CachyOS: sudo pacman -S --needed python git' \
        'Linux Mint: sudo apt install python3 git' >&2
    exit 1
fi
if ! python3 -c 'import sys; assert sys.version_info >= (3, 9); import curses' >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.9+ with curses required / Python 3.9+ mit curses erforderlich.' \
        'CachyOS: sudo pacman -S --needed python git' \
        'Linux Mint: sudo apt install python3 git' >&2
    exit 1
fi
exec python3 "$repo_dir/tools/install-linux.py" "$@"
