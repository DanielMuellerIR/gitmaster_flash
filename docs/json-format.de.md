# JSON-Format, Schema 1

[English](json-format.md) · [Deutsch](json-format.de.md)

`schema_version` bezeichnet den JSON-Vertrag, `version` die messende
Programmversion. Zusätzliche optionale Felder ändern Schema 1 nicht;
ein künftig inkompatibles Schema wird abgelehnt. Ältere Objekt-Snapshots mit
`version` und `repos` bleiben mit ausdrücklichen Fähigkeitsgrenzen lesbar.
Nackte Arrays früher Versionen werden nicht unterstützt.

## Snapshots

Ein vollständiger Snapshot entsteht mit `gmf --json > snapshot.json`.
`--fetch` ergänzt eine neue Remote-Messung und aktualisiert sichere
Remote-Tracking-Refs.

| Feld | Bedeutung |
|---|---|
| `schema_version` | Ganzzahl, derzeit `1` |
| `version` | Programmversion |
| `generated_at` | Erstellungszeit des Snapshots, ISO-8601 mit Zeitzone |
| `root` | Scan-Wurzel; Repos werden über den relativen `rel` zugeordnet, unabhängig von diesem absoluten Pfad |
| `repos` | Repo-Zustände einschließlich `head_oid`, Vorgang, Branches und Remotes |
| `filter` | Optionaler Umfang der Pfadsuche: `query`, `matches`, `total` |
| `view` | Optionaler Umfang der Arbeitsansicht: `id`, `matches`, `total`, `hidden_attention` |

`measured_at` eines Repos ist seine lokale Messzeit. `fetched_at` je Remote ist
der letzte diesem Prozess belegte erfolgreiche Fetch für diesen Branch und
diese Remote-Konfiguration. Ein leerer String bedeutet unbekannt. Eine null-
`head_oid` bezeichnet einen belegten Branch ohne ersten Commit; eine leere
oder fehlende Identität ist unbekannt. Branch-Abdeckung bezieht sich auf lokal
bekannte Remote-Tracking-Refs, benannt durch `branch_coverage_basis`.

Ändern sich HEAD oder Branch-/Remote-Refs während eines Scans, melden `error`
und `branches_error` einen lokalen Messfehler. `head_oid` wird unbekannt und das
Branch-Inventar verworfen. Die verbleibenden Zähler sind unvollständige Messungen,
kein erfolgreich synchronisierter Snapshot; vor weiteren Aktionen neu einlesen.

Textfelder stellen Terminal-Steuerzeichen und Bytes ohne UTF-8-Darstellung
sichtbar maskiert dar. Konfigurierte Remote-URLs haben keine exportierten URL-Felder;
Ziel-Identitäten sind gehasht. Git-Fehlerbelege enthalten redigierten Text. Anzeigetexte nicht in Git-Argumente zurückwandeln.

## Gespeicherte Vergleiche

```sh
gmf --json > vorher.json
# ... später ...
gmf --json > nachher.json
gmf --compare vorher.json nachher.json
gmf --compare vorher.json nachher.json --json --view changes
```

`--compare` liest ausschließlich die beiden UTF-8-JSON-Dateien. Es startet
keinen Scan, SSH oder Fetch und erzeugt keine Config. Pro Datei gelten 64 MiB
als Grenze. Doppelte JSON-Schlüssel, nicht endliche Zahlen, ungültige Zähler,
inkompatible Schemata und Verschachtelung über 128 Container führen zu Exit 2.
UTF-8 mit Byte-Reihenfolge-Markierung wird akzeptiert.

Gespeicherte Pfadfilter müssen dieselbe Wirkung haben: Reihenfolge,
Groß-/Kleinschreibung und wiederholte Begriffe sind unerheblich. Gespeicherte
Arbeitsansichten werden abgelehnt, weil ein ausgefiltertes Repo sein Gegenstück
verbergen könnte. Vollständige Snapshots speichern und `--view` erst beim
Vergleich anwenden. Dann wird ein Repo auf beiden Seiten verglichen, sobald
eine Seite zur Ansicht passt. Verschiedene ausgecheckte Branches erzeugen
keine Drift ihrer Remote-Zähler. Doppelte normalisierte Repo-Namen erhalten
einen Mehrdeutigkeitsbefund und werden nicht willkürlich gepaart.

