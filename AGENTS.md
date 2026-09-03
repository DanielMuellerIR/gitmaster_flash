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
  und damit auch die Commit-IDs in den Demo-Repos und Vorschauen.
- Die Bilder in `docs/` sind **generiert, keine Screenshots** (seit 2026-07-17):
  `python3 docs/make-screens.py` fährt das echte Programm in einem **Pseudo-Terminal**
  auf der `--demo`-Sandbox und baut daraus SVG. Damit entfällt das frühere Gefummel
  (Fenstertransparenz, Titelleiste, Zuschnitt) und die Bilder veralten nicht mehr
  still — die alten PNGs zeigten zuletzt eine Kopfzeile ohne Version.
  `--check` schlägt fehl, wenn sie neu erzeugt werden müssten (nach UI-Änderungen also
  `make-screens.py` laufen lassen und das Ergebnis mitcommitten).
  Nach bestätigten Demo-Aktionen wartet der Generator auf deren konkrete Zeile im
  Befehlsprotokoll; bloße Ruhe auf dem PTY belegt keinen fertigen Git-Aufruf.
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
  Zeilen, und das Bild sieht trotzdem plausibel aus. Genau das trat am 2026-08-23
  ein: ncurses schiebt Zeilen lieber, als sie neu zu malen (Scrollbereich
  `ESC[t;br` plus `ESC[nS`), der Nachbau kannte beides nicht und behielt eine
  alte Repo-Liste im Bild. `r`, `S`, `T`, `L` und `M` sind seither nachgebildet.
  Wer hier eine Sequenz ergänzt, prüft mit `--check`, dass die vorhandenen Bilder
  byteweise gleich bleiben — ändern sie sich, war die Ergänzung falsch oder das
  alte Bild war es.
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
- Zwei Regeln, die mehrere Ansichten teilen, stehen je genau einmal im Code, und
  ein Test hält das fest. `scroll_window()` verschiebt den sichtbaren Ausschnitt
  von Repo-Liste, Änderungsansicht, Commit-Hilfe und Vorschlagsliste — vorher
  viermal von Hand gerechnet, einmal ohne untere Schranke für die Höhe.
  `file_row()` baut die Datei-Zeile für die aufgeklappte Liste UND die
  Änderungsansicht; beide zeigen dieselben Einträge, und `FILE_CODE_COLORS` ist
  die einzige Zuordnung von Anzeigecode zu Farbe. Auch die Zellbreite kommt aus
  einer Quelle: Der Bildgenerator importiert `cell_width()` aus dem Programm,
  statt eine zweite Fassung zu führen.
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
  Commit und Undo sind zusätzlich an den vollständigen symbolischen Branch-Ref
  gebunden; detached HEAD ist gesperrt. Ein Checkout auf einen zweiten Branch mit
  derselben OID darf weder Commit-Ziel noch Undo-Ziel unbemerkt austauschen.
  Im Timeout-Zweig hängt `commit_selected` den freigegebenen Baum an die
  `TimeoutExpired`-Ausnahme (`exc.approved_tree`). `finish_interrupted_commit()`
  prüft Eltern-OID und Baum nur lesend. Es rollt dort nie zurück und zieht auch
  den echten Index nicht nach: Nach dem beendeten Prozess ist nicht beweisbar,
  ob ein neuer HEAD von gmf oder einem parallelen Programm stammt. Der synchrone
  Normalweg darf nur die über einen eindeutigen `GIT_REFLOG_ACTION`-Eintrag
  belegte Commit-OID per CAS zurückrollen; ein fremder Folgecommit bleibt stehen.
  Eltern und Baum werden stets mit `GIT_NO_REPLACE_OBJECTS=1` gelesen, damit ein
  Hook die Prüfung nicht über `refs/replace/*` täuschen kann. Scheitert nach dem
  belegten Commit nur die Übernahme in den echten Index, meldet die TUI
  ausdrücklich „Commit vorhanden, Index nicht übernommen“ statt „Commit
  fehlgeschlagen“ (Entscheidung 2026-08-16).
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

