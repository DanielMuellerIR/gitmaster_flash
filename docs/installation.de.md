# Installation

[English](installation.md) · [Deutsch](installation.de.md) · [Übersicht](../README.de.md)

Gitmaster Flash benötigt Python 3.9+, Git 2.36+ und ein UTF-8-Terminal mit
curses. Das Programm verwendet die Standardbibliothek. Das Checkout oder
entpackte Quellarchiv dauerhaft am selben Ort ablegen.

Aus diesem Ordner startet `python3 gitmaster_flash.py ~/projekte` das
Programm direkt. Enter gibt den ausgewählten Repo-Pfad aus. Die optionale
Shell-Funktion `gmf` startet dasselbe Programm und wechselt anschließend
in der aufrufenden Shell in dieses Verzeichnis.

## Shell-Funktion registrieren

Den passenden Befehl mit dem eigenen Benutzer ausführen.

```sh
./install.sh          # unter macOS standardmäßig Zsh
./install-linux.sh    # unter Linux Bash, Fish oder Zsh aus $SHELL
```

Beide Einstiegsskripte verwenden denselben Installer. Unter Linux wählt auch
`./install.sh` die Shell über `$SHELL`. Mit `--shell bash`, `--shell fish`
oder `--shell zsh` lässt sich die Shell ausdrücklich angeben.
`--shell auto` übernimmt `$SHELL` auch unter macOS.

Bei beiden Skripten führt `--check` nur die Laufzeitprüfungen und Selbsttests
aus. Bei der regulären Installation laufen diese Prüfungen, bevor Dateien
für die Registrierung geschrieben werden. Fehlende Systempakete über die
Paketverwaltung des Systems installieren.

| Shell | Verwendete Dateien |
|---|---|
| Zsh | Ein markierter Block in `${ZDOTDIR:-$HOME}/.zshrc` bindet `gmf.zsh` aus dem Projekt ein. |
| Bash | Ein markierter Block in `$HOME/.bashrc` bindet `${XDG_CONFIG_HOME:-$HOME/.config}/gitmaster_flash/shell/gmf.bash` ein. |
| Fish | Lädt `${XDG_CONFIG_HOME:-$HOME/.config}/fish/functions/gmf.fish` automatisch. |

Eine neue Shell starten, um die Funktion zu laden. Mit `gmf --version` die
Registrierung prüfen und dann `gmf ~/projekte` ausprobieren. Der Wrapper
gibt den Exit-Code des Programms an aufrufende Skripte weiter.

Interaktive Bash-Shells ohne Login lesen `.bashrc`. Für Login-Shells
muss das Login-Profil `.bashrc` laden, damit `gmf` verfügbar ist.

## Bestehende Installationen und verschobene Checkouts

Der Installer erhält funktionierende Registrierungen für dasselbe Checkout,
darunter erkannte direkte Zsh-Source-Zeilen und bislang verwaltete
Linux-Zsh-Wrapper. Ein erneuter Lauf lässt identische Dateien unverändert.
Für jede geänderte Startdatei legt der Installer eine datierte
`.gmf-backup-*`-Kopie an. Benutzer- und Konfigurationsordner müssen absolute
Pfade haben.

Eine andere Registrierung, ein widersprüchlicher Wrapper oder Shell-Code,
dessen Registrierung sich nicht verifizieren lässt, hält die Installation an.
Vor dem Bearbeiten die Meldung und die bezeichnete Datei prüfen. Nach dem
Verschieben eines Checkouts eine direkte Zsh-Source-Zeile auf den neuen Pfad
umstellen. Bei einer verwalteten Installation den zugehörigen Installer-Block
und gegebenenfalls den von ihm eingebundenen generierten Wrapper entfernen,
danach den Installer vom neuen Pfad aus starten. Bei Fish genügt das Entfernen
der automatisch geladenen Datei. Andere Shell-Konfiguration bleibt erhalten.

## App-Tasten auf dem Desktop

App-Tasten verwenden unter macOS `open -a` und unter Linux einen absoluten
Pfad zu einem ausführbaren Starter. Das ausgewählte Repository wird als ein
Ordnerargument übergeben. Die App-Taste meldet den Exit-Code des Starters
und eine begrenzte Diagnose. Ein erfolgreicher Exit bezieht sich auf den
Starterprozess. App-Tasten benötigen eine lokale Desktopsitzung. Die
[Konfiguration](configuration.de.md) beschreibt die Einstellung `apps`.
