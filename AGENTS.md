# gitmaster_flash

TUI-Übersicht über alle Git-Repos unterhalb des aktuellen Ordners, zum schnellen
Aufräumen. Typ: Skript/CLI, Plattform: macOS/Terminal, Python 3 (nur
Standardbibliothek, curses). Name: Anspielung auf Grandmaster Flash.

## Regeln

- Keine externen Abhängigkeiten einführen; alles bleibt Standardbibliothek.
- Reine Logik (Parsing, Heuristiken, Repo-Scan) von der curses-UI getrennt
  halten, damit sie headless testbar bleibt: `python3 -m unittest discover -s tests`.
- Nicht-interaktive Schnittstelle (`--list`, `--json`, Exit-Codes) bei jeder
  Funktionsänderung mitpflegen.
- Repo-Defaults, Beispiele, Tests und Demo-Sandbox generisch halten (`origin`,
  neutrale Repo- und App-Namen). Persönliche Remotes, Hosts und Apps gehören in
  die lokale `~/.config/gitmaster_flash/config.json`, nicht ins Repo.
- Benutzertexte laufen über die i18n-Schicht (`t("key")` + `TR`): Englisch ist
  die Basis, Deutsch die Übersetzung. Neue Strings immer in beiden Sprachen.
- Doku zweisprachig halten: [README.md](README.md) (englisch, Standard) und
  [README.de.md](README.de.md) inhaltlich synchron.
- Der Demo-Modus (`--demo`) ist die Referenz für Screenshots und muss ohne Netz
  und unabhängig von der Maschine gleich aussehen (deshalb `core.excludesFile`
  und `core.hooksPath` in den Demo-Repos abschalten). Alle Demo-Commits tragen den
  festen Zeitstempel `DEMO_DATE`; nur dadurch sind die Commit-IDs überall gleich —
  und damit auch Protokollzeilen wie `git merge --ff-only -- <id>` im Bild.
- Die Bilder in `docs/` sind **generiert, keine Screenshots** (seit 2026-07-17):
  `python3 docs/make-screens.py` fährt das echte Programm in einem **Pseudo-Terminal**
  auf der `--demo`-Sandbox und baut daraus SVG. Damit entfällt das frühere Gefummel
  (Fenstertransparenz, Titelleiste, Zuschnitt) und die Bilder veralten nicht mehr
  still — die alten PNGs zeigten zuletzt eine Kopfzeile ohne Version.
  `--check` schlägt fehl, wenn sie neu erzeugt werden müssten (nach UI-Änderungen also
  `make-screens.py` laufen lassen und das Ergebnis mitcommitten).
  **Weiterhin gilt:** keine globalen synthetischen Tastendrücke — die Eingaben gehen
  ausschließlich in den eigenen pty-Kindprozess, nie an das Fenstersystem.
  Der Generator erzeugt jedes Bild als Sprachpaar: die Dateien ohne Sprachsuffix
  sind Englisch, `.de.svg` ist Deutsch; beide READMEs verlinken ihre passende Fassung.
  Cursortasten nur als `\x1bO…` schicken (Konstanten `UP`/`DOWN`/… in `make-screens.py`):
  ncurses schaltet den Application-Cursor-Modus ein, die `\x1b[…`-Form käme als nacktes
  Esc an — und Esc beendet die TUI mitten in der Aufnahme.
  Grenze des Generators: Nur der **Listen-Screen** ist reproduzierbar. Views, die
  darüber gezeichnet werden (Commit-Hilfe, Pager, Info-Ansicht), bräuchten echte
  Terminal-Zustandslogik — dafür wäre ein voller Terminal-Emulator nötig; ein
  Abstecher durch die Info-Ansicht ließ prompt Reste von ihr auf der Liste darunter
  stehen. Solche Ansichten gehören als vorformatierter Textblock ins README, nicht
  als Bild.
  Der Bildnachbau (`replay()`) ist bewusst von der pty-Mechanik getrennt und ohne
  Kindprozess getestet — ein fehlendes Steuerzeichen verschiebt sonst still ganze
  Zeilen, und das Bild sieht trotzdem plausibel aus.
