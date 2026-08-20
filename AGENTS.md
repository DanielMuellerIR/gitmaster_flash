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
- Eine nicht zerlegbare Remote-URL bekommt über `endpoint_fingerprints()` einen
  Ersatzfingerprint aus dem Hash der Rohadresse. Eine leere Liste sähe auf zwei
  Rechnern gleich aus, und `--diff` verschwiege den Ziel-Drift gerade dann, wenn
  die Konfiguration ohnehin nicht belegbar ist.

## Offene Punkte / Ideen

- [ ] Verlauf umschreiben (`reset`, Squash) bleibt bewusst draußen. Nichts davon ist
      an einer Datei sichtbar, der Zielzustand lässt sich nur mit der Commit-Historie
      im Kopf benennen, und ein falsch geratener `reset --hard` kostet Commits statt
      Dateien. Bleibt Handarbeit im Einzelfall (Entscheidung 2026-08-03).
- [ ] Suche/Filter über die Repo-Liste (wird ab einigen hundert Repos wichtiger als
      die Anzeige selbst; dort ist dann der Scan der Flaschenhals).
- [ ] Kein Bild zeigt bisher einen abgebrochenen Dialog (`⊘`-Zeile), weil der
      einzige Weg dorthin über die Info-Ansicht führt — die der Nachbau nicht
      sauber trifft (siehe Grenze des Generators oben).
- [ ] Einstellungen direkt in der TUI editieren (bisher: config.json von Hand).
- [ ] Fetch im Hintergrund statt blockierend mit Fortschrittsanzeige.
- [ ] Intelligentere Commit-Vorschläge (z.B. Gruppierung nach Dateityp).
- [ ] Screenshots in `docs/` bei UI-Änderungen neu aufnehmen (Rezept oben).
