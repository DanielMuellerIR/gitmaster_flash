<p align="center">
  <img src="docs/gitmaster-flash.png" width="128" alt="Projekt-Icon von Gitmaster Flash">
</p>

<h1 align="center">Gitmaster Flash</h1>

**🌐 Sprache / Language:** [English](README.md) · [Deutsch](README.de.md)

<p align="center">
  <em>“It's like a jungle sometimes”</em><br>
  <sub>— Grandmaster Flash and the Furious Five, “The Message”</sub>
</p>

Eine schnelle Terminal-Übersicht (TUI) über alle Git-Repos unterhalb des
aktuellen Ordners: Man sieht auf einen Blick, wo noch etwas liegen geblieben ist,
und räumt es direkt auf.

Grün heißt sauber und mit dem Remote synchron, Rot und Gelb heißen: da ist noch
was. Eine einzige Python-Datei, nur Standardbibliothek — kein `pip install`, kein
Hintergrunddienst, keine Repo-Registrierung. Gescannt wird schlicht alles
unterhalb des Ordners, in dem man es startet.

![Kompaktansicht: jedes Repo als Marke und Name, darunter das Befehlsprotokoll](docs/compact.de.svg)

<sub>Alle Bilder in dieser README werden aus dem echten Programm auf der `--demo`-Sandbox erzeugt — `python3 docs/make-screens.py` (bzw. `--check`). Keine Screenshots, die bei jeder UI-Änderung neu gemacht werden müssen. Die Befehle im Protokoll unter der Liste liefen wirklich auf der Sandbox: ein Stash-Pop und ein Pull.</sub>


Zum gefahrlosen Ausprobieren, ohne die eigenen Repos anzufassen:

```sh
python3 gitmaster_flash.py --demo
```

`--demo` baut eine Wegwerf-Sandbox aus Fake-Repos in allen denkbaren Zuständen
und startet die Oberfläche darauf. Sie liegt im Temp-Ordner und kann danach
einfach gelöscht werden.

## Die ersten 60 Sekunden

```sh
git clone https://github.com/DanielMuellerIR/gitmaster_flash.git
cd gitmaster_flash
./install.sh          # Selbsttest, dann `gmf`-Shell-Wrapper registrieren
gmf ~/projekte        # oder einfach `gmf` für den aktuellen Ordner
```

Drei Tasten tragen durch die erste Sitzung:

- `↑`/`↓` wählt ein Repo (`←`/`→` springen in der Kompaktansicht eine ganze
  Spalte weiter); die mit offenen Punkten stehen schon oben.
- `A` zeigt, was sich in einer Datei geändert hat, `C` committet sie geführt.
- `H` listet jeden Git-Befehl, den gmf für dich ausgeführt hat — so lernt man
  die Syntax nebenbei mit, ohne sie auswendig zu lernen.

Nichts wird gepusht, verworfen oder gelöscht, ohne dass vorher eine Rückfrage den
genauen Befehl nennt. Die Details stehen weiter unten; zum Loslegen braucht man
sie nicht.

## Zwei Ansichten (`M`)

Bis 20 Repos startet gmf in der **Detailansicht**: pro Repo eine volle Zeile mit
Zählern, Branch und Remotes. Darüber wird dieselbe Liste zur langen Scrollstrecke,
deshalb startet dann die **Kompaktansicht** — pro Repo eine Marke und der Name,
spaltenweise gefüllt wie bei `ls`, also über die Breite des Fensters statt über
seine Höhe. Das ist das Bild ganz oben. Die Schwelle steht als `compact_from` in
der `config.json`.

Die Detailansicht bleibt bis einschließlich 20 Repos bewusst Standard: Bei
dieser Größe ist die direkte Anzeige aller Zähler, des Branches und der Remotes
weiterhin die klarste Übersicht. Die Kompaktansicht ersetzt sie nicht, sondern
ist ihre natürliche Weiterentwicklung für eine wachsende Repo-Sammlung. In
wenigen Tastendrücken ist das gesuchte Repo erreicht; `M` zeigt dort anschließend
alle Details.