- Nach jedem Demo-/PTY-Lauf prüfen, dass kein `gitmaster_flash.py --demo`- oder
  Testprozess übrig ist. Einen Prozess nur mit eindeutigem Projektbezug beenden;
  fremde Python-Dienste und Automationen unangetastet lassen.
- Alle Git-Aufrufe gehen über `run_git()` und laufen strikt nicht interaktiv
  (`GIT_TERMINAL_PROMPT=0`, leeres Askpass, `stdin=DEVNULL`, eigene Session,
  `LC_ALL=C`). Git schreibt Login-Fragen sonst direkt auf `/dev/tty`, zerlegt damit
  das curses-Bild und blockiert bis zum Timeout. Fehlende Zugangsdaten erkennt
  `credentials_missing()` an den englischen Markern (deshalb `LC_ALL=C`) und die UI
  zeigt einen Einrichtungshinweis statt nur eines Exit-Codes.
- Der Commit läuft über einen temporären Index, damit fremdes Staging überlebt.
  Danach muss der echte Index die committeten Pfade übernehmen
  (`adopt_commit_in_real_index`), sonst zeigt `git status` sie weiter als `MM` und
  gmf meldet das Repo trotz erfolgreichem Commit als schmutzig. Referenz ist
  `git commit -- <pfad>`.
- Ein Timeout darf die TUI nie beenden. `run_git()` beendet dabei die ganze
  Prozessgruppe (`start_new_session=True` + `_kill_process_group`), sonst laufen
  vom pre-commit-Hook gestartete Linter/Tests verwaist weiter. Neue Aktionen
  laufen über `dispatch_action()`; dort fängt die Hauptschleife
  `subprocess.TimeoutExpired` ab und zeigt `timeout_message()`. `git commit` hat
  mit `commit_timeout` einen eigenen, großzügigen Wert — der Hook eines Projekts
  braucht regelmäßig mehr als die zehn Sekunden von `git_timeout`.
- Zustandsändernde Aktionen laufen über `run_git_logged()`, damit sie im
  Befehlsprotokoll (`H`) erscheinen; die Lesebefehle des Scans bleiben bei
  `run_git()`, sonst ist das Protokoll wertlos. Neue Aktionen entsprechend anbinden.
- Version: `__version__` in [gitmaster_flash.py](gitmaster_flash.py) bei
  Funktionsänderungen bumpen.

- Fehlgeschlagene Remote-Zugriffe laufen über `classify_remote_check()`. Die
  Trennung von „Repo weg“, „Login fehlt“, „Hostschlüssel unbekannt“ und „kein
  Netz“ ist Produktkern (Fetch-Zeile, `T`-Prüfung) — neue Fälle dort ergänzen,
  nicht in den Aufrufern.
- Destruktive lokale Aktionen (`X` Remote/Branch) verlangen einen Dialog, der die
  Folgen benennt und sowohl den auszuführenden als auch den Rückgängig-Befehl
  zeigt. Branches löscht gmf nur gemergt (`git branch -d`), nie erzwungen.

## Offene Punkte / Ideen

- [ ] Suche/Filter über die Repo-Liste (wird ab einigen hundert Repos wichtiger als
      die Anzeige selbst; dort ist dann der Scan der Flaschenhals).
- [ ] Kein Bild zeigt bisher einen abgebrochenen Dialog (`⊘`-Zeile), weil der
      einzige Weg dorthin über die Info-Ansicht führt — die der Nachbau nicht
      sauber trifft (siehe Grenze des Generators oben).
- [ ] Einstellungen direkt in der TUI editieren (bisher: config.json von Hand).
- [ ] Fetch im Hintergrund statt blockierend mit Fortschrittsanzeige.
- [ ] Intelligentere Commit-Vorschläge (z.B. Gruppierung nach Dateityp).
- [ ] Screenshots in `docs/` bei UI-Änderungen neu aufnehmen (Rezept oben).
