# gitmaster_flash

TUI-Übersicht über alle Git-Repos unterhalb des aktuellen Ordners, zum schnellen
Aufräumen. Typ: Skript/CLI, Plattform: macOS/Terminal, Python 3 (nur
Standardbibliothek, curses). Name: Anspielung auf Grandmaster Flash.

## Einstiegsskripte

- `build.sh` — Selbsttest, dann die README-Bilder in `docs/` neu erzeugen
  (`--check`: nur prüfen). Es gibt keinen Übersetzungsschritt; die generierten
  Bilder sind das, was dieses Projekt „baut".
- `install.sh` — Selbsttest, dann `gmf.zsh` in `~/.zshrc` registrieren.
- `release.sh` — Quellarchiv des committeten Standes nach
  `dist/gitmaster_flash-<version>.tar.gz` + `.sha256` (kein Bundle, kein DMG,
  keine Notarisierung). Verweigert bei nicht committeten Änderungen, ruft
  `./build.sh --check`, prüft das Archiv per Entpacken und `--version`,
  überschreibt nie. Nichts davon pusht nach GitHub.

- `install-linux.sh` — Linux-Installer für Bash, Fish und Zsh; prüft Voraussetzungen
  und Selbsttests. Benutzerdateien unter XDG-Config; bei Bash/Zsh markierter Block
  und Backup der Startdatei, bei Fish Autoload-Funktion. Keine Paketinstallation.
  `--check` prüft Laufzeit und Selbsttests ohne Registrierung.

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

- Git über `run_git()` strikt nicht interaktiv ausführen. Zustandsänderungen über `run_git_logged()` führen. Fremdes Staging erhalten und nur freigegebene Pfade committen.
- Die Änderungsansicht bleibt für Repo-Inhalte rein lesend. Fehler beim Remote-Zugriff sind kein Nachweis für einen anderen Stand.
- Produktversion steht in `gitmaster_flash.py` als `__version__`.

Vor Änderungen den passenden Vertrag lesen.

- [Demo, Bildgenerator, UI-Konventionen und Git-Aufrufe](AGENT-REFERENCE.md#demo-bildgenerator-ui-konventionen-und-git-aufrufe)
- [Commit, Remote-Nachweise und Übertragungen](AGENT-REFERENCE.md#commit-remote-nachweise-und-übertragungen)
- [Repo-Inventar und interaktive Ansichten](AGENT-REFERENCE.md#repo-inventar-und-interaktive-ansichten)
- [JSON-Vertrag, Filter und Installer](AGENT-REFERENCE.md#json-vertrag-filter-und-installer)
- [Commit-Hilfe, Hintergrund-Scan und Einstellungen](AGENT-REFERENCE.md#commit-hilfe-hintergrund-scan-und-einstellungen)

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
  siehe [Bildgenerator](AGENT-REFERENCE.md#demo-bildgenerator-ui-konventionen-und-git-aufrufe). Solche Ansichten gehören als
  vorformatierter Textblock ins README, nicht als Bild.

## Offene Punkte / Ideen

Offene Arbeit und Ideen stehen im [Backlog](AGENT-BACKLOG.md).
