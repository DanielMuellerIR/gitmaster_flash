# Architektur und Entwicklung

[English](architecture.md) · [Deutsch](architecture.de.md) · [Übersicht](../README.de.md)

## Programm und Shell-Funktion

Kommandozeile und curses-Oberfläche verwenden dasselbe Python-Programm,
[gitmaster_flash.py](../gitmaster_flash.py). Es liest Git-Status, Refs und
Konfiguration in Datensätze für die einzelnen Repositories ein.
Listenausgabe, Snapshots und Oberfläche verwenden diese Datensätze.
Der SSH-Vergleich überträgt dieselbe Datei über die Standardeingabe
zum anderen Rechner. So verwenden beide Seiten dieselbe Version und
der andere Rechner benötigt keine Installation.

Git-Aufrufe laufen ohne Eingabeaufforderungen, mit erfasster Ausgabe,
Laufzeitgrenzen und eigenen Prozessgruppen. Beim Abbruch eines Fetchs
im Hintergrund enden die Prozesse dieses Scans. Unabhängige Aktionen
können weiterlaufen. Eine begonnene lokale Tracking-Ref-Änderung
schließt die Konfigurationsprüfung und gegebenenfalls die Rücknahme mit
zeitlich begrenzten lokalen Git-Aufrufen ab. Die Oberfläche wartet höchstens
fünf Sekunden auf den Scan-Thread. Befehle, die den Git-Zustand ändern,
erscheinen im
Sitzungsprotokoll. Die maschinenlesbaren Pfadprotokolle von Git erhalten
die ursprünglichen Bytes einschließlich Zeilenumbrüchen. Die
Terminalansicht maskiert Steuerzeichen. `path_key` im
[JSON-Format](json-format.de.md) erhält die Identität unterstützter Pfade.

Die Einstiegsskripte zur Installation rufen [tools/install.py](../tools/install.py)
auf. Für den Verzeichniswechsel mit Enter läuft die Shell-Funktion in der
aufrufenden Shell. Sie reicht normale CLI-Argumente und den Exit-Code durch
und liest den ausgewählten Pfad aus einer temporären Datei.
Die [Installationsanleitung](installation.de.md) beschreibt Dateipfade und
unterstützte Shells.

## Tests und Demo-Bilder

```sh
python3 -m unittest discover -s tests
python3 docs/make-screens.py --check
```

Die Tests prüfen reine Programmlogik, echte temporäre Git-Repositories,
die Kommandozeile und die Shell-Integration. Der GitHub-Actions-Workflow
führt die Tests und den Bildvergleich unter macOS und Linux aus.
Die [Laufzeitprüfungen](runtime-support.de.md) dokumentieren datierte
Ergebnisse und Befehle zur Wiederholung der Kompatibilitätstests.

Der Bildgenerator startet das echte Programm in einem eigenen
Pseudoterminal. Feste Commit-Metadaten und isolierte Git-Einstellungen
machen das Demo-Inventar reproduzierbar. Der Generator wartet nach
bestätigten Aktionen auf den erwarteten Eintrag im Befehlsprotokoll
und erzeugt aus der Terminalausgabe ein Bild der Liste.
Überlagerte Ansichten wie die Commit-Hilfe stehen als geprüfte
Textbeispiele im [Leitfaden zur Oberfläche](interface.de.md).

Nach einer Änderung an der Oberfläche führt `./build.sh` die Tests aus
und erzeugt die Bilder neu. `./build.sh --check` führt die Tests aus
und vergleicht die erzeugten Bilder mit dem eingecheckten Stand.
Build- und Release-Skripte verwenden Zsh.

## Statische Prüfungen

`requirements-dev.txt` legt die Versionen der Entwicklungswerkzeuge fest.
Sie laufen mit Python 3.11 in einer virtuellen Umgebung. Mypy 0.991 prüft
Anwendung, Python-Werkzeuge und Bildgenerator mit Zielversion Python 3.9.
Ruff 0.14.14 prüft Imports, häufige Fehler und Formatierung im Python-Code.

```sh
python3.11 -m venv /tmp/gmf-checks
. /tmp/gmf-checks/bin/activate
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
mypy
shellcheck install.sh install-linux.sh
for file in build.sh release.sh gmf.zsh tools/check-zshrc.zsh; do zsh -n "$file"; done
```

Die kürzere Funktionsprüfung startet mit
`python3 tools/check-compatibility.py --suite core`. Installer und Release
prüfen weiterhin die ganze Suite einschließlich Shell- und Git-Schutzregeln.

## Scan-Messung

`python3 tools/benchmark-scan.py --baseline /pfad/zur/alten/gitmaster_flash.py`
führt beide Fassungen abwechselnd an denselben temporären Repositories aus und
prüft ihre Ergebnisse auf Unterschiede abseits der Messzeit. Die
[aufgezeichneten Messungen](scan-benchmark.json) nutzen drei lokale Bare-Remotes
und einen oder 25 Branches auf demselben Commit. Von 0.34.1 auf 0.34.2 sank die
Zahl der Git-Aufrufe in beiden Fällen lokal von 37 auf 35 und mit Fetch von
140 auf 88. Git liefert weiterhin die wirksamen URLs. Die Prüfzeitpunkte für
HEAD, Refs und Konfiguration bleiben erhalten. Die Zeiten hängen von der
Systemlast ab.

## Quellrelease

```sh
./release.sh
```

Das Release-Skript setzt voraus, dass alle Änderungen an versionierten
Dateien committet sind. Es prüft Tests und Bilder mit `./build.sh --check`,
packt den dabei geprüften Commit und entpackt das Archiv zur Kontrolle
der Programmversion. Es schreibt `dist/gitmaster_flash-<version>.tar.gz`
und die zugehörige `.sha256`-Datei und überschreibt keine vorhandenen
Ergebnisse. Das Archiv enthält die Tests und Dokumentation für den
Installer und die Build-Prüfungen aus den entpackten Dateien.
