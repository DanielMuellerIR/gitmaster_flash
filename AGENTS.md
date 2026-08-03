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
- Farbige Elemente in einer markierten Zeile immer über `color_attr()` zeichnen,
  nie `A_REVERSE | color_pair(...)` von Hand. Umkehren macht aus Rot eine schwarze
  Schrift auf sattem Rot — unlesbar, und Rot trägt die dringenden Angaben.
  `selected_pair()` entscheidet das zentral; der Bildgenerator kann echte
  Hintergrundfarben (`ANSI_BG`), sonst zeigen die README-Bilder etwas anderes als
  das Programm.
- Der Commit läuft über einen temporären Index, damit fremdes Staging überlebt.
  Danach muss der echte Index die committeten Pfade übernehmen
  (`adopt_commit_in_real_index`), sonst zeigt `git status` sie weiter als `MM` und
  gmf meldet das Repo trotz erfolgreichem Commit als schmutzig. Referenz ist
  `git commit -- <pfad>`. Zwei Konsequenzen daraus: `git commit` vererbt
  `GIT_INDEX_FILE` an die Hooks — ein pre-commit-Hook, der per `git add` weitere
  Pfade stagt, verändert den Commit-Baum; deshalb wird der Baum nach dem Commit
  geprüft und bei Abweichung atomar zurückgerollt
  (`_verify_hooks_kept_approved_tree`). Und ins Befehlsprotokoll kommt nicht das
  Temp-Index-Interna, sondern der terminal-äquivalente Befehl
  `git commit -m … -- <pfade>` — nur der wäre im Terminal gefahrlos wiederholbar.
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
- „Nur lesend" ist eine Zusage über die **Repo-Inhalte**, nicht über den Rechner.
  Zwei Ausnahmen gehören überall dorthin, wo die Zusage steht: `load_config()`
  legt beim ersten Lauf `~/.config/gitmaster_flash/config.json` an — bei `--diff`
  auch auf dem befragten Rechner, weil dort per stdin dasselbe Skript läuft —,
  und `--fetch` führt ein echtes `git fetch --all --prune` aus. Beides lässt
  Branch, Index und Arbeitsbaum unberührt; „ändert nie etwas" wäre trotzdem
  falsch und stand so bis 2026-07-28 in beiden READMEs. Dass die Datei angelegt
  wird, ist dabei bewusst so: gmf zielt ausschließlich auf eigene Rechner, und
  dort darf eine Datei entstehen, wenn sie einen Nutzen hat (Entscheidung
  2026-07-28). Die Zusage muss den Vorgang nur benennen, nicht vermeiden.
- Ändert sich der Remote-Vertrag — JSON-Felder, Exit-Codes, der `ssh`-Aufruf —,
  gehört der Fall nach `tests/test_cli_blackbox.py`. Dort führt ein temporäres
  `ssh` im PATH den echten, über stdin übertragenen Code lokal aus und prüft
  Remote-JSON, Remote-Exit-Code und lokale Auswertung gemeinsam, ohne Netz.
  Unit-Tests mit gemocktem `subprocess` sehen genau diese Grenze nicht.

## Offene Punkte / Ideen

