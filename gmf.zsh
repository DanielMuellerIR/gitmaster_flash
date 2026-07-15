# gitmaster_flash Shell-Wrapper.
#
# Warum: Ein Programm kann das Arbeitsverzeichnis der aufrufenden Shell nicht
# ändern. Diese Funktion startet das Tool, liest danach den bei ⏎ gewählten
# Repo-Pfad aus einer Temp-Datei und macht selbst das `cd`.
#
# Installation (einmalig):
#   echo 'source /pfad/zu/gitmaster_flash/gmf.zsh' >> ~/.zshrc
#
# Danach in einer neuen Shell einfach `gmf` (z.B. im Projekt-Sammelordner) aufrufen.

# Skript-Pfad beim Sourcen festhalten: Hier ist $0 noch diese Datei (in der
# Funktion wäre es der Funktionsname). ${0:A:h} = ihr Ordner, absolut aufgelöst.
# So funktioniert der Wrapper unabhängig davon, wohin das Repo geklont wurde.
GMF_SCRIPT="${0:A:h}/gitmaster_flash.py"

gmf() {
  local cdfile target
  cdfile="$(mktemp -t gmf_cd)" || return 1
  python3 "$GMF_SCRIPT" --cd-file "$cdfile" "$@"
  local rc=$?
  target=""
  [[ -s "$cdfile" ]] && target="$(<"$cdfile")"
  rm -f -- "$cdfile"
  if [[ -n "$target" && -d "$target" ]]; then
    cd -- "$target" || return 1
  fi
  return $rc
}
