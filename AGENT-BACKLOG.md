# Offene Projektarbeit

Offene Punkte vor Umsetzung gegen den aktuellen Projektstand prüfen.

## Offene Punkte / Ideen

- `repo_transport_override()` liest die Repo-Config je REMOTE, und beim Fetch
  zweimal. Seit der Scope-Lesung kostet der Aufruf drei `git config`-Prozesse.
  Die vorgelagerte Prüfung in `collect_status()` beziehungsweise
  `_fetch_remote()` wird unmittelbar vor dem Fetch wiederholt.
  Bei drei Remotes im selben Repo läuft dieselbe
  repo-weite Frage sechsmal. Die naheliegenden Konsolidierungen — den Wert aus
  der Remote-Schleife herausziehen oder das Ergebnis an
  `fetch_remote_safely()` weiterreichen — tauschen dabei eine
  Sicherheitseigenschaft gegen Tempo: Die Prüfung steht bewusst unmittelbar vor
  dem Netzaufruf, und je Remote neu gelesen fängt sie eine Config-Änderung
  mitten im Scan. Deshalb hier notiert statt nebenbei entschieden.
- (die Frage zu `http.proxy` und `http.sslVerify` ist am 2026-09-03
  entschieden — beide werden wie `core.gitProxy` abgelehnt, sobald die Config
  des Repos sie setzt, siehe Regel oben)

- Messzeiten: `measured_at` bezeichnet die lokale Messung, `fetched_at` nur den belegten erfolgreichen Fetch dieses Remotes für den aktuellen Branch. Keine Zeit aus `FETCH_HEAD` oder Dateizeiten erfinden. `carry_fetch_times()` bindet übernommene Zeiten an Branch und `remote_approval_signature()`. Scan-Abbruch läuft ausschließlich über den eigenen `GitCallScope`; der globale Abbruch bleibt dem gesamten Prozess vorbehalten.

- Arbeitsansichten verwenden `repo_matches_view()` für Modelle und JSON; belegte Remote-Probleme laufen über `remote_access_problem()`. Bei Vergleichen wählt `filter_comparison_repos()` die Vereinigung passender Repo-Pfade und erhält beide Gegenstücke. `sync_clean` und `publication_pending` getrennt halten. Der Platz der Kontextzeile kommt aus `context_height()`, auch bei der Navigation.

- GitHub-Inhalt und Historie getrennt bewerten: `content_equal` ist nur nach einem erfolgreichen Vergleich belegt, bei Lesefehlern unbekannt. `publication_content_current()` berücksichtigt zusätzlich Fetch- und Konfigurationsfehler. `remote_badge_parts()` liefert gemeinsame Texte/Farben für Detailzeile, Kompakt-Kontext und Liste. Gleicher öffentlicher Inhalt macht abweichende Historie neutral; bei unterschiedlichen Inhalten und divergenten Historien gibt es Prüfbedarf unter `publish`, aber keine geratene Richtung. Private Sync-Remotes behalten ihre Abstammungsprüfung. Die rohen Commit-Zahlen bleiben in Info und JSON erhalten. `publication_excludes` gehört ausschließlich in die lokale Konfiguration, ist je `owner/repo` gebunden und verändert keine Push-Freigabe.

- JSON hat eine eigene `schema_version`; Produktversion und Datenvertrag nicht vermischen. `diff_findings()` erzeugt strukturierte Befunde und lokalisierte Texte gemeinsam, `diff_status()` bleibt die Textschnittstelle. Offline-Vergleiche verwenden `load_snapshot()`/`parse_snapshot_json()` und prüfen gespeicherte Filtergrenzen. `--compare` darf weder Config erzeugen noch Repos/Netz abfragen. Farbauswahl für Listen läuft über `color_enabled()`.

- Prozessausgaben werden byteerhaltend über `_decode_process_output()` dekodiert; kein `text=True` im zentralen Prozessleser, sonst werden CR und CRLF in NUL-getrennten Pfaden verändert. `_git_reported_path()` entfernt nur den einen Protokoll-LF.
- Worktree-Familien verwenden `collect_worktrees()` und lokale Pfadidentitäten; externe Registrierungen erhalten keinen weiteren Statusscan. Submodule laufen über `collect_submodules()` ohne init/update/fetch. Eltern-HEAD, Index-Verweis und Modul-Checkout getrennt halten; reine Modul-Inhalte bleiben in `C` gesperrt. Optionale fehlende Inventare gelten nie als leer.

- Datei-Diffs verwenden `file_diff(..., mode)` und `files_for_diff_mode()`: combined: HEAD → Arbeitsbaum; staged: HEAD → Index; unstaged: Index → Arbeitsbaum einschließlich neuer Dateien. Externe Diff-/Textconv-Treiber bleiben deaktiviert. `--file-diff` erzeugt einen eigenen JSON-Ergebnistyp, keinen Repo-Snapshot, und startet weder Scan noch Netz.
- Abdeckungszählungen gleicher OIDs dürfen nur im lokalen Dict einer `read_branch_inventory()`-Messung geteilt werden. Keine Übernahme in weitere Scans oder Transfer-Preflights; Ref-/HEAD-Nachprüfungen erhalten. Laufzeitmatrix und lokale Scan-Messung stehen in `tools/check-compatibility.py` und `tools/benchmark-scan.py`.

- `collect_status()` prüft HEAD-Ref und OID einschließlich unborn/detached über den gesamten Scan. Remote-Zähler, Branch-Inventar und Upstream sind zusätzlich von einem gemeinsamen Ref-Snapshot nach dem eigenen Fetch bis zur letzten Rückgabe umschlossen. Ein Wechsel verwirft die HEAD-Identität und das Inventar und bleibt als lokaler Messfehler sichtbar, auch bei zusätzlichem Fetch-Fehler. `action_pull()` hält beim Fetch aller Remotes den ursprünglichen Repo-Pfad fest; die nach einem Refresh sichtbare Auswahl ist kein Aktionsziel.

- Worktree-/Modul-Pfade exportieren `path_key` über `snapshot_path_key()`; die Validierung bindet ihn an den älteren Anzeigetext. Vergleiche verwenden `snapshot_path_identity()`, mehrdeutige Altpfade liefern eine Fähigkeitsgrenze. `display_git_path()` hält echte Steuerzeichen und wörtliche Backslashes in Auswahlansichten auseinander; Befehlsprotokolle bleiben bei `zsh_quote()`.
- Commit-Vorschläge verwenden die bereits geprüfte `committable`-Eigenschaft der Wizard-Einträge. Eine Gruppenauswahl darf gesperrte Submodul-Inhalte oder fehlerhafte Einträge nie wieder auswählen; der letzte Commit-Schritt prüft diese Eigenschaft ebenfalls.

- Demo-Commitbäume enthalten keine temporären absoluten URLs. Das Submodul-Beispiel klont lokal, speichert in `.gitmodules` aber die stabile relative URL und stagt sie vor dem Commit erneut. `test_demo_commit_ids_are_identical_under_different_roots` vergleicht die HEAD-OIDs aller 27 Demo-Repos zwischen zwei Sandboxes.
