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

# 3) Wrapper in der zshrc registrieren. Der absolute Pfad wird als vollständiges
#    zsh-Wort serialisiert; Leerzeichen, Quotes und Metazeichen bleiben Daten.
zshrc="${ZDOTDIR:-$HOME}/.zshrc"
wrapper_path="$repo_dir/gmf.zsh"
quoted_wrapper="${(qqq)wrapper_path}"
source_line="source -- $quoted_wrapper"

# Beide Scanner unten müssen dieselbe zsh-Grammatik zugrunde legen. Erkennt der
# eine ein Wort als Zuweisung oder Trenner, das der andere für den Kommandonamen
# hält, geraten ihre Vorstellungen vom Kommandoanfang auseinander — und der
# Installer registrierte doppelt oder lehnte eine gültige Datei ab. Deshalb
# stehen diese drei Regeln genau einmal.

# Ein Wort der Form NAME=…, NAME[i]=… oder NAME+=… vor dem eigentlichen Kommando.
is_assignment_word() {
  [[ "$1" =~ '^[A-Za-z_][A-Za-z0-9_]*(\[.*\])?\+?=' ]]
}

# Dieselbe Zuweisung, die eine über mehrere Zeilen laufende Array-Klammer öffnet.
opens_assignment_array() {
  [[ "$1" =~ '^[A-Za-z_][A-Za-z0-9_]*(\[.*\])?\+?=\($' ]]
}

# Wortweise Kommandotrenner; danach beginnt ein neues Kommando.
is_command_separator() {
  case "$1" in
    ';'|';;'|';&'|';|'|'|'|'|&'|'&&'|'||'|'&'|'&!'|'&|') return 0 ;;
  esac
  return 1
}

# Präfixe, die zsh vor dem eigentlichen Kommandonamen erlaubt.
is_command_modifier() {
  [[ "$1" == noglob || "$1" == nocorrect || "$1" == time \
     || "$1" == command || "$1" == builtin ]]
}

# Auch die Blockgrammatik gehört zu den Regeln, die beide Scanner teilen
# müssen: `if`, die Schleifen und `case` machen einen Block auf, den jeweils ein
# eigenes Wort wieder schließt. Stünden diese Listen zweimal im Skript, ließe
# schon ein einziges nur einseitig ergänztes Schlüsselwort die beiden
# Vorstellungen vom Blockkontext auseinanderlaufen — der eine Scanner hielte
# eine Zeile für Top-Level, der andere für Blockinneres. Beide Ausgänge sind
# falsch: doppelt eintragen oder ein nie ausgeführtes `source` als
# Registrierung anerkennen. Beide Funktionen liefern die ART des Blocks in
# `block_kind` — der Mehrzeilen-Stapel braucht sie, um Anfang und Ende einander
# zuzuordnen. Über eine Variable statt über die Ausgabe, weil sie je Wort
# aufgerufen werden und eine Kommandoersetzung dafür jedes Mal einen Prozess
# kostete; die Aufrufer deklarieren `block_kind` selbst als `local`.
block_opener_kind() {
  case "$1" in
    if) block_kind=if ;;
    for|foreach|while|until|select|repeat) block_kind=loop ;;
    'case') block_kind=case ;;
    *) block_kind=""; return 1 ;;
  esac
}

block_closer_kind() {
  case "$1" in
    fi) block_kind=if ;;
    # `end` schließt die zsh-eigene Schleifenform `foreach x (a b) … end`.
    # Ohne diese Zeile blieb der Blockstapel bis zum Dateiende offen, und der
    # Installer lehnte eine völlig gültige .zshrc mit "unclosed or
    # unsupported shell block" ab, statt sich zu registrieren (Fund
    # 2026-08-29).
    done|end) block_kind=loop ;;
    'esac') block_kind=case ;;
    *) block_kind=""; return 1 ;;
  esac
}

# zsh kennt zu jeder Schleife eine Kurzform, deren Rumpf kein eigenes Schlusswort
# hat, sondern nach EINER Teilliste zu Ende ist:
#
#   for x (a b) print $x            repeat 2 print hi
#   for ((i=0;i<2;i++)) print $i    select x (a b) print $x
#   for x in a b; print $x          for x (a b) { print $x }
#
# `while` und `until` sehen genauso aus und gehören trotzdem NICHT dazu — die
# Begründung steht unten bei `loop_head_end`.
#
# Ohne sie blieb der Blockstapel bis zum Dateiende offen, und der Installer
# lehnte eine völlig gültige .zshrc mit "unclosed or unsupported shell block" ab
# (Fund 2026-08-29). Der Rumpf lässt sich lexikalisch abgrenzen, sobald der KOPF
# der Schleife feststeht; danach gilt für alle Formen dieselbe Regel: führende
# Trenner überspringen, dann entscheidet das erste Wort —
#
#   `do`    doch die lange Fassung, ihr `done` schließt sie
#   `{`     der Rumpf ist ein Klammerblock, das `}` schließt ihn
#   sonst   der Rumpf ist genau eine Teilliste und endet an ihrem Ende
#
# Belegt am Verhalten der Shell selbst (2026-08-29): `for x (a b) print $x; print
# E` gibt "a b E" aus — das `;` beendet den Rumpf. `repeat 2; print hi` gibt
# dagegen "hi hi" aus — ein Trenner VOR dem Rumpf gehört noch zum Kopf. Und `for
# x (a b)` mit dem Rumpf erst auf der nächsten Zeile ist ebenfalls gültig;
# dafür steht der Stapeleintrag `shortpending`.

