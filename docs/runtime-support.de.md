# Laufzeitgrenzen und wiederholbare Prüfungen

[English](runtime-support.md) · [Deutsch](runtime-support.de.md)

Der vollständige Funktionsumfang setzt Python ab 3.9 und Git ab 2.36 auf macOS
und Linux voraus. Python 3.9 liefert die verwendeten String-Operationen der
Einzeldatei; Git 2.36 liefert NUL-getrennte Worktree-Registrierungen. Siehe
[Python-String-API](https://docs.python.org/3.9/library/stdtypes.html#str.removeprefix)
und [Git-2.36-Worktree-Format](https://git-scm.com/docs/git-worktree/2.36.0).
Kann älteres Git ein Inventar nicht belegen, erscheint es als nicht verfügbar;
eine erratene Rückfallebene mit zeilengetrennten Pfaden gibt es nicht.

Die TUI benötigt curses aus der Standardbibliothek, ein POSIX-Terminal und eine
UTF-8-Locale. Zusätzliche Python-Module sind nicht nötig. App-Tasten nutzen auf
macOS `open -a`; Linux erklärt diese Plattformgrenze und kann über den Wrapper
weiter ins Repo wechseln. Zsh-Wrapper, Installer und Build-/Release-Skripte
benötigen zsh. Ohne zsh laufen auf Linux die Python-TUI und CLI; zsh-spezifische
Tests werden übersprungen. Windows und andere als die erfassten OS-Versionen
sind nicht validiert.

## Prüfung wiederholen

```sh
python3 tools/check-compatibility.py
python3 tools/check-compatibility.py --python python3.9 --python python3.14 --suite core
python3 tools/check-compatibility.py --python python3.15 --git /usr/bin/git --screens
```

Jede Kombination protokolliert Interpreter-, Git- und Produktversion, Quellhash,
Exit-Codes, Dauer und Testzahl. Full ist der Standard und enthält bestehende
Sicherheits-, CLI- und Release-Fixtures. Core wählt nur die separat gepflegten
Funktionsverträge; es ersetzt die vollständige Sicherheitssuite nicht.
`--screens` prüft die sechs erzeugten Bilder. Keine Laufzeit wird installiert oder
aktualisiert. Temporäre PATH-Wrapper wählen das verlangte Git ohne globale
Einstellungen. Fehlende oder zu alte Laufzeiten sind ein Fehler, kein übersprungener
Erfolg.

## Prüfstand, 2026-10-16

### 0.30.3

| System | Python | Git | Ergebnis |
|---|---|---|---|
| macOS 26.7.1, arm64 | 3.15.0 | 2.56.0 | 842 Tests und sechs identische Bilder |
| macOS 26.7.1, arm64 | 3.9.6 | 2.56.0 | 157 Kernprüfungen |
| Linux, x86_64 | 3.12.3 | 2.43.0 | 788 ausgeführt, 54 zsh-Prüfungen übersprungen |

Vier Regressionen wurden vor der Korrektur reproduziert. Geprüft sind HEAD-Wechsel
einschließlich unborn/detached, Ref-Änderungen zwischen den Graph-Lesern und
JSON-Exponentenüberläufe. Echte Terminalabläufe DE/EN halten beim Fetch aller
Remotes das ursprüngliche Repo fest, wenn die Arbeitsansicht die Auswahl ändert.
Echte CLI-Läufe melden einen widersprüchlichen Scan mit Exit 1 und JSON-Überlauf
mit Exit 2.

### 0.30.2

| System | Python | Git | Ergebnis |
|---|---|---|---|
| macOS 26.7.1, arm64 | 3.15.0 | 2.56.0 | 828 Tests und sechs identische Bilder |
| macOS 26.7.1, arm64 | 3.9.6 | 2.56.0 | 144 Kernprüfungen einschließlich 13 Integrationsfällen |
| macOS 26.7.1, arm64 | 3.14.8 | 2.56.0 | 13 Integrationsfälle |
| Linux Mint 22.2, x86_64 | 3.12.3 | 2.43.0 | 774 ausgeführt, 54 zsh-Prüfungen übersprungen; sechs identische Bilder |

Zusätzlich stimmen die HEAD-OIDs aller 27 Demo-Repos zwischen macOS und Linux
überein. Zwei vollständige Sandboxes je Regression prüfen die Unabhängigkeit
vom temporären Wurzelpfad. Die Submodul-URL im Commitbaum ist relativ; der lokale
Clone-Aufbau bleibt netzfrei.

### 0.30.1

| System | Python | Git | Ergebnis |
|---|---|---|---|
| macOS 26.7.1, arm64 | 3.15.0 | 2.56.0 | 827 Tests und sechs identische Bilder |
| macOS 26.7.1, arm64 | 3.9.6 | 2.56.0 | 143 Kernprüfungen einschließlich zwölf Integrationsfällen |
| macOS 26.7.1, arm64 | 3.14.8 | 2.56.0 | zwölf neue Integrationsfälle |
| Linux Mint 22.2, x86_64 | 3.12.3 | 2.43.0 | 773 ausgeführt, 54 zsh-Prüfungen übersprungen; sechs identische Bilder |

Die realen Terminalabläufe wurden auf Deutsch und Englisch geprüft: gezielter
Commit eines LF-Dateinamens, unveränderter Nachbarpfad mit wörtlichem Backslash-n,
erhaltenes fremdes Staging, Offline-Vergleich bei gesperrten Git-/SSH-Aufrufen
sowie bestätigter Push in ein temporäres lokales Bare-Remote. Commit-Vorschläge
behielten gesperrte Modul-Inhalte bei. Nur Test-Repos wurden verändert.

### 0.30.0

| System | Python | Git | Ergebnis |
|---|---|---|---|
| macOS 26.7.1, arm64 | 3.9.6 | 2.56.0 | 815 Tests; Python auch für Shell-Unterprozesse gebunden; sechs Bilder identisch |
| macOS 26.7.1, arm64 | 3.15.0 | 2.56.0 | 815 Tests und `build.sh --check` erfolgreich |
| macOS 26.7.1, arm64 | 3.9.6 / 3.14.8 | 2.54.0 Apple / 2.56.0 | je 131 Kernprüfungen, vier Kombinationen |
| Linux Mint 22.2, x86_64 | 3.12.3 | 2.43.0 | 761 ausgeführt, 54 wegen fehlendem zsh übersprungen; sechs Bilder identisch |

Die zsh-Prüfungen wurden unter macOS ausgeführt. Matrix-Ausgaben nennen übersprungene Tests ausdrücklich.
Eine Versionsuntergrenze ist eine Code-/Funktionsvoraussetzung, kein Beleg für
Läufe jeder Zwischenversion. Die JSON-Berichte unterscheiden Voll- und Kernprüfungen.

## Scan-Messung

`python3 tools/benchmark-scan.py` erzeugt ein temporäres Repo, drei lokale Bare-
Remotes und einen oder 25 Branches auf demselben Commit. Je Fall werden fünf
lokale und fünf Fetch-Scans gemessen; Aufbau zählt nicht mit. Es gibt keinen
externen Netzzugriff, die Test-Repos werden entfernt. Erfasst werden Einzelzeiten,
Mediane und Git-Aufrufzahlen. Laufzeiten mit demselben Interpreter/Git und
vergleichbarer Rechnerlast vergleichen. Die [Vorher-/Nachher-Daten](scan-benchmark.json)
zeigen bei 25 identischen Spitzen lokal 57 → 33 Aufrufe, mit Fetch 160 → 136.
Der Fall mit einem Branch bleibt bei 33 / 136. Laufzeiten schwankten mit der Last;
Aufrufzahlen sind das stabile Ergebnis. Ref-/HEAD-Nachprüfungen und Konfigurations-
prüfungen vor jeder Übertragung bleiben erhalten. Zählungen werden nur innerhalb
eines festgehaltenen Ref-Inventars geteilt.

Die aufgezeichnete Messung `consistency_guard` für 0.30.3 ergänzt vier Prüfungen
über die gesamte Graph-Messung. Der Fall mit einem und der mit 25 Branches
benötigen jeweils 37 lokale oder 140 Fetch-Scan-Aufrufe. Damit werden Änderungen
zwischen Remote-Zählern, Branch-Inventar und Upstream-Lesen erkannt; der Scan
meldet einen lokalen Messfehler.
