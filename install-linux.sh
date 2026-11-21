#!/bin/sh
# Der bisherige Linux-Einstieg behält seine Shell-Auswahl.
exec "$(dirname -- "$0")/install.sh" --linux-only "$@"