- Die Rückfrage des G-Pfads steht seit 0.18.10 in der Vorschau selbst
  (`confirm_in_pager()`), direkt unter Commits und Dateinamen, und verlangt
  J/Y (oder „ja“/„yes“) plus ⏎; jede andere Eingabe, ein leeres ⏎ und Esc
  brechen ab. Vorher kam nach dem Schließen des Pagers unten auf der Liste die
  Aufforderung, `PUSH <remote>` zu tippen — sie wurde übersehen, und ein
  gewohnheitsmäßiges `q` landete im Eingabefeld. Bewusst keine Einzeltaste: Wer
  aus Gewohnheit „J ⏎“ tippt, dessen ⏎ träfe sonst schon die Liste, und dort
  bedeutet ⏎ „beenden und ins Repo wechseln“ (Entscheidung 2026-08-22). Seit
  derselben Version zeigt `_fetch_remote()` während des Fetches eine Busy-Zeile
  (`fetch_busy`), weil ein Fetch zu GitHub bei großen Repos lange dauern kann
  und die TUI sonst eingefroren wirkt. Seit 0.22.3 zeichnen beide Rückfragen
  (`confirm()` und `confirm_in_pager()`) ihre Frage über `draw_question()`:
  weiß auf Schwarz (`C_ASK`), die Ja-Taste (`yes_key`/`yes_accent`, muss wörtlich
  im Fragetext stehen) rot auf Schwarz (`C_ASK_KEY`), eine Leerzeile darüber und
  sichtbarer Terminal-Cursor (`set_cursor_visible()`). Grund: Nach einer langen
  Dateiliste war die gelbe Frage nur eine weitere farbige Zeile (Daniel,
  2026-09-02). Schwarz auf Gelb war der erste Wurf und zu flau: ANSI-Gelb und
  ANSI-Schwarz sind Palettenfarben des Terminals (meist Oliv und Dunkelgrau),
  hellere Töne gäbe es nur über 256-Farben-Indizes, die nicht jedes Terminal
  hat. Feste Vorder- UND Hintergrundfarbe statt `A_REVERSE`, damit der Balken
  in hellen Terminals ein Balken bleibt. Kein `A_BLINK`: Viele Terminals
  ignorieren es, der blinkende Terminal-Cursor genügt.
- Remote-Identität (`canonical_remote_target()`): Übertragungen (P/G) laufen
  nur, wenn Fetch- und Push-Ziel identisch sind (`transfer_safe`). SCP-Pfade
  ohne führenden `/` hängen am Home des SSH-Benutzers (`alice@host:repo` ≠
  `bob@host:repo` ≠ `host:/repo`) — mit einer Ausnahme seit 0.18.2: Der
  virtuelle Benutzer `git` der Hosting-Dienste (GitHub, GitLab, Gitea, …) hat
  kein privates Home, `git@host:org/repo` und `https://host/org/repo` sind
  dasselbe Repo. Ohne die Ausnahme blockierte der übliche Mix (Fetch per HTTPS,
  Push per SSH) jede Übertragung. Ein ausdrückliches `~` im Pfad bleibt auch
  bei `git@` benutzerabhängig. Bewusst in Kauf genommener Rest: Auf einem
  gewöhnlichen SSH-Host mit einem echten Unix-Benutzer `git` gälten dessen
  Home-Pfad und ein gleichnamiger HTTPS-Pfad ebenfalls als dasselbe Ziel. Dazu
  müsste derselbe Host beides anbieten — dann ist er ein Hosting-Dienst
  (Code-Review 2026-08-06 geprüft, Entscheidung bleibt).
- Ohne ausgecheckten Branch (detached HEAD) gibt es keinen Tracking-Ref, den ein
  Fetch aktualisieren könnte. `collect_status()` lässt den Fetch dann ganz aus,
  und `fetch_remote_block_reason()` nennt dafür den eigenen Grund `detached`.
  Vorher fiel dieser Fall mit einer wirklich unsicheren Refspec zusammen: `R`
  und `--fetch` meldeten für jedes Remote „unsichere Fetch-Refspec", färbten das
  Repo rot und ersetzten den ehrlichen Zustand `detached` durch `error` samt
  `fetch_error` — eine Falschaussage über eine völlig gewöhnliche Konfiguration,
  die im Rechnervergleich zusätzlich `error` und `remote_state` aus dem
  Vergleich nahm (Review-Fund 2026-09-03).
