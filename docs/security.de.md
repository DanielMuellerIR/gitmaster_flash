# Git-Übertragungen und Schutzregeln

[English](security.md) · [Deutsch](security.de.md) · [Übersicht](../README.de.md)

Fetch, Pull und Push verwenden geprüfte Ziele und eindeutige Refs. Die Schutzregeln gelten für Aktionen aus Gitmaster Flash.

## Fetch und Scan-Zustand

Der erste Scan liest den lokalen Zustand und die Remote-Tracking-Refs.
`R` aktualisiert sichere Remotes im Hintergrund, `U` ein einzelnes Remote.
Gitmaster Flash holt den gemeldeten Commit des aktuellen Branches ohne
Tags und ohne Submodul-Rekursion. Anschließend wird nur der zugehörige
Tracking-Ref ohne Dereferenzierung symbolischer Refs aktualisiert.
Fetch- und Inhaltsvergleichsfehler bleiben als Messfehler sichtbar,
gegebenenfalls zusammen mit der letzten erfolgreichen Fetch-Zeit.

## Commits und Stashes

Die Commit-Hilfe verwendet für die freigegebenen Pfade des Arbeitsbaums
einen temporären Index. Branch und Commit-Baum werden geprüft, bevor
nur diese Pfade in den tatsächlichen Index übernommen werden. Stash-Aktionen
zeigen die betroffenen Einträge und Dateien vorab. Apply und Pop setzen
einen sauberen Index und Arbeitsbaum voraus. Löschen erfordert eine
eigene Bestätigung. Der [Leitfaden zur Oberfläche](interface.de.md)
beschreibt Auswahl, Konflikte und den Zustand nach einer teilweisen Ausführung.

## Pull und Push

`P` ist absichtlich auf einen nichtöffentlichen Sync-Remote begrenzt. Die Aktion
fetcht zuerst, verlangt einen sauberen Arbeitsbaum und blockiert Rückstand oder
divergente History. Fetch- und Push-URL müssen genau dasselbe zugangsdatenfreie
Host-/Repo-Ziel bezeichnen; mehrere oder abweichende Push-URLs werden gesperrt.
Der bei Hosting-Diensten übliche Mix — Fetch per HTTPS, Push per SSH über den
virtuellen Benutzer `git` — gilt dabei als dasselbe Ziel:
`git@host:org/repo` und `https://host/org/repo` meinen dasselbe Repo.
Unmittelbar vor der bestätigten Mutation werden Branch, HEAD, Index, Arbeitsbaum,
Remote-Identität und Ziel-OID nochmals geprüft. Push überträgt die freigegebene Commit-OID mit
einem expliziten Refspec. Eine exakte Ziel-OID-Lease verhindert, dass eine
Remote-Löschung oder parallele Verschiebung daraus eine ungeprüfte Aktualisierung
macht; Tags und Submodul-Commits werden nie gesendet. Für diesen einen Push sind
außerdem Repo-Hooks abgeschaltet, damit ein pre-push-Hook keine ungeprüften Tags
oder weiteren Refs veröffentlicht. Die Übertragung verwendet die
exakt geprüfte URL statt eines Remote-Namens, den eine parallele Konfigurationsänderung
umleiten könnte. Der Weg zu dieser Adresse gehört dazu: Für diesen einen Aufruf
bleibt ein SSH-Wrapper aus der Umgebung oder der Repo-Config außen vor, und drei
Einstellungen in der *eigenen* Config des Repos sperren die Übertragung ganz —
`core.gitProxy`, `http.proxy` (samt seiner adressgebundenen Form) und
`http.sslVerify = false`. Jede davon kann eine Übertragung woanders landen
lassen als bei der geprüften Adresse, und keine lässt sich für einen einzelnen
Aufruf abschalten: Die erste ist ein mehrwertiger Schlüssel, und die zweite leer
zu erzwingen schaltete auch einen echten Firmen-Proxy ab. In der globalen
Git-Config bleiben alle drei erlaubt: Dort hat man sie selbst hingeschrieben,
und ein Repo kann sie nicht einschleusen. Die Fetch-Refspec muss
`refs/heads/<Branch>` auf den passenden Tracking-Ref dieses Remotes abbilden.

