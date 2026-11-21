# Konfiguration

[English](configuration.md) · [Deutsch](configuration.de.md) · [Übersicht](../README.de.md)

Die Einstellungen stehen über `,` in der Oberfläche oder in der JSON-Konfigurationsdatei bereit.

## Einstellungen in der Oberfläche (`,`)

`,` öffnet eine Liste der Einstellungen, `↑`/`↓` wählt eine aus, `⏎` ändert sie.
In kleinen Terminals scrollt die Liste bis zu den schreibgeschützten Einträgen am Ende.
Die Zeile über dem Footer erklärt jeweils die Zeile, auf der man steht. Ein
geänderter Wert gilt sofort und wird in die `config.json` zurückgeschrieben —
atomar über eine Nachbardatei, damit ein abgebrochener Schreibvorgang nicht eine
halbe Datei und keine einzige eigene Einstellung hinterlässt.

Hier änderbar: Sprache, ab wann die Kompaktansicht startet, die vier Timeouts
und die Ordner, die der Scan auslässt. Jede Eingabe wird vor dem Speichern
geprüft — ein Timeout `abc` erreicht die Datei nie, und ein übersprungener Ordner
muss ein *Name* sein, weil der Scan ihn mit einzelnen Pfadsegmenten vergleicht
und ein Eintrag wie `a/b` deshalb nie zutreffen könnte.

Zwei Gruppen bleiben bewusst der Datei vorbehalten; die Ansicht zeigt sie
schreibgeschützt mit genau diesem Hinweis:

- `sync_remote_names` / `sync_remote_hosts` entscheiden, welches Remote als
  privater Sync-Remote gilt — und damit, wohin `P` pusht. Eine Zieländerung
  gehört nicht hinter einen Tastendruck in einer Liste, durch die man gerade
  scrollt.
- `apps` enthält Programmpfade, die gmf mit `open -a` startet. Ein Pfad, den man
  in eine einzeilige Eingabe tippt, ist kein Pfad, den jemand geprüft hat.

Sie schreibgeschützt zu zeigen ist besser als sie zu verstecken: Eine
Einstellung, die man nicht findet, ist schlimmer als eine, die man in der Datei
ändern muss.

## Konfigurationsdatei

`~/.config/gitmaster_flash/config.json`, wird beim ersten Start angelegt:

Werte aus dieser Datei verwenden dieselben Bereiche und Auswahlmöglichkeiten wie
die Einstellungsansicht. Ungültige Einträge fallen einzeln mit einer Warnung auf
Defaults zurück; die Datei bleibt zur Korrektur erhalten. Ordner- und Remote-Listen
dürfen ausschließlich Zeichenketten enthalten.

- `apps` — Taste → App zum Öffnen des Repos (macOS `open -a`, Linux ausführbarer Starter). Die Taste taucht
  automatisch im Footer auf: `{"Z": {"name": "Zed", "path":
  "/Applications/Zed.app"}}` ergibt `Z Zed`. Eine Taste wählen, die oben in der
  Tabelle nicht schon belegt ist. Innerhalb einer SSH-Sitzung verweigert gmf die
  Aktion mit einer klaren Erklärung: Eine App lässt sich nur in der lokalen Desktopsitzung öffnen.
- `sync_remote_names` / `sync_remote_hosts` — woran der private Sync-Remote
  erkannt wird: am Remote-Namen oder an einem exakt normalisierten Host in der
  Remote-URL (Teiltreffer werden nie akzeptiert). Für eine
  generische Installation ist der Standard `origin`. Alle Remotes werden
  unabhängig davon angezeigt; GitHub wird an seiner URL erkannt und zuletzt
  einsortiert.
- `skip_dirs` — Ordner, die der Scan gar nicht erst betritt.
- `publication_excludes` — ausdrücklich vom GitHub-Inhaltsvergleich ausgenommene
  wörtliche Pfade je `owner/repo`, zum Beispiel
  `{"example/project": ["internal/reviews", "docs/private-notes.md"]}`.
  Verzeichnispfade schließen ihren Inhalt ein; Platzhalter haben keine besondere
  Bedeutung. Standardmäßig ist nichts ausgenommen. Diese Regeln betreffen nur
  den Vergleich; sie entfernen keine Dateien aus einem Push und lockern keine
  Freigabeprüfung. JSON enthält je Remote `content_equal` (`true`, `false` oder
  `null` bei unbekanntem Ergebnis), `content_excludes`, `content_error` und
  `history_differs` zusätzlich zu den unveränderten Commit-Zahlen.
- `lang` — `"en"`, `"de"` oder `null` für automatisch nach `$LANG`.
- `compact_from` — wird diese Repo-Anzahl überschritten, startet gmf in der
  Kompaktansicht (Standard 20: bis einschließlich 20 bleibt die Detailansicht;
  `M` schaltet jederzeit um).
- `git_timeout` — Sekunden pro lokalem git-Aufruf (Standard 10).
- `fetch_timeout` — Sekunden pro Fetch-Aufruf (Standard 30).
- `diff_timeout` — harte Laufzeitgrenze für den gesamten SSH-Vergleich
  (Standard 3600 Sekunden). Einzelne Git-Aufrufe behalten `git_timeout` oder
  `fetch_timeout`; für den SSH-Verbindungsaufbau gilt zusätzlich eine eigene
  Grenze von zehn Sekunden.
- `commit_timeout` — Sekunden allein für `git commit` (Standard 120). Dabei läuft
  der pre-commit-Hook des Repos, der oft Linter oder Tests startet und deutlich
  länger braucht als `git_timeout`. Wird die Grenze erreicht, werden git *und*
  alles vom Hook Gestartete beendet, und gmf meldet das, statt abzustürzen.
