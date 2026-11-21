# Kommandozeilenreferenz

[English](cli.md) · [Deutsch](cli.de.md) · [Übersicht](../README.de.md)

`--help` zeigt die Schalterübersicht und `--demo` startet mit temporären
Beispiel-Repositories. `--view` schränkt das Inventar zusätzlich zum Pfadfilter
auf eine Arbeitsansicht ein. Namen und Auswahlregeln stehen im
[Leitfaden zur Oberfläche](interface.de.md).
`--fetch-remote NAME` schließt `--fetch` ein und aktualisiert das benannte
Remote pro gescanntem Repository. Mit einem Repository als Scan-Wurzel
wird nur dieses Repository aktualisiert.

## Repository-Scans

```sh
python3 gitmaster_flash.py --list          # farbige Textliste
python3 gitmaster_flash.py --json          # maschinenlesbar
python3 gitmaster_flash.py --json --fetch  # vorher je Repo fetchen
python3 gitmaster_flash.py --filter api    # nur Repos, deren Pfad passt
python3 gitmaster_flash.py --lang de       # Ausgabesprache festlegen
python3 gitmaster_flash.py --version       # nur die Versionsnummer, sonst nichts

# Listenkopf und JSON-Snapshot enthalten die Programmversion.
```

`--filter` nimmt dieselben Begriffe wie `/` in der Oberfläche und wirkt auf
`--list`, `--json` und `--diff`; interaktiv gestartet belegt er den Filter vor,
den `/` dann ändert. Bei `--diff` grenzt er **beide** Rechner mit denselben
Begriffen ein — nur eine Seite zu filtern ergäbe „nur hier"-Unterschiede, die es
gar nicht gibt. Jede Ausgabe nennt den aktiven Filter: die `--list`-Kopfzeile,
ein strukturiertes `filter`-Objekt im JSON (Suchtext, Treffer- und Gesamtzahl)
und beim Textbericht von `--diff` eine erste Zeile mit den Zahlen beider Rechner.
Ein Filter ohne einen einzigen Treffer sagt das auf stderr — auf
stdout sähen leere Liste oder „keine Unterschiede" mit Exit-Code 0 sonst genau
wie „alles in Ordnung" aus. Die `--list`-Kopfzeile zählt außerdem die
ausgeblendeten Repos mit, die Aufmerksamkeit bräuchten (`(+7 ausgeblendet)`),
genau wie die Kopfzeile der Oberfläche: Eine einzelne `✔`-Zeile mit Exit-Code 0
darf nicht wie ein sauberer Bestand aussehen, während der Filter still fünfzig
dreckige Repos versteckt.

Exit-Code 0 heißt: alles sauber und synchron. 1 heißt: mindestens ein Repo
braucht Aufmerksamkeit; gezählt wird über die Repos, die den Filter passiert
haben. Ohne TTY gibt das Tool die Liste aus, statt die
Oberfläche zu starten — in einer Pipe passiert also das Erwartbare.

Einen weiteren Schalter gibt es, er ist aber nicht zum Tippen gedacht:
`--cd-file` nennt die Datei, aus der der `gmf`-Shell-Wrapper den ausgewählten
Repo-Pfad liest. Der Wrapper übergibt sie; von Hand aufgerufen bringt sie nichts.

## Zwei Rechner per SSH vergleichen

Wer dieselben Repos auf mehreren Rechnern hat (Laptop + Desktop, Mac + Linux), bekommt
Unterschiede, vor denen Git nicht warnt. **Remotes liegen in `.git/config` und werden von
Git nie übertragen** — ein `github`-Remote auf dem einen Rechner fehlt auf dem anderen
schlicht, ein fälliger Push ist dort also unsichtbar. Dasselbe gilt für Branches, die
gerade nicht ausgecheckt sind.

```sh
gmf --diff meinmac            # ~/git hier gegen ~/git auf meinmac
gmf --diff meinmac --json     # maschinenlesbar
gmf --diff meinmac:~/code     # anderes Verzeichnis drüben
gmf --diff meinmac --fetch    # vorher die ↑/↓-Zahlen auffrischen
```

Ausgegeben werden **nur die Unterschiede**, getrennt in Klassen — diese Trennung
ist der Punkt, ein Report der alles meldet wird ignoriert:

```
DRIFT  website: Remote 'github' nur hier (Git uebertraegt Remotes nie)
DRIFT  notizen: origin hier 4 voraus/2 zurueck, auf meinmac 0/0
SYNC   musik: origin auf beiden Rechnern 0 voraus/3 zurueck
lokal  webapp: [main] hier, [feature/x] auf meinmac
lokal  blog: 3 geaenderte/neue Datei(en) hier
nur auf meinmac: experiment
```