Die Marke ist der ganze Status, auf ein Feld eingedampft:

| Marke | Bedeutung |
|---|---|
| ✔ | sauber und synchron |
| ● | geänderte, gelöschte oder unversionierte Dateien |
| ⚠ | Merge-Konflikt |
| ⚑ | Stash vorhanden |
| ↑n / ↓n | vor/zurück gegenüber dem Sync-Remote |
| ⇅ | divergiert (gleichzeitig vor und zurück) |
| ✘ | Fehler beim Repo-Scan oder letzter Fetch eines Remotes fehlgeschlagen |
| ? | kein Sync-Remote, kein Remote-Branch oder detached HEAD |

Treffen mehrere Zustände zu, zeigt die Marke den dringendsten: zuerst `✘`, dann
`⚠`, `●`, `⚑`, den Abstand zum Sync-Remote und schließlich `?`. Rot steht für
Fehler, Konflikt, lokale Änderung, fehlende eingehende Commits oder Divergenz;
Gelb für Stash, ausgehende Commits oder eine fehlende Sync-Beziehung; das grüne
`✔` bedeutet sauber und synchron. `M` oder `I` zeigt die Details hinter der
verdichteten Marke.

`M` schaltet zwischen kompakt und Detail um und behält das gewählte Repo — man
sucht es also in der breiten Übersicht und arbeitet dann im Detail daran weiter.
`←`/`→` springen eine Spalte weiter, womit 60 Repos in wenigen Tastendrücken
durchquert sind. Aufklappen von Dateien und Stashes bleibt der Detailansicht
vorbehalten.

## Was eine Detailzeile verrät

![Detailansicht: eine Zeile je Repo, problematische zuerst](docs/overview.de.svg)

- **Remote-Namen sind immer sichtbar** — jede Zeile endet mit allen konfigurierten
  Remotes, auch wenn alles synchron ist. Reihenfolge: privater Sync-Remote zuerst,
  sonstige Remotes danach, GitHub ganz rechts.
- **↑n / ↓n neben einem Remote** — Commits vor/zurück gegenüber genau diesem
  Remote für den aktuellen Branch, auf Basis des letzten Fetch. `R` aktualisiert
  alle Remotes aller Repos mit `git fetch --all`, ohne einen Working Tree zu ändern.
- **M / D / U** — Anzahl geänderter, gelöschter und unversionierter Dateien.
- **⚑Stash:n** — vorhandene Stashes. Die übersieht man sonst gern.
- **⚠conflict:n** — ungemergte Dateien, etwa nach einem `git stash pop`, der
  nicht sauber aufging. Bewusst getrennt von „modified", weil dahinter andere
  Arbeit steckt.
- Warnungen wie „kein Sync-Remote" oder „Branch nicht auf dem Remote".

Repos mit offenen Punkten stehen oben, saubere unten. Beide Ansichten benutzen
dieselbe Reihenfolge, in der Kompaktansicht ist also die linke Spalte die
interessante.

## Befehlsprotokoll, immer sichtbar

Die letzten Befehle stehen unter der Liste: in der Detailansicht drei Zeilen, in
der Kompaktansicht alles, was die Spalten übrig lassen. `Tab` setzt den Fokus
dorthin — in der Detailansicht wächst der Bereich dafür auf ein Drittel des
Fensters. Der Auswahlbalken wandert mit dem Fokus: Solange man im Protokoll ist,
hat die Repo-Liste keinen — so ist immer klar, wem die Pfeiltasten gerade
gehören. `↑`/`↓` gehen durch die Befehle, `Tab` führt zurück zur Liste; beide
Seiten merken sich, wo man war.

![Fokus im Befehlsprotokoll: der Auswahlbalken steht dort, die Repo-Liste hat keinen](docs/command-log.de.svg)