- Ein Pfad-Remote hat keinen Host, und das steht in `RemoteTarget.is_local` —
  nicht als Wort „local" im Feld `host`. `local` IST ein gültiger Hostname, etwa
  als ssh-Alias in `~/.ssh/config`. Solange der Merkwert im Hostnamensraum
  stand, galt `ssh://local/srv/repo.git` als lokaler Ordner: gmf holte und
  pushte in das Verzeichnis `/srv/repo.git` DIESES Rechners, während Git selbst
  den Server angesprochen hätte — ein als zielgebunden bestätigter Push landete
  woanders (Review-Fund 2026-09-03). `remote_fetch_url()`, `remote_push_url()`
  und `inspect_transfer()` ersetzen die konfigurierte Adresse deshalb nur noch
  bei `is_local` durch den aufgelösten Pfad. Aus demselben Grund lässt
  `detect_sync_remote()` leere Einträge in `sync_remote_hosts` fallen: Ein
  hostloses Ziel darf nicht auf einen versehentlich leeren Eintrag passen.
- Fehlgeschlagene Remote-Zugriffe laufen über `classify_remote_check()`. Die
  Trennung von „Repo weg“, „Login fehlt“, „Hostschlüssel unbekannt“ und „kein
  Netz“ ist Produktkern (Fetch-Zeile, `T`-Prüfung) — neue Fälle dort ergänzen,
  nicht in den Aufrufern.
  - Seit 0.18.0 gibt es dort `nokeychain`: auf dem Mac hängt der
    Login-Schlüsselbund an der GUI-Sitzung, und die verbreiteten
    Credential-Helper (`osxkeychain`, `gh auth git-credential`) lesen daraus.
    Eine ssh-Sitzung, ein LaunchDaemon oder ein cron-Lauf kommen nicht daran
    und bekommen dieselbe Git-Meldung wie bei einem fehlenden Login — obwohl
    der Login in Ordnung ist. `keychain_session()` erkennt das über
    `launchctl managername` (nur „Aqua“ ist die GUI-Sitzung) und macht daraus
    einen eigenen Fall, damit die Zeile nicht zum Neu-Anmelden auffordert.
    Anlass war ein konkreter Fehlalarm am 2026-08-05. Seit 0.18.8 verlangt der
    Fall zusätzlich den positiven Nachweis eines für die konkrete Remote-URL
    wirksamen schlüsselbundabhängigen Helpers; eine bloße Auth-Meldung in einer
    Nicht-Aqua-Sitzung reicht nicht. Ein abgelehnter SSH-Schlüssel
    („permission denied (publickey)“) bleibt `auth`, denn dort ist gar kein
    Schlüsselbund im Spiel.
- Ein gescheiterter Fetch ist **kein** Unterschied zwischen zwei Rechnern. Er
  beschreibt die Sitzung, die gemessen hat. `diff_status()` blendet deshalb
  `error` und `remote_state` aus, sobald eine Seite `fetch_error` meldet, und
  nennt die Seite stattdessen einmal als `lokal`. Ohne das erzeugte ein einziger
  unerreichbarer Schlüsselbund auf der Gegenseite zwei DRIFT-Zeilen pro Repo —
  am 2026-08-05 rund fünfzig Zeilen, die sich wie ein kaputter Login lasen.
  `conflicts` und `stashes` bleiben davon unberührt und werden weiter verglichen.
  Seit 0.18.3 gehören zwei Feinheiten dazu: Ahead/Behind und „kennt den Branch“
  eines Remotes fallen ebenfalls aus dem Vergleich, sobald für dieses Remote auf
  einer Seite `fetch_failed` steht — sein Tracking-Ref ist dort veraltet. Und
  `error` wird nur auf DER Seite geleert, deren Fetch scheiterte; sonst versteckt
  ein Fetch-Problem hier einen echten lokalen Schaden drüben.
