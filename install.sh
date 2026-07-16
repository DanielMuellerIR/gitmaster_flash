#!/bin/zsh
# gitmaster_flash installieren: Selbsttest laufen lassen und den Shell-Wrapper
# (gmf.zsh) in der ~/.zshrc registrieren. Idempotent — ein zweiter Lauf erkennt
# die bestehende Registrierung und ändert nichts.
#
# Aufruf (aus dem geklonten Repo heraus, egal von wo):
#   ./install.sh
#
# Exit-Codes: 0 = installiert bzw. war schon installiert, 1 = Fehler.
set -eu

# ${0:A:h} = Ordner dieses Skripts, absolut aufgelöst (gleicher Trick wie in
# gmf.zsh) — so funktioniert das Skript unabhängig vom Aufrufort.
repo_dir="${0:A:h}"

# 1) Voraussetzung: Python 3 (das Tool selbst ist reine Standardbibliothek).
if ! command -v python3 >/dev/null 2>&1; then
  print -u2 "error: python3 not found — install Python 3 first."
  exit 1
fi

# 2) "Build"-Ersatz: die Unit-Tests sind der Selbsttest, dass das Tool auf
#    dieser Maschine läuft. Schlagen sie fehl, wird nichts registriert.
print "Running self-test ..."
if ! ( cd -- "$repo_dir" && python3 -m unittest discover -s tests -q ); then
  print -u2 "error: self-test failed — not installing."
  exit 1
fi

# 3) Wrapper in der zshrc registrieren. Pfad unter $HOME als ~ schreiben,
#    damit die Zeile maschinenunabhängig lesbar bleibt.
zshrc="${ZDOTDIR:-$HOME}/.zshrc"
source_line="source ${repo_dir/#$HOME/~}/gmf.zsh"

if [[ -f "$zshrc" ]] && grep -qF "gmf.zsh" -- "$zshrc"; then
  existing="$(grep -F "gmf.zsh" -- "$zshrc" | head -1)"
  if [[ "$existing" == "$source_line" ]]; then
    print "Already installed: $zshrc sources gmf.zsh — nothing to do."
  else
    # Es gibt schon eine gmf-Zeile, aber mit anderem Pfad (Repo umgezogen?).
    # Nicht blind doppelt eintragen, sondern dem Menschen überlassen.
    print -u2 "warning: $zshrc already sources gmf.zsh from a different path:"
    print -u2 "    $existing"
    print -u2 "expected:"
    print -u2 "    $source_line"
    print -u2 "Fix the line manually, then re-run."
    exit 1
  fi
else
  printf '%s\n' "$source_line" >> "$zshrc"
  print "Registered wrapper: added '$source_line' to $zshrc"
fi

print "Done. Open a new shell and run: gmf"
