#!/bin/zsh
# build.sh — Selbsttest laufen lassen und die README-Bilder erzeugen.
#
# gitmaster_flash ist eine einzelne Python-Datei ohne Übersetzungsschritt. Was
# dieses Projekt trotzdem "baut", sind die Bilder in docs/: Sie sind keine
# Screenshots, sondern werden aus dem echten Programm in einem Pseudo-Terminal
# erzeugt (docs/make-screens.py) und mitcommittet. Nach einer UI-Änderung
# müssen sie neu entstehen — genau das tut dieses Skript, nach grünem Selbsttest.
#
# Die drei Einstiegspunkte des Projekts trennen bewusst:
#   ./build.sh    Selbsttest, dann die Bilder in docs/ neu erzeugen
#   ./install.sh  Selbsttest, dann den Shell-Wrapper gmf.zsh in ~/.zshrc registrieren
#   ./release.sh  Selbsttest und Bildprüfung, dann versioniertes Quellarchiv nach dist/
#
# Aufruf:
#   ./build.sh            # Bilder neu erzeugen
#   ./build.sh --check    # nur prüfen, ob die Bilder noch aktuell sind (nichts schreiben)
#
# Beide Wege brauchen kein Netz und kein Fenster; der Bildgenerator schickt
# seine Tastendrücke nur an den eigenen Kindprozess.
#
# Letzte Zeile bei Erfolg: BUILD OK: <docs-ordner> (<version>)
# Exit-Codes: 0 = fertig, 1 = Selbsttest oder Bilder fehlgeschlagen, 2 = Aufruffehler.
set -eu

print_help() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$1"
}

CHECK=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    -h|--help) print_help "$0"; exit 0 ;;
    *) print -u2 -- "Unbekannte Option: $arg"
       print -u2 -- "Aufruf: ./build.sh [--check]"; exit 2 ;;
  esac
done

# ${0:A:h} = Ordner dieses Skripts, absolut aufgelöst — derselbe Trick wie in
# install.sh und gmf.zsh, damit der Aufrufort egal ist.
repo_dir="${0:A:h}"
cd -- "$repo_dir"

if ! command -v python3 >/dev/null 2>&1; then
  print -u2 -- "FEHLER: python3 nicht gefunden."
  exit 1
fi
version="$(python3 gitmaster_flash.py --version)"

print "=== 1/2 Selbsttest ==="
if ! python3 -m unittest discover -s tests -q; then
  print -u2 -- "FEHLER: Selbsttest rot — es werden keine Bilder erzeugt."
  exit 1
fi

if (( CHECK )); then
  print "=== 2/2 README-Bilder prüfen ==="
  # --check schlägt fehl, wenn die Bilder neu erzeugt werden müssten (nach
  # UI-Änderungen also ./build.sh laufen lassen und das Ergebnis committen).
  if ! python3 docs/make-screens.py --check; then
    print -u2 -- "FEHLER: Die Bilder in docs/ sind nicht mehr aktuell — ./build.sh ausführen."
    exit 1
  fi
else
  print "=== 2/2 README-Bilder erzeugen ==="
  if ! python3 docs/make-screens.py; then
    print -u2 -- "FEHLER: Bilder konnten nicht erzeugt werden."
    exit 1
  fi
fi

print "BUILD OK: $repo_dir/docs ($version)"