- [ ] **Änderungen verwerfen (`V` in der Änderungsansicht)** — geplant, Zuschnitt
      entschieden am 2026-08-03. Anlass: Auf einem Rechner, auf dem an einem Projekt
      gar nicht gearbeitet wurde, stehen trotzdem Dateien als geändert im Status
      (beobachtet an `pubspec.lock`) und blockieren den Abgleich zwischen den
      Rechnern. Bisher half nur ein fremdes Werkzeug — ein Fall, den gmf selbst
      können muss.
      Zuschnitt:
      - Die Taste sitzt in `action_file_changes()` (`A`), nicht im Fuß des
        Hauptschirms. Dort steht die Dateiliste bereits, und `⏎` zeigt vorher den
        Diff: Man verwirft nur, was man gesehen hat. Der Hauptschirm bleibt damit
        unverändert, die erzeugten Bilder in `docs/` müssen nicht neu entstehen.
      - **Einzelne Datei: hart** (`git restore`), ohne Sicherungsnetz. Verloren geht
        nur der Unterschied zu einem committeten Stand; Datei und Historie bleiben.
        Das ist die bewusste Ausnahme von der Dialog-Regel oben: Einen
        Rückgängig-Befehl gibt es hier nicht. Der Dialog nennt stattdessen den
        Umfang der Änderung (Zahl geänderter Zeilen), damit eine Datei mit echter
        Arbeit sich sichtbar von einer bloß angefassten unterscheidet.
      - **Alle Dateien: als Stash** (`git stash push`), mit zweiter Bestätigung. Bei
        „alle“ fehlt die Einzelbeurteilung, die den harten Weg trägt. In gmf ist ein
        Stash nichts Verstecktes: Er steht in der Repo-Zeile, `S` zeigt den Inhalt,
        `D` wirft ihn weg — das Aufräumen bleibt also sichtbar und in derselben
        Ansicht erledigbar.
      - **Unverfolgte Dateien (`??`) bleiben außen vor.** Sie waren nie in Git, es
        gibt keinen früheren Stand: Das wäre Löschen, nicht Verwerfen. Sollte sich
        zeigen, dass sie den Abgleich in der Praxis ebenfalls blockieren, bekommen
        sie eine eigene, anders benannte Aktion mit eigenem Dialog — nicht dieselbe
        Taste.
      - Kein CLI-Schalter: Die nicht-interaktive Schnittstelle bleibt lesend.
      Technisch:
      - `parse_porcelain()` muss den rohen XY-Status mitliefern; heute verkürzt es
        ihn auf M/D/U/C. Ohne ihn ist nicht unterscheidbar, ob nur der Arbeitsbaum,
        nur der Index oder beides betroffen ist, und ein neu hinzugefügtes `A ` (das
        keinen HEAD-Stand hat) sieht aus wie eine gewöhnliche Änderung. Mitzuziehen:
        Änderungsansicht, Commit-Hilfe, Tests.
      - Umbenennungen stehen als zwei Einträge (Ziel `M`, Quelle `D`). Einzeln
        verworfen bleibt die halbe Umbenennung liegen: beide Hälften zusammen
        behandeln oder die Aktion dort verweigern.
      - Konflikte (`UNMERGED_CODES`) sperren.
      - Dialog über `_confirm_destructive()`, Ausführung über `run_git_logged()`
        (sonst fehlt sie im Protokoll), danach `refresh_one()`; leert sich die
        Liste, die Ansicht verlassen.
      - `confirm()` kennt nur Ja/Nein und braucht für die Ausweitung eine dritte
        Antwort. Keine zweite Taste dafür: Groß- und Kleinschreibung sind im ganzen
        Programm gleichbedeutend, `v` und `V` dürfen nichts Verschiedenes tun.
      - Ob `git stash push -- <pfad>` fremdes Staging anderer Dateien unberührt
        lässt, ist genau die Annahme, die dieses Repo per Test absichert: Blackbox
        auf einem echten temporären Repo, nicht mit gemocktem `subprocess`. Ebenso
        zu prüfen: Repo ohne HEAD, dort schlägt `git stash` fehl.
- [ ] Verlauf umschreiben (`reset`, Squash) bleibt bewusst draußen. Nichts davon ist
      an einer Datei sichtbar, der Zielzustand lässt sich nur mit der Commit-Historie
      im Kopf benennen, und ein falsch geratener `reset --hard` kostet Commits statt
      Dateien. Bleibt Handarbeit im Einzelfall (Entscheidung 2026-08-03).
- [ ] Suche/Filter über die Repo-Liste (wird ab einigen hundert Repos wichtiger als
      die Anzeige selbst; dort ist dann der Scan der Flaschenhals).
- [ ] `fetch --all` klassifiziert bei mehreren gescheiterten Remotes nur den
      kombinierten stderr — bei gemischten Ursachen (Login fehlt an einem Remote,
      Repo weg an einem anderen) ist mindestens eine Diagnose falsch. Remotes
      einzeln fetchen bzw. stderr pro Remote zuordnen und Ursache/Detail je
      `RemoteStatus` speichern (Code-Review 2026-08-02).
- [ ] `docs/make-screens.py` wertet Ruhe auf dem PTY als „fertig“: läuft nach dem
      letzten Tastendruck noch eine stille Git-Aktion, kann auf langsamen Maschinen
      ein Zwischenzustand aufgenommen werden und `--check` wird timingabhängig.
      Auf einen erwarteten Bildschirm-/Protokollmarker warten statt nur auf Ruhe
      (Code-Review 2026-08-02).
- [ ] Kein Bild zeigt bisher einen abgebrochenen Dialog (`⊘`-Zeile), weil der
      einzige Weg dorthin über die Info-Ansicht führt — die der Nachbau nicht
      sauber trifft (siehe Grenze des Generators oben).
- [ ] Einstellungen direkt in der TUI editieren (bisher: config.json von Hand).
- [ ] Fetch im Hintergrund statt blockierend mit Fortschrittsanzeige.
- [ ] Intelligentere Commit-Vorschläge (z.B. Gruppierung nach Dateityp).
- [ ] Screenshots in `docs/` bei UI-Änderungen neu aufnehmen (Rezept oben).
