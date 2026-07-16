**🌐 Sprache / Language:** [English](README.md) · [Deutsch](README.de.md)

# gitmaster_flash

Eine schnelle Terminal-Übersicht (TUI) über alle Git-Repos unterhalb des
aktuellen Ordners: Man sieht auf einen Blick, wo noch etwas liegen geblieben ist,
und räumt es direkt auf.

Grün heißt sauber und mit dem Remote synchron, Rot und Gelb heißen: da ist noch
was. Eine einzige Python-Datei, nur Standardbibliothek — kein `pip install`, kein
Hintergrunddienst, keine Repo-Registrierung. Gescannt wird schlicht alles
unterhalb des Ordners, in dem man es startet.

![Übersicht mehrerer Repos, problematische zuerst](docs/overview.png)

Zum gefahrlosen Ausprobieren, ohne die eigenen Repos anzufassen:

```sh
python3 gitmaster_flash.py --demo
```

`--demo` baut eine Wegwerf-Sandbox aus Fake-Repos in allen denkbaren Zuständen
und startet die Oberfläche darauf. Sie liegt im Temp-Ordner und kann danach
einfach gelöscht werden.

## Was eine Zeile verrät

- **↑n / ↓n** — Commits voraus/zurück gegenüber dem Sync-Remote, auf Basis des
  letzten Fetch-Stands. `R` holt mit `git fetch` frische Zahlen.
- **↑n github** (cyan) — Zusatzhinweis: Commits, die zwar auf dem Sync-Remote
  liegen, aber nie zum *konfigurierten Upstream* des Branches (typischerweise
  GitHub) gepusht wurden. Rein informativ, erscheint deshalb auch neben einem
  grünen ✔ — so bleibt „nie nach oben gepusht" nicht unbemerkt liegen.
- **M / D / U** — Anzahl geänderter, gelöschter und unversionierter Dateien.
- **⚑Stash:n** — vorhandene Stashes. Die übersieht man sonst gern.
- **⚠conflict:n** — ungemergte Dateien, etwa nach einem `git stash pop`, der
  nicht sauber aufging. Bewusst getrennt von „modified", weil dahinter andere
  Arbeit steckt.
- Warnungen wie „kein Sync-Remote" oder „Branch nicht auf dem Remote".

Repos mit offenen Punkten stehen oben, saubere unten.

## Bedienung

Alle Kürzel stehen dauerhaft im Footer — merken muss man sich nichts.
Groß-/Kleinschreibung ist egal, `f` wirkt wie `F`.

| Taste | Aktion |
|---|---|
| ↑ / ↓ | Repo auswählen |
| → / ← | auf-/zuklappen (Dateien mit M/D/U/C, Stashes) |
| ⏎ | beenden und in den Repo-Ordner wechseln (braucht den `gmf`-Wrapper, siehe unten) |
| E | Repo in einer konfigurierten App öffnen (eigene in `config.json` eintragen) |
| C | Commit-Hilfe (siehe unten) |
| U | neuesten Stash anwenden (`git stash pop`, mit Rückfrage) |
| S | neuesten Stash als Diff ansehen (read-only, scrollbar) |
| D | neuesten Stash endgültig verwerfen (`git stash drop`, mit Rückfrage) |
| R | alles neu einlesen inklusive `git fetch` |
| Q | beenden |

Auf einen bereits konfliktbehafteten Baum wird nie ein weiterer Stash gepoppt —
erst die Konflikte auflösen.

## Commit-Hilfe (`C`)

![Commit-Hilfe mit Vorschlag je Datei](docs/commit-helper.png)

1. Alle geänderten und neuen Dateien werden gelistet, jeweils mit Vorschlag:
   typischer Müll (`node_modules/`, `.DS_Store`, `__pycache__/`, `*.log`, `.env`,
   …) landet im Vorschlag für die **.gitignore**, alles andere im Vorschlag zum
   **Committen**. Beides ist pro Datei umschaltbar (`␣` committen an/aus,
   `i` gitignore an/aus).
2. Vor der Eingabe der Commit-Message zeigt das Tool die letzten fünf Messages
   des Repos als Stil-Vorlage.
3. Die `.gitignore` wird ohne Duplikate ergänzt, die Auswahl gestaged und
   committet. Danach auf Wunsch der Push zum Sync-Remote (nur, wenn der Branch
   nicht hinterherhinkt).

## Installation

Vorausgesetzt werden Python 3 und ein Terminal. Sonst nichts.

```sh
git clone https://github.com/DanielMuellerIR/gitmaster_flash.git
python3 gitmaster_flash/gitmaster_flash.py
```

Damit ⏎ tatsächlich in den Repo-Ordner wechselt, den Shell-Wrapper einbinden.
Der Grund: Ein Kindprozess kann das Arbeitsverzeichnis der aufrufenden Shell
nicht ändern — das muss eine kleine Shell-Funktion übernehmen. `install.sh`
erledigt das: Es führt den Selbsttest aus und registriert `gmf.zsh` in der
`~/.zshrc` (idempotent — ein zweiter Lauf ändert nichts):

```sh
gitmaster_flash/install.sh
```

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

## Nicht-interaktiv (Skripte, CI, Agenten)

```sh
gitmaster_flash.py --list          # farbige Textliste
gitmaster_flash.py --json          # maschinenlesbar
gitmaster_flash.py --json --fetch  # vorher je Repo fetchen
```

Exit-Code 0 heißt: alles sauber und synchron. 1 heißt: mindestens ein Repo
braucht Aufmerksamkeit. Ohne TTY gibt das Tool die Liste aus, statt die
Oberfläche zu starten — in einer Pipe passiert also das Erwartbare.

## Konfiguration

`~/.config/gitmaster_flash/config.json`, wird beim ersten Start angelegt:

- `apps` — Taste → App zum Öffnen des Repos (macOS `open -a`). Die Taste taucht
  automatisch im Footer auf: `{"Z": {"name": "Zed", "path":
  "/Applications/Zed.app"}}` ergibt `Z Zed`. Eine Taste wählen, die oben in der
  Tabelle nicht schon belegt ist.
- `sync_remote_names` / `sync_remote_hosts` — woran der Sync-Remote erkannt wird:
  am Remote-Namen oder am Host in der Remote-URL. Default ist `origin`.
- `skip_dirs` — Ordner, die der Scan gar nicht erst betritt.
- `lang` — `"en"`, `"de"` oder `null` für automatisch nach `$LANG`.
- `git_timeout` / `fetch_timeout` — Sekunden pro git-Aufruf.

## Tests

```sh
python3 -m unittest discover -s tests
```

Die Logik (Status-Parsing, Heuristiken, Repo-Scan) ist von der curses-Oberfläche
getrennt und wird headless gegen echte, temporär angelegte Repos getestet.

## Name

Eine Anspielung auf Grandmaster Flash — es geht ja vor allem ums schnelle
Umschalten zwischen vielen Platten.

## Lizenz

**WTFPL** — siehe [LICENSE](LICENSE).