Berichte nennen Erstellungszeiten, den Zeitraum lokaler Messungen und unbekannte
Fähigkeiten. Zeitunterschiede allein sind keine Drift. Die Quellen-Metadaten
erhalten gespeicherte Filtergrenzen; ein zusätzlicher Vergleichsfilter schränkt
diesen erfassten Bestand weiter ein. Unbekannte Fetch-Zeiten bedeuten keinen
fehlgeschlagenen Fetch.

## Vergleichsausgabe

SSH-`--diff` und gespeichertes `--compare` verwenden dieselbe Befunderzeugung.

| Feld | Bedeutung |
|---|---|
| `schema_version` | Vergleichsvertrag, derzeit `1` |
| `here`, `host` | Linke Programmversion und rechtes Vergleichslabel; aus Kompatibilitätsgründen erhalten |
| `differences` | Lokalisierte lesbare Zeilen, aus Kompatibilitätsgründen erhalten |
| `findings` | Stabile strukturierte Befunde |
| `sources` | Metadaten für `here` und `there`: Versionen, Zeiten, Wurzeln, erfasste Anzahl und gespeicherter Umfang |
| `limitations` | Grenzen ohne Drift: stabile `side`, `code`, `count` |
| `filter` | Optionaler angewendeter Vergleichsfilter, Zähler und Angaben zur Arbeitsansicht |

Jeder Befund enthält `type`, `category`, `repo`, `remote`, `branch`, `field`,
`side`, `values` und `message`. Unbenutzte Kontextfelder sind null. `side` ist
`here`, `there` oder null, unabhängig von Sprache und Labels. `message` ist die
lesbare Zeile; Skripte verwenden die übrigen Felder. `category` ist `drift`,
`sync`, `local` oder `warning`. Ziel-URLs und Fingerprint-Werte erscheinen
nicht in Befunden.

| `type` | Kategorie | Wichtige Felder in `values` |
|---|---|---|
| `version` | warning | `here_version`, `there_version` |
| `ambiguous_repo` | drift | — |
| `only_on` | drift | —; `side` nennt die erfasste Seite |
| `remote_missing` | drift | —; `remote` und `side` |
| `remote_security` | drift | `changed_fields`, ausschließlich Config-Feldnamen |
| `remote_branch` | drift | —; `branch`, `remote`, `side` |
| `remote_state` | drift | `here_ahead`, `here_behind`, `there_ahead`, `there_behind` |
| `sync_even` | sync | `ahead`, `behind`, auf beiden Seiten gleich |
| `branch` | local | `here_branch`, `there_branch` |
| `head` | drift | `here_oid`, `there_oid`; null ist ein Branch ohne ersten Commit |
| `local_branch_missing` | drift | —; `branch`, `side` |
| `branch_coverage` | local | `here_coverage`, `there_coverage`, jeweils mit `state` und `uncovered_commits` |
| `branch_pending` | sync | `count`, gemeinsame nicht abgedeckte Commits |
| `repo_field` | drift | `here_value`, `there_value`; `field` nennt die Eigenschaft |
| `branch_inventory_failed` | local | `error` |
| `branch_coverage_unknown` | local | `count`, Branches mit unbekannter Abdeckung |
| `operation` | local | `operation`, stabile Git-Vorgangs-ID |
| `dirty` | local | `count`, geänderte/neue Dateien |
| `worktree_inventory_failed` | local | `error`; `side` nennt die Seite |
| `submodule_inventory_failed` | local | `error`; `side` nennt die Seite |
| `submodule_inspection_failed` | local | `path`, `path_key`, `error`; `side` nennt die Seite |
| `submodule_state` | local | `path`, `path_key`, `here_state`, `there_state` |
| `fetch_failed_side` | local | `count`, Repos mit fehlgeschlagenem Fetch |

