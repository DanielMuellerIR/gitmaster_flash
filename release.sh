#!/bin/zsh
# release.sh — versioniertes Quellarchiv von gitmaster_flash nach dist/ legen.
#
# gitmaster_flash ist ein reines Kommandozeilenwerkzeug ohne App-Bundle. Ein
# DMG und eine Notarisierung gibt es deshalb nicht; das Release ist ein
# tar.gz des committeten Standes (git archive), dazu eine SHA-256-Prüfsumme:
#
#   dist/gitmaster_flash-<version>.tar.gz
#   dist/gitmaster_flash-<version>.tar.gz.sha256
#
# Die drei Einstiegspunkte des Projekts trennen bewusst:
#   ./build.sh    Selbsttest, dann die Bilder in docs/ neu erzeugen
#   ./install.sh  Selbsttest, dann den Shell-Wrapper gmf.zsh in ~/.zshrc registrieren
#   ./release.sh  Selbsttest und Bildprüfung, dann versioniertes Quellarchiv nach dist/
#
# Was das Skript sicherstellt, bevor ein Archiv entsteht:
#   1. Der Arbeitsbaum ist committet (keine geänderten versionierten Dateien):
#      Das Archiv kommt aus HEAD, und ein Release soll genau einem Commit
#      entsprechen — nicht einem Stand, den es nirgends sonst gibt.
#   2. Selbsttest grün und README-Bilder aktuell (./build.sh --check).
#   3. Das Archiv lässt sich entpacken, und das Programm darin meldet dieselbe
#      Version wie der Arbeitsbaum.
# Erst dann wandert das Paar aus Archiv und Prüfsumme nach dist/, und zwar
# ohne Überschreiben: Liegt dort schon ein Artefakt dieser Version, bricht das
# Skript ab. Die Artefakte bleiben lokal in dist/.
#
# Aufruf:  ./release.sh
# Letzte Zeile bei Erfolg: RELEASE OK: <pfad-zum-archiv> (<version>)
# Exit-Codes: 0 = fertig, 1 = Prüfung fehlgeschlagen, 2 = Aufruffehler.
set -eu

print_help() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$1"
}
case "${1:-}" in
  "") ;;
  -h|--help) print_help "$0"; exit 0 ;;
  *) print -u2 -- "Unbekannte Option: $1"; print -u2 -- "Aufruf: ./release.sh"; exit 2 ;;
esac

repo_dir="${0:A:h}"
cd -- "$repo_dir"
dist="$repo_dir/dist"

# Alles Zwischenzeitliche liegt in einem eigenen Arbeitsordner dieses Laufs.
# Aufräumen löscht ausschließlich diesen — zusätzlich zum EXIT-Trap auch bei
# INT, TERM und HUP, weil zsh den EXIT-Trap bei TERM nicht ausführt. Die Marke wird
# nach dem Löschen geleert, damit ein zweiter Aufruf nichts mehr tut.
stage=""
cleanup() {
  if [[ -n "$stage" ]]; then
    rm -rf -- "$stage"
    stage=""
  fi
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM
trap 'cleanup; exit 129' HUP

print "=== 1/4 Voraussetzungen ==="
if ! command -v python3 >/dev/null 2>&1; then
  print -u2 -- "FEHLER: python3 nicht gefunden."
  exit 1
fi
version="$(python3 gitmaster_flash.py --version)"
if [[ -z "$version" ]]; then
  print -u2 -- "FEHLER: Programm meldet keine Version."
  exit 1
fi
# Nur versionierte Dateien zählen: Unversioniertes (dist/, __pycache__) stört
# das Archiv nicht, weil git archive es ohnehin nicht einpackt.
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  print -u2 -- "FEHLER: Es gibt nicht committete Änderungen. Ein Release entspricht genau einem Commit —"
  print -u2 -- "        erst committen, dann erneut starten."
  git status --short --untracked-files=no >&2
  exit 1
fi
commit="$(git rev-parse --verify HEAD)"
archive_final="$dist/gitmaster_flash-$version.tar.gz"
sha_final="$archive_final.sha256"
for ziel in "$archive_final" "$sha_final"; do
  if [[ -e "$ziel" ]]; then
    print -u2 -- "FEHLER: $ziel existiert schon — Version erhöhen oder Datei bewusst entfernen."
    exit 1
  fi
done
print "Version $version, Commit $commit, Arbeitsbaum committet."

print "=== 2/4 Selbsttest und README-Bilder ==="
# build.sh --check läuft als einfaches Kommando, nicht in einer Prüfliste:
# Unter set -e bricht ein Fehler darin das Skript ab, und der EXIT-Trap räumt auf.
./build.sh --check
if [[ "$(git rev-parse --verify HEAD)" != "$commit" \
      || -n "$(git status --porcelain --untracked-files=no)" ]]; then
  print -u2 -- "FEHLER: Der Quellstand hat sich während der Prüfung geändert — Release erneut starten."
  exit 1
fi

print "=== 3/4 Archiv packen ==="
# Der Arbeitsordner liegt im Ziel-Dateisystem. Dadurch kann das spaetere
# exklusive Hardlink-Ablegen nicht an einer Dateisystemgrenze scheitern.
mkdir -p -- "$dist"
stage="$(mktemp -d "$dist/.gitmaster_flash-release.XXXXXX")"
pending="$stage/gitmaster_flash-$version.tar.gz"
# --prefix: Beim Entpacken entsteht ein Ordner gitmaster_flash-<version>/,
# nicht ein Haufen Dateien im aktuellen Verzeichnis.
git archive --format=tar.gz --prefix="gitmaster_flash-$version/" -o "$pending" "$commit"

# Gegenprobe am Archiv selbst: entpacken und das Programm darin fragen. So
# fällt auf, wenn HEAD eine andere Version trägt als der Arbeitsbaum (etwa
# eine noch nicht committete Versionsänderung, die oben nicht als "geändert"
# zählte, weil sie in .gitignore stünde — oder ein kaputtes Archiv).
mkdir -p "$stage/probe"
tar -xzf "$pending" -C "$stage/probe"
archive_version="$(python3 "$stage/probe/gitmaster_flash-$version/gitmaster_flash.py" --version)"
if [[ "$archive_version" != "$version" ]]; then
  print -u2 -- "FEHLER: Archiv meldet Version '$archive_version', erwartet '$version'."
  exit 1
fi
for required_path in gitmaster_flash.py gmf.zsh install.sh install-linux.sh \
                     tools/install.py tools/check-zshrc.zsh tests \
                     README.md README.de.md LICENSE; do
  if [[ ! -e "$stage/probe/gitmaster_flash-$version/$required_path" ]]; then
    print -u2 -- "FEHLER: $required_path fehlt im Archiv."
    exit 1
  fi
done
# Prüfsumme nennt nur den Dateinamen, damit `shasum -c` überall funktioniert.
( cd -- "$stage" && shasum -a 256 "gitmaster_flash-$version.tar.gz" > "$pending.sha256" )

print "=== 4/4 Ablegen ==="
# Das Hilfsprogramm reserviert die Version mit einem atomaren mkdir und legt
# beide Dateien per Hardlink an. Ein vorhandenes Ziel wird dabei nie ersetzt,
# selbst wenn es erst nach der Vorpruefung oben entsteht.
python3 tools/install-release-pair.py \
  "$pending" "$pending.sha256" "$archive_final" "$sha_final"

print "    Größe:  $(du -h "$archive_final" | cut -f1)"
print "    Prüfen: cd dist && shasum -c $(basename "$sha_final")"
print "RELEASE OK: $archive_final ($version)"
