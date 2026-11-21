# gitmaster_flash

Gitmaster Flash ist eine curses-Übersicht über Git-Repositories unterhalb
des gewählten Ordners. Laufzeit und Installationswerkzeuge verwenden die
Python-Standardbibliothek.

- Reine Logik von der curses-Oberfläche trennen. Änderungen über den
  jeweiligen Einstiegspunkt prüfen und die betroffenen nicht-interaktiven
  Schnittstellen einschließlich JSON und Exit-Codes pflegen.
- Git nicht interaktiv über `run_git()` ausführen. Zustandsänderungen über
  `run_git_logged()` führen. Fremdes Staging erhalten und nur freigegebene
  Pfade committen. Dateivorschauen lesen Repo-Inhalte und führen keine
  externen Diff- oder Textconv-Treiber aus. Remote-Fehler sind keine
  Nachweise für einen anderen Repo-Stand.
- Remote-Ziel, freigegebene Refs und die unmittelbaren Prüfungen vor
  Transfers erhalten. Gleichzeitige Änderungen als solche melden. Beim
  Scan identische OIDs nur innerhalb einer Messung gemeinsam auswerten.
- Benutzertexte in `TR` in Englisch und Deutsch pflegen. Beide READMEs
  und verlinkte Leitfäden inhaltlich synchron halten. Beispiele und
  Demo-Repositories generisch halten. Rechner, persönliche Remotes und
  App-Pfade gehören in die lokale Konfiguration.
- Die Produktversion steht in `gitmaster_flash.py` als `__version__`.
  Die JSON-Schemaversion ist unabhängig davon.
- `python3 -m unittest discover -s tests` führt die Selbsttests aus.
  `./build.sh` erzeugt nach den Tests die Demo-Bilder neu.
  `./build.sh --check` prüft Tests und Bilder. Die Tasteneingaben des
  Bildgenerators bleiben auf seinen eigenen PTY-Prozess beschränkt.
- `./install.sh --check` und `./install-linux.sh --check` prüfen den
  Installer ohne Registrierung. Bestehende Shell-Definitionen schützen.
  Die Tests und ihre Quellen gehören ins installierbare Quellarchiv.

Verträge und Prüfbefehle stehen in [Architektur und Entwicklung](docs/architecture.de.md),
[Git-Übertragungen und Schutzregeln](docs/security.de.md), im
[JSON-Format](docs/json-format.de.md) und in der [Installation](docs/installation.de.md).