Mit `L` wählst du einen Pull-Remote. „Alle Remotes abrufen“ aktualisiert zuerst
alle sicheren Remotes dieses Repos und öffnet danach erneut die Quellenwahl.
Es wird nur der **gleichnamige Remote-Branch** in den aktuellen lokalen Branch
übernommen, der bisherige Upstream bleibt erhalten. gmf zeigt die eingehenden
Commits und Dateinamen vor der Bestätigung mit **J ⏎**. Nur Fast-Forward bei sauberem
Arbeitsbaum ist erlaubt; Divergenzen, Konflikte und laufende Git-Vorgänge stoppen Pull.
Lokale ignorierte Dateien bleiben bei gleichnamigen eingehenden Pfaden erhalten;
Squash-Einstellungen des Branches ändern den Fast-Forward nicht.
Vor dem Anwenden wird der freigegebene Stand erneut geprüft und die feste Commit-ID
integriert, ohne Hooks, Autostash oder rekursive Submodule. Bearbeite das Repo und
wechsle seinen Branch währenddessen nicht in einem anderen Programm: Gegen einen
parallelen Checkout lassen sich Branch, Index und Arbeitsbaum nicht atomar binden.
Merge und Rebase bleiben Terminal-Sonderfälle.

Für GitHub gibt es den getrennten `G`-Pfad. Er funktioniert nur, wenn derselbe
Branch auf genau einem GitHub-Remote bereits existiert und die Historien verbunden
sind. Vor der Veröffentlichung zeigt er alle ausgehenden Commits und geänderten
Dateinamen. Die Rückfrage steht unter dieser Liste als weißer Balken auf Schwarz
mit rotem `J ⏎ = ja`, einer Leerzeile darüber und sichtbarem Terminal-Cursor
dahinter, damit sie auch nach hundert Dateinamen noch auffällt; beantwortet wird
sie mit `J` + ⏎ (jede andere Eingabe, ein leeres ⏎ oder Esc bricht ab):

```text
 GitHub-Push-Vorschau · app → github/master
 …
 M	src/search.py
 A	docs/search.md

 Diesen Branch zu github veröffentlichen? J ⏎ = ja, sonst Abbruch: ▮      ← Balken
 ↑/↓ scrollen · Esc abbrechen · Zeile 41-72 / 72
```

Die `P`-Rückfrage für das private Sync-Remote nutzt denselben Balken, eine Zeile
über der Fußzeile der Repo-Liste. Auch der letzte
Befehl überträgt nur den aktuellen Branch: freigegebene Quell-OID, exakte
Ziel-Lease, keine Tags, kein neuer Branch. Ein Remote mit mehreren oder
abweichenden Fetch-/Push-Zielen wird vollständig gesperrt, selbst wenn beide
Ziele auf GitHub liegen. Komplexe Fälle bleiben bewusst dem Terminal vorbehalten.

Git fragt hier nie nach Zugangsdaten. Jeder Git-Aufruf läuft ohne Terminal-Prompt,
ohne Askpass und in eigener Session, denn Git schreibt so eine Frage
(`Username for 'https://github.com':`) direkt auf das Terminal statt in die
abgefangene Ausgabe — im curses-Bild zerstört das die Anzeige und Git wartet dann
auf eine Eingabe, die nie kommt. Ein Remote, das einen Login braucht, scheitert
deshalb sofort mit `<Remote> braucht einen Login (kein Credential-Helper/SSH-Key).`
statt zu fragen. HTTPS-Zugangsdaten gehören in einen
Credential-Helper (in einer angemeldeten macOS-GUI-Sitzung:
`git config --global credential.helper osxkeychain`),
oder man nutzt SSH mit einem Key im Agenten; beides läuft ohne Rückfrage.