`DRIFT` umfasst außerdem Fehler, Konflikte, Stashes, Branch-Verfügbarkeit,
Remote-Sicherheitsklassen (einschließlich der Frage, ob Fetch-/Push-URLs ohne
offengelegte Zugangsdaten ausführbar sind) und zugangsdatenfreie Ziel-Fingerprints.
Die eingetragene URL eines Remotes wird nie als solche serialisiert, nur ihr
Fingerprint. Zugangsdaten gelangen überhaupt nicht ins JSON: Eine zitierte
Git-Fehlerzeile behält als Beleg die Zieladresse (Host und Repo-Pfad), ihr
Benutzerteil sowie Query und Fragment werden vorher entfernt.
`DRIFT` = sollte gleich sein, ist es nicht
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

Beim gleichen ausgecheckten Branch vergleicht gmf auch die vollständigen
Commit-IDs. Gleiche Voraus-/Zurück-Zahlen können verschiedene Commits beschreiben;
das wird als `DRIFT` statt als gemeinsamer `SYNC`-Stand gemeldet. Älteres JSON ohne
Commit-IDs bleibt lesbar, kann Commit-Gleichheit aber nicht belegen. Laufende
Git-Vorgänge erscheinen als lokale Arbeit, auch bei sauberem Arbeitsbaum. JSON
enthält `head_oid` (null vor dem ersten Commit, leer bei unbekanntem Stand) und
den stabilen Code `operation`.

Der Vergleich prüft außerdem jeden lokalen Branch-Namen und die vollständige
Commit-ID, auch bei auf keinem Rechner ausgecheckten Branches. Ein gemeinsamer
Nebenbranch mit Commits außerhalb bekannter Remote-Refs erzeugt einen `SYNC`-Befund.
Unterschiedliche gespeicherte Abdeckung gilt als lokal; sie belegt keine verschiedenen
Serverinhalte. JSON ergänzt `branches`, `branches_error` und
`branch_coverage_basis: "local-remote-tracking-refs"`. Pro Branch stehen Name, volle
`oid`, `current`, voller Upstream-Ref, Upstream-Remote, Voraus-/Zurück-Zahlen,
Merge-Zustand und `coverage` (`covered`, `uncovered`, `unknown`) mit
`uncovered_commits` (null bei unbekanntem Stand). Ein nicht gemessenes Inventar
wird ausgelassen; ältere Payloads ohne Inventar können Branch-Gleichheit nicht
belegen und erzeugen keine erfundenen fehlenden Branches. Vergleich und Inventar
verändern keinen Ref.

**Branch, Index und Arbeitsbaum bleiben unangetastet** — keine Remotes angelegt,
nichts committet oder gepusht. Es sagt, was anders ist; das Reparieren bleibt bei
dir. Zweierlei schreibt es trotzdem: `--fetch` führt auf beiden Rechnern einen
echten Fetch aus; Tag-Holen, Tag-Pruning und Submodul-Rekursion sind abgeschaltet.
Je Remote holt gmf nur die angekündigte Objekt-ID des aktuellen Branches ohne
lokalen Ziel-Ref und aktualisiert danach ausschließlich
`refs/remotes/<remote>/<Branch>` mit `--no-deref`. Die geprüfte URL läuft über
einen Einmal-Alias; unsichere Remotes werden gemeldet statt ausgeführt. Der
allererste Lauf legt außerdem `~/.config/gitmaster_flash/config.json` an, auf dem
befragten Rechner ebenso, weil dort dasselbe Skript läuft.

**Tipp:** Rechner in `~/.ssh/config` eintragen und `ControlMaster auto` /
`ControlPath ~/.ssh/cm-%C` / `ControlPersist 60s` setzen. Ein `--fetch`-Scan kann
repoübergreifend bis zu acht ssh-Verbindungen gleichzeitig öffnen (jeder Git-Fetch
selbst nutzt genau einen Job), und der sshd-Default (`MaxStartups 10:30:100`) kann
sonst zufällig welche davon verwerfen — das sieht aus wie ein kaputtes Repo, ist
aber keins.

## Gespeicherte Vergleiche und Farbausgabe

```sh
gmf --json > vorher.json
# ... später ...
gmf --json > nachher.json
gmf --compare vorher.json nachher.json
gmf --compare vorher.json nachher.json --json --view changes
gmf --list --color never
```