Die Grenz-Codes sind `legacy_schema`, `head_identity_unknown`,
`branch_inventory_unknown`, `local_measurement_unknown`, `fetch_age_unknown`
und `submodule_inventory_unknown` (nur wenn das Gegenstück ein Modul-Inventar hat)
sowie `submodule_path_identity_unknown` für mehrdeutige alte Modul-Pfade.
Grenzen allein ändern den Exit-Code nicht. Vergleiche enden mit 0 ohne Befunde,
1 mit mindestens einem Befund und 2 bei unlesbarem oder inkompatiblem Vergleich.
Lokale Hinweise und Versionswarnungen behalten das bestehende Exit-1-Verhalten.
Leere Bestände sagen ausdrücklich, dass nichts verglichen wurde.

Bei einer Arbeitsansicht zählen `here_matches` und `host_matches` einbezogene
Gegenstücke; `here_view_matches` und `host_view_matches` zählen tatsächliche
Ansichtstreffer. `selection: "either-side"` hält die Vereinigungsregel fest.

## Optionale Worktree- und Modul-Inventare

`worktree_family` enthält die lokale `id`, das absolute `common_dir`,
`measured_at` und `members`. Ein Mitglied enthält `path`, `head_oid`, `branch`,
`bare`, `detached`, `locked`, `lock_reason`, `prunable`, `prune_reason`,
`present` (bool oder null bei unbekannt), `in_scan_root` und `is_current`.
`worktrees_error` erklärt eine fehlgeschlagene Messung. IDs und absolute Pfade
sind lokale Zuordnung, kein Rechnervergleich. Registrierte externe Pfade werden
nicht zusätzlich gescannt.

`submodules` enthält direkte Module mit `path`, `head_oid` (Eltern-HEAD),
`index_oid`, `checked_out_oid`, `index_mode`, `index_entries` (je `mode`, `oid`,
`stage`), `initialized`, `conflicted`, `worktree_kind`, `modified`, `deleted`,
`untracked`, `conflicts`, `operation` und `error`. `worktree_kind` ist
`repository`, `directory`, `missing`, `file` oder `unavailable`.
`submodules_error` erklärt ein nicht belegbares Gesamtinventar;
`submodule_review_count` zählt Inhaltsänderungen, Konflikte und Vorgänge.
Ein leeres erfolgreich gemessenes Array belegt keine direkten Module; ein
fehlendes Feld belegt keine Messung. Fehlermeldungen gehen vor leeren Ergebnissen.
Modul-URLs und Update-Befehle werden nicht exportiert.

`submodule_state` vergleicht Verweise, Initialisierung, Dateizähler und Vorgänge
als lokale Zustände. Ein Lesefehler erzeugt einen eigenen Befund statt eines
vermeintlich fehlenden Moduls. Neue optionale Felder und Befundtypen können in
Schema 1 hinzukommen; Verbraucher sollten unbekannte Typen erhalten oder
überspringen, statt sie als bekannte Typen auszulegen.

Seit 0.30.1 tragen Worktree-Mitglieder und Module zusätzlich `path_key`: die
kleingeschriebenen Hex-Bytes des originalen UTF-8-/surrogateescape-Pfads.
`path` bleibt der ältere maskierte Anzeigetext. Die Validierung prüft, dass der
Schlüssel genau diesen Pfad beschreibt; unterschiedliche Schlüssel dürfen
denselben alten Anzeigetext haben. Absolute Worktree-Pfade bleiben nur lokale
Metadaten. Modul-Schlüssel bezeichnen relative Git-Pfade und dienen der Zuordnung.
Datei-/Repo-Auswahl und Modul-Ansichten zeigen wörtliche Backslashes doppelt.

Alte normale Modul-Pfade bleiben vergleichbar. Enthält ein alter Pfad eine
mehrdeutige Maskierung ohne Schlüssel, nennt `submodule_path_identity_unknown`
die Grenze. Solche Module und ihre möglichen Gegenstücke erzeugen keinen
erfundenen Unterschied. Neue `submodule_state`- und `submodule_inspection_failed`-
Befunde tragen den Pfadschlüssel mit (null bei unbekannter Altidentität);
ihr `path` ist bei bekanntem Schlüssel der eindeutige Anzeigetext.