# Ein Trenner, der eine Teilliste wirklich BEENDET. `&&`, `||`, `|` und `|&`
# verbinden dagegen weiter zu einer einzigen Teilliste: `for x (a b) print $x &&
# print y` führt beide Befehle je Durchlauf aus, der Rumpf reicht also über das
# `&&` hinweg.
ends_sublist() {
  case "$1" in
    ';'|';;'|';&'|';|'|'&'|'&!'|'&|') return 0 ;;
  esac
  return 1
}

# Wo endet der KOPF einer Schleife? Setzt `loop_head` auf den Index seines
# letzten Wortes und liefert 0, wenn der Kopf auf dieser Zeile vollständig ist.
# Liefert 1, wenn sich das nicht belegen lässt — dann bleibt es bei der
# konservativen langen Fassung.
loop_head_end() {
  local -i start="$1" i n open=0 depth=0
  shift
  local -a words=("$@")
  local closer
  n=${#words}
  loop_head=0
  case "${words[start]}" in
    foreach)
      # `foreach` hat keine Kurzform: Sein Rumpf endet an `end`.
      return 1
      ;;
    while|until)
      # Ihre Bedingung ist eine Liste ohne lexikalisch erkennbares Ende
      # (Begründung unten beim `in`-Fall).
      return 1
      ;;
    repeat)
      # Der Kopf ist das Schlüsselwort und die Anzahl — genau ein Wort.
      (( start + 1 <= n )) || return 1
      ends_sublist "${words[start+1]}" && return 1
      [[ "${words[start+1]}" == do || "${words[start+1]}" == '{' ]] && return 1
      loop_head=$((start + 1))
      return 0
      ;;
  esac
  # `for`/`select` mit Klammerliste oder arithmetischem Kopf: Der Kopf endet an
  # der passenden schließenden Klammer.
  if [[ "${words[start+1]:-}" == '((' ]]; then
    open=$((start + 1))
    closer='))'
  elif [[ "${words[start+2]:-}" == '(' ]]; then
    open=$((start + 2))
    closer=')'
  fi
  if (( open )); then
    for ((i = open; i <= n; i++)); do
      [[ "${words[i]}" == \#* ]] && return 1
      [[ "${words[i]}" == "${words[open]}" ]] && depth=$((depth + 1))
      if [[ "${words[i]}" == "$closer" ]]; then
        depth=$((depth - 1))
        if (( depth == 0 )); then
          loop_head=$i
          return 0
        fi
      fi
    done
    return 1                      # Die Klammer schließt auf dieser Zeile nicht.
  fi
  # Bleibt die `in`-Fassung: `for x in a b; print $x`. Ihre Wortliste kann kein
  # `;` enthalten, der erste echte Trenner beendet also den Kopf. Steht davor
  # ein `do`, ist es die lange Fassung.
  #
  # `while` und `until` sind hier bewusst NICHT dabei, obwohl sie gleich
  # aussehen: Ihre Bedingung ist eine LISTE, und ein `;` darin beendet sie
  # nicht. `while false; print x` läuft endlos — die Bedingung ist `false;
  # print x` und endet mit dem Status von `print`, also wahr (gemessen
  # 2026-08-29). Wer das für Kopf und Rumpf hielte, erklärte eine Zeile für
  # abgeschlossen, die die Datei in Wahrheit nie verlässt, und der Installer
  # meldete „Already installed", ohne dass `gmf` je entsteht.
  [[ "${words[start+2]:-}" == in ]] || return 1
  for ((i = start + 3; i <= n; i++)); do
    [[ "${words[i]}" == \#* ]] && return 1
    [[ "${words[i]}" == do ]] && return 1
    if ends_sublist "${words[i]}"; then
      loop_head=$((i - 1))
      return 0
    fi
  done
  return 1
}

# Wie weit reicht der Rumpf hinter einem fertigen Kopf? Setzt `short_loop_kind`
# auf `line` (Rumpf endet auf dieser Zeile, `short_loop_end` nennt sein letztes
# Wort), `do`, `brace` oder `pending` (der Rumpf beginnt erst später).
short_loop_body() {
  local -i start="$1" i j n begun=0 brace=0
  shift
  local -a words=("$@")
  local word
  n=${#words}
  short_loop_kind=pending
  short_loop_end=0
  for ((i = start; i <= n; i++)); do
    word="${words[i]}"
    [[ "$word" == \#* ]] && break
    if (( ! begun )); then
      ends_sublist "$word" && continue           # Trenner noch vor dem Rumpf
      if [[ "$word" == do ]]; then
        short_loop_kind=do
        return 0
      fi
      if [[ "$word" == '{' ]]; then
        # Der Rumpf ist ein Klammerblock. Schließt er auf derselben Zeile, endet
        # der Rumpf dort; sonst trägt ihn der Blockstapel weiter. Das `}` steht
        # in dieser Form oft ohne vorangehendes `;` und wäre dann für die
        # Kommandoanfang-Erkennung des Stapels unsichtbar.
        for ((j = i; j <= n; j++)); do
          [[ "${words[j]}" == \#* ]] && break
          [[ "${words[j]}" == '{' ]] && brace=$((brace + 1))
          if [[ "${words[j]}" == '}' ]]; then
            brace=$((brace - 1))
            if (( brace == 0 )); then
              short_loop_kind=line
              short_loop_end=$j
              return 0
            fi
          fi
        done
        short_loop_kind=brace
        return 0
      fi
      begun=1
      continue
    fi
    if ends_sublist "$word"; then
      short_loop_kind=line
      short_loop_end=$i
      return 0
    fi
  done
  if (( begun )); then
    # Ohne Trenner endet der Rumpf am Zeilenende.
    short_loop_kind=line
    short_loop_end=$n
  fi
  return 0
}

# Denselben Pfad kann man verschieden schreiben: `~/git/...`, `$HOME/git/...`,
# mit oder ohne Quotes. Ein reiner Textvergleich hielte das für ein anderes Repo
# und verlangte grundlos Handarbeit — deshalb wird der Pfad aus der bestehenden
# Zeile herausgelöst und aufgelöst verglichen. Bewusst OHNE eval: die .zshrc ist
# hier Datei-Inhalt, kein Code, den dieses Skript ausführen darf.
resolve_sourced_path() {
  local line="$1" word path raw_path last_path="" modifier_mode="" block_kind=""
  local short_loop_kind=""
  local -a words top_level
  local -i command_start=1 i paren_depth=0 assignment_depth=0 \
    in_pipeline=0 in_conditional=0 command_wrapper=0 scan_command_start=1 \
    block_depth=0 loop_head=0 short_loop_end=0 short_close=0
  # zshs `(z)`-Lexer trennt wie die Shell, fuehrt den Inhalt aber nicht aus.
  # Quotes bleiben am Token und ein `#` innerhalb von Quotes wird deshalb nie
  # mit einem Kommentar verwechselt. Ebenso bleibt `source` in einem
  # gequoteten echo-Text Daten statt eines vermeintlichen Kommandos.
  words=("${(z)line}")
  # Erster Durchgang: Ein kompletter Kontrollblock kann auf derselben physischen
  # Zeile stehen, und ein `source` DARIN laeuft beim Shellstart womoeglich nie.
  # Frueher verwarf schon ein einziges Kontrollwort die ganze Zeile — damit galt
  # auch ein `source` VOR dem Block oder hinter einem laengst geschlossenen
  # Block nicht als Registrierung, obwohl zsh es nachweislich im Top-Level
  # ausfuehrt; der Installer trug dann ein zweites Mal ein. Deshalb wird jetzt
  # die Blocktiefe mitgezaehlt und je Wort vermerkt, ob es ausserhalb jedes
  # Blocks steht. Nur solche Woerter darf der zweite Durchgang als Kandidat
  # nehmen.
  for ((i = 1; i <= ${#words}; i++)); do
    word="${words[i]}"
    [[ "$word" == \#* ]] && break
    # Hinter dem Rumpf einer Kurzform steht die Zeile wieder auf oberster Ebene.
    if (( short_close && i > short_close )); then
      block_depth=$((block_depth - 1))
      short_close=0
    fi
    top_level[i]=$(( block_depth == 0 ))
    if is_command_separator "$word"; then
      scan_command_start=1
      continue
    fi
    case "$word" in
      '()'|'{'|'}')
        # Funktions-/Brace-Syntax kann hinter einem Namen stehen und ist auch
        # dort ein Kontrollkontext, kein gewöhnliches Argument.
        return 1
        ;;
    esac
    if (( scan_command_start )) && is_assignment_word "$word"; then
      continue
    fi
    if (( scan_command_start )) && is_command_modifier "$word"; then
      continue
    fi
    if (( ! scan_command_start )); then
      continue
    fi
    if block_opener_kind "$word" || [[ "$word" == function ]]; then
      # Blockanfang: alles bis zum passenden Schlüsselwort steht darin. Welche
      # Art es ist, spielt hier keine Rolle — gezählt wird nur die Tiefe.
      block_depth=$((block_depth + 1))
      top_level[i]=0
      # Bei einer Kurzform endet dieser Block schon am Ende ihrer Teilliste.
      # Nur eine je Zeile: Verschachtelte Kurzformen bleiben konservativ offen.
      if [[ "$block_kind" == loop ]] && (( ! short_close )) \
          && loop_head_end "$i" "${words[@]}"; then
        short_loop_body $((loop_head + 1)) "${words[@]}"
        [[ "$short_loop_kind" == line ]] && short_close=$short_loop_end
      fi
    elif block_closer_kind "$word"; then
      if (( block_depth == 0 )); then
        # Ein Blockende ohne Anfang auf dieser Zeile: Der Kontext kommt von
        # weiter oben, hier lässt sich nichts belegen.
        return 1
      fi
      block_depth=$((block_depth - 1))
      top_level[i]=0
    else
      case "$word" in
        then|elif|else|do)
          # Diese Wörter gehören in einen bereits gezählten Block. Ohne
          # Blockanfang auf derselben Zeile bleibt der Kontext unklar. Sie
          # stehen bewusst nur hier: Der Mehrzeilen-Stapel unten braucht von
          # ihnen nur `then` und `do`, und das für eine andere Frage.
          (( block_depth == 0 )) && return 1
          top_level[i]=0
          ;;
      esac
    fi
    scan_command_start=0
  done
  for ((i = 1; i <= ${#words}; i++)); do
    word="${words[i]}"
    [[ "$word" == \#* ]] && break
    if (( assignment_depth )); then
      [[ "$word" == '(' ]] && assignment_depth=$((assignment_depth + 1))
      [[ "$word" == ')' ]] && assignment_depth=$((assignment_depth - 1))
      continue
    fi
    if (( command_start )) && opens_assignment_array "$word"; then
      assignment_depth=1
      continue
    fi
    if (( command_start )) && is_assignment_word "$word"; then
      # Zuweisungen dürfen einem Shell-Kommando vorangestellt sein. Sie ändern
      # nicht, dass ein folgendes return/exit/exec den Rest unerreichbar macht.
      continue
    fi
    if (( command_start )) && [[ "$word" == command ]]; then
      # `command source` sucht ein externes Programm und lädt den Wrapper in
      # zsh gerade nicht. Andere Builtins wie `command exit` bleiben dennoch
      # als folgendes Kommando sichtbar.
      command_wrapper=1
      continue
    fi
    if (( command_start )); then
      case "$word" in
        time)
          # `time` darf am Anfang stehen, aber nicht hinter einem anderen
          # Modifier (`time time`, `noglob time`, `builtin time` sind andere
          # oder ungültige Befehle und sourcen nicht in diese Shell).
          if [[ -n "$modifier_mode" ]]; then
            command_start=0
            modifier_mode=""
            continue
          fi
          modifier_mode=time
          continue
          ;;
        nocorrect)
          # Hinter `noglob`/`builtin` ist nocorrect kein weiterer Modifier.
          if [[ "$modifier_mode" == noglob || "$modifier_mode" == builtin ]]; then
            command_start=0
            modifier_mode=""
            continue
          fi
          modifier_mode=nocorrect
          continue
          ;;
        noglob)
          # zsh akzeptiert noglob auch hinter time, nocorrect und builtin.
          modifier_mode=noglob
          continue
          ;;
        builtin)
          # builtin darf selbst auf einen Modifier folgen; danach bleiben nur
          # echte Builtins beziehungsweise `noglob` als nächste Stufe sicher.
          modifier_mode=builtin
          continue
          ;;
      esac
    fi
    if (( paren_depth == 0 && command_start && ! in_pipeline && ! in_conditional )) \
        && [[ "$word" == "return" || "$word" == "exit" || "$word" == "exec" ]]; then
      # Alles rechts davon ist in dieser Datei unerreichbar. Eine spätere
      # source-Anweisung darf deshalb keine wirksame Registrierung belegen.
      return 1
    fi
    if (( paren_depth == 0 && command_start && ! in_pipeline && ! in_conditional \
          && top_level[i] )) \
        && [[ "$word" == "source" || "$word" == "." ]]; then
      if (( command_wrapper )); then
        # Nur dieses von `command` verdeckte Builtin ist unwirksam. Nach einem
        # echten Separator kann auf derselben Zeile noch eine gültige
        # Registrierung folgen, die für die Idempotenz weiter zählen muss.
        (( i++ ))
        [[ "${words[i]:-}" == "--" ]] && (( i++ ))
        [[ -n "${words[i]:-}" ]] || { command_start=0; continue; }
        command_wrapper=0
        command_start=0
        continue
      fi
      (( i++ ))
      [[ "${words[i]:-}" == "--" ]] && (( i++ ))
      [[ -n "${words[i]:-}" ]] || { command_start=0; continue; }
      raw_path="${words[i]}"
      path="${(Q)raw_path}"          # eine Ebene Shell-Quotes entfernen, ohne eval
      if [[ "$raw_path" == "$quoted_wrapper" ]]; then
        # Genau diese serialisierte Form schreibt der Installer selbst. Darin
        # sind Dollarzeichen und andere Metazeichen bereits als Literale
        # escaped; die allgemeine Variablenpruefung darf sie nicht verwerfen.
        path="$wrapper_path"
      else
      case "$raw_path" in
        \'*\')
          # Innerhalb einfacher Quotes sind alle Zeichen wörtlich. Ein
          # absoluter Pfad darf deshalb auch ein echtes Dollarzeichen tragen;
          # $HOME und ~ fallen unten bereits an der Absolutheitsprüfung durch.
          ;;
        \"*\")
          # Doppelte Quotes erlauben Parameter-, aber keine Tilde-Expansion.
          [[ "$path" == "~"* ]] && { path=""; command_start=0; continue; }
          if [[ "$path" == '${HOME}' || "$path" == '${HOME}/'* ]]; then
            path="$HOME${path#\$\{HOME\}}"
          elif [[ "$path" == '$HOME' || "$path" == '$HOME/'* ]]; then
            path="$HOME${path#\$HOME}"
          fi
          [[ "$path" == *'$'* ]] && { path=""; command_start=0; continue; }
          ;;
        *\'*|*\"*)
          # Gemischte Quote-Formen wären ohne echte Shell-Ausführung
          # mehrdeutig. Für die Erkennung sicherheitshalber nicht raten.
          path=""
          command_start=0
          continue
          ;;
        *)
          # Bei einem escaped Dollar wäre die Expansion ebenfalls wörtlich.
          if [[ "$raw_path" == *\\* ]] \
              && [[ "$path" == *'${HOME}'* || "$path" == *'$HOME'* ]]; then
            path=""
            command_start=0
            continue
          fi
          if [[ "$path" == '${HOME}' || "$path" == '${HOME}/'* ]]; then
            path="$HOME${path#\$\{HOME\}}"
          elif [[ "$path" == '$HOME' || "$path" == '$HOME/'* ]]; then
            path="$HOME${path#\$HOME}"
          elif [[ "$raw_path" == "~" || "$raw_path" == "~/"* ]]; then
            path="$HOME${path#\~}"
          elif [[ "$raw_path" == "~"* ]]; then
            # ~anderer gehört zum Home eines anderen Nutzers. Ohne Shell-
            # Ausführung lässt sich das hier nicht verlässlich auflösen.
            path=""
            command_start=0
            continue
          fi
          [[ "$path" == *'$'* || "$path" == "~"* ]] \
            && { path=""; command_start=0; continue; }
          ;;
      esac
      fi
      # Ein relativer Source-Pfad hängt beim Shellstart vom jeweiligen
      # Arbeitsverzeichnis ab. Der Installer darf ihn nicht zufällig gegen sein
      # eigenes cwd auflösen und daraus eine wirksame Registrierung ableiten.
      [[ "$path" == /* ]] || { path=""; command_start=0; continue; }
      path="${path:A}"
      command_start=0
      continue
    fi
    case "$word" in
      '(')
        command_wrapper=0
        modifier_mode=""
        paren_depth=$((paren_depth + 1))
        path=""
        command_start=1
        ;;
      ')')
        command_wrapper=0
        modifier_mode=""
        if (( paren_depth > 0 )); then
          paren_depth=$((paren_depth - 1))
        fi
        path=""
        command_start=0
        ;;
      '|'|'|&')
        command_wrapper=0
        modifier_mode=""
        # Pipeline-Kommandos laufen nicht verlaesslich in der aktuellen Shell;
        # eine dort definierte gmf-Funktion ist keine wirksame Registrierung.
        path=""
        in_pipeline=1
        command_start=1
        ;;
      '&'|'&!'|'&|')
        command_wrapper=0
        modifier_mode=""
        # Ein Hintergrund-Kommando veraendert die aufrufende Shell ebenfalls
        # nicht. Danach beginnt aber ein neues, wieder wirksames Kommando.
        path=""
        in_pipeline=0
        command_start=1
        ;;
      ';')
        command_wrapper=0
        modifier_mode=""
        if (( paren_depth == 0 && ! in_pipeline && ! in_conditional )) \
            && [[ -n "$path" && "${path:t}" == gmf.zsh ]]; then
          last_path="$path"
        fi
        path=""
        in_pipeline=0
        in_conditional=0
        command_start=1
        ;;
      '&&'|'||')
        command_wrapper=0
        modifier_mode=""
        # Die linke Seite läuft unbedingt. Nur ein source RECHTS vom Operator
        # hängt vom vorherigen Exit-Code ab und darf nicht als belegt gelten.
        if (( paren_depth == 0 && ! in_pipeline && ! in_conditional )) \
            && [[ -n "$path" && "${path:t}" == gmf.zsh ]]; then
          last_path="$path"
        fi
        path=""
        in_conditional=1
        command_start=1
        ;;
      *) command_start=0 ;;
    esac
  done
  if (( paren_depth == 0 && ! in_pipeline && ! in_conditional )) \
      && [[ -n "$path" && "${path:t}" == gmf.zsh ]]; then
    last_path="$path"
  fi
  if [[ -n "$last_path" ]]; then
    print -r -- "$last_path"
    return 0
  fi
  return 1
}

# Mehrzeilige Quotes sind gültiges zsh, aber eine zeilenweise Kandidatensuche
# kann ihren Inhalt nicht von echten Kommandos unterscheiden. Sobald eine Zeile
# einen Quote-Kontext offen lässt, lehnen wir die Datei konservativ ab. Das ist
# ehrlicher als Text innerhalb von `print '…'` als Registrierung zu zählen.
line_leaves_quote_open() {
  local line="$1" char state=""
  local -i i escaped=0 token_start=1 parameter_depth=0
  for ((i = 1; i <= ${#line}; i++)); do
    char="${line[i]}"
    if [[ "$state" == ansi ]]; then
      if (( escaped )); then
        escaped=0
      elif [[ "$char" == '\' ]]; then
        escaped=1
      elif [[ "$char" == "'" ]]; then
        state=""
      fi
      continue
    fi
    if [[ "$state" == single ]]; then
      [[ "$char" == "'" ]] && state=""
      continue
    fi
    if [[ "$state" == double ]]; then
      if (( escaped )); then
        escaped=0
      elif [[ "$char" == '\' ]]; then
        escaped=1
      elif [[ "$char" == '$' && "${line[i + 1]:-}" == '{' ]]; then
        parameter_depth=$((parameter_depth + 1))
        (( i++ ))
      elif [[ "$char" == '}' && $parameter_depth -gt 0 ]]; then
        parameter_depth=$((parameter_depth - 1))
      elif [[ "$char" == '"' ]]; then
        state=""
      fi
      continue
    fi
    if (( escaped )); then
      escaped=0
      token_start=0
      continue
    fi
    if [[ "$char" == '$' && "${line[i + 1]:-}" == "'" ]]; then
      state=ansi
      token_start=0
      (( i++ ))
      continue
    fi
    if [[ "$char" == '$' && "${line[i + 1]:-}" == '{' ]]; then
      parameter_depth=$((parameter_depth + 1))
      token_start=0
      (( i++ ))
      continue
    fi
    if [[ "$char" == '}' && $parameter_depth -gt 0 ]]; then
      parameter_depth=$((parameter_depth - 1))
      token_start=0
      continue
    fi
    case "$char" in
      '\') escaped=1 ;;
      "'") state=single; token_start=0 ;;
      '"') state=double; token_start=0 ;;
      '#') (( token_start )) && break; token_start=0 ;;
      ' '|$'\t'|';'|'|'|'&'|'('|')') token_start=1 ;;
      *) token_start=0 ;;
    esac
  done
  [[ -n "$state" || $escaped -ne 0 || $parameter_depth -ne 0 ]]
}

# Wie viele Kommando- oder Prozessersetzungen ein Lexer-Wort offen lässt.
# Die frühere Prüfung fragte nur, ob im Wort ein `$(` steht und irgendwo KEIN
# `)`. In `value=$(print ')'` stammt das vorhandene `)` aber aus einem
# gequoteten Argument und schließt gar nichts; die Ersetzung blieb offen, der
# Scanner meldete trotzdem Top-Level, und eine folgende `source`-Zeile galt als
# wirksame Registrierung — obwohl zsh sie im Unterprozess der Kommandoersetzung
# ausführt und die `gmf`-Funktion in der aufrufenden Shell nie entsteht.
# Deshalb wird hier zeichenweise gezählt: Quotes sind undurchsichtig, und jede
# Ersetzungsebene bringt ihren eigenen Quote-Zustand mit.
unclosed_substitutions() {
  local word="$1" char next
  local -i i depth=0
  local -a quote
  quote=("")                      # quote[depth+1] = Quote-Zustand dieser Ebene
  for ((i = 1; i <= ${#word}; i++)); do
    char="${word[i]}"
    next="${word[i + 1]:-}"
    case "${quote[depth+1]}" in
      single)
        # Zwischen einfachen Quotes ist jedes Zeichen wörtlich.
        [[ "$char" == "'" ]] && quote[depth+1]=""
        continue
        ;;
      ansi)
        # $'…' kennt zusätzlich Backslash-Escapes.
        [[ "$char" == '\' ]] && { (( i++ )); continue; }
        [[ "$char" == "'" ]] && quote[depth+1]=""
        continue
        ;;
      double)
        [[ "$char" == '\' ]] && { (( i++ )); continue; }
        if [[ "$char" == '$' && "$next" == '(' ]]; then
          # Doppelte Quotes halten eine Kommandoersetzung nicht auf.
          depth=$((depth + 1)); quote[depth+1]=""; (( i++ ))
          continue
        fi
        [[ "$char" == '"' ]] && quote[depth+1]=""
        continue
        ;;
    esac
    [[ "$char" == '\' ]] && { (( i++ )); continue; }
    if [[ "$char" == '$' && "$next" == "'" ]]; then
      quote[depth+1]=ansi; (( i++ ))
      continue
    fi
    if [[ "$char" == '$' && "$next" == '(' ]] \
        || [[ ( "$char" == '<' || "$char" == '>' ) && "$next" == '(' ]]; then
      # Kommando- und Prozessersetzung öffnen beide eine neue Ebene.
      depth=$((depth + 1)); quote[depth+1]=""; (( i++ ))
      continue
    fi
    case "$char" in
      "'") quote[depth+1]=single ;;
      '"') quote[depth+1]=double ;;
      '(')
        # Eine gewöhnliche Klammer zählt nur INNERHALB einer Ersetzung mit
        # (z.B. `$( (a) )` oder `$(( 1 + 2 ))`). Am Zeilen-Top-Level führt der
        # Blockstapel von zshrc_context_state die Subshell-Klammern selbst.
        (( depth )) && { depth=$((depth + 1)); quote[depth+1]=""; }
        ;;
      ')') (( depth )) && depth=$((depth - 1)) ;;
    esac
  done
  print -r -- "$depth"
}

# Eine einzelne Zeile reicht nicht, um ihren Ausführungskontext zu kennen:
# `source` in einem mehrzeiligen if-/Funktionsblock kann syntaktisch echt und
# trotzdem beim Shellstart wirkungslos sein. Dieser bewusst konservative Stapel
# erkennt die zsh-Blockgrenzen, ohne den Dateiinhalt auszuführen. Bei unklarer
# oder unvollständiger Syntax bleibt er lieber in einem Block. Am Dateiende
# bricht der Installer dann mit Exit 1 ab und schreibt nichts — eine Zeile
# anzuhängen, deren Wirksamkeit er nicht belegen kann, wäre schlechter als die
# Handarbeit einzufordern. Belegt in test_cli_blackbox.py durch
# test_unclosed_shell_context_fails_without_appending_a_registration.
zshrc_context_state() {
  local line="$1" state="$2" word previous="" opener="" context
  local block_kind="" short_loop_kind=""
  local -i command_start=1 function_pending=0 nested_safe=0 \
    open_subs=0 open_index=0 wi=0 loop_head=0 short_loop_end=0 short_close=0
  local -a words stack
  [[ -n "$state" ]] && stack=("${(@s:,:)state}")
  if line_leaves_quote_open "$line"; then
    print -r -- opaque
    return 0
  fi
  words=("${(z)line}")
  # Steht oben ein `shortpending`, gehört die erste Teilliste DIESER Zeile noch
  # zum Rumpf der Kurzform von vorhin. Erst jetzt lässt sich sagen, wie er
  # weitergeht: `for x (a b)` kann auf der nächsten Zeile mit `do` in die lange
  # Fassung übergehen, mit `{` in einen Klammerblock — oder eben die eine
  # Teilliste sein, nach der die Schleife zu Ende ist.
  if [[ "${stack[-1]:-}" == shortpending ]]; then
    short_loop_body 1 "${words[@]}"
    case "$short_loop_kind" in
      do)    stack[-1]=loop ;;
      # Den Eintrag hier ABRÄUMEN, nicht durch `brace` ersetzen: Das `{` steht
      # am Anfang dieser Zeile und damit an einem Kommandoanfang — die Schleife
      # unten legt den Klammerblock selbst an. Beides zusammen legte ihn zweimal
      # auf den Stapel, und das eine `}` räumte nur einen davon ab.
      brace) stack[-1]=() ;;
      line)  stack[-1]=shortloop; short_close=$short_loop_end ;;
    esac
  fi
  for ((wi = 1; wi <= ${#words}; wi++)); do
    word="${words[wi]}"
    [[ "$word" == \#* ]] && break
    # Hinter dem Rumpf einer Kurzform ist ihre Schleife zu Ende.
    if (( short_close && wi > short_close )); then
      [[ "${stack[-1]:-}" == shortloop ]] && stack[-1]=()
      short_close=0
    fi
    if [[ "$word" == '<<' || "$word" == '<<-' || "$word" == *'`'* ]]; then
      # Mehrere/nachgestellte Here-Docs und mehrzeilige Backtick-Ersetzungen
      # sicher nachzubauen wäre ein zweiter Shell-Parser. Solche Dateien werden
      # geschlossen abgelehnt, statt eine wirkungslose Source-Zeile zu billigen.
      print -r -- opaque
      return 0
    fi
    # zsh-Prozesssubstitutionen laufen wie Kommandoersetzungen in einem eigenen
    # Prozess; darin definierte Funktionen erreichen die .zshrc nicht. Für den
    # Blockstapel sind beide deshalb derselbe Kontext.
    open_subs=$(unclosed_substitutions "$word")
    for ((open_index = 1; open_index <= open_subs; open_index++)); do
      stack+=(command)
    done
    if opens_assignment_array "$word"; then
      # In einer mehrzeiligen Array-Zuweisung sind folgende Wörter Daten,
      # selbst wenn eines davon `source` heißt.
      stack+=(assignment)
      previous="$word"
      continue
    fi
    if is_command_separator "$word"; then
      command_start=1
      previous=""
      continue
    fi
    if [[ "${stack[-1]:-}" == assignment ]]; then
      [[ "$word" == '(' ]] && stack+=(assignment)
      if [[ "$word" == ')' ]]; then
        stack[-1]=()
        if [[ "${stack[-1]:-}" != assignment ]]; then
          command_start=1
        fi
      fi
      previous="$word"
      continue
    fi
    if (( command_start )) && is_assignment_word "$word"; then
      # Wie im Kandidatenresolver: Präfix-Zuweisungen gehören noch zum
      # folgenden Kommando und dürfen return/exit/exec nicht verdecken.
      previous="$word"
      continue
    fi
    if (( command_start )) && is_command_modifier "$word"; then
      previous="$word"
      continue
    fi
    if [[ "$word" == ')' \
          && ( "${stack[-1]:-}" == paren || "${stack[-1]:-}" == command \
               || "${stack[-1]:-}" == function ) ]]; then
      stack[-1]=()
    fi
    if (( command_start )); then
      opener=""
      # Blockanfang und -ende kommen aus derselben Grammatik wie im
      # Kandidatenresolver; hier wird zusätzlich die ART gebraucht, damit ein
      # `fi` nur ein `if` schließt und nicht irgendeinen offenen Block.
      if block_closer_kind "$word"; then
        [[ "${stack[-1]:-}" == "$block_kind" ]] && stack[-1]=()
      elif block_opener_kind "$word"; then
        opener="$block_kind"
        # Bei einer Kurzform reicht der Block nur bis zum Ende ihrer Teilliste.
        # Nur eine je Zeile; verschachtelte bleiben konservativ offen.
        if [[ "$opener" == loop ]] && (( ! short_close )) \
            && loop_head_end "$wi" "${words[@]}"; then
          short_loop_body $((loop_head + 1)) "${words[@]}"
          case "$short_loop_kind" in
            line)    opener=shortloop; short_close=$short_loop_end ;;
            brace)   opener=brace ;;
            pending) opener=shortpending ;;
          esac
        fi
      else
        case "$word" in
          return|exit|exec)
            # Ein Abbruch in einem beim Sourcen ausgeführten if-/case-/Loop-/
            # Brace-Block kann jede spätere Registrierung unerreichbar machen.
            # Nur Funktions-, Subshell- und Kommandoersetzungs-Körper laufen beim
            # bloßen Sourcen der .zshrc nachweislich nicht in diesem Kontext.
            nested_safe=0
            for context in "${stack[@]}"; do
              [[ "$context" == function || "$context" == paren \
                 || "$context" == command ]] && nested_safe=1
            done
            if (( ! nested_safe )); then
              print -r -- opaque
              return 0
            fi
            ;;
          '}')
            if [[ "${stack[-1]:-}" == brace || "${stack[-1]:-}" == function ]]; then
              stack[-1]=()
            fi
            ;;
          '{') opener=brace ;;
          '(') opener=paren ;;
          function|coproc)
            function_pending=1
            ;;
          then|do)
            command_start=1
            previous="$word"
            continue
            ;;
        esac
      fi
      [[ -n "$opener" ]] && stack+=("$opener")
    fi
    if [[ "$word" == "()" && -n "$previous" ]]; then
      function_pending=1
    elif [[ "$word" == "{" && $function_pending -eq 1 ]]; then
      stack+=(function)
      function_pending=0
    elif [[ "$word" == "(" && $function_pending -eq 1 ]]; then
      stack+=(function)
      function_pending=0
    fi
    command_start=0
    previous="$word"
  done
  # Endet der Rumpf am Zeilenende, wird der Eintrag nicht mehr in der Schleife
  # oben entfernt — sonst bliebe die Kurzform als offener Block stehen und die
  # Datei gälte wieder als unbelegbar.
  if (( short_close )); then
    [[ "${stack[-1]:-}" == shortloop ]] && stack[-1]=()
  fi
  case "${words[-1]:-}" in
    '&&'|'||'|'|'|'|&') stack+=(opaque) ;;
  esac
  # `name()` am Zeilenende kann in zsh erst auf der naechsten Zeile mit `{`
  # oder `(` fortgesetzt werden. Diesen Kontext nicht als Top-Level ausgeben:
  # Ohne einen vollstaendigen Parser laesst sich eine spaetere source-Zeile dort
  # nicht als beim Shellstart ausgefuehrt belegen.
  (( function_pending )) && stack+=(opaque)
  print -r -- "${(j:,:)stack}"
}

# Nur echte, lexikalisch erkannte source-/.-Kommandos betrachten. Der Inhalt der
# .zshrc wird dabei nie ausgeführt.
existing_path=""
zshrc_state=""
if [[ -f "$zshrc" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ -z "$zshrc_state" ]]; then
      candidate="$(resolve_sourced_path "$line")" || candidate=""
      if [[ -n "$candidate" && "${candidate:t}" == "gmf.zsh" ]]; then
        existing_path="$candidate"
      fi
    fi
    zshrc_state="$(zshrc_context_state "$line" "$zshrc_state")"
  done < "$zshrc"
fi
if [[ -n "$zshrc_state" ]]; then
  print -u2 "error: cannot prove a top-level registration in .zshrc (unclosed or unsupported shell block)."
  print -u2 "Fix the file syntax, then re-run. Nothing was registered."
  exit 1
fi
if [[ -n "$existing_path" ]]; then
  if [[ "$existing_path" == "${wrapper_path:A}" ]]; then
    print "Already installed: .zshrc sources this clone's wrapper."
  else
    # Es gibt schon eine gmf-Zeile, aber mit anderem Pfad (Repo umgezogen?).
    # Nicht blind doppelt eintragen, sondern dem Menschen überlassen.
    print -u2 "warning: .zshrc already sources gmf.zsh from a different path."
    print -u2 "Expected the wrapper from this clone."
    print -u2 "Fix the line manually, then re-run."
    exit 1
  fi
else
  # Fuehrendes LF und Registrierung gehoeren in EINEN Append-Aufruf. So kann ein
  # paralleler Writer nicht zwischen einem nachgetragenen Zeilenabschluss und
  # der source-Anweisung Text ohne LF einschieben und beide Zeilen verschmelzen.
  printf '\n%s\n' "$source_line" >> "$zshrc"
  print "Registered wrapper: added it to .zshrc."
fi

print "Done. Open a new shell and run: gmf"