- Seit 0.18.8 wird jedes Remote einzeln gefetcht und erhält dadurch direkt seine
  eigene Ursache, Erklärung und den redigierten Git-Beleg am jeweiligen
  `RemoteStatus`; Fehler verschiedener Remotes können sich nicht mehr in einem
  kombinierten stderr vermischen. Fetches laufen nur mit `--no-tags`,
  `--no-prune-tags`, `--no-recurse-submodules`, leerer `--refmap` und ohne
  lokalen Ziel-Ref. Je Remote wird nur die angekündigte Objekt-ID des aktuellen
  Branches geholt; anschließend setzt `update-ref --no-deref` ausschließlich den
  gleichnamigen Tracking-Ref. Damit kann auch ein dort eingeschleuster Symref
  keinen lokalen Branch verändern. Die Config muss die eindeutige Abbildung
  `refs/heads/<branch>` → `refs/remotes/<remote>/<branch>` belegen. Auch die
  geprüfte konkrete URL wird statt des veränderlichen Remote-Namens übergeben.
  Eine parallele Config-Änderung kann dadurch weder das Ziel umleiten noch lokale
  Branches, Tags oder fremde Refs einschleusen. Pushes sind ebenfalls an die
  geprüfte URL gebunden, setzen `--recurse-submodules=no` und schalten für genau
  diesen Aufruf `core.hooksPath` ab; ein pre-push-Hook darf keine ungeprüften
  Tags oder weiteren Refs veröffentlichen. Unsichere Remotes werden als Fehler ausgewiesen;
  sichere Remotes desselben Repos werden einzeln
  weiter aktualisiert.
  Seit 0.18.9 gehört der Transportweg zur Bindung: Für jeden Aufruf mit gepinnter
  URL setzt `run_git()` `core.sshCommand` auf das gewöhnliche `ssh` und entfernt
  `GIT_SSH` und `GIT_SSH_COMMAND` aus der geerbten Umgebung (`TRANSPORT_GIT_ENV`).
  Ein solcher Wrapper bekommt Host und Pfad zwar als Argumente, muss sich aber
  nicht daran halten — sonst entschiede er über das wahre Ziel, und die geprüfte
  Adresse wäre Dekoration. Ein eigener Schlüssel oder Port gehört deshalb in
  `~/.ssh/config`, nicht in einen Wrapper; das Befehlsprotokoll zeigt das
  `env -u …` mit an. Reine Lesebefehle des Scans behalten die Benutzerumgebung.
- Ein Repo ohne ersten Commit ist ein gewöhnlicher Zustand, kein Fehler. Nach
  dem Klonen eines LEEREN Repos zeigt HEAD auf einen Branch, den es noch nicht
  gibt; sobald jemand den ersten Commit pusht und man fetcht, existiert der
  Tracking-Ref sehr wohl. `git rev-list HEAD...<ref>` bricht dort mit Exit 128
  ab, und gmf meldete das Repo als kaputt („ERROR: git rev-list failed (exit
  128)“, Review-Fund 2026-09-03). `branch_delta()` zählt in diesem Fall die
  Commits des Tracking-Refs: null voraus, n zurück. Der Sonderfall wird erst
  NACH dem fehlgeschlagenen Aufruf geprüft, damit jedes Repo mit Commits
  weiterhin genau einen Git-Aufruf kostet — und ein echter Lesefehler bleibt
  einer, sobald HEAD existiert.
- Werte aus der `config.json` können fehlen: Die App-Tasten trägt man von Hand
  ein, ein vergessenes `name` oder `path` ist ein naheliegender Tippfehler.
  `app_label()` und `app_path()` liefern dafür sichtbare Platzhalter statt eines
  KeyError — den fängt in der Hauptschleife niemand ab, und die Fußzeile
  zeichnet vor der ersten Repo-Zeile (Review-Fund 2026-09-03).
- `curses.curs_set()` steht ausschließlich in `set_cursor_visible()`. Auf einem
  Terminal ohne Cursor-Steuerung wirft curses dort; jeder direkte Aufruf machte
  daraus einen Traceback statt einer Auskunft.
- Der reine lokale Scan darf zwölf Worker nutzen; ein Scan mit Fetch höchstens
  acht, und jeder Fetch läuft mit `--jobs=1`. Der verbreitete sshd-Default
  `MaxStartups 10:30:100` verwirft sonst beim kalten Aufbau eines
  ControlMaster-Sockets zufällig einzelne der zwölf Verbindungen; ohne `--jobs=1`
  könnte zudem `fetch.parallel` oder `submodule.fetchJobs` aus der
  Benutzerkonfiguration innerhalb jedes Aufrufs weitere Verbindungen öffnen. Eine
  Erhöhung braucht deshalb einen echten Kaltstart-Netztest, nicht nur Unit-Tests
  mit gemocktem Git (bestätigter Praxisbefund 2026-08-05).
- Die Info-Ansicht entfernt weder Remotes noch Branches. Git würde dabei auch
  ihre Reflogs löschen; darin können die letzten lokalen Verweise auf Commits
  liegen, und diese Historie lässt sich nicht durch einen ehrlichen Undo-Befehl
  rekonstruieren. Die Ansicht bleibt deshalb rein lesend. Auch Stashes zeigt gmf
  nur als Diff an. Anwenden kann das Ziel nicht atomar gegen einen parallelen
  Checkout sowie Index- oder Arbeitsbaumänderungen binden; Löschen kann keinen
  einzelnen Reflog-Eintrag atomar festhalten. Beides bleibt dem Terminal
  vorbehalten.