Abgebrochene Dialoge erscheinen als `⊘ … (nicht ausgeführt — abgebrochen)`, damit
das Protokoll nie etwas als gelaufen ausweist, das gar nicht lief. Zwei gleiche
`fetch`-Zeilen sind ebenfalls kein Fehler: gmf holt einmal vor der Rückfrage und
einmal nach der Bestätigung und handelt nur, wenn sich dazwischen nichts bewegt
hat (siehe „Sicheres Push und Pull").

`H` zeigt dasselbe Protokoll vollständig, über den Sicherheitsregeln. Die reinen
Lesebefehle des Scans stehen bewusst nicht drin — sie würden die interessanten
Zeilen zumüllen. Argumente sind so gequotet, wie eine Shell sie braucht, eine
Zeile lässt sich also direkt übernehmen. Und destruktive Dialoge zeigen den
Befehl vor der Bestätigung: Man sieht `git remote remove github` beim
Entscheiden, nicht erst danach.

## Bedienung

Alle Kürzel stehen dauerhaft im Footer — merken muss man sich nichts.
Groß-/Kleinschreibung ist egal, `f` wirkt wie `F`.

| Taste | Aktion |
|---|---|
| ↑ / ↓ | Repo auswählen |
| → / ← | auf-/zuklappen (Dateien mit M/D/U/C, Stashes) |
| M | zwischen Kompakt- und Detailansicht umschalten |
| Tab | Fokus ins Befehlsprotokoll und zurück |
| ⏎ | beenden und in den Repo-Ordner wechseln (braucht den `gmf`-Wrapper, siehe unten) |
| E | Repo in einer konfigurierten App öffnen (eigene in `config.json` eintragen) |
| A | Änderungen ansehen: Datei wählen, Diff lesen |
| C | Commit-Hilfe (siehe unten) |
| P | aktuellen Branch sicher zum privaten Sync-Remote pushen |
| L | aktuellen Branch sicher per Fast-forward vom privaten Sync-Remote holen |
| G | geschützter GitHub-Push mit Commit-/Dateivorschau und Texteingabe |
| H | Befehlsprotokoll dieser Sitzung und Git-Sicherheitsregeln anzeigen |
| I | Repo-Details und Remotes; dort `T` Remote prüfen, `X` Remote entfernen |
| U | neuesten Stash anwenden (`git stash pop`, mit Rückfrage) |
| S | neuesten Stash als Diff ansehen (read-only, scrollbar) |
| D | neuesten Stash endgültig verwerfen (`git stash drop`, mit Rückfrage) |
| R | alles neu einlesen inklusive `git fetch --all` |
| Q | beenden |

Auf einen bereits konfliktbehafteten Baum wird nie ein weiterer Stash gepoppt —
erst die Konflikte auflösen. Die Vorschau enthält auch unversionierte und binäre
Dateien; ein fehlgeschlagener oder unerwartet leerer Git-Report wird vor der
destruktiven Verwerfen-Aktion ausdrücklich gekennzeichnet.

## Änderungen ansehen (`A`)

`→` zeigt, *dass* sich eine Datei geändert hat; `A` zeigt, *was* sich darin
geändert hat. Datei mit `↑`/`↓` (oder `Tab`) wählen, `⏎` drücken — der Diff
öffnet sich im scrollbaren Betrachter, auch für neue Dateien, die `git diff`
sonst ignoriert, und für gelöschte.

```text
 Änderungen · api-gateway
 M  README.md
 M  server.py
 U  notizen.txt
 D  alte-config.yml

 ↑/↓ oder Tab Datei wählen · ⏎ Diff ansehen · Q/Esc zurück
```

Rein lesend: weder Index noch Arbeitsbaum werden angefasst. Man kann also erst
schauen und dann entscheiden, was committet oder verworfen wird.

## Repo-Info und Remotes (`I`)

`I` öffnet für das ausgewählte Repo eine scrollbare Übersicht. Sie
zeigt Pfad, Branch und vollständigen HEAD, letzten Commit, Größe der Historie,
Upstream-Stand, Arbeitsbaum-Zähler, Stashes, Tags an HEAD und alle Remotes.
Fetch- und Push-Adressen stehen getrennt da, weil Git dafür unterschiedliche
Ziele konfigurieren kann. GitHub-Remotes erhalten zusätzlich eine
zugangsdatenfreie Web-URL `https://github.com/…`, die sich in unterstützenden
Terminals direkt öffnen lässt. Eingebettete URL-Zugangsdaten, Query-Parameter
und Fragmente werden nie angezeigt. Das Öffnen der Ansicht verändert nichts.

Die Remotes sind auswählbar: `↑`/`↓` oder `Tab` setzt den Auswahlbalken auf den
nächsten Remote-Block, `Bild↑`/`Bild↓` scrollt den Text.

**`T` prüft das ausgewählte Remote.** Dazu läuft `git ls-remote`, das nur die
Ref-Liste erfragt — es überträgt keine Objekte und ändert lokal nichts. Die
Antwort trennt die Fälle, die sonst gleich aussehen:

| Ergebnis | Bedeutung |
|---|---|
| existiert und antwortet (n Branches) | Adresse stimmt, Zugriff klappt |
| antwortet, hat aber keine Branches | erreichbar, Repo noch leer |
| Adresse erreichbar, aber dort ist kein Repo | gelöscht, umbenannt oder kein Zugriff |
| Server verlangt einen Login | Credential-Helper oder SSH-Key fehlt |
| SSH-Hostschlüssel unbekannt oder geändert | einmal im Terminal verbinden und prüfen |
| Hostname nicht auflösbar | kein Netz oder DNS-Problem |
| keine Verbindung zum Host | offline, Firewall oder Server aus |
| Server antwortet mit Fehler | Problem dort, nicht am eigenen Repo |
| keine Antwort in n Sekunden | Netz oder Server zu langsam |

Unterhalb dieser Klartext-Einordnung bewahrt die Info-Ansicht zusätzlich Gits
eigene Fehlermeldung als Beleg auf.

**`X` entfernt das ausgewählte Remote** nach einer Rückfrage, die vorher genau
benennt, was passiert. Es ist ausschließlich eine lokale Konfigurationsänderung:
Der Abschnitt `[remote "<Name>"]` verschwindet aus `.git/config`, die
Remote-Tracking-Branches `refs/remotes/<Name>/*` werden gelöscht, und ein lokaler
Branch mit Upstream dorthin verliert diese Verknüpfung. Commits, Dateien,
Branches und Stashes bleiben unberührt, auf dem Server ändert sich nichts. Der
Dialog zeigt sowohl den Befehl, der ausgeführt wird, als auch die Zeile
`git remote add …`, die alles zurücknimmt.

Zusammen nützlich: Auf GitHub gelöschte Repos behalten lokal ihr totes Remote.
`R` markiert so ein Remote rot (`✘`) in der Repo-Zeile, `T` bestätigt, dass die
Adresse erreichbar ist, das Repo aber weg ist, und `X` räumt es weg.

**Auch die lokalen Branches stehen dort** — der zweite Zustand, den Git nie
überträgt. Man sieht sie nie, weil man immer nur den aktuellen Branch betrachtet;
entsprechend sammeln sich abgeschlossene Features und alte Experimente an. Je
Branch stehen letzter Commit, Upstream mit Vorsprung/Rückstand und der
Merge-Zustand da. `X` löscht einen Branch, aber nur, wenn er vollständig in HEAD
gemergt ist (`git branch -d`): Seine Commits hängen dann ohnehin an HEAD, es kann
also nichts verloren gehen. Nicht gemergte Branches lehnt gmf mit Begründung ab
und nennt den Terminal-Befehl, der es erzwingen würde. Die Rückfrage zeigt sowohl
den genauen Löschbefehl als auch `git branch <Name> <OID>` zum Wiederherstellen
des Branches.

Die Werte stehen linksbündig in einer Spalte, und identische Fetch-/Push-Adressen
teilen sich eine `Fetch+Push`-Zeile — getrennt erscheinen sie nur, wenn sie
wirklich abweichen (`git remote set-url --push` erlaubt das, gmf sperrt dann
Transfers).

```text
 Repo-Info · api-gateway
Pfad:            ~/projekte/api-gateway
Branch:          main
HEAD:            a1b2c3d (a1b2c3d4e5f6789012345678901234567890abcd)
Letzter Commit:  2026-07-25T10:30:00+02:00 · Beispielautor
  feat: add health endpoint
Historie:        42 Commit(s) · vollständiger Clone
Upstream:        origin/main (0 voraus / 0 zurück)
Arbeitsbaum:     sauber
Stashes:         0
Tags an HEAD:    v1.4.0

Remotes:
  origin [Sync]
    Fetch+Push:   git@example.invalid:team/api-gateway.git
    Branch main:  0 voraus / 0 zurück

  github [GitHub, letzter Fetch fehlgeschlagen]
    Fetch+Push:   https://github.com/example/api-gateway.git
    Web:          https://github.com/example/api-gateway
    Branch main:  2 voraus / 0 zurück

Lokale Branches:
  main [aktuell]
    Commit:       a1b2c3d · 2026-07-25 · feat: add health endpoint
    Upstream:     origin/main (0 voraus / 0 zurück)

  spike-caching [gemergt]
    Commit:       9f8e7d6 · 2026-07-11 · einfacheren Cache-Key probiert
    Upstream:     (keine)
```

## Commit-Hilfe (`C`)

```
 Commit-Hilfe · api-gateway · prüfen, dann ⏎
 M  README.md                                                  ✔ committen
 U  notes.txt                                                  ✔ committen
 U  server.py                                                  ✔ committen
 U  build/out.o                                      ✎ .gitignore: build/

 ␣ committen an/aus · i gitignore an/aus · ⏎ weiter · Esc abbrechen
```

1. Alle geänderten und neuen Dateien werden gelistet, jeweils mit Vorschlag:
   typischer Müll (`node_modules/`, `.DS_Store`, `__pycache__/`, `*.log`, `.env`,
   …) landet im Vorschlag für die **.gitignore**, alles andere im Vorschlag zum
   **Committen**. Beides ist pro Datei umschaltbar (`␣` committen an/aus,
   `i` gitignore an/aus).
2. Vor der Eingabe der Commit-Message zeigt das Tool die letzten Messages des
   Repos als Stil-Vorlage — so viele, wie über der Eingabezeile Platz haben; die
   Eingabezeile bleibt immer sichtbar.
3. Merge-Konflikte sperren die Hilfe vollständig. Die `.gitignore` wird atomar und
   ohne Folgen von Symlinks ergänzt. Der Commit entsteht über einen temporären Index,
   der ausschließlich die freigegebenen Pfade enthält; ein bestehender Benutzer-Index
   samt bewusst gestagter, aber abgewählter Arbeit bleibt erhalten. Für die
   committeten Pfade übernimmt der echte Index den neuen Commit — genau wie bei
   `git commit -- <pfad>`; sonst meldete `git status` sie weiterhin als geändert.
   Danach kann der
   Commit optional über denselben geschützten privaten Sync-Pfad wie bei `P` gepusht
   werden. Nach einem lokalen Commit nennt die Ergebniszeile
   `git reset --soft HEAD~1` als Rückgängig-Befehl.

## Installation

Vorausgesetzt werden Python 3 und ein Terminal. Sonst nichts.

```sh
git clone https://github.com/DanielMuellerIR/gitmaster_flash.git
python3 gitmaster_flash/gitmaster_flash.py
```

Damit ⏎ tatsächlich in den Repo-Ordner wechselt, den Shell-Wrapper einbinden.
Der Grund: Ein Kindprozess kann das Arbeitsverzeichnis der aufrufenden Shell
nicht ändern — das muss eine kleine Shell-Funktion übernehmen. `install.sh`
erledigt das: Es führt den Selbsttest aus und registriert den sicher gequoteten
absoluten Pfad zu `gmf.zsh` in der `~/.zshrc` (idempotent — ein zweiter Lauf ändert
nichts, auch bei Clone-Pfaden mit Leerzeichen oder Shell-Metazeichen):

```sh
gitmaster_flash/install.sh
```

Eine bereits vorhandene Zeile auf denselben Wrapper wird in jeder Schreibweise
erkannt (`~/…`, `$HOME/…`, mit oder ohne Quotes); nur ein wirklich anderer Pfad
hält die Installation an und verlangt Handarbeit.

Oder die Zeile von Hand eintragen:

```sh
echo 'source /pfad/zu/gitmaster_flash/gmf.zsh' >> ~/.zshrc
```

In einer neuen Shell startet dann `gmf` das Tool (und wechselt am Ende dorthin,
wohin man wollte):

```sh
cd ~/projekte && gmf
```

Ohne Wrapper funktioniert alles genauso, nur gibt ⏎ den Pfad aus, statt
hineinzuwechseln.

## Zwei Rechner vergleichen: `--diff` (nur lesend)

Wer dieselben Repos auf mehreren Rechnern hat (Laptop + Desktop, Mac + Linux), bekommt
Unterschiede, vor denen Git nicht warnt. **Remotes liegen in `.git/config` und werden von
Git nie übertragen** — ein `github`-Remote auf dem einen Rechner fehlt auf dem anderen
schlicht, ein fälliger Push ist dort also unsichtbar. Dasselbe gilt für Branches, die
gerade nicht ausgecheckt sind.

```sh
gitmaster_flash.py --diff meinmac            # ~/git hier gegen ~/git auf meinmac
gitmaster_flash.py --diff meinmac --json     # maschinenlesbar
gitmaster_flash.py --diff meinmac:~/code     # anderes Verzeichnis drüben
gitmaster_flash.py --diff meinmac --fetch    # vorher die ↑/↓-Zahlen auffrischen
```

Ausgegeben werden **nur die Unterschiede**, getrennt in Klassen — diese Trennung
ist der Punkt, ein Report der alles meldet wird ignoriert:

```
DRIFT  favenio: Remote 'github' nur hier (Git uebertraegt Remotes nie)
DRIFT  notizen: origin hier 4 voraus/2 zurueck, auf meinmac 0/0
SYNC   musik: origin auf beiden Rechnern 0 voraus/3 zurueck
lokal  webapp: [main] hier, [feature/x] auf meinmac
lokal  blog: 3 geaenderte/neue Datei(en) hier
nur auf meinmac: experiment
```

`DRIFT` umfasst außerdem Fehler, Konflikte, Stashes, Branch-Verfügbarkeit,
Remote-Sicherheitsklassen und zugangsdatenfreie Ziel-Fingerprints; rohe Remote-URLs
und Zugangsdaten gelangen nie ins JSON. `DRIFT` = sollte gleich sein, ist es nicht
(Handlungsbedarf). `SYNC` = beide
Rechner sind sich einig, stehen aber gemeinsam vor/hinter dem Sync-Remote — im
reinen Zwei-Rechner-Vergleich unsichtbar, und doch meist die eigentlich
interessante Zahl. `lokal` = erklärbar (anderer Branch ausgecheckt, dirty).
Exit **0** = nichts zu melden, **1** = Befunde (Achtung: bei einer `SYNC`-Zeile
sind sich die Rechner untereinander einig — „beide Rechner identisch" allein
garantiert also keinen Exit 0 mehr), **2** = anderer Rechner nicht erreichbar.

**Voraussetzung:** `ssh HOST` muss funktionieren — mehr nicht. gitmaster_flash muss auf
dem anderen Rechner **nicht** installiert sein: das Skript geht per stdin rüber, drüben
braucht es nur `python3` und `git`. Nebeneffekt: beide Seiten laufen immer in exakt
derselben Fassung, Versionsdrift ist ausgeschlossen. Funktioniert auch gegen Linux.

**Es ändert nie deine Repos** — keine Remotes angelegt, nichts committet oder gepusht,
Branch, Index und Arbeitsbaum bleiben unangetastet. Es sagt, was anders ist; das
Reparieren bleibt bei dir. Zweierlei schreibt es trotzdem, beides außerhalb der
Repo-Inhalte: `--fetch` führt auf beiden Rechnern ein echtes `git fetch --all --prune`
aus — genau dafür ist der Schalter da, und er berührt nur die Remote-Tracking-Refs —,
und der allererste Lauf legt `~/.config/gitmaster_flash/config.json` an, auf dem
befragten Rechner ebenso, weil dort dasselbe Skript läuft.

**Tipp:** Rechner in `~/.ssh/config` eintragen und `ControlMaster auto` /
`ControlPath ~/.ssh/cm-%C` / `ControlPersist 60s` setzen. Beim Scannen vieler Repos
gehen viele ssh-Verbindungen gleichzeitig auf, und der sshd-Default
(`MaxStartups 10:30:100`) wirft davon zufällig welche weg — das sieht aus wie ein
kaputtes Repo, ist aber keins.

## Nicht-interaktiv (Skripte, CI, Agenten)

```sh
gitmaster_flash.py --list          # farbige Textliste
gitmaster_flash.py --json          # maschinenlesbar
gitmaster_flash.py --json --fetch  # vorher je Repo fetchen

# Jede Ausgabe nennt die Version — so zeigt ein Diff zweier Rechner-Ausgaben,
# ob dieselbe Fassung dahintersteckt:
#   --list-Kopfzeile: gitmaster_flash 0.6.0 · /Users/du/git · 61 Repos
#   --json (ab 0.6.0): {"version": "0.6.0", "root": "…", "repos": [ … ]}
#                      (vor 0.6.0 gab --json ein nacktes Array aus)
```

Exit-Code 0 heißt: alles sauber und synchron. 1 heißt: mindestens ein Repo
braucht Aufmerksamkeit. Ohne TTY gibt das Tool die Liste aus, statt die
Oberfläche zu starten — in einer Pipe passiert also das Erwartbare.

## Konfiguration

`~/.config/gitmaster_flash/config.json`, wird beim ersten Start angelegt:

- `apps` — Taste → App zum Öffnen des Repos (macOS `open -a`). Die Taste taucht
  automatisch im Footer auf: `{"Z": {"name": "Zed", "path":
  "/Applications/Zed.app"}}` ergibt `Z Zed`. Eine Taste wählen, die oben in der
  Tabelle nicht schon belegt ist. Innerhalb einer SSH-Sitzung verweigert gmf die
  Aktion mit einer klaren Erklärung: Eine App lässt sich nur auf dem Mac öffnen,
  an dem man direkt arbeitet.
- `sync_remote_names` / `sync_remote_hosts` — woran der private Sync-Remote
  erkannt wird: am Remote-Namen oder an einem exakt normalisierten Host in der
  Remote-URL (Teiltreffer werden nie akzeptiert). Für eine
  generische Installation ist der Standard `origin`. Alle Remotes werden
  unabhängig davon angezeigt; GitHub wird an seiner URL erkannt und zuletzt
  einsortiert.
- `skip_dirs` — Ordner, die der Scan gar nicht erst betritt.
- `lang` — `"en"`, `"de"` oder `null` für automatisch nach `$LANG`.
- `compact_from` — wird diese Repo-Anzahl überschritten, startet gmf in der
  Kompaktansicht (Standard 20: bis einschließlich 20 bleibt die Detailansicht;
  `M` schaltet jederzeit um).
- `git_timeout` / `fetch_timeout` — Sekunden pro git-Aufruf.
- `commit_timeout` — Sekunden allein für `git commit` (Standard 120). Dabei läuft
  der pre-commit-Hook des Repos, der oft Linter oder Tests startet und deutlich
  länger braucht als `git_timeout`. Wird die Grenze erreicht, werden git *und*
  alles vom Hook Gestartete beendet, und gmf meldet das, statt abzustürzen.

## Sicheres Push und Pull

`P` und `L` sind absichtlich auf einen nichtöffentlichen Sync-Remote begrenzt.
Beide fetchen zuerst, verlangen einen sauberen Arbeitsbaum und blockieren
divergente History. Fetch- und Push-URL müssen genau dasselbe zugangsdatenfreie
Host-/Repo-Ziel bezeichnen; mehrere oder abweichende Push-URLs werden gesperrt.
Unmittelbar vor der bestätigten Mutation werden Branch, HEAD, Index, Arbeitsbaum,
Remote-Identität und Ziel-OID nochmals geprüft. Pull übernimmt nur die freigegebene
unveränderliche OID per Fast-forward. Push überträgt die freigegebene Commit-OID mit
einem expliziten Refspec. Eine exakte Ziel-OID-Lease verhindert, dass eine
Remote-Löschung oder parallele Verschiebung daraus eine ungeprüfte Aktualisierung
macht; Tags werden nie gesendet.

Für GitHub gibt es den getrennten `G`-Pfad. Er funktioniert nur, wenn derselbe
Branch auf genau einem GitHub-Remote bereits existiert und die Historien verbunden
sind. Vor der Veröffentlichung zeigt er alle ausgehenden Commits und geänderten
Dateinamen. Danach muss exakt `PUSH <Remote>` eingegeben werden. Auch der letzte
Befehl überträgt nur den aktuellen Branch: freigegebene Quell-OID, exakte
Ziel-Lease, keine Tags, kein neuer Branch. Ein Remote mit mehreren oder
abweichenden Fetch-/Push-Zielen wird vollständig gesperrt, selbst wenn beide
Ziele auf GitHub liegen. Komplexe Fälle bleiben bewusst dem Terminal vorbehalten.

Git fragt hier nie nach Zugangsdaten. Jeder Git-Aufruf läuft ohne Terminal-Prompt,
ohne Askpass und in eigener Session, denn Git schreibt so eine Frage
(`Username for 'https://github.com':`) direkt auf das Terminal statt in die
abgefangene Ausgabe — im curses-Bild zerstört das die Anzeige und Git wartet dann
auf eine Eingabe, die nie kommt. Ein Remote, das einen Login braucht, scheitert
deshalb sofort mit `<Remote> braucht einen Login (kein Credential-Helper/SSH-Key).`
statt zu fragen. HTTPS-Zugangsdaten gehören in einen
Credential-Helper (macOS: `git config --global credential.helper osxkeychain`),
oder man nutzt SSH mit einem Key im Agenten; beides läuft ohne Rückfrage.

## Tests

```sh
python3 -m unittest discover -s tests
```

Die Logik (Status-Parsing, Heuristiken, Repo-Scan) ist von der curses-Oberfläche
getrennt und wird headless gegen echte, temporär angelegte Repos getestet.

```sh
python3 docs/make-screens.py --check   # passen die Bilder noch zur Oberfläche?
```

`--check` schlägt fehl, wenn die Bilder in `docs/` anders herauskämen — nach einer
UI-Änderung also `docs/make-screens.py` laufen lassen und das Ergebnis mitcommitten.

## Name

Eine Anspielung auf Grandmaster Flash — es geht ja vor allem ums schnelle
Umschalten zwischen vielen Platten.

## Lizenz

**WTFPL** — siehe [LICENSE](LICENSE).
