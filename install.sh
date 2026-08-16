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

# Denselben Pfad kann man verschieden schreiben: `~/git/...`, `$HOME/git/...`,
# mit oder ohne Quotes. Ein reiner Textvergleich hielte das für ein anderes Repo
# und verlangte grundlos Handarbeit — deshalb wird der Pfad aus der bestehenden
# Zeile herausgelöst und aufgelöst verglichen. Bewusst OHNE eval: die .zshrc ist
# hier Datei-Inhalt, kein Code, den dieses Skript ausführen darf.
resolve_sourced_path() {
  local line="$1" word path raw_path last_path="" modifier_mode=""
  local -a words
  local -i command_start=1 i paren_depth=0 assignment_depth=0 \
    in_pipeline=0 in_conditional=0 command_wrapper=0 scan_command_start=1
  # zshs `(z)`-Lexer trennt wie die Shell, fuehrt den Inhalt aber nicht aus.
  # Quotes bleiben am Token und ein `#` innerhalb von Quotes wird deshalb nie
  # mit einem Kommentar verwechselt. Ebenso bleibt `source` in einem
  # gequoteten echo-Text Daten statt eines vermeintlichen Kommandos.
  words=("${(z)line}")
  for word in "${words[@]}"; do
    [[ "$word" == \#* ]] && break
    case "$word" in
      ';'|';;'|';&'|';|'|'|'|'|&'|'&&'|'||'|'&'|'&!'|'&|')
        scan_command_start=1
        continue
        ;;
      '()'|'{'|'}')
        # Funktions-/Brace-Syntax kann hinter einem Namen stehen und ist auch
        # dort ein Kontrollkontext, kein gewöhnliches Argument.
        return 1
        ;;
    esac
    if (( scan_command_start )) \
        && [[ "$word" =~ '^[A-Za-z_][A-Za-z0-9_]*(\[.*\])?\+?=' ]]; then
      continue
    fi
    if (( scan_command_start )) \
        && [[ "$word" == noglob || "$word" == nocorrect || "$word" == time \
             || "$word" == command || "$word" == builtin ]]; then
      continue
    fi
    if (( ! scan_command_start )); then
      continue
    fi
    case "$word" in
      if|then|elif|else|fi|for|foreach|while|until|select|repeat|do|done|\
      case|esac|function)
        # Ein kompletter Kontrollblock kann auf derselben physischen Zeile
        # stehen. Die einfache Kandidatensuche dürfte darin sonst ein niemals
        # ausgeführtes source als Top-Level ansehen. Solche Zeilen werden
        # konservativ nicht als bestehende Registrierung anerkannt.
        return 1
        ;;
    esac
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
    if (( command_start )) \
        && [[ "$word" =~ '^[A-Za-z_][A-Za-z0-9_]*(\[.*\])?\+?=\($' ]]; then
      assignment_depth=1
      continue
    fi
    if (( command_start )) \
        && [[ "$word" =~ '^[A-Za-z_][A-Za-z0-9_]*(\[.*\])?\+?=' ]]; then
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
    if (( paren_depth == 0 && command_start && ! in_pipeline && ! in_conditional )) \
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

# Eine einzelne Zeile reicht nicht, um ihren Ausführungskontext zu kennen:
# `source` in einem mehrzeiligen if-/Funktionsblock kann syntaktisch echt und
# trotzdem beim Shellstart wirkungslos sein. Dieser bewusst konservative Stapel
# erkennt die zsh-Blockgrenzen, ohne den Dateiinhalt auszuführen. Bei unklarer
# oder unvollständiger Syntax bleibt er lieber in einem Block; dann ergänzt der
# Installer eine sicher wirksame Top-Level-Zeile.
zshrc_context_state() {
  local line="$1" state="$2" word previous="" opener="" context
  local -i command_start=1 function_pending=0 nested_safe=0
  local -a words stack
  [[ -n "$state" ]] && stack=("${(@s:,:)state}")
  if line_leaves_quote_open "$line"; then
    print -r -- opaque
    return 0
  fi
  words=("${(z)line}")
  for word in "${words[@]}"; do
    [[ "$word" == \#* ]] && break
    if [[ "$word" == '<<' || "$word" == '<<-' || "$word" == *'`'* ]]; then
      # Mehrere/nachgestellte Here-Docs und mehrzeilige Backtick-Ersetzungen
      # sicher nachzubauen wäre ein zweiter Shell-Parser. Solche Dateien werden
      # geschlossen abgelehnt, statt eine wirkungslose Source-Zeile zu billigen.
      print -r -- opaque
      return 0
    fi
    if [[ "$word" == *'$('* && "$word" != *')'* ]]; then
      stack+=(command)
    fi
    if [[ ( "$word" == *'<('* || "$word" == *'>('* ) \
          && "$word" != *')'* ]]; then
      # zsh-Prozesssubstitutionen laufen wie Kommandoersetzungen in einem
      # eigenen Prozess; darin definierte Funktionen erreichen die .zshrc nicht.
      stack+=(paren)
    fi
    if [[ "$word" =~ '^[A-Za-z_][A-Za-z0-9_]*(\[.*\])?\+?=\($' ]]; then
      # In einer mehrzeiligen Array-Zuweisung sind folgende Wörter Daten,
      # selbst wenn eines davon `source` heißt.
      stack+=(assignment)
      previous="$word"
      continue
    fi
    case "$word" in
      ';'|';;'|';&'|';|'|'|'|'|&'|'&&'|'||'|'&'|'&!'|'&|')
        command_start=1
        previous=""
        continue
        ;;
    esac
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
    if (( command_start )) \
        && [[ "$word" =~ '^[A-Za-z_][A-Za-z0-9_]*(\[.*\])?\+?=' ]]; then
      # Wie im Kandidatenresolver: Präfix-Zuweisungen gehören noch zum
      # folgenden Kommando und dürfen return/exit/exec nicht verdecken.
      previous="$word"
      continue
    fi
    if (( command_start )) \
        && [[ "$word" == noglob || "$word" == nocorrect || "$word" == time \
             || "$word" == command || "$word" == builtin ]]; then
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
        fi)   [[ "${stack[-1]:-}" == if ]] && stack[-1]=() ;;
        done) [[ "${stack[-1]:-}" == loop ]] && stack[-1]=() ;;
        'esac') [[ "${stack[-1]:-}" == case ]] && stack[-1]=() ;;
        '}')
          if [[ "${stack[-1]:-}" == brace || "${stack[-1]:-}" == function ]]; then
            stack[-1]=()
          fi
          ;;
        if) opener=if ;;
        for|foreach|while|until|select|repeat) opener=loop ;;
        'case') opener=case ;;
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