- Pull bleibt vollständig dem Terminal vorbehalten. Ein Fast-forward müsste
  Branch-Ref, Index und Arbeitsbaum gemeinsam gegen den freigegebenen Stand
  binden; Git bietet dafür gegenüber einem parallelen Checkout keine portable
  atomare Operation. Die frühere `L`-Aktion wurde deshalb entfernt.
- Die Änderungsansicht (`A`) ist vollständig rein lesend. gmf verwirft keine
  Arbeitsbaumdatei, entfernt nichts aus der Vormerkung und bietet auch kein
  „alle stashen“ an: Zwischen letzter Inhaltsprüfung und `git restore`, `git rm`
  oder `git stash` bleibt gegenüber beliebigen Editoren eine nicht atomar
  schließbare Lücke. Solche Mutationen bleiben nach eigener Diff-Prüfung dem
  Terminal vorbehalten. Die nicht-interaktive Schnittstelle ist ebenfalls
  lesend.
- „Nur lesend" ist eine Zusage über die **Repo-Inhalte**, nicht über den Rechner.
  Zwei Ausnahmen gehören überall dorthin, wo die Zusage steht: `load_config()`
  legt beim ersten Lauf `~/.config/gitmaster_flash/config.json` an — bei `--diff`
  auch auf dem befragten Rechner, weil dort per stdin dasselbe Skript läuft —,
  und `--fetch` führt einen echten, auf sichere Remote-Tracking-Refspecs
  begrenzten Fetch ohne Tags aus. Beides lässt
  Branch, Index und Arbeitsbaum unberührt; „ändert nie etwas" wäre trotzdem
  falsch und stand so bis 2026-07-28 in beiden READMEs. Dass die Datei angelegt
  wird, ist dabei bewusst so: gmf zielt ausschließlich auf eigene Rechner, und
  dort darf eine Datei entstehen, wenn sie einen Nutzen hat (Entscheidung
  2026-07-28). Die Zusage muss den Vorgang nur benennen, nicht vermeiden.
- Dazu gehört eine dritte, kleinere Ausnahme: `GIT_OPTIONAL_LOCKS=0` hält
  `git status`, `git stash show` und `git ls-files` davon ab, nebenbei den
  Index zu schreiben — `git diff` aber nicht. Ist der im Index gespeicherte
  Zeitstempel einer Datei veraltet, liest `git diff` sie neu und schreibt die
  aufgefrischte Stat-Zwischenspeicherung zurück, auch mit gesetzter Variable.
  Die Einträge des Index — Modus, Objekt-ID, Stufe, Pfad — bleiben dabei
  unverändert, es ändert sich also nichts an Vormerkung oder Inhalt. Git
  vergleicht diese Zeitstempel **sekundengenau**: Ein Test, der eine veraltete
  Zwischenspeicherung nachstellen will, muss den Zeitstempel deshalb auf eine
  andere Sekunde setzen, sonst entscheidet der Zufall über sein Ergebnis
  (belegt am 2026-08-19; vorher schlug
  `test_readers_disable_optional_index_writes_and_fsmonitor_hooks` in rund
  30 % der Läufe fehl).
- Ändert sich der Remote-Vertrag — JSON-Felder, Exit-Codes, der `ssh`-Aufruf —,
  gehört der Fall nach `tests/test_cli_blackbox.py`. Dort führt ein temporäres
  `ssh` im PATH den echten, über stdin übertragenen Code lokal aus und prüft
  Remote-JSON, Remote-Exit-Code und lokale Auswertung gemeinsam, ohne Netz.
  Unit-Tests mit gemocktem `subprocess` sehen genau diese Grenze nicht.
- Jeder von Git stammende Text muss vor `json.dumps` durch `json_text()` (also
  `terminal_text()`). Git erlaubt Bytes ohne UTF-8-Bedeutung in Ref- und
  Remote-Namen; die Leser dekodieren sie mit `surrogateescape`, und roh
  serialisiert ergäbe das ungültiges UTF-8 oder einen `UnicodeEncodeError`.
  Neue JSON-Felder deshalb gleich dort anschließen.