`--compare` gibt standardmäßig einen Textbericht aus, mit `--json` einen
JSON-Bericht. Es liest ausschließlich die beiden Dateien, ohne Scan, SSH,
Fetch oder Config-Erzeugung. Gespeicherte Pfadsuchen müssen übereinstimmen. Vollständige
Snapshots speichern und Arbeitsansichten erst beim Vergleich anwenden, damit
kein ausgefiltertes Gegenstück als fehlendes Repo erscheint. Erstellungszeit,
lokale Messzeiten und unbekannte Fähigkeiten bleiben sichtbar; Zeitunterschiede
allein sind keine Drift. Nicht vergleichbare Dateien führen zu Exit 2.

JSON enthält `schema_version: 1`, unabhängig von der Programmversion.
Der JSON-Vergleich enthält lesbare `differences` und stabile
`findings` mit Typen, Kategorien, Seiten und Werten. Ältere Objekt-Snapshots
bleiben mit ausdrücklichen Grenzen lesbar. Der vollständige Vertrag steht in
[JSON-Format und Befundtypen](json-format.de.md).

`--color auto` ist der Standard für `--list`: Farben nur am Terminal und ohne
`NO_COLOR` in der Umgebung. `always` erzwingt sie auch bei Umleitung und hat
Vorrang vor `NO_COLOR`. `never` unterdrückt sie. JSON und Vergleichsberichte
verwenden eigene Formate ohne Farben. Die curses-Oberfläche verwendet weiterhin die
Terminal-Palette.

## Diff-Ebenen und Vorschau per Kommandozeile

In `A` wählt `1` alle Änderungen, `2` vorgemerkte Änderungen (HEAD → Index) und
`3` übrige Änderungen (Index → Arbeitsbaum einschließlich neuer Dateien). Die
Kopfzeile nennt die Ebene, die Dateiliste folgt ihr. Leere Ebenen bleiben
umschaltbar. Eine vorgemerkte und danach bearbeitete Datei kann je Ebene andere
Änderungen zeigen. `C` committet weiterhin gewählte aktuelle Arbeitsbaum-Pfade
und erhält anderes Staging; die Diff-Ebene bestimmt die Vorschau. Vor dem ersten Commit vergleicht vorgemerkt gegen den leeren
Ausgangsbaum. Submodule zeigen Gitlink-Verweise und eine Marke für schmutzigen
Inhalt; `O` erklärt die Inhalte. Die Vorschau führt keine externen Diff- oder
Textkonvertierungsprogramme aus. Index-Einträge und Dateiinhalt bleiben erhalten;
Git darf seinen Index-Stat-Cache erneuern.

Derselbe Leser ist ohne Scan oder Config-Erzeugung verfügbar:

```sh
gmf /pfad/zum/repo --file-diff src/main.py --diff-mode staged
gmf /pfad/zum/repo --file-diff src/main.py --diff-mode unstaged --json
```

Root muss die Arbeitsbaum-Wurzel sein, der Dateipfad relativ dazu. `--diff-mode`
akzeptiert `combined` (Standard), `staged` oder `unstaged` und benötigt
`--file-diff`. Fetches, Vergleiche und Scan-Filter lassen sich damit nicht
verbinden. Text maskiert Terminal-Steuerzeichen und erhält Zeilenumbrüche.
JSON ist ein eigener `kind: "file_diff"`-Datensatz mit `schema_version`, `version`,
`root`, `path`, `path_key`, `mode`, `ok`, `diff` und `error`, kein gespeicherter Repo-Snapshot.
`diff` enthält maskierten Vorschautext, der sich nicht für `git apply` eignet.
Git-Verwaltungspfade wie `.git/config` sind ausgeschlossen. Exit-Codes: 0 ohne
Unterschied, 1 mit Unterschied, 2 bei Fehler.

## Stash-Bericht

Einen rein lesenden Bericht mit derselben Dateigruppierung liefert
`gmf REPO --stashes` (oder `python3 gitmaster_flash.py REPO --stashes --json`).
Er startet weder Scan noch Netzzugriff und wendet nichts an oder löscht etwas.
Exit-Codes: 0 für einen Bericht (auch bei leerer Stash-Liste), 2 bei Lesefehlern
oder ungültigen Optionen. JSON hat den Ergebnistyp `stashes`, mit Stash-OIDs,
Selektoren und Betreff sowie `files` mit `path`, byteerhaltendem `path_key`,
Arbeitsbaum-/Index-Statuscodes, `untracked` und beschrifteten `diff`-Zeilen.
Die bisherigen Stash-Zähler und Exit-Codes von `--list`/Snapshot-`--json` bleiben gleich.
