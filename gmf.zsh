# gitmaster_flash Shell-Wrapper.
#
# Warum: Ein Programm kann das Arbeitsverzeichnis der aufrufenden Shell nicht
# ändern. Diese Funktion startet das Tool, liest danach den bei ⏎ gewählten
# Repo-Pfad aus einer Temp-Datei und macht selbst das `cd`.
#
# Installation (einmalig):
#   printf '\n%s\n' 'source /pfad/zu/gitmaster_flash/gmf.zsh' >> ~/.zshrc
#
# Danach in einer neuen Shell einfach `gmf` (z.B. im Projekt-Sammelordner) aufrufen.

# %x bezeichnet die geladene Datei auch mit abgeschaltetem FUNCTION_ARGZERO.
GMF_SCRIPT="${${(%):-%x}:A:h}/gitmaster_flash.py"

gmf() {
  local cdfile target
  cdfile="$(mktemp "${TMPDIR:-/tmp}/gmf_cd.XXXXXXXX")" || return 1
  python3 "$GMF_SCRIPT" --cd-file "$cdfile" "$@"
  local rc=$?
  target=""
  # Kommandoersetzung entfernt abschließende Zeilenumbrüche auch aus Pfadnamen.
  [[ -s "$cdfile" ]] && { IFS= read -r -d '' target < "$cdfile" || true; }
  rm -f -- "$cdfile"
  if [[ -n "$target" && -d "$target" ]]; then
    cd -- "$target" || return 1
  fi
  return $rc
}