- Der Filter (`/`, `--filter`) grenzt nur die ANZEIGE ein: `all_statuses` bleibt
  der vollständige Scan, `statuses` ist die sichtbare Auswahl, und alles
  Zeichnende sowie die Auswahl arbeiten unverändert auf `statuses`. Wer eine
  Liste anfasst, muss die andere mitziehen — `refresh_one()` tut das über
  Identität (`is`), nicht über Gleichheit: Zwei Repos mit identischem Zustand
  sind als Dataclass gleich, `list.index()` träfe dann womöglich das falsche.
- Die zsh-Kurzformen der Schleifen (`for x (a b) kommando`, `repeat n kommando`,
  `for ((…)) kommando`, `for x in a b; kommando`, `select x (a b) kommando`)
  haben kein `done`: Ihr Rumpf ist genau EINE Teilliste. Beide Scanner in
  `install.sh` behandeln sie über dieselben drei Funktionen — `ends_sublist`,
  `loop_head_end`, `short_loop_body`. Die Regel dahinter: Sobald der Kopf
  feststeht, führende Trenner überspringen; dann entscheidet das erste Wort —
  `do` heißt doch lange Fassung, `{` heißt Klammerrumpf, alles andere ist die
  eine Teilliste. Ein `;` VOR dem Rumpf gehört noch zum Kopf (`repeat 2; print
  hi` gibt „hi hi" aus), ein `;` danach beendet ihn (`for x (a b) print $x;
  print E` gibt „a b E" aus), und `&&`, `||`, `|` beenden ihn nicht. Wer hier
  etwas ergänzt, prüft es zuerst an der Shell selbst: Die Grammatik ist an
  mehreren Stellen anders, als sie aussieht (siehe `while` unter „Bewusst nicht
  umgesetzt").
  Eine Zeile, die auf einen ungequoteten Backslash endet, ist gar keine
  Zeilengrenze: zsh entfernt Backslash und Umbruch, bevor es zu lesen beginnt.
  Der Lesedurchgang fügt solche Zeilen deshalb ohne Trennzeichen zusammen,
  bevor irgendein Scanner sie sieht (`line_continues`, `scan_zshrc_line`) —
  ein eingefügtes Leerzeichen zerrisse `PATH=/a:\` + `/b`. Bis 2026-09-03 galt
  der Backslash wie ein offener Quote-Kontext; `zshrc_context_state` meldete
  danach dauerhaft `opaque`, und der Installer lehnte jede völlig gewöhnliche
  `.zshrc` mit einer Fortsetzung ab. Offene Anführungszeichen und `${…}` bleiben
  dagegen ein Ablehnungsgrund — `scan_line_end()` trennt beide Fälle an einer
  Stelle, `line_leaves_quote_open()` und `line_continues()` sind nur Fragen
  darauf.
  Der Rumpf beginnt an einem KOMMANDOANFANG, und beide Scanner müssen ihn dort
  auch so behandeln (`short_loop_body_at`): Sie prüfen `return`/`exit`/`exec`
  nur an einer solchen Stelle, und `for x (a b) return` bricht die `.zshrc` ab
  — jede spätere `source`-Zeile ist dann unerreichbar. Solange die Kurzform den
  Block bis zum Dateiende offen ließ, fiel das nicht auf; mit ihrer Erkennung
  wurde daraus ein „Already installed" für eine Datei ohne `gmf`
  (Fund 2026-08-29). Bei einem Klammerrumpf zeigt die Marke HINTER das `{`,
  nicht darauf: Auf dem `{` öffnete der Blockstapel den bereits vollständig
  gelesenen Block ein zweites Mal, und das eine `}` räumte nur einen davon ab.
- Ein Repo wird über seinen ECHTEN Pfad (`st.path`) oder über Objektidentität
  wiedererkannt, nie über den Anzeigenamen `rel`. `collect_status()`
  normalisiert `rel` auf NFC, damit die Spaltenbreiten stimmen; zwei
  nebeneinanderliegende Ordner, die sich nur in NFC/NFD unterscheiden — unter
  Linux zulässig —, tragen danach denselben `rel`. Wo Objekte neu entstehen
  (nach `reload()` oder dem Hintergrund-Scan) reicht `is` nicht, dort ist
  `path` der Schlüssel. Dieselbe Verwechslung trat zweimal auf: erst im
  Hintergrund-Fetch (`locally_refreshed`, behoben 2026-08-25), dann beim
  Wiederfinden der Auswahl in `apply_filter()` (behoben 2026-08-29).
- Ein aktiver Filter muss in JEDER Ausgabe stehen, die man später vergleicht
  oder als Übersicht liest: Kopfzeile der TUI, Kopfzeile von `--list`, erste
  Zeile des `--diff`-Berichts. Ohne Treffer kommt überall ein Hinweis auf
  stderr dazu — bei `--list`/`--json` ebenso wie bei `--diff`, wo „keine
  Unterschiede" mit Exit-Code 0 sonst nicht von zwei wirklich gleichen Rechnern
  zu unterscheiden wäre (Fund 2026-08-29). Sonst sieht ein ausgeblendeter
  Bestand wie ein echter Unterschied aus, und eine leere Trefferliste mit
  Exit-Code 0 wie „alles in Ordnung". Der Suchtext kommt roh von der
  Kommandozeile und geht deshalb an jeder Ausgabegrenze durch
  `terminal_text()`. Aus demselben Grund nennt
  die Kopfzeile mit `hidden_dirty()` die ausgeblendeten Repos, die
  Aufmerksamkeit bräuchten — ein Übersichtswerkzeug darf nicht ausgerechnet die
  verstecken.
- `--diff` filtert BEIDE Rechner mit demselben Suchtext (`filter_repo_dicts`).
  Nur eine Seite zu filtern erzeugte „nur hier"-Unterschiede, die es nicht gibt.
- Die Vorschläge der Commit-Hilfe (`G`) wählen nur aus; committet wird weiterhin
  erst nach der Bestätigung im zweiten Schritt. `commit_groups()` darf ein
  Rename-Paar nie trennen (`_expand_rename_groups`) — ein Commit mit nur einer
  Hälfte hinterließe eine halbe Umbenennung. Zieht ein Vorschlag dadurch etwas
  hinein, das nicht zu seinem Namen passt, sagt die Beschriftung das über
  `CommitGroup.completed`; sonst hieße eine Gruppe „gelöschte Dateien (2)“,
  obwohl eine der beiden neu ist.
- Die Commit-Hilfe zeichnet ihr eigenes Bild und hat deshalb eine eigene
  Meldungszeile. `self.message` gehört der Repo-Liste und ist dort unsichtbar —
  Rückmeldungen aus der Hilfe werden zurückgegeben, nicht gesetzt.
- Ein Eintrag, für den es gegenüber HEAD nichts zu committen gibt, ist in der
  Hilfe sichtbar, aber nicht anwählbar (`committable_against_head()`). Das
  betrifft den Porcelain-Status `AD`: im echten Index hinzugefügt, im
  Arbeitsbaum wieder gelöscht — den Pfad gibt es weder in HEAD noch daneben.
  Weil die Hilfe mit allem angehakt startet, ließ ein einziger solcher Pfad
  vorher den GESAMTEN Commit an der Freigabeprüfung scheitern („temporary index
  differs from approved paths“), und auch gewöhnliche Änderungen blieben liegen
  (Review-Fund 2026-09-03). Vorauswahl, `␣`, `A` und die Vorschläge gehen alle
  über dieselbe Funktion. Die Breite der Pfadspalte leitet sich seither aus der
  längsten Beschriftung ab; eine feste Zahl schnitt die neue, längere still ab.
  Der abgedruckte Textblock im README wird nachgebaut und Zeile für Zeile
  geprüft (`DocumentationContractTests`), damit er nicht wieder still veraltet.
- Der Hintergrund-Fetch (`R`) ist der einzige Ort, an dem Git-Aufrufe außerhalb
  des Hauptthreads laufen. Drei Regeln hängen daran:
  1. `_run_process_group()` trägt jeden Prozess in `_LIVE_PROCESSES` ein und
     prüft `_CANCEL` — beides unter derselben Sperre. Nur so kann zwischen der
     Prüfung und dem `Popen` kein Prozess entstehen, den `cancel_git_calls()`
     nicht mehr sieht. Wer dort etwas ändert, muss diese Klammer erhalten.
  2. `run()` beendet den Scan in einem `finally`. Ohne das liefen git und das von
     ihm gestartete ssh nach dem Ende der Oberfläche verwaist weiter.
  3. Das begrenzte Warten auf eine Taste (`scr.timeout`) bleibt strikt in
     `_wait_for_key()`. Die Unteransichten lesen mit `get_wch()`, und das wirft
     bei abgelaufenem Zeitgeber eine `curses.error`, statt zu warten.
- Während des Scans wird die Liste NICHT umsortiert (`_merge_scanned` setzt am
  Platz ein), sonst sprängen Zeilen unter dem Cursor weg. Sortiert wird einmal in
  `_finish_scan`. Ein Repo, das währenddessen lokal neu eingelesen wurde, steht in
  `locally_refreshed` und behält seinen jüngeren Stand — lieber veraltete
  Remote-Zahlen als ein fertiger Commit, der wieder als offene Änderung erscheint.
- Die Einstellungsansicht (`,`) ändert nur, was Anzeige und Geduld betrifft.
  `sync_remote_names`/`sync_remote_hosts` (Ziel von P) und `apps` (was gmf per
  `open -a` startet) stehen in `READ_ONLY_SETTINGS` und bleiben der Datei
  vorbehalten — sichtbar, aber nicht änderbar. Eine neue Einstellung gehört nur
  dann nach `EDITABLE_SETTINGS`, wenn ein Vertipper darin nichts startet und
  nichts umleitet.
- Jede Eingabe läuft durch `parse_setting()`, bevor sie in die Config kommt;
  `load_config()` prüft nichts. Die Zahlenprüfung nutzt bewusst eine Regex und
  nicht `str.isdigit()` — letzteres hält auch „²" für eine Ziffer, `int()` aber
  nicht, und der Wert fiele erst beim nächsten Git-Aufruf auf.
- `save_config()` schreibt atomar (Nachbardatei + `os.replace`). Schlägt das
  Speichern fehl, meldet die Ansicht ausdrücklich „für diese Sitzung übernommen,
  aber nicht gespeichert" statt eines Erfolgs.
- Die TUI bekommt den Config-Pfad übergeben; im Demo-Modus ist er `None`. Ein
  Screenshot- oder Demo-Lauf darf die echte Einstellungsdatei nie anfassen,
  sonst wären die Bilder maschinenabhängig — und die persönlichen Einstellungen
  verstellt.
- Eine nicht zerlegbare Remote-URL bekommt über `endpoint_fingerprints()` einen
  Ersatzfingerprint aus dem Hash der Rohadresse. Eine leere Liste sähe auf zwei
  Rechnern gleich aus, und `--diff` verschwiege den Ziel-Drift gerade dann, wenn
  die Konfiguration ohnehin nicht belegbar ist.

## Bewusst nicht umgesetzt

Keine offenen Aufgaben, sondern getroffene Entscheidungen und bekannte Grenzen.
Sie standen bis 2026-08-23 als offene Kästchen im Backlog und sahen dadurch wie
liegengebliebene Arbeit aus.

- **Verlauf umschreiben (`reset`, Squash) bleibt draußen** (Entscheidung
  2026-08-03). Nichts davon ist an einer Datei sichtbar, der Zielzustand lässt
  sich nur mit der Commit-Historie im Kopf benennen, und ein falsch geratener
  `reset --hard` kostet Commits statt Dateien. Bleibt Handarbeit im Einzelfall.
- **Der zshrc-Scanner in `install.sh` erkennt die Kurzform von `while` und
  `until` nicht** — und darf es auch nicht (Fund 2026-08-29). Bei allen anderen
  Schleifen lässt sich der Kopf lexikalisch abgrenzen und der Rumpf danach als
  genau eine Teilliste lesen; die Bedingung von `while`/`until` ist dagegen
  selbst eine Liste, in der ein `;` nichts beendet. `while false; print x`
  läuft endlos, weil die Bedingung `false; print x` mit dem Status von `print`
  endet. Wer das `;` für die Kopfgrenze hielte, erklärte eine Zeile für
  abgeschlossen, die die Datei nie verlässt — der Installer meldete „Already
  installed", ohne dass `gmf` je entsteht. Solche Dateien werden weiter
  abgelehnt; das ist die sichere Richtung. Ein Blackbox-Test in
  `tests/test_cli_blackbox.py` hält den Fall fest: Wer `while` doch in die
  Kurzform-Erkennung aufnimmt, bekommt dort ein „Already installed" für eine
  Datei ohne `gmf` gemeldet.
- **Kein Bild zeigt einen abgebrochenen Dialog** (`⊘`-Zeile). Der einzige Weg
  dorthin führt über die Info-Ansicht, die der Bildnachbau nicht sauber trifft —
  siehe „Grenze des Generators" oben. Solche Ansichten gehören als
  vorformatierter Textblock ins README, nicht als Bild.

## Offene Punkte / Ideen

- (derzeit keine; die vier Feature-Punkte Suche/Filter, Einstellungen in der
  Oberfläche, Hintergrund-Fetch und Commit-Vorschläge sind am 2026-08-23
  umgesetzt worden)
