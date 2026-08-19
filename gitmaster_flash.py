#!/usr/bin/env python3
"""gitmaster_flash — fast terminal (TUI) overview of every Git repo below the
current directory, so you can tidy up many repos quickly.

Green = clean and in sync with the configured remote; red/yellow = needs
attention (modified/deleted/untracked files, merge conflicts, stashes, commits
ahead/behind). Problem repos sort to the top.

Keys (all shown in the footer, nothing to memorize; case-insensitive — f == F):
  ↑/↓   select a repo
  →     expand (shows files with M/D/U/C and stashes)
  ←     collapse
  ⏎     quit and cd into the repo in your terminal
        (needs the shell wrapper `gmf` from gmf.zsh — a child process cannot
        change the parent shell's working directory)
  F/…   open the repo in a configured app (see config.json)
  A     inspect the changes file by file (read-only)
  C     commit helper: select exactly which changed files to commit
  P     safely push the current branch to the private sync remote
  G     guarded GitHub push (preview + typed confirmation; branch only, no tags)
  H     explain the Git safety rules
  I     show repository details, remote addresses, and clickable GitHub URLs
  S     view the latest stash as a diff (read-only, scrollable)
  R     reload everything and fetch each safe remote separately (shows progress)
  Q     quit

Non-interactive: with --list / --json (or no TTY) it prints the overview as text
or JSON (machine-readable). Exit code 1 if any repo needs attention.

Two machines: `--diff HOST` compares this machine's repos with another one over
ssh and prints only the differences. It leaves branches, index, and working trees
alone; the first run may create config.json on either machine, and `--fetch`
updates safe remote-tracking refs. The only
requirement is that `ssh HOST` works — gitmaster_flash does NOT need to be
installed there: the script is piped over stdin, so both sides always run the
exact same version. Remotes live in .git/config and are never carried by git
itself, so they drift silently between machines — that is what this finds.

Try it risk-free: `gitmaster_flash.py --demo` builds a throwaway sandbox of fake
repos in every state and opens the UI on it (also used for the README screenshots).

Config: ~/.config/gitmaster_flash/config.json (created on first run). Configurable:
app keys, the sync-remote match, scan exclusions, and UI language (en/de).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import curses
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

__version__ = "0.18.8"

# Ein reiner lokaler Scan darf alle zwölf Worker nutzen. Beim Fetch bleiben wir
# dagegen bewusst unter dem verbreiteten sshd-Default ``MaxStartups 10:30:100``:
# Beim ersten Kaltstart ist der ControlMaster-Socket noch nicht da, und zwölf
# gleichzeitige SSH-Anmeldungen würden sonst zufällig einzelne Repos treffen.
# Die zweite Hälfte der Zusage steht am Fetch selbst (``--jobs=1`` in
# ``collect_status``): Ohne sie könnte ein einzelner Aufruf intern weitere
# Verbindungen aufmachen, und die Obergrenze hier wäre nur die halbe Wahrheit.
LOCAL_SCAN_WORKERS = 12
FETCH_SCAN_WORKERS = 8

CONFIG_PATH = Path.home() / ".config" / "gitmaster_flash" / "config.json"

# Defaults; die geschriebene config.json darf einzelne Schlüssel überschreiben.
# Bewusst generisch gehalten: eigene Editoren/Remote-Namen setzt man in der Config.
DEFAULT_CONFIG = {
    # Taste -> App zum Öffnen des Repo-Ordners (macOS `open -a`). Die Taste
    # erscheint automatisch im Footer ("E Editor"). Beispiel für weitere:
    #   "Z": {"name": "Zed", "path": "/Applications/Zed.app"}   (freie Taste waehlen)
    "apps": {
        "E": {"name": "Editor", "path": "/Applications/Visual Studio Code.app"},
    },
    # Woran der Sync-Remote erkannt wird: Remote-Name ODER Host in der URL.
    "sync_remote_names": ["origin"],
    "sync_remote_hosts": [],
    # Ordner, in die der Repo-Scan gar nicht erst hineinschaut (Tempo).
    "skip_dirs": ["node_modules", "Library", ".Trash", "venv", ".venv", "__pycache__"],
    # UI-Sprache: "en", "de" oder null = automatisch aus $LANG (Fallback en).
    "lang": None,
    # Ab so vielen Repos startet die kompakte, mehrspaltige Ansicht (M schaltet um).
    "compact_from": 20,
    # Timeout in Sekunden für einzelne git-Aufrufe (fetch darf länger).
    "git_timeout": 10,
    "fetch_timeout": 30,
    # Harte Laufzeitgrenze für den kompletten SSH-Vergleich. Sie ist bewusst
    # deutlich größer als die Einzelgrenzen oben und kann für sehr große
    # Bestände erhöht werden; ein verbundener, aber festgefahrener Remote-Lauf
    # darf die lokale TUI trotzdem nicht unbegrenzt blockieren.
    "diff_timeout": 3600,
    # `git commit` führt den pre-commit-Hook des Repos aus — und der startet in
    # vielen Projekten Linter oder Tests, die deutlich länger als zehn Sekunden
    # brauchen. Mit dem kurzen git_timeout wäre jeder solche Commit chancenlos.
    "commit_timeout": 120,
}


# ---------------------------------------------------------------------------
# i18n — kleine Übersetzungsschicht (Englisch = Basis, Deutsch optional)
# ---------------------------------------------------------------------------

UI_LANG = "en"  # von main() gesetzt; Tests nutzen die englische Basis.

TR = {
    # Fortschritt / Kopf
    "reading": {"en": "Reading repos", "de": "Lese Repos"},
    "fetching": {"en": "Fetching from remote", "de": "Hole Stand vom Remote (fetch)"},
    "hdr_repos": {"en": "repos", "de": "Repos"},
    # --diff (two machines)
    "diff_here": {"en": "here", "de": "hier"},
    "diff_same": {"en": "No differences to {h}.", "de": "Kein Unterschied zu {h}."},
    "diff_need_host": {"en": "--diff needs a host, e.g. --diff mymac",
                       "de": "--diff braucht einen Host, z.B. --diff meinmac"},
    "diff_bad_host": {"en": "--diff host is not a safe SSH destination",
                      "de": "Der --diff-Host ist kein sicheres SSH-Ziel"},
    "diff_ssh_failed": {"en": "Cannot reach {h}: {e}", "de": "{h} nicht erreichbar: {e}"},
    "diff_ssh_exit": {"en": "ssh exited with code {code}",
                      "de": "ssh endete mit Code {code}"},
    "diff_ssh_no_output": {"en": "no output", "de": "keine Ausgabe"},
    "diff_ssh_bad_json": {"en": "unreadable JSON", "de": "unlesbares JSON"},
    "diff_ssh_bad_schema": {"en": "incompatible JSON structure",
                            "de": "inkompatible JSON-Struktur"},
    "diff_version": {
        "en": "! version differs: {a} {va} vs {b} {vb} — compare with care",
        "de": "! Version verschieden: {a} {va} vs. {b} {vb} — Vergleich mit Vorsicht lesen"},
    # {m}/{a}/{b} kommen vorformatiert aus _loc(): "hier" bleibt nackt,
    # Hostnamen bekommen "auf"/"on" — deshalb steht die Praeposition NICHT im Text.
    "diff_on": {"en": "on {m}", "de": "auf {m}"},
    "diff_only_on": {"en": "only {m}: {rel}", "de": "nur {m}: {rel}"},
    "diff_remote_missing": {
        "en": "DRIFT  {rel}: remote '{r}' only {m} (git never transfers remotes)",
        "de": "DRIFT  {rel}: Remote '{r}' nur {m} (Git uebertraegt Remotes nie)"},
    "diff_remote_state": {
        "en": "DRIFT  {rel}: {r} is {aa} ahead/{ab} behind {a}, {ba}/{bb} {b}",
        "de": "DRIFT  {rel}: {r} {a} {aa} voraus/{ab} zurueck, {b} {ba}/{bb}"},
    "diff_sync_even": {
        "en": "SYNC   {rel}: {r} {aa} ahead/{ab} behind on both machines",
        "de": "SYNC   {rel}: {r} auf beiden Rechnern {aa} voraus/{ab} zurueck"},
    "diff_branch": {"en": "local  {rel}: [{ba}] {a}, [{bb}] {b}",
                    "de": "lokal  {rel}: [{ba}] {a}, [{bb}] {b}"},
    "diff_dirty": {"en": "local  {rel}: {n} changed/new file(s) {m}",
                   "de": "lokal  {rel}: {n} geaenderte/neue Datei(en) {m}"},
    # Einmal je Lauf, nicht je Repo: sonst wiederholt ein einziger fehlgeschlagener
    # Fetch dieselbe Zeile fuer jedes Repo des Rechners.
    "diff_fetch_failed_side": {
        "en": ("local  fetch failed {m} for {n} repo(s) — remote state not measurable "
               "there, not a difference between the machines"),
        "de": ("lokal  Fetch scheiterte {m} bei {n} Repo(s) — der Remote-Stand ist dort "
               "nicht messbar, das ist kein Unterschied zwischen den Rechnern")},
    "diff_repo_field": {
        "en": "DRIFT  {rel}: {field} is {va} {a}, {vb} {b}",
        "de": "DRIFT  {rel}: {field} {a}={va}, {b}={vb}"},
    "diff_remote_security": {
        "en": "DRIFT  {rel}: security/endpoint identity for {r} differs",
        "de": "DRIFT  {rel}: Sicherheit/Ziel-Identität für {r} unterscheidet sich"},
    "diff_remote_branch": {
        "en": "DRIFT  {rel}: branch '{br}' exists on {r} only {m}",
        "de": "DRIFT  {rel}: Branch '{br}' existiert auf {r} nur {m}"},
    "hdr_review": {"en": "{n} to review", "de": "{n} zu prüfen"},
    "hdr_clean": {"en": "all clean ✔", "de": "alles sauber ✔"},
    # Repo-Zeile
    "clean_synced": {"en": "✔ clean & synced", "de": "✔ sauber & synchron"},
    "no_sync_remote": {"en": "no sync remote", "de": "kein Sync-Remote"},
    "branch_not_on": {"en": "branch '{b}' not on {r}", "de": "Branch '{b}' nicht auf {r}"},
    "detached": {"en": "detached HEAD", "de": "detached HEAD"},
    "error_prefix": {"en": "ERROR: {e}", "de": "FEHLER: {e}"},
    "conflict_n": {"en": "conflict:{n}", "de": "Konflikt:{n}"},
    # Detailzeilen
    "conflict_label": {"en": "C=conflict ", "de": "C=Konflikt "},
    "stash_row_hint": {"en": "(S preview)", "de": "(S Vorschau)"},
    "no_changes": {"en": "(no changes)", "de": "(keine Änderungen)"},
    # Änderungen ansehen (A)
    "changes_title": {"en": "Changes · {rel}", "de": "Änderungen · {rel}"},
    "changes_footer": {
        "en": " ↑/↓ or Tab select file · ⏎ show diff · Q/Esc back",
        "de": " ↑/↓ oder Tab Datei wählen · ⏎ Diff ansehen · Q/Esc zurück"},
    "no_changes_to_show": {"en": "Nothing changed in this repository.",
                           "de": "In diesem Repo hat sich nichts geändert."},
    "diff_title": {"en": "Diff · {p}", "de": "Diff · {p}"},
    "diff_empty": {"en": "(no textual difference — binary or mode change only)",
                   "de": "(kein Textunterschied — nur binär oder Rechte geändert)"},
    "diff_failed": {"en": "Diff for {p} failed: {e}",
                    "de": "Diff für {p} fehlgeschlagen: {e}"},
    # Footer
    "f1": {"en": " ↑/↓/←/→ select · ⏎ cd & quit · M view · Tab log · I info · H help",
           "de": " ↑/↓/←/→ wählen · ⏎ cd & Exit · M Ansicht · Tab Log · I Info · H Hilfe"},
    "f2": {"en": " {apps} · A changes · C commit · S stash view",
           "de": " {apps} · A Änderungen · C Commit · S Stash-Blick"},
    "f3": {"en": " R fetch safe remotes · P sync push · G GitHub push · Q quit",
           "de": " R sichere Remotes fetchen · P Sync-Push · G GitHub-Push · Q Beenden"},
    # Kompakte Ansicht und Protokollbereich
    "compact_more": {"en": "columns {a}-{b}/{n}", "de": "Spalten {a}-{b}/{n}"},
    "log_pane_title": {"en": " Commands", "de": " Befehle"},
    "log_pane_hint": {"en": "  (Tab to scroll)", "de": "  (Tab zum Scrollen)"},
    "log_pane_focus": {"en": "  ↑/↓ scroll · Tab back to the list",
                       "de": "  ↑/↓ scrollen · Tab zurück zur Liste"},
    "yesno": {"en": "  (Y/N)", "de": "  (J/N)"},
    "yesno_extra": {"en": "  (Y/N/{k})", "de": "  (J/N/{k})"},
    "cancelled": {"en": "Cancelled.", "de": "Abgebrochen."},
    # Apps
    "app_not_found": {"en": "App not found: {p} (edit config.json)",
                      "de": "App nicht gefunden: {p} (config.json anpassen)"},
    "app_opened": {"en": "Opened {name}: {rel}", "de": "{name} geöffnet: {rel}"},
    "app_open_failed": {"en": "Failed to open {name}: {e}",
                        "de": "{name} öffnen fehlgeschlagen: {e}"},
    "app_over_ssh": {
        "en": "This is an SSH session — {name} can only open on the Mac you sit at.",
        "de": "Das ist eine SSH-Sitzung — {name} öffnet nur auf dem Mac vor dir."},
    "cd_hint": {"en": "(Tip: install the `gmf` shell wrapper from gmf.zsh, "
                      "then you land there automatically.)",
                "de": "(Tipp: Shell-Wrapper `gmf` aus gmf.zsh installieren, "
                      "dann landet man automatisch dort.)"},
    # Stash
    "no_stash": {"en": "No stash in this repo.", "de": "Kein Stash in diesem Repo."},
    "stash_preview_failed": {"en": "Stash preview failed: {e}",
                              "de": "Stash-Vorschau fehlgeschlagen: {e}"},
    "stash_preview_empty": {
        "en": "(stash exists, but Git produced no displayable patch)",
        "de": "(Stash vorhanden, aber Git erzeugte keinen darstellbaren Patch)"},
    "stash_preview_title": {"en": "Stash preview · {rel} · {s}",
                            "de": "Stash-Vorschau · {rel} · {s}"},
    # Pager
    "pager_footer": {"en": " ↑/↓ scroll · Q/Esc close · line {a}-{b} / {n}",
                     "de": " ↑/↓ scrollen · Q/Esc schließen · Zeile {a}-{b} / {n}"},
    # Commit-Hilfe
    "commit_title": {"en": "Commit helper · {rel} — review, then ⏎",
                     "de": "Commit-Hilfe · {rel} — Vorschlag prüfen, dann ⏎"},
    "do_commit": {"en": "✔ commit", "de": "✔ committen"},
    "do_skip": {"en": "✘ skip", "de": "✘ auslassen"},
    "commit_footer": {"en": " ␣ commit on/off · ⏎ next · Esc cancel",
                      "de": " ␣ committen an/aus · ⏎ weiter · Esc abbrechen"},
    "commit_cancelled": {"en": "Commit helper cancelled.", "de": "Commit-Hilfe abgebrochen."},
    "nothing_selected": {"en": "Nothing selected.", "de": "Nichts ausgewählt."},
    "commit_in": {"en": "Commit in {rel}", "de": "Commit in {rel}"},
    "to_commit_n": {"en": "To commit: {n} file(s)", "de": "Zu committen: {n} Datei(en)"},
    "recent_msgs": {"en": "Recent commit messages (style reference):",
                    "de": "Letzte Commit-Messages (Stil-Vorlage):"},
    "commit_msg_prompt": {"en": "Commit message: ", "de": "Commit-Message: "},
    "empty_msg": {"en": "Empty message — cancelled.", "de": "Leere Message — abgebrochen."},
    "commit_failed": {"en": "Commit failed: {e}", "de": "Commit fehlgeschlagen: {e}"},
    "commit_exists_index_failed": {
        "en": "Commit {oid} exists, but the real index was not updated — inspect git status before continuing.",
        "de": "Commit {oid} ist vorhanden, aber der echte Index wurde nicht übernommen — vor dem Weiterarbeiten git status prüfen."},
    "commit_outcome_unknown": {
        "en": "The commit outcome could not be verified — a commit may exist; inspect git log and git status before continuing.",
        "de": "Der Commit-Ausgang konnte nicht sicher geprüft werden — ein Commit kann vorhanden sein; vor dem Weiterarbeiten git log und git status prüfen."},
    # Ein Commit läuft nicht immer sofort durch: der pre-commit-Hook des Repos kann
    # Linter oder Tests starten. Ohne diese Zeile sähe die TUI so lange tot aus.
    "commit_running": {
        "en": "Committing … a pre-commit hook may run (up to {s}s).",
        "de": "Committe … ein pre-commit-Hook kann laufen (bis zu {s}s)."},
    "commit_timeout_none": {
        "en": "Commit cancelled after {s}s — nothing was committed. Raise commit_timeout "
              "in the config file if the hook needs longer.",
        "de": "Commit nach {s}s abgebrochen — es wurde nichts committet. Bei Bedarf "
              "commit_timeout in der config.json erhöhen."},
    "commit_timeout_done": {
        "en": "Commit cancelled after {s}s, but the commit exists — check git log.",
        "de": "Commit nach {s}s abgebrochen, aber der Commit ist da — git log prüfen."},
    "commit_conflicts": {
        "en": "Commit helper is blocked while merge conflicts exist.",
        "de": "Die Commit-Hilfe ist gesperrt, solange Merge-Konflikte bestehen."},
    "commit_operation": {
        "en": "Commit helper is blocked while a merge, rebase, cherry-pick, or revert is active.",
        "de": "Die Commit-Hilfe ist während Merge, Rebase, Cherry-Pick oder Revert gesperrt."},
    "commit_detached": {
        "en": "Commit helper is blocked on a detached HEAD; switch to a branch first.",
        "de": "Die Commit-Hilfe ist bei detached HEAD gesperrt; zuerst auf einen Branch wechseln."},
    # Der Rückgängig-Befehl steht bewusst in der Meldung: Wer gerade committet hat,
    # soll nicht suchen müssen, wie er es zurücknimmt.
    "committed_in": {"en": "Committed in {rel}. Undo: {undo}",
                     "de": "Committet in {rel}. Rückgängig: {undo}"},
    "push_outcome_unknown": {
        "en": "Push outcome is unknown (Git exit code {code}); fetch/check the remote before retrying.",
        "de": "Push-Ausgang unklar (Git-Exit-Code {code}); vor dem Wiederholen Remote fetchen/prüfen."},
    "push_io_unknown": {
        "en": "Push outcome is unknown; the process result could not be read — fetch/check the remote before retrying.",
        "de": "Push-Ausgang unklar; das Prozessergebnis konnte nicht gelesen werden — vor dem Wiederholen Remote fetchen/prüfen."},
    "push_tracking_changed": {
        "en": "Push succeeded, but the local tracking ref changed concurrently; reload before another push.",
        "de": "Push erfolgreich, aber der lokale Tracking-Ref änderte sich parallel; vor einem weiteren Push neu laden."},
    "nothing_to_commit": {"en": "Nothing to commit in this repo.",
                          "de": "Nichts zu committen in diesem Repo."},
    # Sichere Push-Hilfe
    "no_sync_for_action": {"en": "No sync remote is configured for this repository.",
                           "de": "Für dieses Repo ist kein Sync-Remote konfiguriert."},
    "public_simple_block": {
        "en": "The sync remote is public. Use G for the guarded GitHub preview.",
        "de": "Der Sync-Remote ist öffentlich. Nutze G für die geschützte GitHub-Vorschau."},
    "transfer_fetch_failed": {"en": "Fetch from {r} failed (Git exit code {code}).",
                              "de": "Fetch von {r} fehlgeschlagen (Git-Exit-Code {code})."},
    "fetch_outcome_unknown": {
        "en": "Fetch outcome for {r} is unknown; its tracking ref may have changed — reload before retrying.",
        "de": "Fetch-Ausgang für {r} unklar; sein Tracking-Ref kann geändert sein — vor dem Wiederholen neu laden."},
    # Kurz halten: diese Meldung erscheint auch als Badge in der Repo-Zeile und
    # wird dort auf die Terminalbreite abgeschnitten. Die Langfassung steht im README.
    "transfer_auth_missing": {
        "en": "{r} needs a login (no credential helper or SSH key).",
        "de": "{r} braucht einen Login (kein Credential-Helper/SSH-Key)."},
    "transfer_inspect_failed": {
        "en": "Git could not inspect the branch safely; no transfer was attempted.",
        "de": "Git konnte den Branch nicht sicher prüfen; es wurde nichts übertragen."},
    "remote_url_mismatch": {
        "en": "{r} has multiple or differing fetch/push targets; transfer is blocked.",
        "de": "{r} hat mehrere oder abweichende Fetch-/Push-Ziele; Transfer ist gesperrt."},
    "transfer_changed": {
        "en": "Branch, files, index, remote, or target changed after approval; review again.",
        "de": "Branch, Dateien, Index, Remote oder Ziel änderten sich nach der Freigabe; erneut prüfen."},
    "transfer_dirty": {"en": "Working tree is not clean — commit, ignore, or stash first.",
                       "de": "Arbeitsbaum ist nicht sauber — erst committen, ignorieren oder stashen."},
    "transfer_detached": {"en": "Detached HEAD — use the terminal for this special case.",
                          "de": "Detached HEAD — diesen Sonderfall im Terminal bearbeiten."},
    "transfer_missing": {"en": "Branch '{b}' does not exist on {r}; creating remote branches is blocked here.",
                         "de": "Branch '{b}' existiert nicht auf {r}; neue Remote-Branches sind hier gesperrt."},
    "transfer_divergent": {"en": "Local and {r} have diverged ({a} ahead, {b} behind); no automatic reconciliation.",
                           "de": "Lokal und {r} sind divergiert ({a} voraus, {b} zurück); kein automatischer Abgleich."},
    "transfer_behind": {"en": "Local branch is {n} commit(s) behind {r}; pull first.",
                        "de": "Der lokale Branch ist {n} Commit(s) hinter {r}; zuerst pullen."},
    "nothing_to_push": {"en": "Nothing to push to {r}.", "de": "Nichts zu {r} zu pushen."},
    "confirm_sync_push": {"en": "Push {n} commit(s) to the private sync remote {r}?",
                          "de": "{n} Commit(s) zum privaten Sync-Remote {r} pushen?"},
    "sync_pushed": {"en": "Pushed current branch to {r} (no tags).",
                    "de": "Aktuellen Branch zu {r} gepusht (keine Tags)."},
    "no_github": {"en": "No GitHub remote in this repository.",
                  "de": "Dieses Repo hat keinen GitHub-Remote."},
    "many_github": {"en": "Several GitHub remotes ({names}); use the terminal to choose deliberately.",
                    "de": "Mehrere GitHub-Remotes ({names}); bitte im Terminal bewusst auswählen."},
    "github_preview": {"en": "GitHub push preview · {rel} → {r}/{b}",
                       "de": "GitHub-Push-Vorschau · {rel} → {r}/{b}"},
    "github_type": {"en": "Type '{phrase}' to publish this branch only: ",
                    "de": "Zum Veröffentlichen nur dieses Branches '{phrase}' eingeben: "},
    "github_cancelled": {"en": "GitHub push cancelled — nothing was published.",
                         "de": "GitHub-Push abgebrochen — nichts wurde veröffentlicht."},
    "github_pushed": {"en": "Published current branch to {r}; no tags were sent.",
                      "de": "Aktuellen Branch zu {r} veröffentlicht; keine Tags übertragen."},
    "github_changed": {"en": "Remote or outgoing files changed after the preview; review again.",
                       "de": "Remote oder ausgehende Dateien änderten sich nach der Vorschau; bitte erneut prüfen."},
    "preview_branch_only": {
        "en": "Branch only: approved OID + target lease, no tags or new remote branch.",
        "de": "Nur Branch: freigegebene OID + Ziel-Lease, keine Tags/neuen Remote-Branches."},
    "preview_privacy": {
        "en": "Review every outgoing commit and file name; this is not an automatic privacy approval.",
        "de": "Jeden ausgehenden Commit und Dateinamen prüfen; dies ist keine automatische Privacy-Freigabe."},
    "outgoing_commits": {"en": "Outgoing commits:", "de": "Ausgehende Commits:"},
    "changed_files": {"en": "Changed files:", "de": "Geänderte Dateien:"},
    "none_label": {"en": "(none)", "de": "(keine)"},
    "git_help_title": {"en": "Safe Git actions & command log",
                       "de": "Sichere Git-Aktionen & Befehlsprotokoll"},
    "git_help_body": {
        "en": "P  Push only the current branch to the private sync remote.\n"
              "   Requires a clean tree, fetches first, and rejects behind/divergent history.\n\n"
              "G  Guarded GitHub push. Shows outgoing commits and file names first.\n"
              "   Requires typing PUSH <remote>; pins source and target OIDs and sends no tags.\n"
              "   New or unrelated GitHub branches remain terminal-only special cases.\n\n"
              "R  Fetches each safe remote separately; working trees stay unchanged.\n\n"
              "A  Shows every changed file and its diff without modifying the repository.\n"
              "   Discarding or unstaging remains an explicit terminal operation.",
        "de": "P  Nur den aktuellen Branch zum privaten Sync-Remote pushen.\n"
              "   Verlangt einen sauberen Tree, fetcht zuerst und blockiert Rückstand/Divergenz.\n\n"
              "G  Geschützter GitHub-Push mit Vorschau von Commits und Dateinamen.\n"
              "   Verlangt PUSH <Remote>; pinnt Quell-/Ziel-OID und sendet keine Tags.\n"
              "   Neue oder unverbundene GitHub-Branches bleiben Terminal-Sonderfälle.\n\n"
              "R  Fetcht jedes sichere Remote einzeln; Working Trees bleiben unverändert.\n\n"
              "A  Zeigt jede geänderte Datei und ihren Diff, ohne das Repo zu verändern.\n"
              "   Verwerfen oder aus der Vormerkung nehmen bleibt eine Terminal-Aktion."},
    # Repo-Info
    "repo_info_title": {"en": "Repository info · {rel}", "de": "Repo-Info · {rel}"},
    "info_path": {"en": "Path", "de": "Pfad"},
    "info_branch": {"en": "Branch", "de": "Branch"},
    "info_head": {"en": "HEAD", "de": "HEAD"},
    "info_last_commit": {"en": "Last commit", "de": "Letzter Commit"},
    "info_history": {"en": "History", "de": "Historie"},
    "info_history_value": {
        "en": "{n} commit(s) · {kind}",
        "de": "{n} Commit(s) · {kind}"},
    "info_full_clone": {"en": "full clone", "de": "vollständiger Clone"},
    "info_shallow_clone": {"en": "shallow clone", "de": "flacher Clone"},
    "info_no_commits": {"en": "(no commits)", "de": "(keine Commits)"},
    "info_upstream": {"en": "Upstream", "de": "Upstream"},
    "info_delta": {"en": "{a} ahead / {b} behind",
                   "de": "{a} voraus / {b} zurück"},
    "info_worktree": {"en": "Working tree", "de": "Arbeitsbaum"},
    "info_clean": {"en": "clean", "de": "sauber"},
    "info_changes": {
        "en": "M:{m} · D:{d} · U:{u} · conflicts:{c}",
        "de": "M:{m} · D:{d} · U:{u} · Konflikte:{c}"},
    "info_stashes": {"en": "Stashes", "de": "Stashes"},
    "info_tags": {"en": "Tags at HEAD", "de": "Tags an HEAD"},
    "info_remotes": {"en": "Remotes", "de": "Remotes"},
    "info_sync": {"en": "sync", "de": "Sync"},
    "info_github": {"en": "GitHub", "de": "GitHub"},
    "info_unsafe_remote": {
        "en": "fetch/push targets differ",
        "de": "Fetch-/Push-Ziele weichen ab"},
    "info_fetch_url": {"en": "fetch", "de": "Fetch"},
    "info_push_url": {"en": "push", "de": "Push"},
    "info_fetch_push_url": {"en": "fetch+push", "de": "Fetch+Push"},
    "info_web_url": {"en": "web", "de": "Web"},
    "info_branch_label": {"en": "branch {b}", "de": "Branch {b}"},
    "info_branch_missing_value": {"en": "not on this remote",
                                  "de": "nicht auf diesem Remote"},
    "info_last_error": {"en": "Last error", "de": "Letzter Fehler"},
    "info_git_said": {"en": "Git said", "de": "Git sagte"},
    # Branch-Abschnitt der Info-Seite
    "info_branches": {"en": "Local branches", "de": "Lokale Branches"},
    "info_branch_current": {"en": "current", "de": "aktuell"},
    "info_branch_merged": {"en": "merged", "de": "gemergt"},
    "info_branch_upstream_gone": {"en": "upstream gone", "de": "Upstream weg"},
    "info_branch_commit": {"en": "commit", "de": "Commit"},
    "info_remote_error": {
        "en": "Remote details unavailable: {e}",
        "de": "Remote-Details nicht verfügbar: {e}"},
    "info_fetch_failed": {"en": "last fetch failed", "de": "letzter Fetch fehlgeschlagen"},
    # Info-Ansicht: Navigation und Remote-Aktionen
    "info_footer_nav": {
        "en": " ↑/↓ or Tab select remote/branch · PgUp/PgDn scroll · Q/Esc close",
        "de": " ↑/↓ oder Tab Remote/Branch wählen · Bild↑/Bild↓ scrollen · Q/Esc schließen"},
    "info_footer_actions": {
        "en": " T test selected remote (read-only)",
        "de": " T gewähltes Remote prüfen (rein lesend)"},
    "info_footer_actions_branch": {
        "en": " Branch details are read-only",
        "de": " Branch-Details sind rein lesend"},
    "info_no_remotes": {"en": "This repository has no remote.",
                        "de": "Dieses Repo hat kein Remote."},
    "info_check_remote_only": {"en": "T tests remotes; a branch is local anyway.",
                               "de": "T prüft Remotes; ein Branch ist ohnehin lokal."},
    # Remote prüfen (T)
    "check_running": {"en": "Testing {r} …", "de": "Prüfe {r} …"},
    "check_ok": {"en": "{r} exists and answers ({n} branch(es) there).",
                 "de": "{r} existiert und antwortet ({n} Branch(es) dort)."},
    "check_empty": {"en": "{r} answers but has no branches yet (empty repository).",
                    "de": "{r} antwortet, hat aber noch keine Branches (leeres Repo)."},
    "check_gone": {
        "en": "{r}: reachable, but no repository there (or no access).",
        "de": "{r}: erreichbar, aber dort kein Repo (oder kein Zugriff)."},
    "check_auth": {"en": "{r}: server wants a login (credential helper or SSH key missing).",
                   "de": "{r}: Server verlangt einen Login (Credential-Helper/SSH-Key fehlt)."},
    "check_nokeychain": {
        "en": ("{r}: not measurable from this session — the credential helper reads the "
               "login keychain, which only the GUI session can open. The login itself is "
               "fine; check it with the GUI session of that machine."),
        "de": ("{r}: aus dieser Sitzung nicht messbar — der Credential-Helper liest den "
               "Login-Schlüsselbund, und den öffnet nur die GUI-Sitzung. Der Login selbst "
               "ist in Ordnung; prüfen in der GUI-Sitzung des Rechners.")},
    "check_hostkey": {
        "en": "{r}: host key unknown or changed — connect once in a terminal.",
        "de": "{r}: Hostschlüssel unbekannt/geändert — einmal im Terminal verbinden."},
    "check_dns": {"en": "{r}: host name does not resolve (no network or DNS).",
                  "de": "{r}: Hostname nicht auflösbar (kein Netz oder DNS)."},
    "check_unreachable": {
        "en": "{r}: no connection — offline, firewall, or server down.",
        "de": "{r}: keine Verbindung — offline, Firewall oder Server aus."},
    "check_server": {
        "en": "{r}: server error there — their problem, not your repository.",
        "de": "{r}: Serverfehler dort — deren Problem, nicht dein Repo."},
    "check_timeout": {"en": "{r}: no answer within {s}s — network or server too slow.",
                      "de": "{r}: keine Antwort in {s}s — Netz oder Server zu langsam."},
    "check_unsafe_refspec": {
        "en": "{r}: fetch blocked — its refspec can change refs outside refs/remotes/{r}/.",
        "de": "{r}: Fetch gesperrt — seine Refspec kann Refs außerhalb refs/remotes/{r}/ ändern."},
    "check_unsafe_url": {
        "en": "{r}: check blocked — its fetch address is ambiguous, secret-bearing, or uses an external remote helper.",
        "de": "{r}: Prüfung gesperrt — die Fetch-Adresse ist mehrdeutig, enthält Zugangsdaten oder nutzt einen externen Remote-Helper."},
    "check_changed": {
        "en": "{r}: remote configuration changed during fetch — the result is not trusted; tracking refs may already have changed.",
        "de": "{r}: Remote-Konfiguration änderte sich während des Fetchs — dem Ergebnis wird nicht vertraut; Tracking-Refs können bereits geändert sein."},
    "check_outcome_unknown": {
        "en": "{r}: fetch outcome unknown — tracking refs may already have changed; refresh before retrying.",
        "de": "{r}: Fetch-Ausgang unklar — Tracking-Refs können bereits geändert sein; vor dem Wiederholen neu einlesen."},
    "check_unknown": {"en": "{r}: unclear result — {e}", "de": "{r}: unklares Ergebnis — {e}"},
    # Stichworte für die Repo-Zeile (der ganze Satz steht auf der Info-Seite)
    "short_gone": {"en": "repository gone", "de": "Repo weg"},
    "short_auth": {"en": "login missing", "de": "Login fehlt"},
    "short_nokeychain": {"en": "keychain unavailable", "de": "Schlüsselbund unerreichbar"},
    "short_hostkey": {"en": "host key unknown", "de": "Hostschlüssel unbekannt"},
    "short_dns": {"en": "host not found", "de": "Host nicht gefunden"},
    "short_unreachable": {"en": "no connection", "de": "keine Verbindung"},
    "short_server": {"en": "server error", "de": "Serverfehler"},
    "short_timeout": {"en": "no answer", "de": "keine Antwort"},
    "short_unsafe_refspec": {"en": "unsafe fetch refspec", "de": "unsichere Fetch-Refspec"},
    "short_unsafe_url": {"en": "unsafe remote address", "de": "unsichere Remote-Adresse"},
    "short_changed": {"en": "remote changed", "de": "Remote geändert"},
    "short_outcome_unknown": {"en": "fetch outcome unknown",
                              "de": "Fetch-Ausgang unklar"},
    "short_unknown": {"en": "fetch failed", "de": "Fetch fehlgeschlagen"},
    # Befehlsprotokoll
    "cmdlog_title": {"en": "Commands this session ran",
                     "de": "In dieser Sitzung ausgeführte Befehle"},
    "cmdlog_cancelled": {"en": "not run — cancelled", "de": "nicht ausgeführt — abgebrochen"},
    "cmdlog_empty": {
        "en": "(none yet — actions like C, P, G and R are listed here)",
        "de": "(noch keine — Aktionen wie C, P, G und R stehen hier)"},
    "cmdlog_hint": {
        "en": "Every line is a real Git command; you can run it in a terminal yourself.",
        "de": "Jede Zeile ist ein echter Git-Befehl; genauso im Terminal ausführbar."},
    # main
    "not_a_dir": {"en": "Not a directory: {p}", "de": "Kein Ordner: {p}"},
    "git_timeout": {"en": "git timeout", "de": "git-Timeout"},
    "action_timeout": {
        "en": "git {cmd} took longer than {s}s and was cancelled "
              "(slow pre-commit hook or slow network?).",
        "de": "git {cmd} brauchte länger als {s}s und wurde abgebrochen "
              "(langsamer pre-commit-Hook oder langsames Netz?)."},
    "transfer_timeout": {
        "en": "git {cmd} took longer than {s}s; the outcome is unknown. Remote or "
              "tracking refs may have changed — fetch/check before retrying.",
        "de": "git {cmd} brauchte länger als {s}s; der Ausgang ist unklar. Remote- "
              "oder Tracking-Refs können geändert sein — vor dem Wiederholen fetchen/prüfen."},
    "demo_built": {"en": "Demo sandbox: {p}\n(fake repos; delete the folder when done)",
                   "de": "Demo-Sandbox: {p}\n(Fake-Repos; Ordner danach löschen)"},
}


def t(key: str, **kw) -> str:
    entry = TR.get(key, {})
    s = entry.get(UI_LANG) or entry.get("en") or key
    return s.format(**kw) if kw else s


def resolve_lang(cfg: dict, override: str | None = None) -> str:
    if override in ("en", "de"):
        return override
    v = (cfg.get("lang") or "").lower()
    if v in ("en", "de"):
        return v
    env = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
    return "de" if env.startswith("de") else "en"


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Config laden; fehlt sie, mit Defaults anlegen (selbsterklärender Start)."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # tiefe Kopie
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: cannot read {CONFIG_PATH} ({exc}) — using defaults.",
                  file=sys.stderr)
    else:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n")
    # App-Tasten intern immer groß (Tastendruck wird ebenfalls großgezogen).
    cfg["apps"] = {k.upper(): v for k, v in cfg.get("apps", {}).items()}
    return cfg


# ---------------------------------------------------------------------------
# Git-Datensammlung (reine Logik, testbar)
# ---------------------------------------------------------------------------

@dataclass
class RepoStatus:
    path: Path
    rel: str                      # Pfad relativ zum Scan-Start (Anzeigename)
    branch: str = "?"
    # Volle OID aus demselben Scan wie `branch`; None bedeutet belegten unborn
    # Branch, der leere String nur einen von Tests/Altaufrufern unbekannten Stand.
    head_oid: str | None = ""
    remote: str | None = None     # Name des erkannten Sync-Remotes (z.B. origin)
    remote_state: str = "ok"      # ok | no-remote | no-branch | detached | error
    ahead: int = 0
    behind: int = 0
    # Zusatz-Info: Stand gegenüber dem *konfigurierten Upstream*, falls das ein
    # ANDERER Remote als der Sync-Remote ist (typisch: github). So werden Commits
    # sichtbar, die zwar auf dem Sync-Remote, aber nie z.B. zu GitHub gepusht wurden.
    upstream: str | None = None   # z.B. "github/main"
    upstream_ahead: int = 0
    upstream_behind: int = 0
    remotes: list = field(default_factory=list)  # RemoteStatus, GitHub immer zuletzt
    modified: int = 0
    deleted: int = 0
    untracked: int = 0
    conflicts: int = 0            # ungemergte Dateien (z.B. nach stash apply)
    files: list[ChangedFile] = field(default_factory=list)
    stashes: list = field(default_factory=list)  # ["stash@{0} WIP ...", ...]
    error: str = ""
    # Die Repo-Zeile bekommt das Stichwort (`error`), die Info-Seite den ganzen
    # Satz (`error_long`) und Gits eigenen Wortlaut als Beweis (`error_detail`).
    error_long: str = ""
    error_detail: str = ""
    # Herkunft des Fehlers: True = ein Fetch übers Netz schlug fehl. Nur solche
    # Fehler überleben einen lokalen Refresh (carry_fetch_failure) — ein lokaler
    # Lesefehler wird dagegen bei jedem Refresh neu festgestellt oder ist weg.
    fetch_error: bool = False

    @property
    def dirty(self) -> bool:
        return bool(self.modified or self.deleted or self.untracked or self.conflicts)

    @property
    def clean_and_synced(self) -> bool:
        return (not self.dirty and not self.stashes and self.ahead == 0
                and self.behind == 0 and self.remote_state == "ok")

    def severity(self) -> int:
        """Sortierschlüssel: Problematisches nach oben."""
        if self.error:
            return 0
        if self.dirty or self.stashes:
            return 1
        if self.ahead or self.behind:
            return 2
        if self.remote_state != "ok":
            return 3
        return 4


@dataclass
class RemoteStatus:
    """Anzeigezustand eines Remotes für den aktuellen Branch.

    URLs bleiben absichtlich aus UI/JSON heraus. `public` wird ausschließlich aus
    der URL-Klasse abgeleitet; dadurch kann auch ein Remote namens `origin`
    verständlich und mit der GitHub-Sicherheitsstufe behandelt werden.
    """

    name: str
    public: bool = False
    mixed_public: bool = False
    is_sync: bool = False
    branch_exists: bool = False
    ahead: int = 0
    behind: int = 0
    fetch_fingerprint: str = ""
    fetch_fingerprints: list[str] = field(default_factory=list)
    push_fingerprints: list[str] = field(default_factory=list)
    target_mismatch: bool = False
    multiple_pushurls: bool = False
    fetch_refspecs_safe: bool = False
    fetch_refspec_fingerprint: str = ""
    branch_mapping_safe: bool = False
    fetch_url_safe: bool = False
    push_url_safe: bool = False
    fetch_failed: bool = False
    fetch_outcome: str = ""
    fetch_error_long: str = ""
    fetch_error_detail: str = ""

    @property
    def transfer_safe(self) -> bool:
        return not (self.mixed_public or self.target_mismatch or self.multiple_pushurls)

    def badge(self) -> str:
        arrows = ""
        if self.ahead:
            arrows += f"↑{self.ahead}"
        if self.behind:
            arrows += f"↓{self.behind}"
        if not self.branch_exists:
            arrows = "?"
        if self.fetch_failed:
            # ✘ heißt: der Stand daneben ist der letzte bekannte, nicht der aktuelle.
            arrows = f"✘{arrows}" if arrows else "✘"
        return f"{arrows} {self.name}" if arrows else self.name


@dataclass
class TransferCheck:
    """Deterministischer Preflight für genau einen Branch und einen Remote."""

    reason: str
    ahead: int = 0
    behind: int = 0
    remote_ref: str = ""
    commits: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    branch: str = ""
    head_oid: str = ""
    index_oid: str = ""
    worktree_fingerprint: str = ""
    fetch_fingerprint: str = ""
    push_fingerprint: str = ""
    remote_name: str = ""
    remote_config_signature: tuple = ()
    transfer_url: str = ""
    target_oid: str = ""

    @property
    def ready(self) -> bool:
        return self.reason == "ready"

    def approval_signature(self) -> tuple:
        return (self.branch, self.head_oid, self.index_oid, self.worktree_fingerprint,
                self.fetch_fingerprint, self.push_fingerprint, self.transfer_url,
                self.remote_name, self.remote_config_signature, self.target_oid,
                tuple(self.commits), tuple(self.files), self.ahead, self.behind)


@dataclass
class BranchInfo:
    """Ein lokaler Branch — der zweite Zustand, den Git nie überträgt und niemand sieht."""

    name: str
    is_head: bool = False
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    upstream_gone: bool = False   # Upstream war da, ist auf dem Remote aber weg
    oid: str = ""
    date: str = ""
    subject: str = ""
    merged: bool = False          # vollständig in HEAD enthalten


@dataclass(frozen=True)
class RemoteTarget:
    host: str
    repo_id: str
    fingerprint: str

    @property
    def is_github(self) -> bool:
        """Nur der exakte Host github.com bekommt die Public-Push-Klassifikation.

        Zentrale Stelle für diese Entscheidung: Produktionscode (Badges,
        Web-URLs) und der Helfer is_github_url() laufen beide hierüber.
        """
        return self.host == "github.com"


@dataclass
class RemoteConfig:
    name: str
    fetch_urls: list[str]
    push_urls: list[str]
    fetch_targets: list[RemoteTarget]
    push_targets: list[RemoteTarget]
    # Rohwerte aus remote.<name>.*. Fetches werden daraus einmal geprüft und
    # anschließend mit genau diesen Refspecs ausgeführt; die veränderbare Config
    # darf zwischen Schutzprüfung und Git-Aufruf nichts Neues einschleusen.
    settings: list[tuple[str, str]] = field(default_factory=list)
    fetch_invalid_reason: str = ""
    push_invalid_reason: str = ""

    @property
    def transfer_safe(self) -> bool:
        return (not self.fetch_invalid_reason and not self.push_invalid_reason
                and len(self.fetch_targets) == 1 and len(self.push_targets) == 1
                and self.fetch_targets[0] == self.push_targets[0])


def _valid_refspec_pattern(value: str) -> bool:
    """Git-Ref-Grammatik mit optional genau einem Refspec-Stern prüfen."""
    if (not value.startswith("refs/") or value.endswith("/")
            or value.endswith(".") or "//" in value or ".." in value
            or "@{" in value or value.count("*") > 1):
        return False
    if any(ord(char) < 32 or ord(char) == 127
           or char in " ~^:?[\\" for char in value):
        return False
    for component in value.split("/"):
        if (not component or component.startswith(".")
                or component.lower().endswith(".lock")):
            return False
    return True


def fetch_refspecs_safe(remote: RemoteConfig) -> bool:
    """Nur Tracking-Refs im Namensraum dieses Remotes aktualisieren lassen.

    Negative Refspecs und reine Quell-Refspecs schreiben keine Ziel-Ref. Jede
    ausdrueckliche Zielseite muss dagegen unter refs/remotes/<name>/ liegen.
    Der eigentliche Netz-Fetch schreibt zwar keinen Ziel-Ref mehr; die Prüfung
    belegt aber weiterhin, welchen Tracking-Ref gmf danach übernehmen darf.
    """
    if remote.fetch_invalid_reason:
        return False
    prefix = f"refs/remotes/{remote.name}/"
    specs = [raw for key, raw in remote.settings if key.lower() == "fetch"]
    if not specs:
        # Ohne explizite Refspec kann ein späterer Fetch nicht an den geprüften
        # Ziel-Namensraum gebunden werden. FETCH_HEAD allein wäre zwar harmlos,
        # aktualisierte aber auch keinen Stand für den Repo-Vergleich.
        return False
    tracking_update = False
    for raw in specs:
        spec = raw.strip()
        if spec != raw:
            return False
        if spec.startswith("^"):
            if (spec.startswith("^+") or ":" in spec
                    or not _valid_refspec_pattern(spec[1:])):
                return False
            continue
        spec = spec.removeprefix("+")
        if ":" not in spec:
            if not _valid_refspec_pattern(spec):
                return False
            continue
        if spec.count(":") != 1:
            return False
        source, destination = spec.split(":", 1)
        if (not _valid_refspec_pattern(source)
                or not _valid_refspec_pattern(destination)
                or source.count("*") != destination.count("*")
                or not destination.startswith(prefix)):
            return False
        tracking_update = True
    return tracking_update


def _ref_pattern_matches(pattern: str, ref: str) -> bool:
    """Eine Git-Refspec mit höchstens einem `*` gegen einen vollen Ref prüfen."""
    if "*" not in pattern:
        return pattern == ref
    before, after = pattern.split("*", 1)
    return ref.startswith(before) and ref.endswith(after) \
        and len(ref) >= len(before) + len(after)


def fetch_maps_branch_exactly(remote: RemoteConfig, branch: str) -> bool:
    """Belegt die erwartete Quelle für den Tracking-Ref dieses Branches.

    Ein sicherer Ziel-Namensraum allein reicht für einen Push nicht: Eine
    Refspec `dev:refs/remotes/origin/main` würde sonst Remote-dev als main
    anzeigen und später sogar in den lokalen main mergen.
    """
    if not fetch_refspecs_safe(remote):
        return False
    expected_source = f"refs/heads/{branch}"
    expected_destination = f"refs/remotes/{remote.name}/{branch}"
    negatives: list[str] = []
    sources: set[str] = set()
    for key, raw in remote.settings:
        if key.lower() != "fetch":
            continue
        spec = raw.strip()
        if spec.startswith("^"):
            negatives.append(spec[1:])
            continue
        spec = spec.removeprefix("+")
        if ":" not in spec:
            continue
        source, destination = spec.split(":", 1)
        if not _ref_pattern_matches(destination, expected_destination):
            continue
        if "*" in destination:
            before, after = destination.split("*", 1)
            middle = expected_destination[len(before):]
            if after:
                middle = middle[:-len(after)]
            source = source.replace("*", middle)
        sources.add(source)
    if any(_ref_pattern_matches(pattern, expected_source) for pattern in negatives):
        return False
    return sources == {expected_source}


def _argv_safe_remote_url(url: str) -> bool:
    """Nur eine unverändert darstellbare Remote-Adresse darf in argv landen."""
    if not url or display_remote_url(url) != url or terminal_text(url) != url:
        return False
    raw = str(url).strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*::", raw):
        return False
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme:
        return parsed.scheme.lower() in {
            "http", "https", "ssh", "git", "file", "git+ssh", "ssh+git",
        }
    # Kein Schema: lokaler Pfad oder die von Git eingebaute SCP-SSH-Syntax.
    return True


def remote_fetch_url(remote: RemoteConfig) -> str | None:
    """Die tatsächlich verwendete, normalisierte Fetch-Adresse oder None."""
    if (remote.fetch_invalid_reason or not remote.fetch_urls
            or not remote.fetch_targets):
        return None
    return (remote.fetch_targets[0].repo_id
            if remote.fetch_targets[0].host == "local"
            else remote.fetch_urls[0])


def remote_push_url(remote: RemoteConfig) -> str | None:
    """Die eindeutig verwendbare, normalisierte Push-Adresse oder None."""
    if (remote.push_invalid_reason or len(remote.push_urls) != 1
            or len(remote.push_targets) != 1):
        return None
    return (remote.push_targets[0].repo_id
            if remote.push_targets[0].host == "local"
            else remote.push_urls[0])


def remote_fetch_url_safe(remote: RemoteConfig) -> bool:
    url = remote_fetch_url(remote)
    return url is not None and _argv_safe_remote_url(url)


def remote_push_url_safe(remote: RemoteConfig) -> bool:
    url = remote_push_url(remote)
    return url is not None and _argv_safe_remote_url(url)


NO_GIT_HOOKS_ARGS = ("-c", "core.hooksPath=/dev/null")


def safe_update_ref_args(*args: str) -> tuple[str, ...]:
    """Interne Ref-CAS ohne vom Repo kontrollierte Nebenwirkungs-Hooks."""
    return (*NO_GIT_HOOKS_ARGS, "update-ref", *args)


class _PinnedRemoteURL(str):
    """Str-Markierung: `run_git` bindet genau diese URL gegen Rewrite-Regeln."""

    def __new__(cls, remote_url: str):
        alias = "gmf-pin-" + uuid.uuid4().hex + "://approved"
        value = super().__new__(cls, alias)
        value.remote_url = remote_url
        return value


def _pinned_url_config(url: str) -> tuple[tuple[str, ...], str]:
    """Eine einmalige, nicht weiter umschreibbare URL-Bindung erzeugen.

    `remote get-url` liefert bereits eine umgeschriebene Adresse. Übergäbe gmf
    sie danach nackt erneut an Git, könnte eine zweite Regel diese Adresse noch
    einmal auf ein anderes Ziel umleiten. Stattdessen sieht Git nur einen
    zufälligen Einmal-Alias; die pro Prozess gesetzte Regel bildet genau diesen
    Alias auf die freigegebene URL ab. Fremde Rewrite-Regeln kennen den Alias
    nicht. `run_git()` übergibt Schlüssel und Werte getrennt über
    `GIT_CONFIG_KEY/VALUE`, damit ein `=` in der URL nicht versehentlich
    Schlüssel und Wert trennt.
    """
    return (), _PinnedRemoteURL(url)


def approved_fetch_args(remote: RemoteConfig, branch: str,
                        source_oid: str) -> tuple[str, ...] | None:
    """Nur das freigegebene Branch-Objekt holen, ohne einen lokalen Ref zu setzen.

    Git Fetch dereferenziert Ziel-Symrefs. Deshalb bekommt der Netzaufruf
    absichtlich gar keine Ziel-Refspec; `fetch_remote_safely()` übernimmt den
    Tracking-Ref danach selbst mit `update-ref --no-deref`.
    """
    if not remote.fetch_urls or not remote.fetch_targets:
        return None
    fetch_url = remote_fetch_url(remote)
    if (not fetch_refspecs_safe(remote)
            or fetch_url is None or not _argv_safe_remote_url(fetch_url)
            or not fetch_maps_branch_exactly(remote, branch)
            or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", source_oid)):
        return None
    pin_config, pinned_url = _pinned_url_config(fetch_url)
    return (
        *pin_config, *NO_GIT_HOOKS_ARGS,
        "fetch", "--no-tags", "--no-prune-tags", "--no-recurse-submodules",
        "--no-write-fetch-head", "--quiet", "--jobs=1", "--refmap=", "--",
        pinned_url, source_oid,
    )


def fetch_remote_block_reason(remote: RemoteConfig, branch: str) -> str | None:
    """Den belegten Sicherheitsgrund nennen, bevor irgendein Netzaufruf läuft."""
    if remote.fetch_invalid_reason:
        return remote.fetch_invalid_reason
    if (branch in ("?", "(detached)")
            or not fetch_refspecs_safe(remote)
            or not fetch_maps_branch_exactly(remote, branch)
            or not remote.fetch_urls or not remote.fetch_targets):
        return "unsafe_refspec"
    fetch_url = remote_fetch_url(remote)
    if fetch_url is None or not _argv_safe_remote_url(fetch_url):
        return "unsafe_url"
    return None


def fetch_remote_safely(repo: Path, remote: RemoteConfig, branch: str,
                        timeout: int) -> subprocess.CompletedProcess:
    """Einen Branch holen und seinen Tracking-Ref ohne Symref-Dereferenz setzen."""
    def update_tracking(*args: str) -> subprocess.CompletedProcess:
        try:
            return run_git_logged(repo, *args, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Die Netzseite kann bereits erfolgreich gewesen und der lokale Ref
            # trotz Timeout schon geändert sein. Für die UI bleibt das deshalb
            # ein unklarer Fetch-Ausgang, kein sicher abgebrochenes update-ref.
            raise FetchTrackingTimeout(
                ["git", "-C", str(repo), "fetch"], exc.timeout,
                output=exc.output, stderr=exc.stderr) from None

    def config_matches() -> bool:
        current = read_remote_config_or_none(
            repo, timeout_config(timeout), remote.name)
        if current is None:
            return False
        return (tuple(current.fetch_urls), tuple(current.push_urls),
                tuple(current.settings), current.fetch_invalid_reason,
                current.push_invalid_reason) == (
                    tuple(remote.fetch_urls), tuple(remote.push_urls),
                    tuple(remote.settings), remote.fetch_invalid_reason,
                    remote.push_invalid_reason)

    def roll_back_tracking_change(source_oid: str | None, old_oid: str,
                                  old_existed: bool
                                  ) -> subprocess.CompletedProcess:
        if source_oid is None:
            # Unsere Löschung wiederherstellen, aber nur solange kein anderer
            # Prozess inzwischen einen neuen Ref angelegt hat.
            return update_tracking(*safe_update_ref_args(
                "--no-deref", destination, old_oid, "0" * len(old_oid)))
        if old_existed:
            return update_tracking(*safe_update_ref_args(
                "--no-deref", destination, old_oid, source_oid))
        return update_tracking(*safe_update_ref_args(
            "--no-deref", "-d", destination, source_oid))

    reason = fetch_remote_block_reason(remote, branch)
    if reason is not None:
        return subprocess.CompletedProcess(
            ["git", "fetch"], 128, "", reason)
    fetch_url = remote_fetch_url(remote)
    if fetch_url is None:
        return subprocess.CompletedProcess(
            ["git", "fetch"], 128, "", "unsafe_url")

    source_ref = f"refs/heads/{branch}"
    destination = f"refs/remotes/{remote.name}/{branch}"
    pin_config, pinned_url = _pinned_url_config(fetch_url)
    advertised = run_git(
        repo, *pin_config, "ls-remote", "--refs", "--", pinned_url,
        source_ref, timeout=timeout)
    if advertised.returncode != 0:
        return advertised
    rows = [line.split("\t", 1) for line in advertised.stdout.splitlines()
            if line.strip()]
    if any(len(row) != 2 for row in rows) or len(rows) > 1:
        return subprocess.CompletedProcess(
            advertised.args, 128, advertised.stdout, "ambiguous remote branch")
    source_oid = rows[0][0] if rows else None
    if source_oid is not None and not re.fullmatch(
            r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", source_oid):
        return subprocess.CompletedProcess(
            advertised.args, 128, advertised.stdout, "invalid remote object ID")

    old = run_git(
        repo, "rev-parse", "--verify", "-q", destination, timeout=timeout,
        env=RAW_OBJECT_ENV)
    if old.returncode not in (0, 1):
        return old
    old_oid = (old.stdout.strip() if old.returncode == 0
               else "0" * (len(source_oid) if source_oid else 40))
    if source_oid is None:
        if old.returncode == 1:
            return subprocess.CompletedProcess(advertised.args, 0, "", "")
        if not config_matches():
            raise RemoteConfigChangedError(remote.name)
        updated = update_tracking(*safe_update_ref_args(
            "--no-deref", "-d", destination, old_oid))
        if updated.returncode != 0 or config_matches():
            return updated
        rolled_back = roll_back_tracking_change(None, old_oid, True)
        if rolled_back.returncode != 0:
            raise OSError("fetch tracking rollback outcome is unknown")
        raise RemoteConfigChangedError(remote.name)

    args = approved_fetch_args(remote, branch, source_oid)
    if args is None:
        return subprocess.CompletedProcess(
            ["git", "fetch"], 128, "", "unsafe fetch configuration")
    fetched = run_git_logged(repo, *args, timeout=timeout, env=RAW_OBJECT_ENV)
    if fetched.returncode != 0:
        return fetched
    present = run_git(
        repo, "cat-file", "-e", source_oid + "^{commit}", timeout=timeout,
        env=RAW_OBJECT_ENV)
    if present.returncode != 0:
        return subprocess.CompletedProcess(
            present.args, 128, present.stdout, "fetched object is unavailable")
    if not config_matches():
        raise RemoteConfigChangedError(remote.name)
    updated = update_tracking(*safe_update_ref_args(
        "--no-deref", destination, source_oid, old_oid))
    if updated.returncode != 0 or config_matches():
        return updated
    rolled_back = roll_back_tracking_change(
        source_oid, old_oid, old.returncode == 0)
    if rolled_back.returncode != 0:
        raise OSError("fetch tracking rollback outcome is unknown")
    raise RemoteConfigChangedError(remote.name)


# Zwei-Buchstaben-Codes, die einen ungemergten Zustand (Merge-Konflikt) bedeuten.
# git status meldet solche Dateien z.B. nach einem `stash apply` mit Konflikt.
UNMERGED_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


class ChangedFile(NamedTuple):
    """Eine geänderte Datei — einmal fürs Auge, einmal für Git.

    ``code`` ist die Vereinfachung für die Anzeige (M/D/U/C, siehe
    parse_porcelain). ``xy`` ist das rohe Statusfeld von ``git status
    --porcelain``: erstes Zeichen der Index (was gestaget ist), zweites Zeichen
    der Arbeitsbaum (was daneben noch geändert ist). Erst daran ist erkennbar,
    ob eine Änderung nur im Arbeitsbaum liegt (`` M``), nur im Index (``M ``)
    oder in beiden (``MM``) — und ob es eine neu hinzugefügte Datei (``A ``) ist.
    """

    code: str
    path: str
    xy: str
    # Ziel und Quelle eines Porcelain-Renames tragen dieselbe Paar-ID. Der
    # Commit-Wizard darf sie dadurch nur gemeinsam an- oder abwaehlen; das rohe
    # xy allein reicht bei mehreren gleichzeitigen Renames nicht zur Zuordnung.
    rename_group: str = ""


def parse_porcelain(output: str) -> tuple[int, int, int, int, list[ChangedFile]]:
    """NUL-getrenntes ``git status --porcelain=v1 -z`` auswerten.

    -> (modified, deleted, untracked, conflicts, dateien).
    Vereinfachung fürs Auge: Konflikt = C, Untracked = U, Gelöschtes = D, jede
    andere Änderung (modified/added/renamed/…) = M. Konflikte werden ZUERST
    geprüft, sonst würde z.B. `UD` fälschlich als Löschung zählen.

    ``-z`` ist für die Commit-Hilfe entscheidend: Ohne diese Option setzt Git
    Pfade mit Umlauten oder Steuerzeichen in Anführungszeichen und maskiert sie.
    Diese Anzeigeform ist kein gültiger Pfad für ein späteres ``git add``.
    Rename-/Copy-Einträge besitzen bei ``-z`` ein zweites Feld mit dem alten
    Namen. Ein Rename ist Ziel UND Quelle: der Zielpfad erscheint als ``M``, der
    Quellpfad zusätzlich als ``D`` — sonst würde die Commit-Hilfe nur den
    Zielpfad stagen, den Rename als Kopie committen und die Löschung des alten
    Namens bliebe im Repo zurück. Bei einer Kopie (``C``) bleibt die Quelle
    unverändert und bekommt keinen Eintrag.

    Beide Hälften eines Renames tragen dasselbe rohe ``xy`` (also ``R…``). Nur
    daran ist später erkennbar, dass sie zusammengehören: Wer eine der beiden
    Hälften allein zurücksetzt, lässt die halbe Umbenennung im Repo stehen.
    """
    m = d = u = c = 0
    files: list[ChangedFile] = []
    fields = output.split("\0")
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if not record:
            continue
        xy, path = record[:2], record[3:]
        source = None
        if "R" in xy or "C" in xy:
            # Bei -z folgt nach dem Zielpfad noch der Quellpfad.
            source = fields[i] if i < len(fields) else None
            i += 1
        if xy in UNMERGED_CODES:
            c += 1
            files.append(ChangedFile("C", path, xy))
        elif xy == "??":
            u += 1
            files.append(ChangedFile("U", path, xy))
        elif "D" in xy:
            d += 1
            files.append(ChangedFile("D", path, xy))
        else:
            m += 1
            files.append(ChangedFile("M", path, xy))
        if source and "R" in xy:
            group = hashlib.sha256(
                (path + "\0" + source).encode("utf-8", "surrogateescape")
            ).hexdigest()
            files[-1] = files[-1]._replace(rename_group=group)
            d += 1
            files.append(ChangedFile("D", source, xy, rename_group=group))
    return m, d, u, c, files


class CommitSafetyError(RuntimeError):
    pass


class CommitAdoptionError(CommitSafetyError):
    """Der Commit steht bereits im Verlauf, nur die Indexübernahme scheiterte."""

    def __init__(self, committed_head: str, cause: Exception):
        super().__init__(str(cause))
        self.committed_head = committed_head


class CommitOutcomeUnknownError(RuntimeError):
    """HEAD kann bewegt sein; Erfolg oder sicherer Rollback ist nicht belegt."""


class RemoteConfigChangedError(RuntimeError):
    """Ein Fetch wurde wegen geänderter Remote-Konfiguration zurückgenommen."""


class FetchTrackingTimeout(subprocess.TimeoutExpired):
    """Ein Tracking-Ref-CAS kann trotz Timeout bereits gewirkt haben."""


def _path_signature(path: Path) -> tuple | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_mode,
            info.st_size, info.st_mtime_ns)


_SIGNATURE_UNSET = object()


def _real_index_signature(repo: Path, timeout: int) -> tuple | None:
    index_r = _required_git(repo, "rev-parse", "--git-path", "index", timeout=timeout)
    index = Path(index_r.stdout.strip())
    if not index.is_absolute():
        index = repo / index
    signature = _path_signature(index)
    if signature is None:
        return None
    try:
        content_hash = hashlib.sha256(index.read_bytes()).hexdigest()
    except OSError as e:
        raise CommitSafetyError("cannot read Git index: %s" % e) from e
    return signature + (content_hash,)


def has_unmerged_entries(repo: Path, timeout: int) -> bool:
    diff = _required_git(repo, "diff", "--name-only", "--diff-filter=U", "-z",
                         timeout=timeout)
    index = _required_git(repo, "ls-files", "-u", "-z", timeout=timeout)
    return bool(diff.stdout or index.stdout)


def repository_operation_in_progress(repo: Path, timeout: int) -> bool:
    """True, wenn ein Commit Teil einer laufenden Git-Sequenz waere.

    Ein konfliktfrei vorbereiteter Merge hat keine ungemergten Indexeintraege,
    aber `git commit` erzeugt trotzdem einen Merge-Commit. Der temporaere
    Teilbaum der Commit-Hilfe darf einen solchen Commit niemals abschliessen.
    Dasselbe gilt fuer Cherry-Pick, Revert, Rebase und den Sequencer.
    """
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                   "rebase-merge", "rebase-apply", "sequencer"):
        result = _required_git(repo, "rev-parse", "--git-path", marker,
                               timeout=timeout)
        path = Path(result.stdout.strip())
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            return True
    return False


def commit_selected(repo: Path, paths: list[str], message: str, timeout: int,
                    commit_timeout: int | None = None, *,
                    expected_head=_SIGNATURE_UNSET,
                    expected_ref=_SIGNATURE_UNSET
                    ) -> subprocess.CompletedProcess:
    """Commit exactly paths through a temporary index; preserve the user's index bytes.

    `timeout` gilt für die schnellen Vorbereitungsschritte (Index lesen, Baum
    schreiben). `commit_timeout` gilt nur für den Commit selbst, weil dort der
    pre-commit-Hook des Repos läuft; ohne Angabe bleibt es beim selben Wert.
    """
    if not paths:
        raise CommitSafetyError("no approved paths")
    if has_unmerged_entries(repo, timeout):
        raise CommitSafetyError("merge conflicts exist")
    if repository_operation_in_progress(repo, timeout):
        raise CommitSafetyError("a merge or sequencer operation is active")
    real_before = _real_index_signature(repo, timeout)
    head_before = current_head(repo, timeout)
    head_ref_before = current_symbolic_head_ref(repo, timeout)
    if head_ref_before is None:
        raise CommitSafetyError("detached HEAD")
    if ((expected_head is not _SIGNATURE_UNSET
         and head_before != expected_head)
            or (expected_ref is not _SIGNATURE_UNSET
                and head_ref_before != expected_ref)):
        raise CommitSafetyError("HEAD changed after UI approval")
    approved = set(paths)
    with tempfile.TemporaryDirectory(prefix="gmf-index-") as temp:
        index_path = str(Path(temp) / "index")
        env = {
            **RAW_OBJECT_ENV,
            "GIT_INDEX_FILE": index_path,
            "GIT_LITERAL_PATHSPECS": "1",
        }
        if head_before is None:
            # Frisches Repo ohne ersten Commit: HEAD existiert noch nicht, der
            # temporäre Index startet leer statt vom HEAD-Baum.
            _required_git(repo, *NO_GIT_HOOKS_ARGS, "read-tree", "--empty",
                          timeout=timeout, env=env)
        else:
            # Nicht den beweglichen Namen HEAD erneut lesen: Ein kurzzeitiger
            # Checkout könnte sonst fremde, nicht freigegebene Baum-Einträge in
            # den temporären Index bringen und vor dem späten Guard zurückwechseln.
            _required_git(repo, *NO_GIT_HOOKS_ARGS, "read-tree", head_before,
                          timeout=timeout, env=env)
        # Interna des temporären Index werden NICHT protokolliert: ein kopiertes
        # `git add`/`git commit` liefe im Terminal gegen den ECHTEN Index (dem
        # Protokoll fehlt das entscheidende GIT_INDEX_FILE) und könnte dort
        # fremdes Staging mitcommitten. Statt dessen wird unten der
        # terminal-äquivalente Befehl `git commit -m … -- <pfade>` protokolliert.
        staged = _stage_approved(repo, paths, timeout, env)
        if staged.returncode != 0:
            return staged
        # --no-renames: die Freigabeprüfung vergleicht ROHE Pfade. Gits
        # Rename-Erkennung würde Ziel+Quelle eines Renames zu EINEM Eintrag
        # zusammenfassen und die approvte Quell-Löschung scheinbar verschwinden
        # lassen.
        names = _required_git(repo, "diff", "--cached", "--no-renames",
                              "--name-only", "-z", "--", timeout=timeout, env=env)
        actual = {path for path in names.stdout.split("\0") if path}
        if actual != approved:
            raise CommitSafetyError("temporary index differs from approved paths")
        tree_before = _required_git(repo, "write-tree", timeout=timeout, env=env).stdout.strip()
        if has_unmerged_entries(repo, timeout):
            raise CommitSafetyError("merge conflicts appeared before commit")
        if _real_index_signature(repo, timeout) != real_before:
            raise CommitSafetyError("Git index changed during approval")
        # Stage once more and compare the complete tree to catch worktree races.
        restaged = _stage_approved(repo, paths, timeout, env)
        if restaged.returncode != 0:
            raise GitReadError("git add failed (exit %d)" % restaged.returncode)
        tree_after = _required_git(repo, "write-tree", timeout=timeout, env=env).stdout.strip()
        if tree_after != tree_before:
            raise CommitSafetyError("approved files changed during commit preparation")
        # Der echte Index-Lock verhindert ab hier, dass Merge, Cherry-pick,
        # Revert oder Checkout ihre gemeinsame Sequenz NACH der Markerprüfung
        # beginnen. `git commit` arbeitet mit dem separaten GIT_INDEX_FILE und
        # braucht deshalb ausschließlich dessen eigenen Index-Lock. HEAD kann
        # Git selbst nur aktualisieren, wenn wir HEAD.lock noch nicht halten;
        # die Branch-Identität wird direkt vor und nach dem Commit geprüft.
        guard_paths = (
            Path(str(_git_path(repo, "index", timeout)) + ".lock"),
        )
        guards: list[tuple[Path, int | None, tuple[int, int]]] = []
        commit_started = False
        committed_head = None
        try:
            for guard_path in guard_paths:
                fd, identity = _acquire_git_lock(guard_path)
                guards.append((guard_path, fd, identity))
            if has_unmerged_entries(repo, timeout):
                raise CommitSafetyError("merge conflicts appeared before commit")
            if repository_operation_in_progress(repo, timeout):
                raise CommitSafetyError(
                    "a merge or sequencer operation appeared before commit")
            if _real_index_signature(repo, timeout) != real_before:
                raise CommitSafetyError("Git index changed during approval")
            if current_head(repo, timeout) != head_before:
                raise CommitSafetyError("HEAD changed during commit preparation")
            if current_symbolic_head_ref(repo, timeout) != head_ref_before:
                raise CommitSafetyError("HEAD branch changed during commit preparation")
            literal_paths = tuple(":(literal)" + path for path in paths)
            logged_args = ("commit", "-m", message, "--", *literal_paths)
            reflog_action = "gmf-" + uuid.uuid4().hex
            commit_env = {
                **RAW_OBJECT_ENV,
                "GIT_INDEX_FILE": index_path,
                "GIT_REFLOG_ACTION": reflog_action,
            }
            try:
                commit_started = True
                result = run_git(
                    repo, "-c", "core.logAllRefUpdates=true",
                    "commit", "-m", message, env=commit_env,
                    timeout=timeout if commit_timeout is None else commit_timeout)
            except subprocess.TimeoutExpired as exc:
                log_command(repo, logged_args, returncode=None)
                # Diese Attribute markieren ausschließlich einen Timeout IM
                # eigentlichen Commit-Aufruf. Frühere Timeouts besitzen sie
                # nicht und dürfen im TUI-Pfad keinen Commit vortäuschen.
                exc.approved_tree = tree_after
                exc.approved_head = head_before
                exc.approved_ref = head_ref_before
                exc.approved_paths = tuple(paths)
                exc.reflog_action = reflog_action
                raise
            except OSError as exc:
                # Popen kann bereits gestartet sein und communicate() erst nach
                # einem wirksamen Commit scheitern. Ohne verlässlichen Exit-Code
                # darf die UI niemals zu einem Wiederholungsversuch einladen.
                log_command(repo, logged_args, returncode=None)
                raise CommitOutcomeUnknownError(str(exc)) from exc
            log_command(repo, logged_args, result.returncode)
            if result.returncode != 0:
                try:
                    reflog_matches = _commit_reflog_matches(
                        repo, reflog_action, timeout)
                    moved = (current_head(repo, timeout) != head_before
                             or current_symbolic_head_ref(repo, timeout)
                             != head_ref_before)
                except (subprocess.TimeoutExpired, GitReadError,
                        CommitSafetyError, OSError) as exc:
                    raise CommitOutcomeUnknownError(str(exc)) from exc
                if reflog_matches or moved:
                    # Ein Hook kann selbst einen Commit schreiben und danach
                    # den äußeren Commit mit Exit != 0 scheitern lassen. Er kann
                    # HEAD sogar zurückstellen; der zufällige Reflog-Marker
                    # belegt die zwischenzeitliche Branch-Mutation trotzdem.
                    raise CommitOutcomeUnknownError(
                        "repository changed although git commit reported failure")
            try:
                committed_head = None
                if result.returncode == 0:
                    try:
                        committed_head, committed_ref = _commit_reflog_proof(
                            repo, reflog_action, timeout)
                    except CommitSafetyError as exc:
                        # Ohne Reflog-Nachweis kennen wir die eigene Commit-OID
                        # nicht sicher. Git hat dennoch bereits Erfolg gemeldet.
                        raise CommitOutcomeUnknownError(str(exc)) from exc
                    committed_head = _verify_hooks_kept_approved_tree(
                        repo, head_before, tree_after, timeout, head_ref_before,
                        committed_head, committed_ref)
                try:
                    real_after = _real_index_signature(repo, timeout)
                except (CommitSafetyError, GitReadError, OSError,
                        subprocess.TimeoutExpired) as exc:
                    if result.returncode == 0:
                        raise CommitAdoptionError(committed_head, exc) from exc
                    raise
                if real_after != real_before:
                    error = CommitSafetyError(
                        "Git changed the real index unexpectedly")
                    if result.returncode == 0:
                        raise CommitAdoptionError(committed_head, error)
                    raise error
                if result.returncode == 0:
                    try:
                        adopt_commit_in_real_index(
                            repo, paths, committed_head, head_ref_before,
                            real_before, timeout, _held_guards=guards)
                    except (CommitSafetyError, GitReadError, OSError,
                            subprocess.TimeoutExpired) as exc:
                        raise CommitAdoptionError(committed_head, exc) from exc
                    # Der Aufrufer braucht die ungekürzte neue OID für einen
                    # sicheren Rückgängig-Befehl beim Erst-Commit.
                    result.committed_head = committed_head
                    result.approved_head = head_before
                    result.approved_ref = head_ref_before
                return result
            except (subprocess.TimeoutExpired, GitReadError, OSError) as exc:
                if result.returncode == 0:
                    raise CommitOutcomeUnknownError(str(exc)) from exc
                raise
        finally:
            pending_exception = sys.exc_info()[0] is not None
            cleanup_error = None
            for guard_path, fd, identity in reversed(guards):
                try:
                    _release_owned_git_lock(guard_path, fd, identity)
                except OSError as exc:
                    cleanup_error = cleanup_error or exc
            if cleanup_error is not None and not pending_exception:
                if commit_started:
                    # Der Commit kann bereits existieren; selbst ein Fehler
                    # beim bloßen Schließen/Löschen des Guards darf daraus nie
                    # die irreführende Aussage "Commit failed" machen.
                    raise CommitOutcomeUnknownError(
                        str(cleanup_error)) from cleanup_error
                raise CommitSafetyError(str(cleanup_error)) from cleanup_error


def _stage_approved(repo: Path, paths: list[str], timeout: int,
                    env: dict) -> subprocess.CompletedProcess:
    """Freigegebene Pfade in den temporären Index stagen — auch Löschungen.

    `git add` kann eine Löschung nur stagen, solange der Pfad noch im Index
    steht. Beim zweiten Race-Check-Staging ist er dort bereits entfernt und im
    Arbeitsbaum fehlt er ebenfalls — ein nacktes `git add` bräche dann mit
    "pathspec did not match" ab. Genau daran konnte die Commit-Hilfe Löschungen
    (und damit die Quellseite eines Renames) nie committen. Fehlende Pfade
    werden deshalb über `git rm --cached --ignore-unmatch` als Löschung gestagt;
    vorhandene normal über `git add`. Taucht ein Pfad zwischen den beiden
    Staging-Läufen auf oder verschwindet er, ändert sich der Baum — das fängt
    der write-tree-Vergleich des Aufrufers wie bisher ab.
    """
    present: list[str] = []
    missing: list[str] = []
    for path in paths:
        (present if os.path.lexists(repo / path) else missing).append(path)
    result = subprocess.CompletedProcess(["git", "add"], 0, "", "")
    if present:
        result = run_git(repo, *NO_GIT_HOOKS_ARGS, "add", "--", *present,
                         timeout=timeout, env=env)
        if result.returncode != 0:
            return result
    if missing:
        result = run_git(
            repo, *NO_GIT_HOOKS_ARGS, "rm", "--cached", "-q",
            "--ignore-unmatch", "--", *missing, timeout=timeout, env=env)
        if result.returncode != 0:
            return result
    return result


def _commit_reflog_matches(repo: Path, action: str,
                           timeout: int) -> list[tuple[str, str]]:
    """Alle Branch-Reflog-Einträge des zufälligen Commit-Markers lesen."""
    result = _required_git(
        repo, "reflog", "show", "--all", "--format=%H%x00%gD%x00%gs",
        "--fixed-strings", f"--grep-reflog={action}", timeout=timeout,
        env=RAW_OBJECT_ENV)
    matches: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split("\0", 2)
        if len(fields) != 3 or not fields[2].startswith(action + ":"):
            continue
        selector = fields[1]
        ref = selector.rsplit("@{", 1)[0]
        if fields[0] and (ref == "HEAD" or ref.startswith("refs/heads/")):
            matches.append((fields[0], ref))
    return matches


def _commit_reflog_proof(repo: Path, action: str,
                         timeout: int) -> tuple[str, str]:
    """Die von diesem ``git commit`` geschriebene OID und Ref eindeutig lesen.

    Der zufällige ``GIT_REFLOG_ACTION`` steht nur für diesen Aufruf im Reflog.
    Dadurch bleibt ein späterer fremder Commit unterscheidbar, selbst wenn er
    den Branch noch vor unserer Nachprüfung weiterbewegt.
    """
    matches = [(oid, ref) for oid, ref in _commit_reflog_matches(
        repo, action, timeout) if ref.startswith("refs/heads/")]
    if len(matches) != 1:
        raise CommitSafetyError(
            "cannot identify the committed branch safely; check git log")
    return matches[0]


def _verify_hooks_kept_approved_tree(repo: Path, head_before: str | None,
                                     approved_tree: str, timeout: int,
                                     approved_ref: str, committed_head: str,
                                     committed_ref: str) -> str:
    """Nach dem Commit prüfen, dass kein Hook den freigegebenen Baum verändert hat.

    `git commit` erbt GIT_INDEX_FILE — ein pre-commit-Hook kann darüber mit
    `git add` zusätzliche, bewusst NICHT freigegebene Pfade in den temporären
    Index stagen, und Git committet dann diesen erweiterten Baum. Das bräche die
    Zusage, exakt die freigegebenen Pfade zu committen (schlimmstenfalls landet
    eine abgewählte vertrauliche Datei im Commit). Prüfen lässt sich das erst
    NACH dem Commit. Weicht der Baum ab, wird der eben erzeugte, noch nicht als
    Erfolg gemeldete Commit atomar zurückgenommen: `update-ref` mit
    Alt-Wert-Prüfung bewegt den Branch nur dann, wenn er noch exakt auf unserem
    Commit steht — fremde Commits kann das nicht treffen.
    """
    raw_env = RAW_OBJECT_ENV
    head_now = _required_git(repo, "rev-parse", "HEAD", timeout=timeout,
                             env=raw_env).stdout.strip()
    actual_ref = current_symbolic_head_ref(repo, timeout)
    approved_now = run_git(
        repo, "rev-parse", "--verify", "-q", approved_ref,
        timeout=timeout, env=raw_env)
    approved_oid = approved_now.stdout.strip() if approved_now.returncode == 0 else None

    def rollback_commit_ref(restore_oid: str | None) -> subprocess.CompletedProcess:
        if restore_oid is None:
            args = safe_update_ref_args(
                "--no-deref", "-d", committed_ref, committed_head)
        else:
            args = safe_update_ref_args(
                "--no-deref", committed_ref, restore_oid, committed_head)
        return run_git_logged(repo, *args, timeout=timeout)

    actual_parents = commit_parents(repo, committed_head, timeout)
    if committed_ref != approved_ref:
        # Der Commit landete auf einem anderen Branch. Dessen vorherige Spitze
        # ist der eindeutige Eltern-Commit des dort erzeugten Normalcommits —
        # NICHT `head_before` des freigegebenen Branches. Bei mehreren Eltern
        # gibt es keinen sicheren Rücksetzpunkt und wir mutieren gar nichts.
        if len(actual_parents) > 1:
            raise CommitOutcomeUnknownError(
                "commit landed on another branch with ambiguous parents")
        restore_oid = actual_parents[0] if actual_parents else None
        if rollback_commit_ref(restore_oid).returncode == 0:
            raise CommitSafetyError(
                "commit landed on another branch and was rolled back")
        raise CommitOutcomeUnknownError(
            "commit landed on another branch and could not be rolled back")
    expected_parents = [] if head_before is None else [head_before]
    if actual_parents != expected_parents:
        if len(actual_parents) > 1:
            raise CommitOutcomeUnknownError(
                "the commit has ambiguous parents and was not rolled back")
        restore_oid = actual_parents[0] if actual_parents else None
        if rollback_commit_ref(restore_oid).returncode == 0:
            raise CommitSafetyError(
                "the commit has unexpected parents and was rolled back")
        raise CommitOutcomeUnknownError(
            "the commit has unexpected parents and could not be rolled back")
    committed_tree = _required_git(
        repo, "rev-parse", committed_head + "^{tree}", timeout=timeout,
        env=raw_env).stdout.strip()
    if committed_tree != approved_tree:
        if rollback_commit_ref(head_before).returncode == 0:
            raise CommitSafetyError(
                "a hook changed the approved files; the commit was rolled back")
        raise CommitOutcomeUnknownError(
            "a hook changed the committed tree and rollback failed")
    if (actual_ref != approved_ref or approved_oid != committed_head
            or head_now != committed_head):
        raise CommitOutcomeUnknownError(
            "HEAD branch changed after commit; commit remains on the approved branch")
    return committed_head


def finish_interrupted_commit(repo: Path, head_before: str | None,
                              paths: list[str], timeout: int,
                              approved_tree: str | None = None,
                              approved_ref: str | None = None,
                              reflog_action: str | None = None) -> bool:
    """Nach einem Commit-Timeout klären, ob der Commit doch entstanden ist.

    `git commit` kann den Commit längst geschrieben haben und erst danach — etwa
    in einem hängenden post-commit-Hook — in den Timeout laufen. Dann existiert
    der Commit, aber der echte Index wurde noch nicht nachgezogen: `git status`
    (und damit gmf) zeigte die committeten Dateien weiter als geändert.
    Rückgabe: True, wenn ein neuer Commit existiert.

    `approved_tree` ist der vor dem Commit festgeschriebene Baum (aus der
    Timeout-Ausnahme von commit_selected). Vollständige Elternliste und Baum
    werden damit nur lesend geprüft. Anders als im synchronen Normalweg darf
    dieser Pfad weder den Index nachziehen noch einen abweichenden Commit
    zurückrollen: Nach dem Timeout lässt sich nicht beweisen, ob er von gmf oder
    einem parallelen Programm kam. Ohne freigegebenen Baum wird deshalb nur ein
    geänderter HEAD gemeldet, aber nicht als inhaltlich freigegeben behandelt.
    """
    # Timeouts absichtlich unverändert weiterreichen: Der TUI-Aufrufer zeigt
    # dafür den unklaren Commit-Ausgang an. Eine Umwandlung in GitReadError
    # würde fälschlich wie ein gewöhnlicher Commit-Fehler aussehen.
    head_after = current_head(repo, timeout)
    if head_after is None or head_after == head_before:
        if reflog_action is not None:
            # Ein Hook kann auf einem anderen Branch committen, HEAD danach auf
            # den Ausgangsstand zurückstellen und erst dann hängen. Der zufällige
            # Marker belegt diese Mutation, auch wenn HEAD unverändert aussieht.
            if _commit_reflog_matches(repo, reflog_action, timeout):
                raise CommitSafetyError(
                    "a hook commit exists on another branch; check git log")
        return False
    if (approved_ref is not None
            and current_symbolic_head_ref(repo, timeout) != approved_ref):
        raise CommitSafetyError(
            "HEAD branch changed during interrupted commit; check git log")
    if approved_tree is not None:
        # Zuerst nur lesend pruefen. `_verify_hooks_kept_approved_tree()` kann
        # zurueckrollen und ist hier deshalb absichtlich tabu.
        expected_parents = [] if head_before is None else [head_before]
        if commit_parents(repo, head_after, timeout) != expected_parents:
            raise CommitSafetyError(
                "the new commit has unexpected parents; check git log")
        committed_tree = _required_git(
            repo, "rev-parse", head_after + "^{tree}", timeout=timeout,
            env=RAW_OBJECT_ENV).stdout.strip()
        if committed_tree != approved_tree:
            raise CommitSafetyError(
                "the new commit does not match the approved tree; check git log")
        # Selbst Basis+Baum beweisen nach dem Timeout nicht, dass dieser Commit
        # von unserem inzwischen beendeten Prozess stammt. Deshalb keine
        # Indexmutation; der Status bleibt bis zur manuellen Pruefung ehrlich.
        return True
    # Ohne freigegebenen Baum ist nicht einmal der Inhalt belastbar zuordenbar.
    # Auch dann nur melden, niemals Index oder HEAD verändern.
    return True


def _git_path(repo: Path, name: str, timeout: int) -> Path:
    """Einen von Git bestimmten Verwaltungs-Pfad absolut zurückgeben."""
    result = _required_git(repo, "rev-parse", "--git-path", name, timeout=timeout)
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else repo / path


def _acquire_git_lock(path: Path) -> tuple[int, tuple[int, int]]:
    """Eine Git-Lockdatei ohne Warten anlegen und ihre Identität festhalten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CommitSafetyError("cannot lock %s: %s" % (path.name, exc)) from exc
    info = os.fstat(fd)
    return fd, (info.st_dev, info.st_ino)


def _release_owned_git_lock(path: Path, fd: int | None,
                            identity: tuple[int, int]) -> None:
    """Nur die von diesem Prozess angelegte, noch identische Lockdatei lösen."""
    if fd is not None:
        os.close(fd)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (info.st_dev, info.st_ino) == identity:
        path.unlink()


def adopt_commit_in_real_index(repo: Path, paths: list[str],
                               committed_head: str, approved_ref: str,
                               expected_index_signature: tuple | None,
                               timeout: int, *,
                               _held_guards: list[tuple[
                                   Path, int | None, tuple[int, int]
                               ]] | None = None) -> None:
    """Den echten Index für die committeten Pfade auf den verifizierten Commit ziehen.

    Ohne diesen Schritt bleibt der echte Index auf dem Stand von vor dem Commit
    stehen: Er zeigt für die eben committete Datei noch den alten Inhalt. `git
    status` vergleicht Arbeitsbaum und Index gegen HEAD und meldet die Datei
    deshalb weiter als geändert (`MM`) — obwohl der Commit einwandfrei ist. Genau
    das tut auch Git selbst nach einem `git commit -- <pfad>`.

    `git reset` klingt nach mehr, als es hier ist: In der Pfad-Form (mit `--`)
    fasst es ausschließlich diese Index-Einträge an — nie den Arbeitsbaum, nie
    einen Commit und nie die übrigen, bewusst gestageten Änderungen.
    """
    # Drei gewöhnliche Git-Locks schließen die Lücke zwischen Zustandsprüfung
    # und Indexmutation: Checkout braucht index.lock/HEAD.lock, update-ref den
    # Branch-Lock. Die Pfade werden zuerst in einem zweiten temporären Index
    # aktualisiert; erst nach erneuter Prüfung ersetzt dessen Inhalt atomar den
    # echten Index. Ein Fehler lässt den bereits geschriebenen Commit bestehen,
    # verändert aber weder einen fremden Index noch einen fremden Branch.
    index = _git_path(repo, "index", timeout)
    head = _git_path(repo, "HEAD", timeout)
    branch = _git_path(repo, approved_ref, timeout)
    lock_paths = (Path(str(index) + ".lock"), Path(str(head) + ".lock"),
                  Path(str(branch) + ".lock"))
    external_guards = _held_guards is not None
    held = _held_guards if _held_guards is not None else []
    temp_index: Path | None = None
    try:
        if external_guards:
            if [entry[0] for entry in held] != list(lock_paths[:1]):
                raise CommitSafetyError("commit guard identity changed")
            lock_targets = lock_paths[1:]
        else:
            lock_targets = lock_paths
        for lock_path in lock_targets:
            fd, identity = _acquire_git_lock(lock_path)
            held.append((lock_path, fd, identity))
        index_fd = held[0][1]
        if index_fd is None:  # ausschließlich für den Typprüfer
            raise CommitSafetyError("lost index lock")
        if _real_index_signature(repo, timeout) != expected_index_signature:
            raise CommitSafetyError("Git index changed before real-index adoption")
        if (current_head(repo, timeout) != committed_head
                or current_symbolic_head_ref(repo, timeout) != approved_ref):
            raise CommitSafetyError("HEAD changed before real-index adoption")

        temp_fd, temp_name = tempfile.mkstemp(
            prefix="gmf-adopt-index-", dir=index.parent)
        os.close(temp_fd)
        temp_index = Path(temp_name)
        if index.exists():
            temp_index.write_bytes(index.read_bytes())
        else:
            temp_index.unlink()
            empty = run_git(
                repo, *NO_GIT_HOOKS_ARGS, "read-tree", "--empty",
                timeout=timeout,
                env={**RAW_OBJECT_ENV, "GIT_INDEX_FILE": str(temp_index)})
            if empty.returncode != 0:
                raise GitReadError("git read-tree failed (exit %d)" % empty.returncode)
        updated = run_git(
            repo, *NO_GIT_HOOKS_ARGS, "reset", "-q", committed_head,
            "--", *paths,
            timeout=timeout,
            env={**RAW_OBJECT_ENV,
                 "GIT_INDEX_FILE": str(temp_index),
                 "GIT_LITERAL_PATHSPECS": "1",
                 })
        if updated.returncode != 0:
            raise GitReadError("git reset failed (exit %d)" % updated.returncode)
        if (current_head(repo, timeout) != committed_head
                or current_symbolic_head_ref(repo, timeout) != approved_ref):
            raise CommitSafetyError("HEAD changed during real-index adoption")

        content = temp_index.read_bytes()
        os.ftruncate(index_fd, 0)
        offset = 0
        while offset < len(content):
            offset += os.write(index_fd, content[offset:])
        if index.exists():
            # Einen bestehenden Indexmodus erhalten. Beim Erst-Commit bleibt
            # dagegen der von `_acquire_git_lock()` sicher mit 0600 angelegte
            # Modus bestehen; 0644 würde eine strenge Prozess-Umask umgehen.
            os.fchmod(index_fd, stat.S_IMODE(index.lstat().st_mode))
        os.fsync(index_fd)
        os.close(index_fd)
        held[0] = (held[0][0], None, held[0][2])
        os.replace(lock_paths[0], index)
        log_command(
            repo, (*NO_GIT_HOOKS_ARGS, "reset", "-q", committed_head, "--",
                   *(":(literal)" + path for path in paths)), 0)
    finally:
        if temp_index is not None:
            try:
                temp_index.unlink()
            except FileNotFoundError:
                pass
        if external_guards:
            # Der Aufrufer besitzt den Index-Lock weiterhin; die hier ergänzten
            # HEAD-/Branch-Locks lösen.
            for lock_path, fd, identity in reversed(held[1:]):
                _release_owned_git_lock(lock_path, fd, identity)
            del held[1:]
        else:
            for lock_path, fd, identity in reversed(held):
                _release_owned_git_lock(lock_path, fd, identity)


def current_head(repo: Path, timeout: int) -> str | None:
    """Commit-ID von HEAD — oder nur beim belegten unborn Branch ``None``.

    Timeout und andere Lesefehler werden propagiert; sie dürfen niemals wie ein
    frisches Repo ohne Commit aussehen.
    """
    r = run_git(repo, "rev-parse", "--verify", "-q", "HEAD", timeout=timeout)
    if r.returncode == 0:
        return r.stdout.strip()
    if r.returncode == 1:
        return None
    raise GitReadError("git rev-parse HEAD failed (exit %d)" % r.returncode)


def current_symbolic_head_ref(repo: Path, timeout: int) -> str | None:
    """Vollständiger Branch-Ref von HEAD; bei detached HEAD ``None``."""
    result = run_git(repo, "symbolic-ref", "-q", "HEAD", timeout=timeout)
    if result.returncode == 0:
        ref = result.stdout.strip()
        if ref.startswith("refs/heads/"):
            return ref
        raise GitReadError("HEAD points outside refs/heads")
    if result.returncode == 1:
        return None
    raise GitReadError("git symbolic-ref HEAD failed (exit %d)" % result.returncode)


def commit_parents(repo: Path, oid: str, timeout: int) -> list[str]:
    """Alle Eltern einer Commit-OID lesen; ein Normalcommit hat genau einen."""
    result = _required_git(
        repo, "rev-list", "--parents", "-n", "1", oid, "--", timeout=timeout,
        env=RAW_OBJECT_ENV)
    fields = result.stdout.split()
    if not fields or fields[0] != oid:
        raise GitReadError("git rev-list returned malformed parents")
    return fields[1:]


def stash_preview(repo: Path, timeout: int, oid: str | None = None) -> tuple[bool, str]:
    """Return one OID-bound stash patch, including untracked and binary contents."""
    if oid is None:
        try:
            current = latest_stash(repo, timeout)
        except (GitReadError, OSError) as exc:
            return False, str(exc)[:240]
        if current is None:
            return False, "no stash"
        oid = current[0]
    r = run_git(
        repo, "stash", "show", "-p", "--binary", "--include-untracked",
        "--no-ext-diff", "--no-textconv", "--ignore-submodules=none",
        oid, timeout=timeout,
        env=RAW_OBJECT_ENV)
    if r.returncode != 0:
        detail = (r.stderr or "Git exit %d" % r.returncode).strip()[:240]
        return False, detail
    return True, r.stdout


def latest_stash(repo: Path, timeout: int) -> tuple[str, str] | None:
    """OID und sichtbare Bezeichnung des aktuellen ``stash@{0}`` lesen."""
    result = _required_git(
        repo, "stash", "list", "-1", "--format=%H%x00%gd %gs", timeout=timeout)
    line = result.stdout.rstrip("\n")
    if not line:
        return None
    oid, separator, label = line.partition("\0")
    if not separator or not oid or not label:
        raise GitReadError("git stash list returned malformed output")
    return oid, label


# Git darf uns nie nach Zugangsdaten fragen. Seinen Prompt ("Username for
# 'https://github.com':") schreibt Git nämlich direkt auf das Terminal (/dev/tty)
# und nicht auf die von uns abgefangenen Kanäle: das zerlegt das curses-Bild und
# Git wartet dann bis zum Timeout auf eine Eingabe, die die TUI nie liefert. Bei
# `R` (fetch über alle Repos) laufen zwölf solche Fragen gleichzeitig — daher blieb
# nur noch Strg-C. Mit diesen Variablen scheitert der Aufruf stattdessen sofort mit
# einer Fehlermeldung, die wir lesen und anzeigen können.
NONINTERACTIVE_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",     # keine Login-Frage auf dem Terminal
    "GIT_ASKPASS": "",              # kein Askpass-Programm/-Dialog
    "SSH_ASKPASS": "",
    "SSH_ASKPASS_REQUIRE": "never",  # OpenSSH: auch keinen GUI-Dialog aufmachen
    "GIT_OPTIONAL_LOCKS": "0",       # Leser aktualisieren Index-Caches nie nebenbei
    "LC_ALL": "C",                  # Meldungen bleiben stabil englisch (s.u.)
}

# Diese Variablen schlagen `git -C <repo>` und koennen einen Aufruf auf Refs,
# Objekte, Index oder Arbeitsbaum eines anderen Repos umlenken. Von der
# aufrufenden Shell werden sie deshalb nie geerbt. Nur GIT_INDEX_FILE darf ein
# interner Aufrufer kontrolliert fuer den temporaeren Commit-Index setzen.
REPOSITORY_GIT_ENV = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH", "GIT_PREFIX", "GIT_NAMESPACE",
    # Diese Variablen schreiben zwar nicht in ein anderes Repo, deuten aber
    # seinen Commit-Graph um. Ein geerbtes Graft/Shallow-File darf eine echte
    # Divergenz niemals als Fast-forward erscheinen lassen.
    "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE",
}

# Objekt- und Historienprüfungen müssen den echten Commit-Graph sehen. Das leere
# Graft-/Shallow-Dateien unterdrücken sowohl geerbte Umleitungen als auch die
# repo-lokalen Graph-Grenzen; GIT_NO_REPLACE_OBJECTS schaltet Replace-Refs ab.
# `run_git()` lässt genau diese kontrollierten leeren Dateien intern zu.
RAW_OBJECT_ENV = {
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_GRAFT_FILE": os.devnull,
    "GIT_SHALLOW_FILE": os.devnull,
}

# Ursachen, die ein fehlgeschlagener Remote-Zugriff haben kann — in dieser Reihenfolge
# geprüft. Git übersetzt seine Meldungen je nach Systemsprache, deshalb laufen die
# Kindprozesse mit LC_ALL=C; nur so sind diese englischen Marker verlässlich.
# Entscheidend sind drei Trennungen, die sonst alle gleich aussehen und doch völlig
# verschiedene Reaktionen verlangen: "der Server hat geantwortet, das Repo gibt es
# nicht" (Remote entfernen), "wir kamen nicht hin" (Netz reparieren/warten) und
# "der Hostschlüssel ist unbekannt" (einmal bestätigen — kein fehlender Login!).
REMOTE_CHECK_CAUSES = (
    # (Ergebnis, Marker in der englischen Git-/SSH-Meldung)
    ("dns", ("could not resolve host", "could not resolve hostname",
             "name or service not known", "nodename nor servname",
             "temporary failure in name resolution", "no address associated")),
    ("unreachable", ("connection refused", "connection timed out",
                     "no route to host", "network is unreachable",
                     "operation timed out", "connection closed by remote host",
                     "connection reset by peer", "couldn't connect to server",
                     "failed to connect to")),
    ("server", ("the requested url returned error: 5", "http code = 5",
                "error: 502", "error: 503", "internal server error",
                "service unavailable", "bad gateway")),
    ("hostkey", ("host key verification failed", "no matching host key",
                 "remote host identification has changed",
                 "no ed25519 host key is known", "no rsa host key is known")),
    ("auth", ("terminal prompts disabled", "could not read username",
              "could not read password", "authentication failed",
              "invalid username or password", "permission denied (publickey",
              "no supported authentication methods")),
    ("gone", ("repository not found", "not found", "does not appear to be a git repository",
              "does not exist", "access denied", "the requested url returned error: 404",
              "no such file or directory")),
)

# Die Teilmenge der "auth"-Marker, die den Weg ueber einen Credential-Helper
# belegt (HTTPS: Git fragt nach Benutzername/Passwort). Nur dieser Weg liest den
# Login-Schluesselbund — und nur er kann deshalb an einer Sitzung ohne
# Schluesselbund scheitern (siehe keychain_session). Ein abgelehnter SSH-Schluessel
# ("permission denied (publickey)") benutzt keinen Helfer; ihn als "nokeychain"
# zu entschuldigen behauptete, der Login sei in Ordnung, und schickte die
# Fehlersuche in die falsche Richtung.
CREDENTIAL_HELPER_MARKERS = (
    "terminal prompts disabled", "could not read username",
    "could not read password", "authentication failed",
    "invalid username or password",
)


_KEYCHAIN_SESSION: bool | None = None


def keychain_session() -> bool:
    """Kann diese Sitzung an den macOS-Schlüsselbund?

    Auf dem Mac hängt der Login-Schlüsselbund an der GUI-Sitzung ("Aqua"). Eine
    ssh-Sitzung, ein LaunchDaemon oder ein cron-Lauf leben daneben und kommen
    nicht daran — jeder Zugriff scheitert dort, obwohl der Login vollkommen in
    Ordnung ist. `launchctl managername` benennt genau diesen Unterschied und
    liefert "Aqua" nur in der GUI-Sitzung.

    Wichtig für die Bewertung: Git holt seine GitHub-Zugangsdaten über einen
    Credential-Helper, und die verbreiteten Helfer (`osxkeychain`,
    `gh auth git-credential`) lesen aus genau diesem Schlüsselbund. Scheitert
    ein Fetch hier an "Authentifizierung", ist deshalb nicht der Login kaputt,
    sondern die Messung am falschen Ort gelaufen.

    Außerhalb von macOS immer True: dort gibt es dieses Problem nicht, und ein
    "nein" würde echte Auth-Fehler fälschlich entschuldigen. Das Ergebnis ändert
    sich innerhalb eines Prozesses nicht und wird deshalb gemerkt.
    """
    global _KEYCHAIN_SESSION
    if _KEYCHAIN_SESSION is None:
        if sys.platform != "darwin":
            _KEYCHAIN_SESSION = True
        else:
            try:
                out = subprocess.run(["launchctl", "managername"],
                                     capture_output=True, text=True, timeout=5)
                # Nur eine erfolgreiche Abfrage ist auswertbar. Bei jedem anderen
                # Exit-Code ist stdout meist leer — das ergäbe "nicht Aqua" und
                # damit dauerhaft (der Wert wird gemerkt) die Ausrede
                # "Schlüsselbund unerreichbar" für jeden echten Auth-Fehler.
                if out.returncode != 0:
                    _KEYCHAIN_SESSION = True
                else:
                    _KEYCHAIN_SESSION = out.stdout.strip() == "Aqua"
            except (OSError, subprocess.SubprocessError):
                # Lieber echte Auth-Fehler zeigen als sie stillschweigend
                # entschuldigen: im Zweifel gilt der Schlüsselbund als erreichbar.
                _KEYCHAIN_SESSION = True
    return _KEYCHAIN_SESSION


def classify_remote_check(result: subprocess.CompletedProcess, *,
                          keychain_helper: bool = False) -> str:
    """Warum ist der Zugriff auf das Remote gescheitert?

    Liefert "dns", "unreachable", "server", "hostkey", "auth", "nokeychain",
    "gone" oder "unknown". Reine Textauswertung von Gits Meldung (deshalb
    LC_ALL=C).

    "nokeychain" ist ein Sonderfall von "auth": Dieselbe Git-Meldung, aber die
    Sitzung kommt gar nicht an den Schlüsselbund (siehe `keychain_session`).
    Die Unterscheidung ist wichtig, weil "Login fehlt" zum Neu-Anmelden auffordert
    und damit in die falsche Richtung schickt. Sie gilt nur für die Meldungen des
    Credential-Helpers (`CREDENTIAL_HELPER_MARKERS`); ein abgelehnter
    SSH-Schlüssel bleibt "auth", denn dort ist gar kein Schlüsselbund im Spiel.
    """
    text = ((result.stderr or "") + "\n" + (result.stdout or "")).lower()
    for cause, markers in REMOTE_CHECK_CAUSES:
        if any(marker in text for marker in markers):
            if (cause == "auth" and keychain_helper and not keychain_session()
                    and any(m in text for m in CREDENTIAL_HELPER_MARKERS)):
                return "nokeychain"
            return cause
    return "unknown"


def credentials_missing(result: subprocess.CompletedProcess, *,
                        keychain_helper: bool = False) -> bool:
    """Fehlen wirklich Zugangsdaten? (Ein unbekannter Hostschlüssel ist etwas anderes.)

    Bei "nokeychain" bewusst False: Dort fehlen keine Zugangsdaten, sie sind nur
    aus dieser Sitzung nicht lesbar.
    """
    return classify_remote_check(result, keychain_helper=keychain_helper) == "auth"


def remote_uses_keychain_helper(repo: Path, name: str, timeout: int, *,
                                for_push: bool = False) -> bool:
    """Positiv belegen, dass dieses Remote einen Keychain-Helper verwendet.

    Eine Nicht-Aqua-Sitzung allein beweist keine Schlüsselbundursache. Erst der
    fuer die konkrete URL wirksame `credential.helper` macht aus einem sonstigen
    Auth-Fehler den Sonderfall `nokeychain`.
    """
    remote = read_remote_config_or_none(repo, timeout_config(timeout), name)
    if remote is None:
        return False
    urls = remote.push_urls if for_push else remote.fetch_urls
    # Git verwendet beim Fetch nur die erste URL. Ein Helper, der erst auf eine
    # spätere Ersatz-URL passt, belegt daher nicht die Ursache des vorliegenden
    # Fehlers. Push-Remotes mit mehreren Zielen sind ohnehin nicht transfer_safe;
    # auch dort ist ausschließlich der tatsächlich gewählte erste Endpunkt
    # relevant.
    for url in urls[:1]:
        # Die Match-URL landet in argv und ist damit fuer andere lokale Prozesse
        # sichtbar. Enthält sie Benutzerinfo, Query oder Fragment, darf selbst
        # die Helper-Diagnose das Secret nicht dorthin kopieren.
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        if (parsed.scheme.lower() not in ("http", "https")
                or display_remote_url(url) != url):
            continue
        try:
            result = run_git(
                repo, "config", "--get-urlmatch", "credential.helper", url,
                timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            # Diese Abfrage ist nur eine nachgelagerte Diagnose. Ihr eigener
            # Fehler darf die eigentliche Fetch-/Push-Meldung nicht verdecken.
            return False
        if result.returncode not in (0, 1):
            continue
        for line in result.stdout.splitlines():
            shell_helper = line.startswith("!")
            command = line[1:].lstrip() if shell_helper else line
            try:
                words = shlex.split(command)
            except ValueError:
                continue
            if not words:
                continue
            if not shell_helper and words == ["osxkeychain"]:
                return True
            if (len(words) == 3
                    and words[1:3] == ["auth", "git-credential"]):
                executable = Path(words[0])
                if (shell_helper and words[0] == "gh"
                        and shutil.which("gh") is not None):
                    return True
                if (executable.is_absolute()
                        and executable.name == "gh"
                        and os.access(executable, os.X_OK)):
                    return True
    return False


# Git zitiert in Fehlermeldungen die komplette Remote-URL — inklusive eines
# eingebetteten Logins (https://user:token@host/…) und der Query-Parameter
# (?token=…). Beides sind potenzielle Zugangsdaten.
# _URL_PASSWORD greift bei JEDEM Schema und wirft nur den Teil nach dem
# Doppelpunkt weg (ssh://user:geheim@host → ssh://user@host); der Benutzername
# bleibt, weil er Teil der hilfreichen Adresse ist. _HTTP_USERINFO nimmt bei
# HTTP(S) danach auch noch den Benutzernamen, denn dort steht an seiner Stelle
# regelmäßig ein Token.
# `_SCHEME` ist die vollständige Schema-Grammatik aus RFC 3986: Buchstabe, dann
# beliebig viele Buchstaben, Ziffern, "+", "-" und ".". Ein bloßes \w+ deckte
# genau die Schemata NICHT ab, deren letztes Zeichen ein Satzzeichen ist
# (z.B. ein Remote-Helper "foo+://") — dort bliebe das Passwort stehen.
_SCHEME = r"[A-Za-z][A-Za-z0-9+.-]*"
_URL_USERINFO = re.compile(r"(?i)\b(%s://)([^/@\s]+)@" % _SCHEME)
_URL_QUERY_FRAGMENT = re.compile(r"(?i)\b(%s://[^\s'?#]*)[?#][^\s']*" % _SCHEME)
_SSH_URL_SCHEMES = {"ssh", "git+ssh", "ssh+git"}


def redact_remote_error(line: str) -> str:
    """Zugangsdaten-Anteile aus einer Git-/SSH-Fehlerzeile entfernen.

    Dieselbe Politik wie display_remote_url(): das Passwort fällt bei jedem
    Schema weg, bei HTTP(S) zusätzlich der ganze Benutzername (genau dort stehen
    Personal Access Tokens), Query und Fragment fallen ebenfalls bei jedem Schema
    weg. Der SSH-Benutzer (git@…) bleibt stehen — er ist Teil der hilfreichen
    Adresse, kein Geheimnis. Diese Zeilen erscheinen in der TUI, auf der
    Info-Seite und über error_long auch in --json; deshalb wird schon an der
    Eingangsgrenze redigiert, nicht erst bei der Anzeige.
    """
    def redact_userinfo(match: re.Match) -> str:
        scheme_prefix, userinfo = match.groups()
        scheme = scheme_prefix[:-3].lower()
        if scheme in _SSH_URL_SCHEMES:
            # Ein SSH-Benutzer wie `git` ist Zielidentitaet; ein Passwort nie.
            username = userinfo.split(":", 1)[0]
            return scheme_prefix + username + "@"
        # Bei HTTP, FTP und unbekannten Helper-Schemata kann schon das einzelne
        # Userinfo-Feld ein Zugangstoken sein. Nur SSH ist explizit erlaubt.
        return scheme_prefix

    line = _URL_USERINFO.sub(redact_userinfo, line)
    return _URL_QUERY_FRAGMENT.sub(r"\1", line)


def last_error_line(result: subprocess.CompletedProcess) -> str:
    """Die aussagekräftigste Fehlerzeile von Git — der Beleg für die Ursache.

    Eine etwaige Sammelzeile "error: could not fetch <name>" erklärt nichts.
    Die eigentliche Ursache steht davor, deshalb wird die Sammelzeile übersprungen.
    """
    lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
    detailed = [line for line in lines if "could not fetch" not in line]
    return redact_remote_error((detailed or lines or [""])[-1])


def check_remote(repo: Path, name: str, timeout: int) -> tuple[str, int, str]:
    """Existiert das Remote-Repo unter seiner Adresse — und wenn nicht, warum?

    `git ls-remote` fragt nur die Ref-Liste ab: es überträgt keine Objekte, ändert
    lokal nichts und ist damit der harmloseste echte Zugriffstest.
    Rückgabe: (Ergebnis, Anzahl Refs, letzte Fehlerzeile). Ergebnis ist "ok",
    "empty", "timeout" oder eine Ursache aus classify_remote_check().
    """
    try:
        remote = read_remote_configs(repo, timeout_config(timeout)).get(name)
        if (remote is None or remote.fetch_invalid_reason
                or len(remote.fetch_urls) != 1 or not remote.fetch_targets):
            return "unsafe_url", 0, ""
        fetch_url = remote_fetch_url(remote)
        if not _argv_safe_remote_url(fetch_url):
            return "unsafe_url", 0, ""
        pin_config, pinned_url = _pinned_url_config(fetch_url)
        r = run_git_logged(
            repo, *pin_config, "ls-remote", "--heads", "--", pinned_url,
            timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", 0, ""
    except (GitReadError, OSError, ValueError, RuntimeError) as exc:
        return "unknown", 0, terminal_text(str(exc))[:160]
    if r.returncode == 0:
        refs = [line for line in r.stdout.splitlines() if line.strip()]
        return ("ok" if refs else "empty"), len(refs), ""
    detail = next((line.strip() for line in reversed((r.stderr or "").splitlines())
                   if line.strip()), "")
    return classify_remote_check(
        r, keychain_helper=remote_uses_keychain_helper(repo, name, timeout)
    ), 0, redact_remote_error(detail)[:160]


def remote_failure_short(name: str, outcome: str) -> str:
    """Ganz knappe Fassung für die Repo-Zeile.

    Dort teilt sich die Meldung den Platz mit den Remote-Badges. Ein langer Satz
    schiebt genau die Marke aus dem Bild, die den kaputten Remote zeigt — deshalb
    steht hier nur das Stichwort, der ganze Satz auf der Info-Seite.
    """
    key = "short_" + outcome
    return f"{name}: {t(key)}" if key in TR else f"{name}: {outcome}"


def remote_check_message(name: str, outcome: str, refs: int, detail: str,
                         timeout: int) -> str:
    """Prüfergebnis als fertiger, erklärender Satz für die Meldungszeile."""
    if outcome == "ok":
        return t("check_ok", r=name, n=refs)
    if outcome == "empty":
        return t("check_empty", r=name)
    if outcome == "timeout":
        return t("check_timeout", r=name, s=timeout)
    if outcome == "unsafe_refspec":
        return t("check_unsafe_refspec", r=name)
    if outcome == "unsafe_url":
        return t("check_unsafe_url", r=name)
    if outcome == "changed":
        return t("check_changed", r=name)
    if outcome == "outcome_unknown":
        return t("check_outcome_unknown", r=name)
    if outcome in ("dns", "unreachable", "server", "hostkey", "auth", "nokeychain",
                   "gone"):
        return t("check_" + outcome, r=name)
    return t("check_unknown", r=name, e=detail or outcome)


def summarize_fetch_failures(st: RepoStatus) -> None:
    """Remote-spezifische Fetch-Fehler knapp auf den Repo-Status projizieren."""
    failed = [remote for remote in st.remotes if remote.fetch_failed]
    if not failed:
        return
    st.error = "; ".join(remote_failure_short(
        remote.name, remote.fetch_outcome or "unknown") for remote in failed)
    st.error_long = " ".join(
        remote.fetch_error_long or remote_check_message(
            remote.name, remote.fetch_outcome or "unknown", 0,
            remote.fetch_error_detail, 0)
        for remote in failed)
    st.error_detail = " | ".join(
        f"{remote.name}: {remote.fetch_error_detail}"
        for remote in failed if remote.fetch_error_detail)
    st.remote_state = "error"
    st.fetch_error = True


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Nach einem Timeout nicht nur git, sondern alles beenden, was es gestartet hat.

    `subprocess.run()` würde allein den git-Prozess killen. Ein `git commit` startet
    aber den pre-commit-Hook, der seinerseits Linter oder Tests startet — die liefen
    dann als verwaiste Prozesse weiter, hielten unsere Ausgabe-Pipes offen und
    stolperten beim nächsten Versuch übereinander. Dank `start_new_session=True`
    hängt diese ganze Verwandtschaft in einer eigenen Prozessgruppe, die wir in
    einem Rutsch beenden können.
    """
    try:
        # `start_new_session=True` macht die Kind-PID zugleich zur PGID. Git
        # selbst kann bereits beendet sein, während ein vom Hook gestartetes
        # Kind die Pipes noch offen hält; `getpgid(proc.pid)` lieferte dann
        # ESRCH und ließ genau dieses Kind zurück. Die bekannte PGID bleibt bis
        # zum letzten Gruppenmitglied gültig und kann direkt beendet werden.
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        # Prozess schon weg, oder eine Plattform ohne Prozessgruppen: dann wenigstens
        # das direkte Kind beenden.
        try:
            proc.kill()
        except OSError:
            pass


def _stop_and_collect_process_group(
        proc: subprocess.Popen) -> tuple[str, str]:
    """Eine eigene Prozessgruppe beenden und höchstens fünf Sekunden einsammeln."""
    _kill_process_group(proc)
    try:
        return proc.communicate(timeout=5)
    except Exception:
        # Der ursprüngliche communicate()-Fehler bleibt die relevante Ursache;
        # Cleanupfehler dürfen ihn nicht ersetzen oder unendlich warten lassen.
        return "", ""


def _run_process_group(cmd: list[str], *, stdin, timeout: int | None
                       ) -> subprocess.CompletedProcess:
    """Einen Prozess mit eigener, bei Timeout vollständig beendeter Gruppe starten."""
    with subprocess.Popen(
        cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="surrogateescape",
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            out, err = _stop_and_collect_process_group(proc)
            raise subprocess.TimeoutExpired(
                cmd, timeout, output=out, stderr=err) from None
        except Exception:
            _stop_and_collect_process_group(proc)
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _pinned_remote_url(args: tuple | list) -> tuple[str, str] | None:
    """Die einzige URL-Bindung dieses Aufrufs als (URL, Alias) — sonst None."""
    pins = {(arg.remote_url, str(arg)) for arg in args
            if isinstance(arg, _PinnedRemoteURL)}
    if not pins:
        return None
    if len(pins) != 1:
        raise ValueError("conflicting pinned remote URLs")
    return pins.pop()


def _git_config_entries(args: tuple | list) -> list[tuple[str, str]]:
    """Die Ersatzkonfiguration, mit der `run_git()` jeden Git-Aufruf startet.

    Ausgeführter und protokollierter Befehl müssen dieselbe Liste verwenden.
    Stünde sie zweimal im Code, zeigte das Befehlsprotokoll (H) nach der
    nächsten Ergänzung einen Befehl, der im Terminal anders liefe — und genau
    diese Wiederholbarkeit ist die Zusage des Protokolls.
    """
    entries = [
        ("core.fsmonitor", "false"),
        ("log.showSignature", "false"),
    ]
    pin = _pinned_remote_url(args)
    if pin is not None:
        remote_url, alias = pin
        entries.extend((
            (f"url.{remote_url}.insteadOf", alias),
            (f"url.{remote_url}.pushInsteadOf", alias),
        ))
    return entries


def _git_config_env(entries: list[tuple[str, str]]) -> dict[str, str]:
    """Konfigurationsliste als GIT_CONFIG_*-Variablen.

    Schlüssel und Werte bleiben getrennt, damit ein `=` in einer URL nicht
    versehentlich Schlüssel und Wert trennt.
    """
    env = {"GIT_CONFIG_COUNT": str(len(entries))}
    for number, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{number}"] = key
        env[f"GIT_CONFIG_VALUE_{number}"] = value
    return env


def run_git(repo: Path, *args: str, timeout: int = 10,
            env: dict | None = None) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo), *args]
    child_env = dict(os.environ)
    for key in list(child_env):
        if (key == "GIT_CONFIG_PARAMETERS" or key == "GIT_CONFIG_COUNT"
                or re.fullmatch(r"GIT_CONFIG_(?:KEY|VALUE)_\d+", key)):
            child_env.pop(key, None)
    for key in REPOSITORY_GIT_ENV:
        child_env.pop(key, None)
    if env:
        child_env.update({key: value for key, value in env.items()
                          if key not in REPOSITORY_GIT_ENV})
        if "GIT_INDEX_FILE" in env:
            child_env["GIT_INDEX_FILE"] = env["GIT_INDEX_FILE"]
        if env.get("GIT_GRAFT_FILE") == os.devnull:
            child_env["GIT_GRAFT_FILE"] = os.devnull
        if env.get("GIT_SHALLOW_FILE") == os.devnull:
            child_env["GIT_SHALLOW_FILE"] = os.devnull
    child_env.update(_git_config_env(_git_config_entries(args)))
    child_env.update(NONINTERACTIVE_GIT_ENV)
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="surrogateescape",
        env=child_env,
        stdin=subprocess.DEVNULL,
        # Eigene Session = kein kontrollierendes Terminal. Damit kommt auch ein
        # von Git gestartetes ssh nicht mehr an unser /dev/tty, um dort nach einer
        # Passphrase zu fragen; der ssh-agent funktioniert davon unberührt weiter.
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            out, err = _stop_and_collect_process_group(proc)
            raise subprocess.TimeoutExpired(cmd, timeout, output=out,
                                            stderr=err) from None
        except Exception:
            _stop_and_collect_process_group(proc)
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


# Protokoll der Befehle, die diese Sitzung bewusst abgesetzt hat (Reihenfolge = Verlauf).
# Zweck: Wer gmf benutzt, soll die Git-Syntax nebenbei mitlesen können, statt sie zu
# erraten. Nur ausgelöste Aktionen landen hier — die Lesebefehle des Repo-Scans würden
# das Protokoll unbrauchbar zumüllen.
COMMAND_LOG: list[str] = []
COMMAND_LOG_MAX = 200


def format_git_command(args: tuple[str, ...] | list[str]) -> str:
    """Den Befehl so schreiben, wie man ihn im Repo-Ordner selbst eintippen würde."""
    def visible_quote(value) -> str:
        value = str(value)
        return (shlex.quote(value) if terminal_text(value) == value
                else zsh_quote(value))

    prefix = ""
    if _pinned_remote_url(args) is not None:
        # Nur der Einmal-Alias braucht die Umgebung im Protokoll: Ohne ihn liefe
        # der kopierte Befehl gegen eine Adresse, die es im Terminal nicht gibt.
        prefix = " ".join(
            f"{key}={visible_quote(value)}" for key, value
            in _git_config_env(_git_config_entries(args)).items()) + " "
    return prefix + "git " + " ".join(visible_quote(a) for a in args)


def commit_undo_command(head_before: str | None, head_after: str,
                        head_ref: str) -> str:
    """Passenden Rückgängig-Befehl für einen gerade erzeugten Commit liefern.

    Beide Seiten sind an volle OIDs gebunden, zusätzlich steht der freigegebene
    vollständige Branch-Ref im Befehl. Ein später ausgecheckter anderer Branch
    kann dadurch nie zum versehentlichen Ziel werden. Index und Arbeitsbaum
    bleiben erhalten, also wie bei einem weichen Reset.
    """
    if head_before is None:
        return format_git_command(
            safe_update_ref_args(
                "--no-deref", "-d", head_ref, head_after))
    return format_git_command(
        safe_update_ref_args(
            "--no-deref", head_ref, head_before, head_after))


def log_command(repo: Path, args: tuple[str, ...] | list[str],
                returncode: int | None = None) -> str:
    """Einen abgesetzten Git-Befehl protokollieren und die Protokollzeile liefern."""
    mark = "…" if returncode is None else ("✔" if returncode == 0 else "✘")
    line = f"{mark} {repo.name}: {format_git_command(args)}"
    if returncode not in (None, 0):
        line += f"   (Exit {returncode})"
    COMMAND_LOG.append(line)
    del COMMAND_LOG[:-COMMAND_LOG_MAX]
    return line


def log_cancelled(repo: Path, args: tuple[str, ...] | list[str]) -> str:
    """Eine abgebrochene Aktion vermerken.

    Ohne diesen Eintrag bliebe im Protokoll offen, ob der eben gezeigte Befehl nun
    gelaufen ist oder nicht — "⊘ nicht ausgeführt" beantwortet das eindeutig.
    """
    line = f"⊘ {repo.name}: {format_git_command(args)}   ({t('cmdlog_cancelled')})"
    COMMAND_LOG.append(line)
    del COMMAND_LOG[:-COMMAND_LOG_MAX]
    return line


def run_git_logged(repo: Path, *args: str, timeout: int = 10,
                   env: dict | None = None) -> subprocess.CompletedProcess:
    """Wie run_git, protokolliert den Aufruf aber für die Befehlsansicht (H)."""
    try:
        r = run_git(repo, *args, timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, OSError):
        log_command(repo, args, returncode=None)
        raise
    log_command(repo, args, r.returncode)
    return r


def timeout_message(exc: subprocess.TimeoutExpired) -> str:
    """Aus einem abgelaufenen Git-Aufruf einen lesbaren Satz machen.

    Ein Timeout beendete die TUI früher mit einem Python-Traceback. Er ist aber
    kein Programmfehler, sondern eine Auskunft: Dieser eine Befehl hat zu lange
    gebraucht. Deshalb nennt die Meldung den Unterbefehl (`commit`, `push`, …)
    und die häufigsten Ursachen.
    """
    cmd = list(exc.cmd or [])
    # Nach `git -C <repo>` können globale Optionen stehen. Die URL-Bindung
    # verwendet etwa zwei `-c <key=value>`-Paare vor `fetch`/`push`; die Meldung
    # soll trotzdem den verständlichen Unterbefehl nennen.
    index = 3
    takes_value = {"-c", "--config-env", "-C", "--git-dir", "--work-tree",
                   "--namespace", "--super-prefix"}
    while index < len(cmd):
        token = cmd[index]
        if token in takes_value:
            index += 2
        elif token.startswith("-"):
            index += 1
        else:
            break
    name = cmd[index] if index < len(cmd) else "git"
    key = "transfer_timeout" if name in {"fetch", "push"} else "action_timeout"
    return t(key, cmd=name, s=int(exc.timeout or 0))


class GitReadError(RuntimeError):
    pass


def _required_git(repo: Path, *args: str, timeout: int = 10,
                  env: dict | None = None) -> subprocess.CompletedProcess:
    r = run_git(repo, *args, timeout=timeout, env=env)
    if r.returncode != 0:
        raise GitReadError("git %s failed (exit %d)" % (args[0], r.returncode))
    return r


def find_repos(root: Path, skip_dirs: list[str]) -> list[Path]:
    """Alle Git-Repos unterhalb von root finden.

    In ein gefundenes Repo wird nicht weiter hinabgestiegen (verschachtelte
    Repos wären ohnehin Submodule o.ä. und verlangsamen den Scan nur).
    """
    skip = set(skip_dirs)
    repos: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # .git kann Ordner (normales Repo) oder Datei (Worktree/Submodul) sein.
        if ".git" in dirnames or ".git" in filenames:
            repos.append(Path(dirpath))
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in skip and d != ".git"]
    return sorted(repos)


def _normal_host(host: str) -> str:
    host = host.strip().rstrip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _canonical_network_path(path: str) -> str:
    """Nur unkritische Prozent-Escapes einer Netzwerk-URL normalisieren.

    Reservierte Zeichen wie `/`, `%`, `?` und `#` bleiben codiert. Auch ein
    codierter Punkt bleibt codiert, damit `%2e%2e` niemals durch `normpath()`
    zum Elternsegment einer anderen Remote-Adresse wird.
    """
    def replace(match: re.Match) -> str:
        value = int(match.group(1), 16)
        char = chr(value)
        if char.isascii() and (char.isalnum() or char in "-_~"):
            return char
        return "%" + match.group(1).upper()

    return re.sub(r"%([0-9A-Fa-f]{2})", replace, path)


def canonical_remote_target(url: str, repo: Path | None = None) -> RemoteTarget:
    """Credential-free host/repository identity for URL, SCP, and local syntax."""
    raw = url.strip()
    host = "local"
    port = None
    path = raw
    user = ""
    scheme = ""
    home_relative = False   # Pfad wird im Home-Verzeichnis des SSH-Benutzers aufgelöst
    if "://" not in raw and ":" in raw and not raw.startswith(("/", "./", "../", "~")):
        hostpart, rest = raw.split(":", 1)
        if "/" not in hostpart:
            scheme = "scp"
            host = _normal_host(hostpart.rsplit("@", 1)[-1])
            path = rest
            user = hostpart.rsplit("@", 1)[0] if "@" in hostpart else ""
            # SCP-Syntax: ein Pfad ohne führenden "/" liegt im Home des
            # SSH-Benutzers — alice@host:repo und bob@host:repo sind also
            # verschiedene Repositories, host:repo und host:/repo ebenfalls.
            home_relative = not rest.startswith("/")
            # Ausnahme: Hosting-Dienste (GitHub, GitLab, Gitea, …) routen alle
            # SSH-Zugriffe über den virtuellen Benutzer "git". Dessen Pfad ist
            # kein privates Home-Verzeichnis, sondern derselbe Namensraum wie
            # der HTTPS-Pfad: git@host:org/repo und https://host/org/repo
            # meinen dasselbe Repository. Ohne diese Ausnahme gälte der übliche
            # Mix (Fetch per HTTPS, Push per SSH) als zwei verschiedene Ziele,
            # und P/G verweigerten die Übertragung. Ein ausdrückliches "~" im
            # Pfad bleibt benutzerabhängig (Prüfung unten).
            if user == "git" and not rest.startswith("~"):
                home_relative = False
    elif urllib.parse.urlsplit(raw).scheme:
        parsed = urllib.parse.urlsplit(raw)
        scheme = parsed.scheme.lower()
        if parsed.scheme == "file":
            path = urllib.parse.unquote(parsed.path)
        else:
            host = _normal_host(parsed.hostname or "")
            port = parsed.port
            path = _canonical_network_path(parsed.path)
            user = parsed.username or ""
    if host == "local":
        local = Path(path).expanduser()
        if not local.is_absolute() and repo is not None:
            local = repo / local
        repo_id = str(local.resolve(strict=False))
        canonical = json.dumps(["local", repo_id], separators=(",", ":"))
    else:
        # Netzwerkpfade sind keine lokalen Dateipfade: `//`, `.` und `..` koennen
        # serverseitig eigene Segmentnamen beziehungsweise Routing-Semantik
        # besitzen. Nur ein fehlender fuehrender Slash der SCP-Syntax wird fuer
        # die Anzeige ergaenzt; der Rest bleibt bytegenau normalisiert.
        repo_id = path if path.startswith("/") else "/" + path
        if repo_id.endswith(".git"):
            repo_id = repo_id[:-4]
        # Auch "~"-Pfade (ssh://host/~/repo, host:~/repo) hängen am Benutzer.
        home_relative = home_relative or (
            scheme in {"scp", "ssh", "git+ssh", "ssh+git"}
            and repo_id.startswith("/~"))
        default_port = ((raw.startswith("ssh://") and port == 22)
                        or (raw.startswith("https://") and port == 443)
                        or (raw.startswith("http://") and port == 80))
        # Der Benutzername bleibt aus der Anzeige (repo_id) heraus, gehört bei
        # benutzerabhängigen Pfaden aber in die Identität: sonst gälten zwei
        # verschiedene Home-Verzeichnisse als dasselbe Ziel — und ein
        # "sicherer" Push ginge in das Repository des falschen Benutzers.
        qualifier = f"~{user}" if home_relative else ""
        canonical = json.dumps(
            ["network", host, None if default_port else port, qualifier, repo_id],
            separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return RemoteTarget(host, repo_id, fingerprint)


def read_remote_configs(repo: Path, cfg: dict) -> dict[str, RemoteConfig]:
    """Read every effective fetch/push target; any Git read error is fatal."""
    t_ = cfg["git_timeout"]
    names_r = _required_git(repo, "remote", timeout=t_)
    result = {}
    for name in [line for line in names_r.stdout.splitlines() if line]:
        raw_r = _required_git(
            repo, "config", "--null", "--get-regexp",
            r"^remote\.%s\." % re.escape(name), timeout=t_)
        prefix = f"remote.{name}."
        settings = []
        for record in raw_r.stdout.split("\0"):
            key, separator, value = record.partition("\n")
            if separator and key.lower().startswith(prefix.lower()):
                settings.append((key[len(prefix):], value))
        fetch_r = _required_git(repo, "remote", "get-url", "--all", "--", name,
                                timeout=t_)
        push_r = _required_git(repo, "remote", "get-url", "--push", "--all", "--", name,
                               timeout=t_)
        fetch_urls = [line for line in fetch_r.stdout.splitlines() if line]
        push_urls = [line for line in push_r.stdout.splitlines() if line]
        if not fetch_urls or not push_urls:
            raise GitReadError("remote target is empty")
        try:
            fetch_targets = [canonical_remote_target(url, repo)
                             for url in fetch_urls]
            fetch_invalid_reason = ""
        except (ValueError, RuntimeError, OSError):
            fetch_targets = []
            fetch_invalid_reason = "unsafe_url"
        try:
            push_targets = [canonical_remote_target(url, repo)
                            for url in push_urls]
            push_invalid_reason = ""
        except (ValueError, RuntimeError, OSError):
            push_targets = []
            push_invalid_reason = "unsafe_url"
        # Eine kaputte Seite sperrt nur die Richtung dieses Remotes. Andere
        # Remotes — und ein sicherer Fetch bei kaputter Push-URL — bleiben
        # funktionsfähig; Roh-URLs gelangen dabei nie in UI oder JSON.
        result[name] = RemoteConfig(
            name, fetch_urls, push_urls, fetch_targets, push_targets, settings,
            fetch_invalid_reason, push_invalid_reason,
        )
    return result


def timeout_config(timeout: int) -> dict:
    """Die Standardkonfiguration, aber mit dem Timeout dieses Aufrufs.

    Die Remote-Prüfungen lesen die Git-Config nur nebenbei; sie sollen dabei
    dieselbe Zeitgrenze einhalten wie der Aufruf, zu dem sie gehören.
    """
    return {**DEFAULT_CONFIG, "git_timeout": timeout}


def read_remote_config_or_none(repo: Path, cfg: dict,
                               name: str) -> RemoteConfig | None:
    """Ein einzelnes Remote lesen; jeder Lesefehler gilt als nicht belegbar.

    Die Aufrufer treffen damit Sicherheitsentscheidungen: Ist die Config noch
    dieselbe wie bei der Freigabe? Hängt der Login wirklich am Schlüsselbund?
    Eine Config, die gerade nicht lesbar ist, darf keine dieser Aussagen
    stützen. Deshalb wird jeder Lesefehler wie ein fehlendes Remote behandelt —
    an einer Stelle statt an fünf, damit die Liste der abgefangenen Fehler nicht
    auseinanderläuft.
    """
    try:
        return read_remote_configs(repo, cfg).get(name)
    except (subprocess.TimeoutExpired, GitReadError, OSError,
            ValueError, RuntimeError):
        return None


def detect_sync_remote(repo: Path, cfg: dict,
                       configs: dict[str, RemoteConfig] | None = None) -> str | None:
    """Sync remote by configured name, then by exact normalized hostname."""
    configs = configs if configs is not None else read_remote_configs(repo, cfg)
    for name in cfg["sync_remote_names"]:
        if name in configs:
            return name
    wanted = {_normal_host(host) for host in cfg["sync_remote_hosts"]}
    for name, remote in configs.items():
        if any(target.host in wanted for target in remote.fetch_targets + remote.push_targets):
            return name
    return None


def is_github_url(url: str) -> bool:
    """Only an exact github.com host receives the public-push classification."""
    return canonical_remote_target(url).is_github


def collect_remote_statuses(repo: Path, branch: str, sync_remote: str | None,
                            cfg: dict,
                            configs: dict[str, RemoteConfig] | None = None,
                            fetch_failures: dict[str, tuple[str, str, str]] | None = None
                            ) -> list[RemoteStatus]:
    """Alle Remotes samt Branch-Delta lesen; öffentliche Remotes immer zuletzt.

    `fetch_failures` ordnet jedem gerade gescheiterten Remote Ursache, erklärenden
    Satz und redigierten Git-Beleg zu. Da jedes Remote einzeln gefetcht wird,
    können sich die Diagnosen nicht über einen kombinierten stderr vermischen.
    """
    states: list[RemoteStatus] = []
    configs = configs if configs is not None else read_remote_configs(repo, cfg)
    failures = fetch_failures or {}
    for name, remote in configs.items():
        targets = remote.fetch_targets + remote.push_targets
        public_classes = {target.is_github for target in targets}
        failure = failures.get(name, ("", "", ""))
        state = RemoteStatus(
            name=name,
            public=True in public_classes,
            mixed_public=len(public_classes) > 1,
            is_sync=name == sync_remote,
            fetch_fingerprint=(remote.fetch_targets[0].fingerprint
                               if len(remote.fetch_targets) == 1 else ""),
            fetch_fingerprints=[target.fingerprint for target in remote.fetch_targets],
            push_fingerprints=[target.fingerprint for target in remote.push_targets],
            target_mismatch=not remote.transfer_safe,
            multiple_pushurls=len(remote.push_targets) != 1,
            fetch_refspecs_safe=fetch_refspecs_safe(remote),
            fetch_refspec_fingerprint=hashlib.sha256("\0".join(
                value for key, value in remote.settings
                if key.lower() == "fetch").encode(
                    "utf-8", "surrogateescape")).hexdigest()[:20],
            branch_mapping_safe=(branch not in ("?", "(detached)")
                                 and fetch_maps_branch_exactly(remote, branch)),
            fetch_url_safe=remote_fetch_url_safe(remote),
            push_url_safe=remote_push_url_safe(remote),
            fetch_failed=name in failures,
            fetch_outcome=failure[0],
            fetch_error_long=failure[1],
            fetch_error_detail=failure[2],
        )
        if branch not in ("?", "(detached)"):
            ref = f"refs/remotes/{name}/{branch}"
            r = run_git(repo, "show-ref", "--verify", "--quiet", ref,
                        timeout=cfg["git_timeout"])
            if r.returncode not in (0, 1):
                raise GitReadError("git show-ref failed (exit %d)" % r.returncode)
            state.branch_exists = r.returncode == 0
            if state.branch_exists:
                r = _required_git(repo, "rev-list", "--left-right", "--count",
                                  f"HEAD...{ref}", timeout=cfg["git_timeout"])
                values = r.stdout.split()
                if len(values) != 2:
                    raise GitReadError("git rev-list returned malformed output")
                state.ahead, state.behind = map(int, values)
        states.append(state)
    # Sync-Remote zuerst; GitHub unabhängig vom tatsächlichen Namen ganz rechts.
    states.sort(key=lambda r: (r.public, not r.is_sync, r.name.lower()))
    return states


def read_branches(repo: Path, cfg: dict, *, strict: bool = False) -> list[BranchInfo]:
    """Alle lokalen Branches mit Stand, Upstream und Merge-Zustand lesen.

    Zwei Git-Aufrufe reichen: einer für die Daten, einer für die rein lesende
    Anzeige, welche Branches vollständig in HEAD stecken.
    """
    t_ = cfg["git_timeout"]
    fields = ("%(HEAD)", "%(refname:short)", "%(upstream:short)", "%(upstream:track)",
              "%(objectname:short)", "%(committerdate:short)", "%(contents:subject)")
    r = run_git(repo, "branch", "--format=" + "%00".join(fields), timeout=t_)
    if r.returncode != 0:
        if strict:
            raise GitReadError("git branch failed (exit %d)" % r.returncode)
        return []
    merged_r = run_git(repo, "branch", "--merged", "HEAD",
                       "--format=%(refname:short)", timeout=t_)
    if merged_r.returncode != 0 and strict and repo_has_head(repo, t_):
        raise GitReadError("git branch --merged failed (exit %d)" % merged_r.returncode)
    merged = {line.strip() for line in merged_r.stdout.splitlines() if line.strip()}
    branches = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\0")
        if len(parts) < 7:
            if strict:
                raise GitReadError("git branch returned malformed output")
            continue
        head, name, upstream, track, oid, date, subject = parts[:7]
        # `upstream:track` ist dank LC_ALL=C stabil englisch: "[ahead 2, behind 1]",
        # "[gone]" oder leer.
        ahead = re.search(r"ahead (\d+)", track)
        behind = re.search(r"behind (\d+)", track)
        branches.append(BranchInfo(
            name=name, is_head=head.strip() == "*", upstream=upstream,
            ahead=int(ahead.group(1)) if ahead else 0,
            behind=int(behind.group(1)) if behind else 0,
            upstream_gone="gone" in track,
            oid=oid, date=date, subject=subject, merged=name in merged,
        ))
    return branches


def repo_has_head(repo: Path, timeout: int) -> bool:
    """Gibt es in diesem Repo überhaupt schon einen Commit?

    Vor dem allerersten Commit existiert HEAD nur als Verweis ins Leere. Git
    kann dort weder etwas stashen noch auf einen früheren Stand zurückgehen.
    """
    result = run_git(repo, "rev-parse", "--verify", "-q", "HEAD",
                     timeout=timeout)
    if result.returncode == 0:
        return True
    if result.returncode == 1:  # belegter normaler Fall: unborn HEAD
        return False
    raise GitReadError("git rev-parse HEAD failed (exit %d)" % result.returncode)


def file_diff(repo: Path, code: str, path: str, timeout: int) -> tuple[bool, str]:
    """Diff einer einzelnen Datei, ohne Index oder Arbeitsbaum anzufassen.

    Unversionierte Dateien kennt `git diff` nicht — sie werden über `--no-index`
    gegen /dev/null gezeigt, damit auch neue Dateien sichtbar sind.
    """
    safe_diff = ("diff", "--no-ext-diff", "--no-textconv",
                 "--ignore-submodules=none")
    if code == "U":
        r = run_git(
            repo, *safe_diff, "--no-index", "--", os.devnull, path,
            timeout=timeout)
    elif repo_has_head(repo, timeout):
        # Gegen HEAD, damit gestagte UND ungestagte Änderungen zusammen erscheinen.
        r = run_git(
            repo, *safe_diff, "HEAD", "--", path, timeout=timeout,
            env={**RAW_OBJECT_ENV, "GIT_LITERAL_PATHSPECS": "1"})
    else:
        # Vor dem ersten Commit ist der Index kein ehrlicher Vergleichspunkt:
        # bei Status `AM` kann dort eine ältere Fassung liegen, während die
        # Commit-Hilfe anschließend absichtlich den aktuellen Arbeitsbaum stagt.
        # Deshalb genau den jetzigen Pfad vollständig gegen /dev/null zeigen.
        if not os.path.lexists(repo / path):
            return True, ""
        r = run_git(
            repo, *safe_diff, "--no-index", "--", os.devnull, path,
            timeout=timeout)
    # `git diff` meldet mit Unterschieden je nach Modus 0 oder 1 — beides ist
    # Erfolg. Exit 1 mit leerer Ausgabe und einer Fehlermeldung ist dagegen ein
    # echter Fehler: `--no-index` gegen ein unversioniertes VERZEICHNIS (Status
    # fasst dessen Dateien zu "dir/" zusammen) endet genau so — ein leerer Diff
    # wäre dann gelogen.
    if r.returncode == 0 or (r.returncode == 1 and (r.stdout or not r.stderr)):
        return True, r.stdout
    return False, (r.stderr or "").strip()[:240]


def display_remote_url(url: str) -> str:
    """Remote-Adresse für die lokale Anzeige, aber ohne eingebettete Secrets."""
    raw = url.strip()
    # Externe Remote-Helper erhalten nach ``transport::`` beliebige Befehlsdaten.
    # Deren Grammatik kennt gmf nicht; Optionen oder Payload können Secrets sein.
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*::", raw):
        return "(remote helper hidden)"
    if "://" not in raw:
        # SCP-Syntax (git@host:org/repo.git) enthält normalerweise keine Query.
        if ":" in raw and "/" not in raw.split(":", 1)[0]:
            raw = raw.split("?", 1)[0].split("#", 1)[0]
        return terminal_text(raw)

    try:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme.lower() == "file":
            clean = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
            return terminal_text(clean)
        hostname = parsed.hostname
        if not hostname:
            return terminal_text(f"{parsed.scheme}://(invalid target)")
        host = f"[{hostname}]" if ":" in hostname else hostname
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None:
            host += f":{port}"
        # Nur bei ausdruecklichen SSH-Schemata ist der Benutzer (meist `git`)
        # Teil der hilfreichen Adresse. Bei allen anderen Schemata kann schon das
        # einzelne Userinfo-Feld ein Token sein und wird vollstaendig entfernt.
        user = ""
        if parsed.scheme.lower() in _SSH_URL_SCHEMES and parsed.username:
            user = urllib.parse.quote(
                urllib.parse.unquote(parsed.username), safe="") + "@"
        clean = urllib.parse.urlunsplit(
            (parsed.scheme, user + host, parsed.path, "", ""))
        return terminal_text(clean)
    except (UnicodeError, ValueError):
        return terminal_text("(invalid remote target)")


def github_web_urls(remote: RemoteConfig) -> list[str]:
    """Anklickbare, zugangsdatenfreie Web-URLs für alle GitHub-Ziele."""
    urls = []
    for target in remote.fetch_targets + remote.push_targets:
        if not target.is_github:
            continue
        path = urllib.parse.quote(target.repo_id, safe="/-._~")
        url = "https://github.com" + path
        if url not in urls:
            urls.append(url)
    return urls


@dataclass
class InfoView:
    """Repo-Details als Text plus die Info, welche Zeilen zu welchem Remote gehören.

    Damit kann die TUI einen Auswahlbalken über die Remote-Blöcke legen, ohne den
    Text erneut zu parsen — und `repo_info_lines()` bleibt reine Textausgabe.
    """

    lines: list[str] = field(default_factory=list)
    # (Art, Name, erste Zeile, letzte Zeile) — Art ist "remote" oder "branch".
    # Beide sind auswählbar, damit ihre rein lesenden Details navigierbar sind.
    blocks: list[tuple[str, str, int, int]] = field(default_factory=list)

    @property
    def remote_blocks(self) -> list[tuple[str, int, int]]:
        return [(name, a, b) for kind, name, a, b in self.blocks if kind == "remote"]

    @property
    def remote_names(self) -> list[str]:
        return [name for name, _, _ in self.remote_blocks]


def aligned_rows(rows: list[tuple[str, str]], indent: str = "",
                 width: int | None = None) -> list[str]:
    """Label/Wert-Zeilen so setzen, dass alle Werte in derselben Spalte beginnen.

    `width` gibt die Label-Breite vor, wenn mehrere getrennt gebaute Blöcke
    dieselbe Spalte teilen sollen.
    """
    if width is None:
        width = max((cell_width(label) for label, _ in rows), default=0)
    return [f"{indent}{pad_cells(label + ':', width + 1)}  {value}"
            for label, value in rows]


def repo_info_lines(st: RepoStatus, cfg: dict) -> list[str]:
    """Read-only Repo-Details als Textzeilen; keinerlei curses-Abhängigkeit."""
    return build_info_view(st, cfg).lines


def build_info_view(st: RepoStatus, cfg: dict) -> InfoView:
    """Wie repo_info_lines, liefert zusätzlich die Zeilenbereiche der Remotes."""
    t_ = cfg["git_timeout"]

    def read_git(*args: str) -> subprocess.CompletedProcess:
        """Optionale Info darf bei einem kaputten/langsamen Repo nie die TUI beenden."""
        try:
            return run_git(st.path, *args, timeout=t_)
        except (OSError, subprocess.SubprocessError):
            return subprocess.CompletedProcess(args, 1, "", "")

    path = terminal_text(st.path)
    branch = terminal_text(st.branch)
    # Label/Wert-Paare sammeln und erst am Ende ausrichten: so stehen die Werte
    # linksbündig untereinander, statt an unterschiedlich langen Labels zu kleben.
    head_rows: list[tuple[str, str]] = [
        (t("info_path"), path),
        (t("info_branch"), branch),
    ]

    commit = read_git(
        "show", "-s", "--format=%H%x00%h%x00%aI%x00%an%x00%s", "HEAD")
    fields = commit.stdout.rstrip("\n").split("\0", 4) if commit.returncode == 0 else []
    subject_line = ""
    if len(fields) == 5:
        full_oid, short_oid, date, author, subject = map(terminal_text, fields)
        head_rows.append((t("info_head"), f"{short_oid} ({full_oid})"))
        head_rows.append((t("info_last_commit"), f"{date} · {author}"))
        subject_line = subject

        count = read_git("rev-list", "--count", "HEAD")
        shallow = read_git("rev-parse", "--is-shallow-repository")
        if count.returncode == 0:
            clone_kind = (t("info_shallow_clone")
                          if shallow.returncode == 0 and shallow.stdout.strip() == "true"
                          else t("info_full_clone"))
            head_rows.append((t("info_history"), t(
                "info_history_value", n=count.stdout.strip(), kind=clone_kind)))
    else:
        head_rows.append((t("info_head"), t("info_no_commits")))

    upstream = read_git(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if upstream.returncode == 0 and upstream.stdout.strip():
        upstream_name = upstream.stdout.strip()
        delta = read_git(
            "rev-list", "--left-right", "--count", f"HEAD...{upstream_name}")
        delta_fields = delta.stdout.split()
        if delta.returncode == 0 and len(delta_fields) == 2:
            upstream_value = (f"{terminal_text(upstream_name)} "
                              f"({t('info_delta', a=delta_fields[0], b=delta_fields[1])})")
        else:
            upstream_value = terminal_text(upstream_name)
    else:
        upstream_value = t("none_label")
    head_rows.append((t("info_upstream"), upstream_value))

    worktree = (t("info_clean") if not st.dirty else
                t("info_changes", m=st.modified, d=st.deleted,
                  u=st.untracked, c=st.conflicts))
    head_rows.append((t("info_worktree"), worktree))
    head_rows.append((t("info_stashes"), str(len(st.stashes))))

    tags = read_git("tag", "--points-at", "HEAD")
    tag_names = [terminal_text(name) for name in tags.stdout.splitlines()
                 if name.strip()] if tags.returncode == 0 else []
    head_rows.append((t("info_tags"), ", ".join(tag_names) if tag_names
                      else t("none_label")))
    if st.error:
        head_rows.append((t("info_last_error"),
                          terminal_text(st.error_long or st.error)))
    if st.error_detail:
        head_rows.append((t("info_git_said"), terminal_text(st.error_detail)))

    lines = aligned_rows(head_rows)
    if subject_line:
        # Die Commit-Betreffzeile gehört direkt unter "Letzter Commit" und nicht in
        # die Wertespalte — deshalb erst nach dem Ausrichten einschieben.
        for index, (label, _) in enumerate(head_rows):
            if label == t("info_last_commit"):
                lines.insert(index + 1, "  " + subject_line)
                break
    lines.extend(["", f"{t('info_remotes')}:"])
    view = InfoView(lines=lines)
    remote_note = ""
    try:
        configs = read_remote_configs(st.path, cfg)
    # Auch malformed Fremdkonfigurationen (etwa eine ungültige URL) sollen nur
    # diesen Abschnitt degradieren — die Branches darunter bleiben nutzbar.
    except Exception as exc:
        configs = {}
        remote_note = t("info_remote_error", e=terminal_text(exc))

    states = {remote.name: remote for remote in st.remotes}
    ordered_names = [remote.name for remote in st.remotes if remote.name in configs]
    ordered_names.extend(name for name in configs if name not in ordered_names)
    specs: list[tuple[str, str, str, list[tuple[str, str]]]] = []
    for name in ordered_names:
        remote = configs[name]
        state = states.get(name)
        web_urls = github_web_urls(remote)
        labels = []
        if state and state.is_sync:
            labels.append(t("info_sync"))
        if web_urls:
            labels.append(t("info_github"))
        if not remote.transfer_safe:
            labels.append(t("info_unsafe_remote"))
        if state and state.fetch_failed:
            labels.append(t("info_fetch_failed"))
        suffix = f" [{', '.join(labels)}]" if labels else ""
        rows: list[tuple[str, str]] = []
        fetch_shown = [display_remote_url(url) for url in remote.fetch_urls]
        push_shown = [display_remote_url(url) for url in remote.push_urls]
        if fetch_shown == push_shown:
            # Der Normalfall: eine Adresse für beides. Zwei identische Zeilen sind
            # nur Rauschen — getrennt stehen sie erst, wenn sie WIRKLICH abweichen
            # (`git remote set-url --push` erlaubt das, gmf sperrt dann Transfers).
            for url in fetch_shown:
                rows.append((t("info_fetch_push_url"), url))
        else:
            rows.extend((t("info_fetch_url"), url) for url in fetch_shown)
            rows.extend((t("info_push_url"), url) for url in push_shown)
        rows.extend((t("info_web_url"), url) for url in web_urls)
        if state and st.branch not in ("?", "(detached)"):
            value = (t("info_delta", a=state.ahead, b=state.behind)
                     if state.branch_exists else t("info_branch_missing_value"))
            rows.append((t("info_branch_label", b=branch), value))
        if state and state.fetch_failed:
            rows.append((t("info_last_error"), terminal_text(
                state.fetch_error_long
                or remote_failure_short(state.name, state.fetch_outcome or "unknown"))))
            if state.fetch_error_detail:
                rows.append((t("info_git_said"), terminal_text(
                    state.fetch_error_detail)))
        specs.append(("remote", name, f"  {terminal_text(name)}{suffix}", rows))

    specs.extend(branch_block_specs(read_branches(st.path, cfg)))
    # Eine gemeinsame Label-Breite für ALLE Detailzeilen der Seite: erst dadurch
    # stehen die Werte über Remotes und Branches hinweg in derselben Spalte.
    width = max((cell_width(label) for _, _, _, rows in specs for label, _ in rows),
                default=0)
    remote_specs = [s for s in specs if s[0] == "remote"]
    branch_specs = [s for s in specs if s[0] == "branch"]
    if remote_specs:
        render_info_blocks(view, remote_specs, width)
    else:
        lines.append("  " + (remote_note or t("none_label")))
    lines.extend(["", f"{t('info_branches')}:"])
    if branch_specs:
        render_info_blocks(view, branch_specs, width)
    else:
        lines.append("  " + t("none_label"))
    return view


def branch_block_specs(branches: list[BranchInfo]) -> list:
    """Lokale Branches als Blöcke — der zweite Zustand, den Git nie überträgt.

    Genau wie bei Remotes sammeln sich hier Reste an (abgeschlossene Features,
    alte Experimente), die niemand sieht, weil man immer nur den aktuellen
    Branch betrachtet. Die Info-Seite macht sie rein lesend sichtbar.
    """
    specs = []
    for branch in branches:
        labels = []
        if branch.is_head:
            labels.append(t("info_branch_current"))
        if branch.merged and not branch.is_head:
            labels.append(t("info_branch_merged"))
        if branch.upstream_gone:
            labels.append(t("info_branch_upstream_gone"))
        suffix = f" [{', '.join(labels)}]" if labels else ""
        rows = [(t("info_branch_commit"),
                 f"{branch.oid} · {branch.date} · {terminal_text(branch.subject)}")]
        if branch.upstream:
            rows.append((t("info_upstream"),
                         f"{terminal_text(branch.upstream)} "
                         f"({t('info_delta', a=branch.ahead, b=branch.behind)})"))
        else:
            rows.append((t("info_upstream"), t("none_label")))
        specs.append(("branch", branch.name,
                      f"  {terminal_text(branch.name)}{suffix}", rows))
    return specs


def render_info_blocks(view: InfoView, specs: list, width: int) -> None:
    """Blöcke ausgeben und ihre Zeilenbereiche für den Auswahlbalken merken."""
    for index, (kind, name, header, rows) in enumerate(specs):
        if index:
            view.lines.append("")
        first = len(view.lines)
        view.lines.append(header)
        view.lines.extend(aligned_rows(rows, indent="    ", width=width))
        view.blocks.append((kind, name, first, len(view.lines) - 1))


def inspect_transfer(repo: Path, remote: str, branch: str, action: str,
                     timeout: int = 10, *,
                     expected_public: bool | None = None) -> TransferCheck:
    """Prüft einen Push, ohne etwas zu verändern.

    Bewusst eng: sauberer Tree, vorhandener Remote-Branch und verwandte,
    fast-forward-fähige History. Neue Branches und Divergenzen gehören ins
    Terminal, wo der Mensch den Sonderfall ausdrücklich auflöst.
    """
    if branch in ("?", "(detached)"):
        return TransferCheck("detached")
    cfg = timeout_config(timeout)
    raw_env = RAW_OBJECT_ENV
    try:
        configs = read_remote_configs(repo, cfg)
        remote_cfg = configs.get(remote)
        if (remote_cfg is None or not remote_cfg.transfer_safe
                or not fetch_maps_branch_exactly(remote_cfg, branch)):
            return TransferCheck("remote-unsafe")
        actual_public = remote_cfg.fetch_targets[0].is_github
        if expected_public is not None and actual_public != expected_public:
            return TransferCheck("remote-unsafe")
        if action != "push":
            raise ValueError(f"unknown transfer action: {action}")
        urls = remote_cfg.push_urls
        if len(urls) != 1 or not _argv_safe_remote_url(urls[0]):
            return TransferCheck("remote-unsafe")
        transfer_url = (remote_cfg.push_targets[0].repo_id
                        if remote_cfg.push_targets[0].host == "local"
                        else urls[0])
        if not _argv_safe_remote_url(transfer_url):
            return TransferCheck("remote-unsafe")
        branch_r = _required_git(
            repo, "symbolic-ref", "-q", "HEAD", timeout=timeout,
            env=raw_env)
        approved_ref = branch_r.stdout.strip()
        if approved_ref != f"refs/heads/{branch}":
            return TransferCheck("inspect-failed")
        head = _required_git(
            repo, "rev-parse", "--verify", approved_ref, timeout=timeout,
            env=raw_env).stdout.strip()
        index_oid = _required_git(
            repo, "write-tree", timeout=timeout, env=raw_env).stdout.strip()
        dirty = _required_git(
            repo, "status", "--porcelain=v1", "-z", "--untracked-files=all",
            timeout=timeout,
            env=raw_env)
    except (GitReadError, ValueError, RuntimeError, OSError):
        return TransferCheck("inspect-failed")
    worktree_fingerprint = hashlib.sha256(
        dirty.stdout.encode("utf-8", "surrogateescape")).hexdigest()
    if dirty.stdout:
        return TransferCheck("dirty")
    ref = f"refs/remotes/{remote}/{branch}"
    exists = run_git(
        repo, "show-ref", "--verify", "--quiet", ref, timeout=timeout,
        env=raw_env)
    if exists.returncode == 1:
        return TransferCheck("missing-branch", remote_ref=ref)
    if exists.returncode != 0:
        return TransferCheck("inspect-failed", remote_ref=ref)
    try:
        target_oid = _required_git(
            repo, "rev-parse", "--verify", ref, timeout=timeout,
            env=raw_env).stdout.strip()
    except GitReadError:
        return TransferCheck("inspect-failed", remote_ref=ref)
    delta = run_git(
        repo, "rev-list", "--left-right", "--count",
        f"{head}...{target_oid}", timeout=timeout, env=raw_env)
    if delta.returncode != 0 or len(delta.stdout.split()) != 2:
        return TransferCheck("inspect-failed", remote_ref=ref)
    ahead_s, behind_s = delta.stdout.split()
    ahead, behind = int(ahead_s), int(behind_s)
    if ahead and behind:
        return TransferCheck("divergent", ahead=ahead, behind=behind, remote_ref=ref)
    if behind:
        return TransferCheck("behind", ahead=ahead, behind=behind, remote_ref=ref)
    if not ahead:
        return TransferCheck("nothing-push", remote_ref=ref)
    commits_r = run_git(
        repo, "log", "--oneline", "--no-decorate",
        f"{target_oid}..{head}", timeout=timeout, env=raw_env)
    # Nicht nur die beiden Endbaeume vergleichen: Eine Datei kann in einem
    # ausgehenden Commit hinzugefuegt und in einem spaeteren wieder geloescht
    # worden sein. Sie waere trotzdem Teil der veroeffentlichten Historie. `-m`
    # zeigt Merge-Commits gegen jeden Elternteil; doppelte Statuszeilen sind als
    # konservativer Privacy-Hinweis beabsichtigt.
    files_r = run_git(
        repo, "log", "-m", "--root", "--format=", "--name-status",
        "--no-renames", "--no-ext-diff", "--no-textconv",
        "--ignore-submodules=none",
        f"{target_oid}..{head}", timeout=timeout, env=raw_env)
    if commits_r.returncode != 0 or files_r.returncode != 0:
        return TransferCheck("inspect-failed", ahead, behind, ref)
    try:
        # Die getrennten Git-Leser bilden nur dann einen freigegebenen Snapshot,
        # wenn Branch-Ref, dessen OID, Index und Arbeitsbaum am Ende noch exakt
        # zusammenpassen. Der Push selbst verwendet anschließend diese feste OID.
        final_ref = _required_git(
            repo, "symbolic-ref", "-q", "HEAD", timeout=timeout,
            env=raw_env).stdout.strip()
        final_head = _required_git(
            repo, "rev-parse", "--verify", approved_ref, timeout=timeout,
            env=raw_env).stdout.strip()
        final_index = _required_git(
            repo, "write-tree", timeout=timeout, env=raw_env).stdout.strip()
        final_dirty = _required_git(
            repo, "status", "--porcelain=v1", "-z", "--untracked-files=all",
            timeout=timeout, env=raw_env)
    except GitReadError:
        return TransferCheck("inspect-failed", ahead, behind, ref)
    if (final_ref != approved_ref or final_head != head
            or final_index != index_oid or final_dirty.stdout != dirty.stdout):
        return TransferCheck("inspect-failed", ahead, behind, ref)
    return TransferCheck(
        "ready", ahead=ahead, behind=behind, remote_ref=ref,
        commits=[line for line in commits_r.stdout.splitlines() if line.strip()],
        files=[line for line in files_r.stdout.splitlines() if line.strip()],
        branch=branch, head_oid=head, index_oid=index_oid,
        worktree_fingerprint=worktree_fingerprint,
        fetch_fingerprint=remote_cfg.fetch_targets[0].fingerprint,
        push_fingerprint=remote_cfg.push_targets[0].fingerprint,
        remote_name=remote,
        remote_config_signature=(tuple(remote_cfg.fetch_urls),
                                 tuple(remote_cfg.push_urls),
                                 tuple(remote_cfg.settings)),
        transfer_url=transfer_url,
        target_oid=target_oid,
    )


def safe_push_args(destination: str, branch: str, source_oid: str,
                   target_oid: str) -> tuple[str, ...]:
    """Push an die geprüfte URL, OID-/Lease-gebunden, ohne Hooks/Submodule/Tags."""
    lease = f"--force-with-lease=refs/heads/{branch}:{target_oid}"
    pin_config, pinned_url = _pinned_url_config(destination)
    return (*pin_config, *NO_GIT_HOOKS_ARGS,
            "-c", "push.pushOption=",
            "push", "--porcelain", "--no-follow-tags",
            "--no-signed", "--recurse-submodules=no", lease, "--", pinned_url,
            f"{source_oid}:refs/heads/{branch}")


def update_tracking_after_push(repo: Path, check: TransferCheck,
                               timeout: int) -> bool:
    """Den geprüften Tracking-Ref nach belegtem Push-Erfolg per OID-CAS nachziehen.

    Der Remote-Name ist veränderliche Konfiguration. Vor und nach der Mutation
    muss deshalb exakt derselbe URL-/Refspec-Snapshot gelten wie beim Push.
    Ändert er sich im Fenster, wird unsere eigene CAS-Aktualisierung ebenfalls
    per CAS zurückgenommen; ein paralleler Fetch wird dabei nie überschrieben.
    """
    def config_matches() -> bool:
        remote = read_remote_config_or_none(
            repo, timeout_config(timeout), check.remote_name)
        if remote is None:
            return False
        signature = (tuple(remote.fetch_urls), tuple(remote.push_urls),
                     tuple(remote.settings))
        return (signature == check.remote_config_signature
                and remote.transfer_safe
                and fetch_maps_branch_exactly(remote, check.branch))

    if not config_matches():
        return False
    try:
        updated = run_git_logged(
            repo, *safe_update_ref_args(
                "--no-deref", check.remote_ref,
                check.head_oid, check.target_oid), timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return False
    if updated.returncode == 0:
        if config_matches():
            return True
        try:
            run_git_logged(
                repo, *safe_update_ref_args(
                    "--no-deref", check.remote_ref,
                    check.target_oid, check.head_oid), timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return False
    # Ein paralleler Fetch kann denselben Zielstand schon eingetragen haben.
    try:
        current = run_git(
            repo, "rev-parse", "--verify", check.remote_ref, timeout=timeout,
            env=RAW_OBJECT_ENV)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return (current.returncode == 0
            and current.stdout.strip() == check.head_oid
            and config_matches())


def upstream_delta(repo: Path, sync_remote: str | None,
                   cfg: dict) -> tuple[str | None, int, int]:
    """Stand gegenüber dem konfigurierten Upstream, WENN dieser ein anderer
    Remote als der Sync-Remote ist (typisch: github).

    -> (upstream_ref oder None, ahead, behind). None, wenn kein (fremder) Upstream
    gesetzt ist oder dessen Tracking-Ref fehlt. Basis ist der letzte fetch-Stand
    dieses Remotes (wir fetchen hier NICHT übers Netz nach — wie in einem Editor).
    """
    t_ = cfg["git_timeout"]
    r = run_git(repo, "rev-parse", "--abbrev-ref", "@{upstream}", timeout=t_)
    if r.returncode != 0:
        return None, 0, 0
    up = r.stdout.strip()                 # z.B. "github/main"
    up_remote = up.split("/", 1)[0]
    if not up or up_remote == sync_remote:
        return None, 0, 0                 # kein Upstream oder == Sync-Remote (schon gezeigt)
    r = run_git(repo, "rev-list", "--left-right", "--count", f"HEAD...{up}", timeout=t_)
    if r.returncode != 0:
        return None, 0, 0                 # Tracking-Ref (noch) nicht lokal vorhanden
    ahead, behind = r.stdout.split()
    return up, int(ahead), int(behind)


def collect_status(repo: Path, root: Path, cfg: dict, fetch: bool = False) -> RepoStatus:
    """Kompletten Zustand eines Repos einsammeln (läuft parallel in Threads)."""
    rel = str(repo.relative_to(root)) if repo != root else repo.name
    # macOS liefert Dateinamen je nach Herkunft in NFD ("ö" = "o" + kombinierender
    # Punkt = 2 Codepoints) oder NFC (1 Codepoint). Fuer die Anzeige zaehlt aber die
    # Zahl der Terminal-Zellen: bei NFD verrechnet sich len() und die Spalten hinter
    # dem Namen verrutschen. rel ist reiner Anzeigename (der echte Pfad steht in
    # st.path), deshalb hier auf NFC normalisieren — das haelt zugleich die
    # --json-Ausgabe ueber Macs hinweg vergleichbar.
    rel = unicodedata.normalize("NFC", rel)
    st = RepoStatus(path=repo, rel=rel)
    t_ = cfg["git_timeout"]
    detached = False
    try:
        # Branch (oder detached HEAD)
        r = run_git(repo, "symbolic-ref", "--short", "-q", "HEAD", timeout=t_)
        if r.returncode == 0:
            st.branch = r.stdout.strip()
        else:
            _required_git(repo, "rev-parse", "--verify", "HEAD", timeout=t_)
            st.branch = "(detached)"
            st.remote_state = "detached"
            detached = True
        st.head_oid = current_head(repo, t_)

        # Arbeitsverzeichnis-Zustand
        r = _required_git(repo, "status", "--porcelain=v1", "-z",
                          "--untracked-files=all", timeout=t_)
        st.modified, st.deleted, st.untracked, st.conflicts, st.files = parse_porcelain(
            r.stdout)
        # Stashes (leicht zu übersehen — deshalb deutlich anzeigen)
        r = _required_git(repo, "stash", "list", "--format=%gd %gs", timeout=t_)
        st.stashes = [l for l in r.stdout.splitlines() if l.strip()]

        # Vergleich mit ALLEN Remotes (auf Basis des letzten fetch-Stands).
        configs = read_remote_configs(repo, cfg)
        st.remote = detect_sync_remote(repo, cfg, configs)
        fetch_failures: dict[str, tuple[str, str, str]] = {}
        if fetch:
            # R aktualisiert nicht nur alle Repos, sondern je Repo auch alle Remotes.
            # Fetch verändert weder Branch noch Working Tree. Als bewusst
            # ausgelöste, zustandsändernde Aktion gehört er ins Befehlsprotokoll.
            for name, remote in configs.items():
                block_reason = fetch_remote_block_reason(remote, st.branch)
                if block_reason is not None:
                    fetch_failures[name] = (
                        block_reason,
                        remote_check_message(
                            name, block_reason, 0, "", cfg["fetch_timeout"]),
                        "",
                    )
                    continue
                try:
                    fetched = fetch_remote_safely(
                        repo, remote, st.branch, cfg["fetch_timeout"])
                except RemoteConfigChangedError:
                    outcome, detail = "changed", ""
                except FetchTrackingTimeout:
                    outcome, detail = "outcome_unknown", ""
                except subprocess.TimeoutExpired:
                    outcome, detail = "timeout", ""
                except OSError:
                    # Fetch oder Tracking-CAS können bereits gewirkt haben.
                    # Der Fehler gehört nur zu diesem Remote; weitere sichere
                    # Remotes werden trotzdem aktualisiert und angezeigt.
                    outcome, detail = "outcome_unknown", ""
                else:
                    if fetched.returncode == 0:
                        continue
                    outcome = classify_remote_check(
                        fetched,
                        keychain_helper=remote_uses_keychain_helper(
                            repo, name, cfg["git_timeout"]),
                    )
                    detail = last_error_line(fetched)
                fetch_failures[name] = (
                    outcome,
                    remote_check_message(
                        name, outcome, 0, detail, cfg["fetch_timeout"]),
                    detail,
                )
            # Auch nach einem Teilfehler sind vorhandene Remotes und ihre zuletzt
            # bekannten Tracking-Refs wertvoll. Ohne sie sähe ein Auth-Fehler wie
            # ein gelöschtes Remote aus und erzeugte irreführende DRIFT-Zeilen.
            fetched_configs = configs
            configs = read_remote_configs(repo, cfg)
            for name in set(fetched_configs) | set(configs):
                if fetched_configs.get(name) != configs.get(name):
                    fetch_failures[name] = (
                        "changed",
                        remote_check_message(
                            name, "changed", 0, "", cfg["fetch_timeout"]),
                        "",
                    )
            st.remote = detect_sync_remote(repo, cfg, configs)
        st.remotes = collect_remote_statuses(
            repo, st.branch, st.remote, cfg, configs,
            fetch_failures=fetch_failures)
        summarize_fetch_failures(st)
        removed_during_fetch = set(fetch_failures) - set(configs)
        if removed_during_fetch:
            messages = [remote_check_message(
                name, "changed", 0, "", cfg["fetch_timeout"])
                for name in sorted(removed_during_fetch)]
            st.error = "; ".join(filter(None, [st.error, *(
                f"{name}: {t('short_changed')}" for name in sorted(
                    removed_during_fetch))]))
            st.error_long = " ".join(filter(None, [st.error_long, *messages]))
            st.remote_state = "error"
            st.fetch_error = True
        if st.remote is None:
            if not st.error and not detached:
                st.remote_state = "no-remote"
            st.upstream, st.upstream_ahead, st.upstream_behind = upstream_delta(
                repo, None, cfg)
            return st
        if detached:
            return st
        sync = next((remote for remote in st.remotes if remote.is_sync), None)
        if sync is None or not sync.branch_exists:
            if not st.error:
                st.remote_state = "no-branch"
            return st
        st.ahead, st.behind = sync.ahead, sync.behind

        # Zusatz: Stand gegenüber einem fremden Upstream (z.B. github).
        st.upstream, st.upstream_ahead, st.upstream_behind = upstream_delta(
            repo, st.remote, cfg)
    except subprocess.TimeoutExpired:
        st.error = t("git_timeout")
        st.remote_state = "error"
    except Exception as exc:  # Ein kaputtes Repo darf die Übersicht nicht killen.
        st.error = str(exc)
        st.remote_state = "error"
    return st


def carry_fetch_failure(old: RepoStatus, new: RepoStatus,
                        refetched: str | None = None) -> None:
    """Einen bekannten Fetch-Fehler in den frisch gelesenen Zustand übernehmen.

    `collect_status` ohne `fetch` geht nur über lokale Daten und weiß deshalb
    nichts von einem toten Remote. Ohne diese Übernahme sähe ein Repo nach jeder
    Aktion (Commit, Stash, abgebrochener Dialog) plötzlich sauber aus, obwohl der
    letzte Fetch gescheitert ist — der Fehler verschwände scheinbar von selbst.
    Ein neuer Fetch überschreibt das ordnungsgemäß.

    Zwei Grenzen halten den Fehler ehrlich: `refetched` ist der Name eines
    Remotes, dessen Fetch soeben nachweislich gelang — sein alter Fehlerzustand
    ist damit widerlegt und bleibt nicht als rotes Badge stehen. Und übernommen
    werden nur Fehler aus einem Fetch (`fetch_error`); ein lokaler Lese-/
    Indexfehler wird vom frischen Lauf entweder erneut festgestellt oder er ist
    repariert.
    """
    failed = {remote.name: remote for remote in old.remotes
              if remote.fetch_failed and remote.name != refetched}
    if not failed:
        return
    for remote in new.remotes:
        previous = failed.get(remote.name)
        same_endpoint = previous and (
            tuple(previous.fetch_fingerprints),
            tuple(previous.push_fingerprints),
            previous.public,
            previous.mixed_public,
            previous.target_mismatch,
            previous.multiple_pushurls,
            previous.fetch_refspecs_safe,
            previous.fetch_refspec_fingerprint,
            previous.branch_mapping_safe,
            previous.fetch_url_safe,
            previous.push_url_safe,
        ) == (
            tuple(remote.fetch_fingerprints),
            tuple(remote.push_fingerprints),
            remote.public,
            remote.mixed_public,
            remote.target_mismatch,
            remote.multiple_pushurls,
            remote.fetch_refspecs_safe,
            remote.fetch_refspec_fingerprint,
            remote.branch_mapping_safe,
            remote.fetch_url_safe,
            remote.push_url_safe,
        )
        if same_endpoint:
            remote.fetch_failed = True
            remote.fetch_outcome = previous.fetch_outcome
            remote.fetch_error_long = previous.fetch_error_long
            remote.fetch_error_detail = previous.fetch_error_detail
    if not new.error:
        # Neu aus den tatsächlich verbleibenden Remotes zusammensetzen. Der alte
        # Sammeltext könnte noch ein inzwischen erfolgreich refetchtes Remote
        # nennen und wäre dann ebenso veraltet wie sein rotes Badge.
        summarize_fetch_failures(new)


def collect_all(root: Path, cfg: dict, fetch: bool = False,
                progress=None) -> list[RepoStatus]:
    """Alle Repos parallel einsammeln; optional Fortschritts-Callback (done, total)."""
    repos = find_repos(root, cfg["skip_dirs"])
    results: list[RepoStatus] = []
    workers = FETCH_SCAN_WORKERS if fetch else LOCAL_SCAN_WORKERS
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(collect_status, r, root, cfg, fetch) for r in repos]
        for done, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            results.append(fut.result())
            if progress:
                progress(done, len(repos))
    results.sort(key=lambda s: (s.severity(), s.rel.lower()))
    return results


# ---------------------------------------------------------------------------
# Nicht-interaktive Ausgabe (--list / --json / kein TTY)
# ---------------------------------------------------------------------------

def status_dict(st: RepoStatus) -> dict:
    return {
        "path": terminal_text(st.path), "rel": terminal_text(st.rel),
        "branch": st.branch,
        "remote": st.remote, "remote_state": st.remote_state,
        "ahead": st.ahead, "behind": st.behind,
        "upstream": st.upstream,
        "upstream_ahead": st.upstream_ahead, "upstream_behind": st.upstream_behind,
        "remotes": [
            {"name": r.name, "public": r.public, "mixed_public": r.mixed_public,
             "sync": r.is_sync,
             "branch_exists": r.branch_exists, "ahead": r.ahead, "behind": r.behind,
             "fetch_fingerprint": r.fetch_fingerprint,
             "fetch_fingerprints": r.fetch_fingerprints,
             "push_fingerprints": r.push_fingerprints,
             "target_mismatch": r.target_mismatch,
             "multiple_pushurls": r.multiple_pushurls,
             "fetch_refspecs_safe": r.fetch_refspecs_safe,
            "fetch_refspec_fingerprint": r.fetch_refspec_fingerprint,
            "branch_mapping_safe": r.branch_mapping_safe,
            "fetch_url_safe": r.fetch_url_safe,
            "push_url_safe": r.push_url_safe,
             # Bewusst NICHT im --diff-Vergleich: dieser Zustand hängt am Netz des
             # jeweiligen Rechners, sonst meldete eine Offline-Seite lauter Drift.
             "fetch_failed": r.fetch_failed,
             "fetch_outcome": r.fetch_outcome,
             "fetch_error_long": r.fetch_error_long,
             "fetch_error_detail": r.fetch_error_detail}
            for r in st.remotes
        ],
        "modified": st.modified, "deleted": st.deleted, "untracked": st.untracked,
        "conflicts": st.conflicts,
        "stashes": len(st.stashes), "clean_and_synced": st.clean_and_synced,
        "error": st.error,
        "error_long": st.error_long,
        # Aus demselben Grund wie `fetch_failed` oben nicht als Wert verglichen,
        # sondern als Schalter benutzt: Ein gescheiterter Fetch beschreibt die
        # Sitzung, die gemessen hat (kein Netz, gesperrter Schlüsselbund), nicht
        # das Repo. `diff_status` blendet damit `error` und `remote_state` aus.
        "fetch_error": st.fetch_error,
    }


# --- Comparing two machines (--diff) ---------------------------------------
# Why this exists: remotes live in .git/config and git NEVER transfers them. Clone a
# repo on machine A, add a `github` remote there, and machine B simply doesn't have it
# — so a pending push is invisible on B. Same for branches you don't have checked out.
# Nothing warns you; you have to look. This makes looking a one-liner.
#
# The remote side runs THIS script via `ssh HOST python3 - --json <root>`, i.e. piped
# over stdin. Two nice consequences: gitmaster_flash needs no installation there, and
# both sides always run the identical version (no drift to reason about). The remote
# only needs python3 + git.


def _remote_root(local_root: Path, spec_path: str | None) -> str:
    """Which directory to scan on the other machine.

    `HOST:/some/path` wins. Otherwise: if the local root sits under $HOME, use the same
    path relative to the REMOTE $HOME (home dirs differ — /Users/anna/git vs
    /home/bob/git). Only if it is outside $HOME do we reuse the absolute path."""
    if spec_path:
        return spec_path
    home = Path.home()
    try:
        relative = str(local_root.relative_to(home))
        return "~" if relative == "." else "~/" + relative
    except ValueError:
        return str(local_root)


def _valid_remote_payload(payload) -> bool:
    """Den --json-Vertrag direkt an der nicht vertrauenswürdigen ssh-Grenze prüfen."""
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("version"), str):
        return False
    repos = payload.get("repos")
    if not isinstance(repos, list):
        return False
    for repo in repos:
        if not isinstance(repo, dict) or not isinstance(repo.get("rel"), str):
            return False
        for field_name in ("modified", "deleted", "untracked", "conflicts", "stashes"):
            if field_name in repo and type(repo[field_name]) is not int:
                return False
        if "fetch_error" in repo and type(repo["fetch_error"]) is not bool:
            return False
        for field_name in ("branch", "remote_state", "error"):
            if field_name in repo and not isinstance(repo[field_name], str):
                return False
        remotes = repo.get("remotes", [])
        if not isinstance(remotes, list):
            return False
        for remote in remotes:
            if not isinstance(remote, dict) or not isinstance(remote.get("name"), str):
                return False
            for field_name in ("ahead", "behind"):
                if field_name in remote and type(remote[field_name]) is not int:
                    return False
            for field_name in ("public", "mixed_public", "sync", "branch_exists",
                               "target_mismatch", "multiple_pushurls",
                               "fetch_refspecs_safe", "branch_mapping_safe",
                               "fetch_url_safe", "push_url_safe",
                               "fetch_failed"):
                if field_name in remote and type(remote[field_name]) is not bool:
                    return False
            for field_name in ("fetch_fingerprint", "fetch_refspec_fingerprint",
                               "fetch_outcome",
                               "fetch_error_long", "fetch_error_detail"):
                if field_name in remote and not isinstance(remote[field_name], str):
                    return False
            for field_name in ("fetch_fingerprints", "push_fingerprints"):
                values = remote.get(field_name, [])
                if (not isinstance(values, list)
                        or any(not isinstance(value, str) for value in values)):
                    return False
    return True


def fetch_remote_status(host: str, root: str, *, fetch: bool,
                        process_timeout: int | None = 300) -> dict:
    """Run this very script on `host` over ssh and return its --json output."""
    if not host or host.startswith("-") or terminal_text(host) != host:
        raise RuntimeError(t("diff_bad_host"))
    # Die Fehlertexte im JSON (z.B. `error`) sind lokalisiert. Ohne --lang wählte
    # die Gegenseite ihre Sprache selbst (Config/Locale) — derselbe Fetch-Fehler
    # sähe im Vergleich dann wie DRIFT aus. Deshalb bekommt sie unsere UI-Sprache
    # fest vorgegeben; sie bestimmt ohnehin nur die Darstellung.
    remote_args = ["python3", "-", "--json", "--lang", UI_LANG]
    if fetch:
        remote_args.append("--fetch")
    # Ein Pfad darf selbst mit `-` beginnen. Ohne Optionsende würde etwa der
    # ausdrücklich angegebene Ordner `--fetch` auf der Gegenseite zum Schalter
    # und löste einen nicht freigegebenen Netz-Fetch im falschen Verzeichnis aus.
    remote_args.append("--")
    remote_args.append(root)
    # ssh passes one remote command string to the remote shell. Quote every argv
    # element here; root remains exactly one argument even with spaces/metacharacters.
    inner = shlex.join(remote_args)
    safe_host = terminal_text(host)[:120]

    def ssh_error(detail: str) -> RuntimeError:
        safe_detail = terminal_text(redact_remote_error(detail))[:120]
        return RuntimeError(t("diff_ssh_failed", h=safe_host, e=safe_detail))

    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            r = _run_process_group(
                ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host, inner],
                stdin=fh, timeout=process_timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise ssh_error(str(e)[:100])
    # `--json` behält den normalen CLI-Exit-Code bei: 1 bedeutet, dass mindestens
    # ein Repo Aufmerksamkeit braucht. Das JSON ist trotzdem vollständig und muss
    # für den Rechnervergleich ausgewertet werden. Nur echte SSH-/Prozessfehler
    # (Exit-Codes außerhalb 0/1) machen die Gegenstelle unerreichbar.
    if r.returncode not in (0, 1):
        last = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
        raise ssh_error(last[-1] if last else
                        t("diff_ssh_exit", code=r.returncode))
    if not (r.stdout or "").strip():
        last = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
        raise ssh_error(last[-1] if last else t("diff_ssh_no_output"))
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise ssh_error(t("diff_ssh_bad_json"))
    if not _valid_remote_payload(payload):
        raise ssh_error(t("diff_ssh_bad_schema"))
    return payload


def _remotes_by_name(repo: dict) -> dict:
    return {x["name"]: x for x in (repo.get("remotes") or [])}


def _loc(name: str) -> str:
    """Ortsangabe fuer die Diff-Zeilen: Hostnamen bekommen eine Praeposition
    ("auf mymac"/"on mymac"), die eigene Maschine bleibt nackt — "auf hier" ist
    kein Deutsch, "hier" schon."""
    return name if name == t("diff_here") else t("diff_on", m=name)


def diff_status(here: dict, there: dict, here_name: str, there_name: str) -> list:
    """Compare two --json payloads -> list of difference lines. Pure -> testable.

    The split matters more than the comparison: `DRIFT` = should be identical but
    isn't (actionable); `SYNC` = both machines agree but sit ahead/behind the sync
    remote (invisible in a pure two-machine diff, yet exactly the number you care
    about); `local` = explainable (different branch checked out, dirty working
    tree). A report that lists everything gets ignored.

    Ein gescheiterter Fetch zaehlt ausdruecklich NICHT als Unterschied. Er
    beschreibt die Sitzung, die gemessen hat — kein Netz, oder ein Schluesselbund,
    an den nur die GUI-Sitzung kommt. Genau daran ist am 2026-08-05 ein ganzer
    Bericht gescheitert: `gmf --diff` laeuft auf der Gegenseite per ssh, dort
    verweigerte der Credential-Helper jedes GitHub-Remote, und das erschien als
    zwei DRIFT-Zeilen (`error`, `remote_state`) je Repo. Deshalb blendet der
    Vergleich diese beiden Felder aus, sobald eine Seite `fetch_error` meldet, und
    nennt die betroffene Seite stattdessen EINMAL als `lokal`."""
    out = []
    if here.get("version") != there.get("version"):
        out.append(t("diff_version", a=here_name, va=here.get("version"),
                     b=there_name, vb=there.get("version")))
    here_at, there_at = _loc(here_name), _loc(there_name)
    # Je Seite gezaehlt, damit die Erklaerung EINMAL erscheint und nicht je Repo.
    fetch_gescheitert: dict[str, int] = {}
    ra = {r["rel"]: r for r in here.get("repos", [])}
    rb = {r["rel"]: r for r in there.get("repos", [])}
    for rel in sorted(set(ra) - set(rb)):
        out.append(t("diff_only_on", rel=rel, m=here_at))
    for rel in sorted(set(rb) - set(ra)):
        out.append(t("diff_only_on", rel=rel, m=there_at))

    for rel in sorted(set(ra) & set(rb)):
        x, y = ra[rel], rb[rel]
        xr, yr = _remotes_by_name(x), _remotes_by_name(y)
        for name, mine, other in ((here_at, xr, yr), (there_at, yr, xr)):
            for rn in sorted(set(mine) - set(other)):
                out.append(t("diff_remote_missing", rel=rel, r=rn, m=name))
        # Sync-Remote zuerst: sein Stand ist die interessantere Zahl als z.B. github.
        for rn in sorted(set(xr) & set(yr),
                         key=lambda n: (not (xr[n].get("sync") or yr[n].get("sync")), n)):
            pa, pb = xr[rn], yr[rn]
            # `branch_exists` fehlt hier bewusst: es hängt am jeweils
            # ausgecheckten Branch und ist damit kein Endpunkt-Merkmal — zwei
            # Rechner mit identischen Remotes, aber verschiedenen Branches,
            # meldeten sonst dauerhaft eine Sicherheits-Drift.
            security_fields = ("public", "mixed_public", "sync",
                               "fetch_fingerprints", "push_fingerprints",
                               "target_mismatch", "multiple_pushurls",
                               "fetch_refspecs_safe", "fetch_refspec_fingerprint",
                               "fetch_url_safe", "push_url_safe")
            left = tuple(pa.get(k) for k in security_fields)
            right = tuple(pb.get(k) for k in security_fields)
            # Abwaertskompatibel zu Payloads vor 0.18.8: Dort gab es nur den
            # Singularwert, der bei genau einer URL dieselbe Aussage traegt.
            if "fetch_fingerprints" not in pa:
                left = (*left[:3], [pa.get("fetch_fingerprint")], *left[4:])
            if "fetch_fingerprints" not in pb:
                right = (*right[:3], [pb.get("fetch_fingerprint")], *right[4:])
            valid_same_branch = (x.get("branch") == y.get("branch")
                                 and x.get("branch") not in (None, "?", "(detached)"))
            if (left != right or (valid_same_branch
                                  and pa.get("branch_mapping_safe")
                                  != pb.get("branch_mapping_safe"))):
                out.append(t("diff_remote_security", rel=rel, r=rn))
            if pa.get("fetch_failed") or pb.get("fetch_failed"):
                # Scheiterte der Fetch dieses Remotes auf einer Seite, steht dort
                # ein veralteter Tracking-Ref. Ahead/Behind und "kennt den Branch"
                # beschrieben dann die misslungene Messung, nicht die Rechner —
                # genau der Fehlalarm, den dieser Vergleich vermeiden soll. Die
                # Sicherheitsfelder oben bleiben vergleichbar: sie stammen aus der
                # Konfiguration und nicht aus dem Fetch.
                continue
            sa = (pa.get("ahead"), pa.get("behind"))
            sb = (pb.get("ahead"), pb.get("behind"))
            if (x.get("branch") == y.get("branch")
                    and bool(pa.get("branch_exists")) != bool(pb.get("branch_exists"))):
                # Gleicher Branch, aber nur eine Seite kennt ihn auf dem Remote:
                # DAS ist der echte Zustandsunterschied. Die Ahead/Behind-Zahlen
                # der Seite ohne Branch wären daneben nur irreführende Nullen.
                side = here_at if pa.get("branch_exists") else there_at
                out.append(t("diff_remote_branch", rel=rel, r=rn,
                             br=x.get("branch"), m=side))
            elif sa != sb:
                out.append(t("diff_remote_state", rel=rel, r=rn,
                             a=here_at, aa=pa.get("ahead"), ab=pa.get("behind"),
                             b=there_at, ba=pb.get("ahead"), bb=pb.get("behind")))
            elif ((pa.get("sync") or pb.get("sync"))
                  and (pa.get("ahead") or pa.get("behind"))):
                # Beide Rechner gleichauf, aber gemeinsam neben dem Sync-Remote:
                # im reinen Zwei-Rechner-Vergleich unsichtbar, trotzdem Handlungsbedarf.
                out.append(t("diff_sync_even", rel=rel, r=rn,
                             aa=pa.get("ahead"), ab=pa.get("behind")))
        if x.get("branch") != y.get("branch"):
            out.append(t("diff_branch", rel=rel, a=here_at, ba=x.get("branch"),
                         b=there_at, bb=y.get("branch")))
        # Scheiterte auf einer Seite der Fetch, sagen `error` und `remote_state`
        # nichts ueber das Repo, sondern nur etwas ueber jene Sitzung. Konflikte
        # und Stashes sind davon unberuehrt und werden weiter verglichen.
        #
        # Die beiden Felder werden dabei unterschiedlich behandelt:
        # `error` wird nur auf DER Seite geleert, deren Fetch scheiterte. Sonst
        # verdeckte ein Fetch-Problem hier einen echten lokalen Schaden drueben
        # (unlesbarer Index z.B.) — der haette mit dem Fetch nichts zu tun.
        # `remote_state` beschreibt dagegen den Stand gegenueber dem Remote und
        # ist ohne gelungenen Fetch auf KEINER Seite vergleichbar; er faellt
        # deshalb ganz aus dem Vergleich.
        messbar = not (x.get("fetch_error") or y.get("fetch_error"))
        werte = {
            # "" heisst hier "kein (messbarer) Fehler" — dieselbe Schreibweise,
            # die ein fehlerfreies Repo ohnehin liefert.
            "error": ("" if x.get("fetch_error") else (x.get("error") or ""),
                      "" if y.get("fetch_error") else (y.get("error") or "")),
            "conflicts": (x.get("conflicts"), y.get("conflicts")),
            "stashes": (x.get("stashes"), y.get("stashes")),
        }
        if messbar:
            werte["remote_state"] = (x.get("remote_state"), y.get("remote_state"))
        for field_name, (va, vb) in werte.items():
            if va != vb:
                out.append(t("diff_repo_field", rel=rel, field=field_name,
                             a=here_at, va=va, b=there_at, vb=vb))
        for name, r in ((here_at, x), (there_at, y)):
            if r.get("fetch_error"):
                fetch_gescheitert[name] = fetch_gescheitert.get(name, 0) + 1
        for name, r in ((here_at, x), (there_at, y)):
            n = (r.get("modified") or 0) + (r.get("untracked") or 0) + (r.get("deleted") or 0)
            if n:
                out.append(t("diff_dirty", rel=rel, m=name, n=n))
    for name in (here_at, there_at):
        if fetch_gescheitert.get(name):
            out.append(t("diff_fetch_failed_side", m=name, n=fetch_gescheitert[name]))
    return [terminal_text(line) for line in out]


def run_diff(spec: str, root: Path, cfg: dict, *, fetch: bool, as_json: bool) -> int:
    """Rechner vergleichen; Config-Erzeugung und optionaler Fetch sind die Ausnahmen."""
    host, _, path = spec.partition(":")
    if not host or host.startswith("-") or terminal_text(host) != host:
        print(t("diff_bad_host") if host else t("diff_need_host"), file=sys.stderr)
        return 2
    try:
        remote_root = _remote_root(root, path or None)
        # Die Gegenseite begrenzt jeden Git-Aufruf einzeln. Zusätzlich bleibt
        # eine großzügige, konfigurierbare harte Grenze für den gesamten SSH-
        # Prozess: Nach erfolgreichem Verbindungsaufbau können auch Python,
        # Dateisystem oder Sitzung selbst hängen bleiben.
        there = fetch_remote_status(
            host, remote_root, fetch=fetch,
            process_timeout=cfg["diff_timeout"])
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    here = {"version": __version__, "root": str(root),
            "repos": [status_dict(s) for s in collect_all(root, cfg, fetch=fetch)]}
    lines = diff_status(here, there, t("diff_here"), host)
    if as_json:
        print(json.dumps({"here": here.get("version"), "host": host,
                          "differences": lines}, indent=2, ensure_ascii=False))
    else:
        print("\n".join(lines) if lines else t("diff_same", h=host))
    return 1 if lines else 0


# ---------------------------------------------------------------------------
# Kompakte Übersicht (mehrspaltig, wie `ls`)
# ---------------------------------------------------------------------------
# Bei 60+ Repos ist die einzeilige Detailansicht vor allem eines: lang. Die
# kompakte Ansicht nutzt die Breite statt der Höhe — pro Repo nur Name und ein
# Symbol, dafür drei bis vier Spalten nebeneinander und der ganze Bestand auf
# einen Blick. Die Details holt man sich mit M (umschalten) oder I zurück.

def compact_mark(st: RepoStatus) -> tuple[str, int]:
    """Kürzestmögliche Zustandsmarke eines Repos plus ihre Farbe.

    Reihenfolge nach Dringlichkeit: kaputter Zugriff, Konflikt, offene Änderungen,
    Stash, Abstand zum Sync-Remote — und ✔, wenn nichts davon zutrifft.
    """
    if st.error or any(remote.fetch_failed for remote in st.remotes):
        return "✘", C_RED
    if st.conflicts:
        return "⚠", C_RED
    if st.modified or st.deleted or st.untracked:
        return "●", C_RED
    if st.stashes:
        return "⚑", C_YELLOW
    if st.ahead and st.behind:
        # Divergenz bekommt EIN Zeichen: die genauen Zahlen stehen im Detail und
        # würden die Markenspalte für alle anderen unnötig verbreitern.
        return "⇅", C_RED
    if st.ahead or st.behind:
        return (f"↑{st.ahead}" if st.ahead else f"↓{st.behind}"), (
            C_RED if st.behind else C_YELLOW)
    if st.remote_state in ("no-remote", "no-branch", "detached"):
        return "?", C_YELLOW
    return "✔", C_GREEN


def compact_cells(statuses: list[RepoStatus]) -> list[tuple[str, str, int]]:
    """Je Repo (Marke, Name, Farbe) für die kompakte Ansicht."""
    cells = []
    for st in statuses:
        mark, pair = compact_mark(st)
        cells.append((mark, st.rel, pair))
    return cells


COMPACT_TARGET_COLUMNS = 3      # so viele Spalten sollen mindestens entstehen
COMPACT_MIN_WIDTH = 14          # darunter wird ein Name unlesbar
COMPACT_MARK_WIDTH = 3          # Platz für "↑12"; Divergenz ist ⇅ (ein Zeichen)


def compact_layout(count: int, width: int, height: int,
                   cell_width_hint: int) -> tuple[int, int, int]:
    """Spaltenaufteilung für `count` Einträge: (Zeilen, Spalten, Spaltenbreite).

    Gefüllt wird spaltenweise wie bei `ls`: die ersten `rows` Einträge stehen
    untereinander in der ersten Spalte. Dadurch bleibt die Sortierung (dringend
    zuerst) beim Lesen von oben links erhalten. Ein einzelner sehr langer
    Repo-Name darf die Spalten nicht auf zwei zusammenschrumpfen — deshalb ist die
    Breite gedeckelt und lange Namen werden gekürzt.
    """
    height = max(1, height)
    budget = max(COMPACT_MIN_WIDTH, (width - 1) // COMPACT_TARGET_COLUMNS - 2)
    column_width = max(COMPACT_MIN_WIDTH, min(cell_width_hint, budget))
    columns = max(1, (width - 1) // (column_width + 2))
    rows = max(1, math.ceil(count / columns)) if count else 1
    # Passt nicht alles auf den Schirm, bleibt die Höhe der Anschlag und es wird
    # seitlich gescrollt — deshalb hier bewusst nicht die Zeilen aufblähen.
    return min(rows, height), columns, column_width


def compact_plan(count: int, width: int, height: int,
                 cell_width_hint: int) -> tuple[int, int, int, bool]:
    """Layout der Kompaktansicht inklusive Platz für den Scroll-Hinweis.

    -> (Zeilen, Spalten, Spaltenbreite, hinweis). `hinweis` heißt: nicht alle
    Spalten passen ins Fenster, und unterhalb des Rasters ist eine EIGENE Zeile
    für den Hinweis "Spalten a-b/n" reserviert. Vorher wurde der Hinweis in die
    letzte Rasterzeile gezeichnet und überschrieb dort ab etwa 40 Spalten Breite
    den Namen und Status eines sichtbaren Repos.
    """
    rows, columns, column_width = compact_layout(count, width, height, cell_width_hint)
    total_columns = max(1, math.ceil(count / rows)) if count else 1
    if total_columns <= columns:
        return rows, columns, column_width, False
    if rows >= height:
        if height <= 1:
            # Degeneriert klein: lieber kein Hinweis als eine überschriebene Zeile.
            return rows, columns, column_width, False
        # Das Raster gibt seine unterste Zeile an den Hinweis ab. Weniger Zeilen
        # heißt mehr Gesamtspalten — scrollbar bleibt es also auf jeden Fall.
        rows, columns, column_width = compact_layout(count, width, height - 1,
                                                     cell_width_hint)
    return rows, columns, column_width, True


def ellipsize(text: str, width: int) -> str:
    """Text auf `width` Zellen kürzen und die Kürzung mit „…" sichtbar machen."""
    if cell_width(text) <= width or width <= 1:
        return truncate_cells(text, width)
    return truncate_cells(text, width - 1) + "…"


def compact_position(index: int, rows: int) -> tuple[int, int]:
    """Zeile und Spalte eines Eintrags in der spaltenweise gefüllten Anordnung."""
    return index % rows, index // rows


def print_list(statuses: list[RepoStatus], root: Path | None = None) -> None:
    green, red, yellow, cyan, reset = (
        "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m")
    # Kopfzeile wie in der TUI: Version + Wurzel. Genau dieser Text landet in einer
    # umgeleiteten Datei, die man spaeter gegen die eines anderen Macs diffed —
    # ohne Version haelt man einen Versionsunterschied fuer einen Repo-Unterschied.
    print(f"gitmaster_flash {__version__}"
          + (f" · {terminal_text(root)}" if root else "")
          + f" · {len(statuses)} {t('hdr_repos')}")
    for st in statuses:
        remote_bits = []
        for remote in st.remotes:
            color = red if remote.fetch_failed else (
                cyan if remote.public else (
                    red if remote.behind else yellow if remote.ahead else green))
            remote_bits.append(f"{color}{terminal_text(remote.badge())}{reset}")
        badge_txt = ("  " + "  ".join(remote_bits)) if remote_bits else ""
        if st.clean_and_synced:
            print(f"{green}✔ {terminal_text(st.rel)}{reset}{badge_txt}")
            continue
        bits = []
        if st.error:
            bits.append(f"{red}{terminal_text(t('error_prefix', e=st.error))}{reset}")
        if st.conflicts:
            bits.append(f"{red}{t('conflict_n', n=st.conflicts)}{reset}")
        if st.modified:
            bits.append(f"{red}M:{st.modified}{reset}")
        if st.deleted:
            bits.append(f"{red}D:{st.deleted}{reset}")
        if st.untracked:
            bits.append(f"{red}U:{st.untracked}{reset}")
        if st.stashes:
            bits.append(f"{yellow}Stash:{len(st.stashes)}{reset}")
        if st.remote_state == "no-remote":
            bits.append(f"{yellow}{t('no_sync_remote')}{reset}")
        elif st.remote_state == "no-branch":
            bits.append(f"{yellow}{t('branch_not_on', b=st.branch, r=st.remote)}{reset}")
        bits.extend(remote_bits)
        print(f"{red}✘{reset} {terminal_text(st.rel)}  {' '.join(bits)}")


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------

# Farb-Paar-Nummern. C_SEL ist kein eigener Farbton, sondern die Notlösung für
# Rot in der markierten Zeile — siehe selected_pair().
C_GREEN, C_RED, C_YELLOW, C_DIM, C_SEL, C_CYAN = 1, 2, 3, 4, 5, 6


def selected_pair(pair: int) -> tuple[int, bool]:
    """Wie ein farbiges Element in der markierten Zeile dargestellt wird.

    Markiert wird sonst durch Umkehren von Vorder- und Hintergrund. Bei Rot
    ergibt das die dunkelste Schrift auf der dunkelsten Farbe — schwarz auf
    sattem Rot ist kaum noch zu entziffern, und ausgerechnet die roten Angaben
    (M:, D:, ↓hinterher, gescheiterter Fetch) sind die wichtigen. Rot bekommt
    deshalb ein eigenes Paar: helle Schrift auf rotem Grund, nicht umgekehrt.
    Grün, Gelb und Cyan sind hell genug, dass die dunkle Schrift darauf gut
    steht; sie bleiben bei der einfachen Umkehrung.

    Rückgabe: (Farbpaar, ob umgekehrt wird).
    """
    if pair == C_RED:
        return C_SEL, False
    return pair, True


def color_attr(pair: int, is_selected: bool) -> int:
    """curses-Attribut für ein farbiges Element, markiert oder nicht."""
    if not is_selected:
        return curses.color_pair(pair)
    chosen, reverse = selected_pair(pair)
    attr = curses.color_pair(chosen)
    return attr | (curses.A_REVERSE if reverse else curses.A_BOLD)


def terminal_text(value) -> str:
    """Neutralize terminal controls while preserving the raw value for Git operations."""
    out = []
    for ch in str(value):
        code = ord(ch)
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code == 0x1B:
            out.append("\\x1b")
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            out.append("\\x%02x" % code if code <= 0xFF else "\\u%04x" % code)
        elif unicodedata.category(ch) in {"Cf", "Zl", "Zp"}:
            out.append("\\u%04x" % code if code <= 0xFFFF else "\\U%08x" % code)
        elif unicodedata.category(ch) == "Cs":
            out.append("\\u%04x" % code)
        else:
            out.append(ch)
    return "".join(out)


def zsh_quote(value) -> str:
    """Einen Pfad als sichtbares, direkt ausführbares zsh-Wort serialisieren."""
    out = []
    for ch in str(value):
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "'":
            out.append("\\'")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            # \xNN beschreibt in zsh ein einzelnes Byte. Nicht-ASCII-Codepoints
            # müssen als Unicode-Escape stehen, damit ihr UTF-8-Pfadbytebild
            # unverändert bleibt.
            out.append("\\x%02x" % code if code < 0x80 else "\\u%04x" % code)
        elif unicodedata.category(ch) in {"Cf", "Zl", "Zp"}:
            out.append("\\u%04x" % code if code <= 0xFFFF else "\\U%08x" % code)
        elif 0xDC80 <= code <= 0xDCFF:
            # Pythons surrogateescape bildet ein nicht dekodierbares Pfadbyte
            # b auf U+DC00+b ab. zshs ANSI-C-Quote stellt genau dieses Byte her.
            out.append("\\x%02x" % (code - 0xDC00))
        elif unicodedata.category(ch) == "Cs":
            out.append("\\u%04x" % code)
        else:
            out.append(ch)
    return "$'" + "".join(out) + "'"


def cell_width(text: str) -> int:
    width = 0
    for ch in text:
        if unicodedata.combining(ch) or ch in ("\ufe0e", "\ufe0f"):
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def truncate_cells(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    out = []
    used = 0
    for ch in text:
        char_width = cell_width(ch)
        if used + char_width > limit:
            break
        out.append(ch)
        used += char_width
    return "".join(out)


def wrap_cells(text: str, limit: int) -> list[str]:
    """Eine Anzeigezeile verlustfrei an Terminalzellen umbrechen."""
    if limit <= 0:
        return [text]
    if not text:
        return [""]
    lines: list[str] = []
    current: list[str] = []
    width = 0
    for char in text:
        char_width = cell_width(char)
        if current and width + char_width > limit:
            lines.append("".join(current))
            current = []
            width = 0
        current.append(char)
        width += char_width
    if current:
        lines.append("".join(current))
    return lines


def pad_cells(text: str, width: int) -> str:
    text = truncate_cells(terminal_text(text), max(0, width))
    return text + " " * max(0, width - cell_width(text))


def safe_addstr(win, y, x, text, attr=0):
    """addstr, das am Bildschirmrand nicht crasht."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    text = terminal_text(text)
    try:
        win.addstr(y, x, truncate_cells(text, w - x - 1), attr)
    except curses.error:
        pass


class TUI:
    def __init__(self, stdscr, root: Path, cfg: dict, cd_file: str | None):
        self.scr = stdscr
        self.root = root
        self.cfg = cfg
        self.cd_file = cd_file
        self.statuses: list[RepoStatus] = []
        self.selected = 0
        self.offset = 0            # Scroll-Position
        self.expanded: set[str] = set()   # rel-Pfade der aufgeklappten Repos
        self.message = ""          # Feedback-Zeile über dem Footer
        # "detail" = eine Zeile je Repo mit allem; "compact" = mehrspaltige
        # Kurzfassung. Der Startmodus richtet sich nach der Repo-Zahl (s. reload).
        self.view_mode = "detail"
        self.focus = "repos"       # "repos" oder "log" (Tab wechselt)
        self.log_top = 0           # erste sichtbare Protokollzeile
        self.log_selected = None   # gewählte Protokollzeile (None = noch nie dort)
        self.compact_col = 0       # erste sichtbare Spalte der Kompaktansicht

    # -- Datenbeschaffung ---------------------------------------------------

    def reload(self, fetch: bool = False):
        label = t("fetching") if fetch else t("reading")

        def progress(done, total):
            self.scr.erase()
            safe_addstr(self.scr, 1, 2, f"{label} … {done}/{total}",
                        curses.color_pair(C_YELLOW))
            self.scr.refresh()

        progress(0, 0)
        first_load = not self.statuses
        # Die Ladeanzeige ist breiter als eine kurze Kompaktzeile. curses schickt
        # nur Differenzen, deshalb hier ein vollständiges Neuzeichnen erzwingen.
        self.scr.clear()
        self.statuses = collect_all(self.root, self.cfg, fetch, progress)
        self.selected = min(self.selected, max(0, len(self.statuses) - 1))
        # Viele Repos: kompakt starten, weil die Detailansicht dann seitenweise
        # gescrollt werden müsste. Eine spätere Umschaltung bleibt erhalten.
        if first_load and len(self.statuses) > self.cfg["compact_from"]:
            self.view_mode = "compact"

    def refresh_one(self, st: RepoStatus, refetched: str | None = None):
        """Nur ein Repo neu einlesen (nach commit/stash), Sortierung beibehalten.

        `refetched` nennt ein Remote, dessen Fetch soeben nachweislich gelang —
        dessen alter Fehlerzustand wird dann nicht mitgeschleppt (sonst bliebe
        ein längst reparierter Fetch-Fehler bis zum kompletten Reload rot).
        """
        new = collect_status(st.path, self.root, self.cfg)
        carry_fetch_failure(st, new, refetched)
        idx = self.statuses.index(st)
        self.statuses[idx] = new
        return new

    # -- Zeichnen -----------------------------------------------------------

    def build_rows(self):
        """Sichtbare Zeilen: pro Repo eine Zeile, aufgeklappt + Datei-/Stash-Zeilen."""
        rows = []  # (art, repo_index, ...) — art: 'repo' | 'file' | 'stash' | 'empty'
        for i, st in enumerate(self.statuses):
            rows.append(("repo", i))
            if st.rel in self.expanded:
                for entry in st.files:
                    rows.append(("file", i, entry.code, entry.path))
                for stash in st.stashes:
                    rows.append(("stash", i, stash))
                if not st.files and not st.stashes:
                    rows.append(("empty", i))
        return rows

    def draw_repo_line(self, y, st: RepoStatus, is_selected: bool):
        sel = curses.A_REVERSE if is_selected else 0
        arrow = "▼" if st.rel in self.expanded else "▶"
        x = 1
        safe_addstr(self.scr, y, x, f"{arrow} ", sel)
        x += 2
        name = st.rel
        safe_addstr(self.scr, y, x, name, sel | curses.A_BOLD)
        x += cell_width(terminal_text(name)) + 2

        def part(text, pair):
            nonlocal x
            safe_addstr(self.scr, y, x, text, color_attr(pair, is_selected))
            x += cell_width(terminal_text(text)) + 1

        if st.error:
            # Der Fehler ersetzt die Statuszählung, aber NICHT Branch und
            # Remote-Badges: gerade jetzt muss sichtbar bleiben, WELCHES Remote
            # das ✘ trägt (README verspricht das ausdrücklich).
            part(t("error_prefix", e=st.error), C_RED)
        elif st.clean_and_synced:
            part(t("clean_synced"), C_GREEN)
        else:
            if st.conflicts:
                part("⚠" + t("conflict_n", n=st.conflicts), C_RED)
            if st.modified:
                part(f"M:{st.modified}", C_RED)
            if st.deleted:
                part(f"D:{st.deleted}", C_RED)
            if st.untracked:
                part(f"U:{st.untracked}", C_RED)
            if st.stashes:
                part(f"⚑Stash:{len(st.stashes)}", C_YELLOW)
            if st.remote_state == "no-remote":
                part(t("no_sync_remote"), C_YELLOW)
            elif st.remote_state == "no-branch":
                part(t("branch_not_on", b=st.branch, r=st.remote), C_YELLOW)
            elif st.remote_state == "detached":
                part(t("detached"), C_YELLOW)
        # Branch steht VOR den Remotes, damit ein GitHub-Remote garantiert ganz
        # rechts bleibt. Auch synchrone Remotes werden immer angezeigt.
        part(f"[{st.branch}]", C_DIM)
        for remote in st.remotes:
            if remote.fetch_failed:
                # Fetch scheiterte: rot vor allem anderen, damit man es nicht übersieht.
                pair = C_RED
            elif remote.public:
                pair = C_CYAN
            elif remote.behind:
                pair = C_RED
            elif remote.ahead:
                pair = C_YELLOW
            elif remote.is_sync:
                pair = C_GREEN
            else:
                pair = C_DIM
            part(remote.badge(), pair)

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        dirty = sum(1 for s in self.statuses if not s.clean_and_synced)
        tail = t("hdr_review", n=dirty) if dirty else t("hdr_clean")
        # Version mit in die Kopfzeile: Wer zwei Ausgaben von verschiedenen Macs
        # vergleicht, muss sehen, ob dieselbe Fassung dahintersteckt — sonst haelt man
        # einen Versionsunterschied fuer einen echten Repo-Unterschied. Auch eine
        # LAUFENDE Instanz zeigt den Code von ihrem Start: nach einem Sync im
        # Hintergrund vergleicht man sonst unbemerkt zwei Staende.
        head = (f" gitmaster_flash {__version__} · {self.root} · "
                f"{len(self.statuses)} {t('hdr_repos')} · {tail}")
        safe_addstr(self.scr, 0, 0, head.ljust(w - 1), curses.A_BOLD)

        # Höhe aufteilen: Kopf, Repo-Bereich, Protokoll, Meldung, 3 Footerzeilen.
        available = max(1, h - 5)
        log_h = self.log_height(h)
        body_h = max(1, available - log_h)
        if self.view_mode == "compact":
            # Die kompakte Liste braucht nur so viele Zeilen, wie ihre Spalten hoch
            # sind (plus ggf. die Hinweiszeile) — der frei bleibende Platz darunter
            # gehört dem Protokoll.
            rows, _, _, hint = self.compact_geometry(body_h, w)
            body_h = min(body_h, rows + (1 if hint else 0))
            log_h = max(log_h, available - body_h) if log_h else 0
            self.draw_compact(1, body_h, w)
        else:
            self.draw_detail(1, body_h, w)
        if log_h:
            self.draw_log(1 + body_h, min(log_h, available - body_h), w)

        safe_addstr(self.scr, h - 4, 1, self.message, curses.color_pair(C_YELLOW))
        # Footer dreizeilig, damit auch in schmalen Fenstern nichts abgeschnitten wird.
        # Alle Tastenkürzel groß geschrieben; sie sind bewusst redundant sichtbar.
        app_hints = " · ".join(f"{key.upper()} {app['name']}"
                               for key, app in self.cfg["apps"].items())
        footer_dim = curses.color_pair(C_DIM) | curses.A_REVERSE
        safe_addstr(self.scr, h - 3, 0, t("f1").ljust(w - 1), footer_dim)
        safe_addstr(self.scr, h - 2, 0, t("f2", apps=app_hints).ljust(w - 1), footer_dim)
        safe_addstr(self.scr, h - 1, 0, t("f3").ljust(w - 1), footer_dim)
        self.scr.refresh()

    def compact_step(self, direction: int) -> int:
        """Auswahl in der kompakten Ansicht um eine Spalte verschieben."""
        h, w = self.scr.getmaxyx()
        body_h = max(1, h - 5 - self.log_height(h))
        rows, _, _, _ = self.compact_geometry(body_h, w)
        target = self.selected + direction * rows
        if 0 <= target < len(self.statuses):
            return target
        # Am Rand: auf den ersten/letzten Eintrag springen statt stecken zu bleiben.
        return 0 if direction < 0 else len(self.statuses) - 1

    def log_height(self, h: int) -> int:
        """Wie viele Zeilen das Protokoll bekommt.

        Drei Zeilen sind das Minimum, damit man die letzten Befehle immer im Blick
        hat; im Fokus (Tab) wächst der Bereich auf ein Drittel des Fensters, um
        darin lesen und scrollen zu können.
        """
        if h < 14:      # sehr kleines Fenster: Repos gehen vor
            return 0
        return max(3, h // 3) if self.focus == "log" else 3

    def draw_detail(self, top: int, body_h: int, w: int) -> None:
        """Ausführliche Ansicht: eine Zeile je Repo, aufklappbar."""
        rows = self.build_rows()
        # Zeile des ausgewählten Repos finden, damit sie sichtbar bleibt
        sel_row = next((i for i, r in enumerate(rows)
                        if r[0] == "repo" and r[1] == self.selected), 0)
        if sel_row < self.offset:
            self.offset = sel_row
        if sel_row >= self.offset + body_h:
            self.offset = sel_row - body_h + 1
        y = top
        for row in rows[self.offset:self.offset + body_h]:
            kind = row[0]
            if kind == "repo":
                self.draw_repo_line(y, self.statuses[row[1]],
                                    row[1] == self.selected and self.focus == "repos")
            elif kind == "file":
                code, path = row[2], row[3]
                pair = {"M": C_RED, "D": C_RED, "U": C_YELLOW, "C": C_RED}[code]
                label = t("conflict_label") if code == "C" else ""
                safe_addstr(self.scr, y, 5, f"{code}  {label}{path}",
                            curses.color_pair(pair))
            elif kind == "stash":
                safe_addstr(self.scr, y, 5, f"⚑  {row[2]}   {t('stash_row_hint')}",
                            curses.color_pair(C_YELLOW))
            elif kind == "empty":
                safe_addstr(self.scr, y, 5, t("no_changes"), curses.color_pair(C_DIM))
            y += 1

    def compact_geometry(self, body_h: int, w: int) -> tuple[int, int, int, bool]:
        """Zeilen, Spalten, Spaltenbreite und Hinweisbedarf der kompakten Ansicht."""
        cells = compact_cells(self.statuses)
        longest = max((COMPACT_MARK_WIDTH + 1 + cell_width(name)
                       for _, name, _ in cells), default=12)
        return compact_plan(len(cells), w, body_h, longest + 1)

    def draw_compact(self, top: int, body_h: int, w: int) -> None:
        """Kompakte Ansicht: Marke + Name, spaltenweise wie `ls`."""
        cells = compact_cells(self.statuses)
        rows, columns, column_width, hint = self.compact_geometry(body_h, w)
        # Zeilen zuerst leeren: sonst bleiben rechts Reste des vorigen Bildes
        # stehen (die Ladeanzeige ist breiter als eine kurze Repo-Spalte).
        for row in range(rows + (1 if hint else 0)):
            safe_addstr(self.scr, top + row, 0, " " * max(0, w - 1))
        sel_row, sel_col = compact_position(self.selected, rows)
        # Immer so scrollen, dass die Auswahl sichtbar bleibt.
        if sel_col < self.compact_col:
            self.compact_col = sel_col
        if sel_col >= self.compact_col + columns:
            self.compact_col = sel_col - columns + 1
        total_columns = max(1, math.ceil(len(cells) / rows)) if cells else 1
        self.compact_col = max(0, min(self.compact_col, max(0, total_columns - columns)))
        for index, (mark, name, pair) in enumerate(cells):
            row, column = compact_position(index, rows)
            if not (self.compact_col <= column < self.compact_col + columns):
                continue
            x = 1 + (column - self.compact_col) * (column_width + 2)
            # Feste Markenspalte, damit die Namen aller Zeilen auch bei Marken mit
            # Zähler fluchten. Die Breitenberechnung muss dabei dieselbe sein wie
            # die von curses; sonst verschieben ↑2/↓3 die Namen um eine Zelle.
            name_width = max(1, column_width - COMPACT_MARK_WIDTH - 1)
            text = (pad_cells(mark, COMPACT_MARK_WIDTH) + " "
                    + pad_cells(ellipsize(name, name_width), name_width))
            attr = color_attr(pair, index == self.selected and self.focus == "repos")
            safe_addstr(self.scr, top + row, x, text, attr)
        if hint:
            # Ohne diesen Hinweis wirkt die Liste abgeschnitten statt scrollbar.
            # Er steht auf seiner eigenen, von compact_plan() reservierten Zeile
            # UNTER dem Raster — in der letzten Rasterzeile überschrieb er sonst
            # einen sichtbaren Repo-Namen samt Status.
            safe_addstr(self.scr, top + rows, max(1, w - 22),
                        t("compact_more", a=self.compact_col + 1,
                          b=min(total_columns, self.compact_col + columns),
                          n=total_columns),
                        curses.color_pair(C_DIM))

    def draw_log(self, top: int, log_h: int, w: int) -> None:
        """Befehlsprotokoll unter der Liste — immer sichtbar, mit Tab bedienbar.

        Der Auswahlbalken gehört immer nur einem Bereich: liegt der Fokus hier,
        verschwindet er oben in der Repo-Liste. Sonst sähe man zwei Balken und
        müsste raten, welche Taste wohin geht.
        """
        focused = self.focus == "log"
        title = t("log_pane_title") + (t("log_pane_focus") if focused
                                       else t("log_pane_hint"))
        safe_addstr(self.scr, top, 0, pad_cells(title, w - 1),
                    curses.color_pair(C_DIM)
                    | (curses.A_REVERSE if focused else curses.A_BOLD))
        visible = max(1, log_h - 1)
        entries = COMMAND_LOG or [t("cmdlog_empty")]
        max_top = max(0, len(entries) - visible)
        if focused:
            # Die Auswahl bleibt beim Hin- und Herwechseln stehen; sie muss nur
            # sichtbar sein, deshalb wandert der Ausschnitt hinter ihr her.
            self.log_selected = min(max(0, self.log_selected or 0), len(entries) - 1)
            if self.log_selected < self.log_top:
                self.log_top = self.log_selected
            if self.log_selected >= self.log_top + visible:
                self.log_top = self.log_selected - visible + 1
            self.log_top = max(0, min(self.log_top, max_top))
        else:
            # Ohne Fokus immer am Ende: die letzte Aktion ist die interessante.
            self.log_top = max_top
        for offset, entry in enumerate(entries[self.log_top:self.log_top + visible]):
            index = self.log_top + offset
            pair = C_DIM
            if entry.startswith("✘"):
                pair = C_RED
            elif entry.startswith("⊘"):
                pair = C_YELLOW
            attr = color_attr(pair, focused and index == self.log_selected)
            safe_addstr(self.scr, top + 1 + offset, 1,
                        pad_cells(entry, w - 2), attr)

    # -- Dialog-Helfer ------------------------------------------------------

    def confirm(self, question: str, extra_key: str = "") -> bool | str:
        """Rückfrage in der vorletzten Zeile: J/Y bestätigt, N/Esc bricht ab.

        Mit ``extra_key`` bekommt der Dialog eine dritte Antwort — für den Fall,
        dass er neben Ja und Nein noch einen anderen Weg anbietet (etwa
        „stattdessen alle Dateien"). Diese Taste liefert dann sich selbst als
        Rückgabewert. Aufrufer müssen deshalb auf ``is True`` prüfen: Ein
        Buchstabe ist in Python wahr und würde sonst als Zustimmung durchgehen.
        """
        h, w = self.scr.getmaxyx()
        hint = t("yesno_extra", k=extra_key.upper()) if extra_key else t("yesno")
        safe_addstr(self.scr, h - 4, 1, (question + hint).ljust(w - 2),
                    curses.color_pair(C_YELLOW) | curses.A_BOLD)
        self.scr.refresh()
        while True:
            ch = self.scr.getch()
            if ch in (ord("j"), ord("J"), ord("y"), ord("Y")):
                return True
            if ch in (ord("n"), ord("N"), 27):
                return False
            if extra_key and ch in (ord(extra_key.lower()), ord(extra_key.upper())):
                return extra_key.upper()

    def show_busy(self, text: str) -> None:
        """Eine Zwischenmeldung sofort auf den Schirm bringen.

        Vor einem Git-Aufruf, der dauern kann: curses zeichnet erst beim nächsten
        `refresh()`, ohne diesen Zwischenschritt bliebe das alte Bild stehen und die
        TUI sähe abgestürzt aus.
        """
        h, w = self.scr.getmaxyx()
        safe_addstr(self.scr, h - 1, 0, terminal_text(text).ljust(w - 1),
                    curses.color_pair(C_DIM) | curses.A_REVERSE)
        self.scr.refresh()

    def prompt_line(self, y: int, prompt: str) -> str | None:
        """Einzeilige Texteingabe; Esc bricht ab, ⏎ bestätigt."""
        buf: list[str] = []
        while True:
            h, w = self.scr.getmaxyx()
            safe_addstr(self.scr, y, 1, (prompt + "".join(buf)).ljust(w - 2),
                        curses.A_BOLD)
            cursor_x = 1 + cell_width(terminal_text(prompt + "".join(buf)))
            self.scr.move(min(y, h - 1), min(cursor_x, max(0, w - 2)))
            self.scr.refresh()
            ch = self.scr.get_wch()
            if ch in ("\n", "\r"):
                return "".join(buf).strip()
            if ch == "\x1b":  # Esc
                return None
            if ch in ("\x7f", "\b") or ch == curses.KEY_BACKSPACE:
                if buf:
                    buf.pop()
            elif isinstance(ch, str) and ch.isprintable():
                buf.append(ch)

    # -- Aktionen -----------------------------------------------------------

    def current(self) -> RepoStatus | None:
        return self.statuses[self.selected] if self.statuses else None

    def action_open_app(self, key: str):
        st = self.current()
        app = self.cfg["apps"].get(key)
        if not st or not app:
            return
        if not Path(app["path"]).exists():
            self.message = t("app_not_found", p=app["path"])
            return
        if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
            # Über SSH gibt es keine Fenstersitzung: `open` würde nichts Sichtbares
            # tun. Das ehrlich sagen, statt einen kryptischen macOS-Fehler zu zeigen.
            self.message = t("app_over_ssh", name=app["name"])
            return
        # `open -a <App> <Ordner>` öffnet den Repo-Ordner in der App. Fehler (z.B.
        # App kann Ordner nicht öffnen) sichtbar machen, statt still zu schlucken.
        r = subprocess.run(["open", "-a", app["path"], str(st.path)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            self.message = t("app_opened", name=app["name"], rel=st.rel)
        else:
            self.message = t("app_open_failed", name=app["name"], e=r.stderr.strip()[:120])

    def action_cd_and_quit(self) -> bool:
        st = self.current()
        if not st:
            return False
        if self.cd_file:
            Path(self.cd_file).write_text(str(st.path))
        else:
            # Ohne Wrapper können wir das cwd der Shell nicht ändern — Hinweis geben.
            print(f"\ncd -- {zsh_quote(st.path)}")
            print(t("cd_hint"))
        return True

    def action_stash_show(self):
        """Neuesten Stash als Diff anzeigen (read-only), scrollbar. Für den
        Fall redundanter Alt-Stashes: erst schauen, dann entscheiden."""
        st = self.current()
        if not st or not st.stashes:
            self.message = t("no_stash")
            return
        try:
            snapshot = latest_stash(st.path, self.cfg["git_timeout"])
        except (GitReadError, OSError) as exc:
            self.message = t("stash_preview_failed", e=str(exc)[:120])
            return
        if snapshot is None:
            self.message = t("no_stash")
            return
        oid, label = snapshot
        ok, preview = stash_preview(st.path, self.cfg["git_timeout"], oid)
        if not ok:
            text = t("stash_preview_failed", e=preview)
        else:
            text = preview or t("stash_preview_empty")
        title = t("stash_preview_title", rel=st.rel, s=label)
        self.show_pager(title, text.splitlines())

    def show_pager(self, title: str, lines: list[str]):
        """Einfacher scrollbarer Textbetrachter (↑/↓/PgUp/PgDn, q/Esc schließt)."""
        top = 0
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            safe_addstr(self.scr, 0, 0, (" " + title).ljust(w - 1), curses.A_BOLD)
            body_h = h - 2
            # Erst neutralisieren, dann nach sichtbaren Terminalzellen umbrechen.
            # Sonst wird etwa U+202E nach dem Umbruch zu sechs sichtbaren Zeichen
            # und die verlustfreie H-Ansicht schneidet sie wieder ab.
            wrapped = [part for line in lines
                       for part in wrap_cells(
                           terminal_text(line), max(1, w - 1))]
            top = min(top, max(0, len(wrapped) - body_h))
            for y, line in enumerate(wrapped[top:top + body_h], start=1):
                # Diff-Zeilen leicht einfärben: + grün, - rot, @@ gelb.
                pair = 0
                if line.startswith("+") and not line.startswith("+++"):
                    pair = curses.color_pair(C_GREEN)
                elif line.startswith("-") and not line.startswith("---"):
                    pair = curses.color_pair(C_RED)
                elif line.startswith("@@"):
                    pair = curses.color_pair(C_YELLOW)
                safe_addstr(self.scr, y, 0, line, pair)
            a = top + 1
            b = min(len(wrapped), top + body_h)
            safe_addstr(self.scr, h - 1, 0,
                        t("pager_footer", a=a, b=b, n=len(wrapped)).ljust(w - 1),
                        curses.color_pair(C_DIM) | curses.A_REVERSE)
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (ord("q"), ord("Q"), 27):
                return
            elif ch == curses.KEY_UP:
                top = max(0, top - 1)
            elif ch == curses.KEY_DOWN:
                top = min(max(0, len(wrapped) - body_h), top + 1)
            elif ch == curses.KEY_NPAGE:
                top = min(max(0, len(wrapped) - body_h), top + body_h)
            elif ch == curses.KEY_PPAGE:
                top = max(0, top - body_h)

    # -- Sichere Push-Aktionen ---------------------------------------------

    @staticmethod
    def _remote(st: RepoStatus, name: str | None) -> RemoteStatus | None:
        return next((remote for remote in st.remotes if remote.name == name), None)

    @staticmethod
    def _remote_approval_signature(remote: RemoteStatus | None) -> tuple | None:
        """Die im Listen-Screen sichtbare Remote- und Sicherheitsidentitaet."""
        if remote is None:
            return None
        return (
            tuple(remote.fetch_fingerprints), tuple(remote.push_fingerprints),
            remote.public, remote.mixed_public, remote.target_mismatch,
            remote.multiple_pushurls, remote.fetch_refspecs_safe,
            remote.fetch_refspec_fingerprint, remote.branch_mapping_safe,
            remote.fetch_url_safe, remote.push_url_safe,
        )

    def _fetch_remote(self, st: RepoStatus, remote: str) -> RepoStatus | None:
        config = read_remote_config_or_none(st.path, self.cfg, remote)
        block_reason = ("unsafe_refspec" if config is None
                        else fetch_remote_block_reason(config, st.branch))
        if block_reason is not None:
            self.message = remote_check_message(
                remote, block_reason, 0, "", self.cfg["fetch_timeout"])
            return None
        try:
            r = fetch_remote_safely(
                st.path, config, st.branch, self.cfg["fetch_timeout"])
        except RemoteConfigChangedError:
            self.refresh_one(st)
            self.message = t("transfer_changed")
            return None
        except OSError:
            # Der Netz-Fetch oder die anschließende OID-CAS-Übernahme kann
            # bereits gewirkt haben. Ein lokaler Start-/Dateifehler beweist
            # deshalb keinen unveränderten Tracking-Ref.
            self.refresh_one(st)
            self.message = t("fetch_outcome_unknown", r=remote)
            return None
        if r.returncode != 0:
            self.message = (t("transfer_auth_missing", r=remote)
                            if credentials_missing(
                                r, keychain_helper=remote_uses_keychain_helper(
                                    st.path, remote, self.cfg["git_timeout"]))
                            else t("transfer_fetch_failed", r=remote,
                                   code=r.returncode))
            return None
        after = read_remote_config_or_none(st.path, self.cfg, remote)
        if after != config:
            self.refresh_one(st)
            self.message = t("transfer_changed")
            return None
        # Der Fetch hat gerade bewiesen, dass DIESES Remote wieder erreichbar
        # ist — ein alter Fetch-Fehler dazu darf den Refresh nicht überleben.
        return self.refresh_one(st, refetched=remote)

    def _transfer_message(self, check: TransferCheck, remote: str,
                          branch: str) -> str:
        if check.reason == "dirty":
            return t("transfer_dirty")
        if check.reason == "detached":
            return t("transfer_detached")
        if check.reason == "inspect-failed":
            return t("transfer_inspect_failed")
        if check.reason == "remote-unsafe":
            return t("remote_url_mismatch", r=remote)
        if check.reason == "missing-branch":
            return t("transfer_missing", b=branch, r=remote)
        if check.reason == "divergent":
            return t("transfer_divergent", r=remote, a=check.ahead, b=check.behind)
        if check.reason == "behind":
            return t("transfer_behind", n=check.behind, r=remote)
        if check.reason == "nothing-push":
            return t("nothing_to_push", r=remote)
        return check.reason

    def _run_approved_push(self, st: RepoStatus,
                           check: TransferCheck) -> subprocess.CompletedProcess | None:
        """Den gebundenen Push ausführen; fehlendes Prozessergebnis bleibt unklar."""
        args = safe_push_args(
            check.transfer_url, check.branch, check.head_oid, check.target_oid)
        try:
            return run_git_logged(
                st.path, *args, timeout=self.cfg["fetch_timeout"],
                env=RAW_OBJECT_ENV)
        except OSError:
            # Wie beim Commit kann der Prozess bereits serverseitig gewirkt
            # haben, obwohl Python sein Ergebnis nicht mehr lesen konnte.
            self.refresh_one(st)
            self.message = t("push_io_unknown")
            return None

    def action_sync_push(self):
        """Einfacher Push ausschließlich zum nichtöffentlichen Sync-Remote."""
        st = self.current()
        if not st or not st.remote:
            self.message = t("no_sync_for_action")
            return
        remote = self._remote(st, st.remote)
        if not remote:
            self.message = t("no_sync_for_action")
            return
        if not remote.transfer_safe:
            self.message = t("remote_url_mismatch", r=remote.name)
            return
        if remote.public:
            self.message = t("public_simple_block")
            return
        fresh = self._fetch_remote(st, remote.name)
        if not fresh:
            return
        if (fresh.branch != st.branch
                or self._remote_approval_signature(
                    self._remote(fresh, remote.name))
                != self._remote_approval_signature(remote)):
            self.message = t("transfer_changed")
            return
        check = inspect_transfer(fresh.path, remote.name, fresh.branch, "push",
                                 self.cfg["git_timeout"], expected_public=False)
        if not check.ready:
            self.message = self._transfer_message(check, remote.name, fresh.branch)
            return
        if not self.confirm(t("confirm_sync_push", n=check.ahead, r=remote.name)):
            log_cancelled(fresh.path, safe_push_args(
                check.transfer_url, check.branch, check.head_oid,
                check.target_oid))
            self.message = t("cancelled")
            return
        newest = self._fetch_remote(fresh, remote.name)
        if not newest:
            return
        final = inspect_transfer(newest.path, remote.name, newest.branch, "push",
                                 self.cfg["git_timeout"], expected_public=False)
        if not final.ready or final.approval_signature() != check.approval_signature():
            self.message = t("transfer_changed")
            return
        r = self._run_approved_push(newest, final)
        if r is None:
            return
        if r.returncode == 0:
            tracking_ok = update_tracking_after_push(
                newest.path, final, self.cfg["git_timeout"])
            self.refresh_one(newest)
            self.message = (t("sync_pushed", r=remote.name) if tracking_ok
                            else t("push_tracking_changed"))
        elif credentials_missing(
                r, keychain_helper=remote_uses_keychain_helper(
                    newest.path, remote.name, self.cfg["git_timeout"],
                    for_push=True)):
            self.refresh_one(newest)
            self.message = t("transfer_auth_missing", r=remote.name)
        else:
            self.refresh_one(newest)
            self.message = t("push_outcome_unknown", code=r.returncode)

    def action_github_push(self):
        """Öffentlicher Push nur nach Vorschau + ausgeschriebener Bestätigung."""
        st = self.current()
        if not st:
            return
        public = [remote for remote in st.remotes if remote.public]
        if not public:
            self.message = t("no_github")
            return
        if len(public) != 1:
            self.message = t("many_github", names=", ".join(r.name for r in public))
            return
        remote = public[0]
        if not remote.transfer_safe:
            self.message = t("remote_url_mismatch", r=remote.name)
            return
        fresh = self._fetch_remote(st, remote.name)
        if not fresh:
            return
        if (fresh.branch != st.branch
                or self._remote_approval_signature(
                    self._remote(fresh, remote.name))
                != self._remote_approval_signature(remote)):
            self.message = t("github_changed")
            return
        check = inspect_transfer(fresh.path, remote.name, fresh.branch, "push",
                                 self.cfg["git_timeout"], expected_public=True)
        if not check.ready:
            self.message = self._transfer_message(check, remote.name, fresh.branch)
            return

        lines = [
            t("preview_branch_only"),
            t("preview_privacy"),
            "",
            t("outgoing_commits"),
            *(check.commits or [t("none_label")]),
            "",
            t("changed_files"),
            *(check.files or [t("none_label")]),
        ]
        self.show_pager(t("github_preview", rel=fresh.rel, r=remote.name,
                          b=fresh.branch), lines)
        self.draw()
        phrase = f"PUSH {remote.name}"
        h, _ = self.scr.getmaxyx()
        typed = self.prompt_line(h - 4, t("github_type", phrase=phrase))
        if typed != phrase:
            log_cancelled(fresh.path, safe_push_args(
                check.transfer_url, check.branch, check.head_oid,
                check.target_oid))
            self.message = t("github_cancelled")
            return

        # Unmittelbar vor dem öffentlichen Push erneut fetchen. Ändert sich der
        # ausgehende Satz seit der Vorschau, wird nicht mit veralteter Freigabe gepusht.
        newest = self._fetch_remote(fresh, remote.name)
        if not newest:
            return
        final = inspect_transfer(newest.path, remote.name, newest.branch, "push",
                                 self.cfg["git_timeout"], expected_public=True)
        if (not final.ready
                or final.approval_signature() != check.approval_signature()):
            self.message = t("github_changed")
            return
        r = self._run_approved_push(newest, final)
        if r is None:
            return
        if r.returncode == 0:
            tracking_ok = update_tracking_after_push(
                newest.path, final, self.cfg["git_timeout"])
            self.refresh_one(newest)
            self.message = (t("github_pushed", r=remote.name) if tracking_ok
                            else t("push_tracking_changed"))
        elif credentials_missing(
                r, keychain_helper=remote_uses_keychain_helper(
                    newest.path, remote.name, self.cfg["git_timeout"],
                    for_push=True)):
            self.refresh_one(newest)
            self.message = t("transfer_auth_missing", r=remote.name)
        else:
            self.refresh_one(newest)
            self.message = t("push_outcome_unknown", code=r.returncode)

    def action_git_help(self):
        """Kurzhilfe — und darüber das Protokoll der wirklich abgesetzten Befehle."""
        lines = [t("cmdlog_title"), ""]
        if COMMAND_LOG:
            lines.extend("  " + terminal_text(entry) for entry in COMMAND_LOG)
            lines.extend(["", "  " + t("cmdlog_hint")])
        else:
            lines.append("  " + t("cmdlog_empty"))
        lines.extend(["", "", *t("git_help_body").splitlines()])
        self.show_pager(t("git_help_title"), lines)

    # -- Änderungen ansehen (A) ---------------------------------------------

    def action_file_changes(self):
        """Geänderte Dateien durchgehen und ihren Diff rein lesend ansehen."""
        st = self.current()
        if not st:
            return
        if not st.files:
            self.message = t("no_changes_to_show")
            return
        sel = 0
        off = 0
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            safe_addstr(self.scr, 0, 0,
                        (" " + t("changes_title", rel=terminal_text(st.rel))).ljust(w - 1),
                        curses.A_BOLD)
            body_h = max(1, h - 3)
            if sel < off:
                off = sel
            if sel >= off + body_h:
                off = sel - body_h + 1
            for y, index in enumerate(range(off, min(len(st.files), off + body_h)),
                                      start=1):
                entry = st.files[index]
                pair = {"M": C_RED, "D": C_RED, "U": C_YELLOW, "C": C_RED}[entry.code]
                label = t("conflict_label") if entry.code == "C" else ""
                safe_addstr(self.scr, y, 1, f"{entry.code}  {label}{entry.path}",
                            color_attr(pair, index == sel))
            safe_addstr(self.scr, h - 1, 0, t("changes_footer").ljust(w - 1),
                        curses.color_pair(C_DIM) | curses.A_REVERSE)
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (ord("q"), ord("Q"), 27):
                return
            elif ch == curses.KEY_UP:
                sel = max(0, sel - 1)
            elif ch == curses.KEY_DOWN:
                sel = min(len(st.files) - 1, sel + 1)
            elif ch == 9:                      # Tab wie ↓, ohne Escape-Sequenz
                sel = (sel + 1) % len(st.files)
            elif ch in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
                entry = st.files[sel]
                path = entry.path
                ok, text = file_diff(st.path, entry.code, path, self.cfg["git_timeout"])
                if not ok:
                    self.message = t("diff_failed", p=path, e=text)
                    return
                self.show_pager(t("diff_title", p=terminal_text(path)),
                                (text or t("diff_empty")).splitlines())
    # -- Repo-Info mit Remote- und Branch-Auswahl ---------------------------

    def action_repo_info(self):
        """Repo-Details; Remotes sind mit T rein lesend prüfbar."""
        st = self.current()
        if not st:
            return
        fresh = self.refresh_one(st)
        view = build_info_view(fresh, self.cfg)
        selected = 0        # Index in view.blocks (Remotes und Branches)
        top = 0             # erste sichtbare Zeile
        note = ""           # Ergebnis der letzten Prüfung/Aktion
        followed = None     # Block, zu dem zuletzt hingescrollt wurde
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            title = t("repo_info_title", rel=terminal_text(fresh.rel))
            safe_addstr(self.scr, 0, 0, (" " + title).ljust(w - 1), curses.A_BOLD)
            body_h = max(1, h - 4)
            selected = min(selected, max(0, len(view.blocks) - 1))
            block = view.blocks[selected] if view.blocks else None
            if block and block != followed:
                # Ein NEU gewählter Block soll komplett sichtbar werden. Nur dann:
                # Ein PgUp/PgDn-Sprung darf im nächsten Durchlauf nicht wieder
                # zurückgesetzt werden, sonst blieben Kopf- und Zwischenbereiche
                # auf kleinen Fenstern unerreichbar (der Footer verspricht die
                # freie Seitennavigation ausdrücklich).
                _, _, first, last = block
                if first < top:
                    top = first
                if last >= top + body_h:
                    top = min(first, max(0, last - body_h + 1))
            followed = block
            top = max(0, min(top, max(0, len(view.lines) - body_h)))
            for y, index in enumerate(range(top, min(len(view.lines), top + body_h)),
                                      start=1):
                mark = (curses.A_REVERSE
                        if block and block[2] <= index <= block[3] else 0)
                safe_addstr(self.scr, y, 0, view.lines[index], mark)
            safe_addstr(self.scr, h - 3, 1, note, curses.color_pair(C_YELLOW))
            footer_dim = curses.color_pair(C_DIM) | curses.A_REVERSE
            safe_addstr(self.scr, h - 2, 0, t("info_footer_nav").ljust(w - 1), footer_dim)
            # Die Aktionszeile richtet sich nach dem gewählten Block: ein Branch
            # kennt kein "prüfen", ein Remote kein "gemergt".
            actions = t("info_footer_actions_branch" if block and block[0] == "branch"
                        else "info_footer_actions")
            safe_addstr(self.scr, h - 1, 0, actions.ljust(w - 1), footer_dim)
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (ord("q"), ord("Q"), 27):
                return
            elif ch == curses.KEY_UP:
                selected = max(0, selected - 1)
            elif ch == curses.KEY_DOWN:
                selected = min(max(0, len(view.blocks) - 1), selected + 1)
            elif ch == 9 and view.blocks:      # Tab: durchzykeln
                selected = (selected + 1) % len(view.blocks)
            elif ch == curses.KEY_BTAB and view.blocks:   # Shift-Tab: zurück
                selected = (selected - 1) % len(view.blocks)
            elif ch == curses.KEY_NPAGE:
                top = min(max(0, len(view.lines) - body_h), top + body_h)
            elif ch == curses.KEY_PPAGE:
                top = max(0, top - body_h)
            elif ch in (ord("t"), ord("T")):
                if block and block[0] == "branch":
                    note = t("info_check_remote_only")
                else:
                    note = self._check_selected_remote(fresh, block)

    def _check_selected_remote(self, st: RepoStatus,
                               block: tuple[str, str, int, int] | None) -> str:
        """`git ls-remote` gegen das gewählte Remote; nennt die Ursache beim Namen."""
        if not block:
            return t("info_no_remotes")
        name = block[1]
        h, w = self.scr.getmaxyx()
        safe_addstr(self.scr, h - 3, 1, t("check_running", r=name).ljust(w - 2),
                    curses.color_pair(C_YELLOW))
        self.scr.refresh()
        timeout = self.cfg["fetch_timeout"]
        outcome, refs, detail = check_remote(st.path, name, timeout)
        message = remote_check_message(name, outcome, refs, detail, timeout)
        self.message = message
        return message

    # -- Commit-Hilfe --------------------------------------------------------

    def action_commit_wizard(self):
        st = self.current()
        if not st:
            return
        if not st.files:
            self.message = t("nothing_to_commit")
            return
        if st.conflicts:
            self.message = t("commit_conflicts")
            return
        # Alle Pfade starten ausgewaehlt; Space nimmt einzelne Pfade oder ein
        # zusammengehoeriges Rename-Paar aus dem Commit. Die Hilfe veraendert
        # den Arbeitsbaum vor dem Commit nicht (insbesondere keine .gitignore).
        items = [
            {"code": entry.code, "path": entry.path, "include": True,
             "rename_group": entry.rename_group}
            for entry in st.files
        ]
        sel = 0
        off = 0
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            safe_addstr(self.scr, 0, 0, (" " + t("commit_title", rel=st.rel)).ljust(w - 1),
                        curses.A_BOLD)
            body_h = h - 4
            if sel < off:
                off = sel
            if sel >= off + body_h:
                off = sel - body_h + 1
            for y, i in enumerate(range(off, min(len(items), off + body_h)), start=1):
                it = items[i]
                if it["include"]:
                    label, pair = t("do_commit"), C_GREEN
                else:
                    label, pair = t("do_skip"), C_DIM
                path_width = max(1, w - 40)
                safe_addstr(self.scr, y, 1,
                            f"{it['code']}  {pad_cells(it['path'], path_width)} {label}",
                            color_attr(pair, i == sel))
            safe_addstr(self.scr, h - 2, 0, t("commit_footer").ljust(w - 1),
                        curses.color_pair(C_DIM) | curses.A_REVERSE)
            self.scr.refresh()
            ch = self.scr.getch()
            if ch == curses.KEY_UP:
                sel = max(0, sel - 1)
            elif ch == curses.KEY_DOWN:
                sel = min(len(items) - 1, sel + 1)
            elif ch == ord(" "):
                selected = items[sel]
                include = not selected["include"]
                related = [it for it in items
                           if selected["rename_group"]
                           and it["rename_group"] == selected["rename_group"]]
                for it in related or [selected]:
                    it["include"] = include
            elif ch in (10, 13, curses.KEY_ENTER):
                if self._commit_step2(st, items):
                    return
            elif ch == 27:
                self.message = t("commit_cancelled")
                return

    def _commit_step2(self, st: RepoStatus, items: list) -> bool:
        """Schritt 2: letzte Commit-Messages zeigen, Message erfragen, ausführen."""
        # Dieser Snapshot stammt im normalen TUI-Pfad aus dem Listen-Scan VOR
        # Dateiauswahl und Message. Ein Checkout während des Dialogs darf nicht
        # still den neuen Branch zur Freigabegrundlage machen.
        approved_ui_head = (st.head_oid if st.head_oid != ""
                            else _SIGNATURE_UNSET)
        approved_ui_ref = (f"refs/heads/{st.branch}"
                           if st.branch not in ("?", "(detached)")
                           else _SIGNATURE_UNSET)
        to_commit = [it["path"] for it in items if it["include"]]
        if not to_commit:
            self.message = t("nothing_selected")
            return True
        # Stil-Vorlage: die letzten Commit-Messages dieses Repos. Wer sie beim Tippen
        # sieht, schreibt die neue Message im gleichen Stil weiter.
        r = run_git(st.path, "log", "-8", "--format=%s", timeout=self.cfg["git_timeout"])
        recent = [line for line in r.stdout.splitlines() if line.strip()]
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        safe_addstr(self.scr, 0, 0, (" " + t("commit_in", rel=st.rel)).ljust(w - 1),
                    curses.A_BOLD)
        # Layout von unten her planen: die Eingabezeile muss sichtbar bleiben.
        prompt_y = max(4, h - 2)
        y = 2
        safe_addstr(self.scr, y, 1, t("to_commit_n", n=len(to_commit)),
                    curses.color_pair(C_GREEN))
        y += 2
        # Nur so viele Beispiele zeigen, wie über der Eingabezeile Platz haben.
        room = max(0, prompt_y - y - 2)
        shown_recent = recent[:room]
        if shown_recent:
            safe_addstr(self.scr, y, 1, t("recent_msgs"))
            y += 1
            for msg in shown_recent:
                safe_addstr(self.scr, y, 3, f"· {terminal_text(msg)}",
                            curses.color_pair(C_DIM))
                y += 1
        msg = self.prompt_line(prompt_y, t("commit_msg_prompt"))
        if msg is None:
            return False  # Esc -> zurück zur Dateiauswahl
        if not msg:
            self.message = t("empty_msg")
            return True

        # Exakt freigegebene Pfade über einen temporären Index committen. Der
        # echte Benutzer-Index bleibt erhalten.
        t_ = self.cfg["git_timeout"]
        commit_t = self.cfg["commit_timeout"]
        try:
            head_before = current_head(st.path, t_)
            approved_ref = current_symbolic_head_ref(st.path, t_)
            if approved_ref is None:
                self.message = t("commit_detached")
                return True
            if ((approved_ui_head is not _SIGNATURE_UNSET
                 and head_before != approved_ui_head)
                    or (approved_ui_ref is not _SIGNATURE_UNSET
                        and approved_ref != approved_ui_ref)):
                self.message = t("commit_failed", e="HEAD changed after UI approval")
                self.refresh_one(st)
                return True
            if repository_operation_in_progress(st.path, t_):
                self.message = t("commit_operation")
                return True
            # Ab hier kann es dauern: `git commit` führt den pre-commit-Hook des
            # Repos aus, der oft Linter oder Tests startet.
            self.show_busy(t("commit_running", s=commit_t))
            r = commit_selected(
                st.path, list(dict.fromkeys(to_commit)), msg, t_, commit_t,
                expected_head=head_before, expected_ref=approved_ref)
        except CommitAdoptionError as e:
            self.message = t(
                "commit_exists_index_failed", oid=e.committed_head[:12])
            self.refresh_one(st)
            return True
        except CommitOutcomeUnknownError:
            self.message = t("commit_outcome_unknown")
            self.refresh_one(st)
            return True
        except (CommitSafetyError, GitReadError, OSError) as e:
            self.message = t("commit_failed", e=str(e)[:120])
            self.refresh_one(st)
            return True
        except subprocess.TimeoutExpired as exc:
            # Git und der von ihm gestartete Hook wurden beendet. Ob der Commit
            # vorher noch fertig wurde (z.B. hing nur der post-commit-Hook), weiß
            # nur das Repo selbst — deshalb den HEAD vergleichen, statt zu raten.
            # Existiert ein neuer Commit, prüft finish_interrupted_commit Eltern
            # und Baum nur lesend. Nach einem Timeout darf weder HEAD noch Index
            # automatisch verändert werden, weil die Herkunft nicht beweisbar ist.
            proof_names = ("approved_head", "approved_ref", "approved_tree",
                           "approved_paths", "reflog_action")
            if not all(hasattr(exc, name) for name in proof_names):
                # Der Timeout lag in einer Vorbereitung/Schutzprüfung; `git
                # commit` wurde noch nicht gestartet und ein ungebundenes
                # Dateiabild darf hier keinesfalls als Commit-Nachweis dienen.
                self.message = t("commit_timeout_none", s=commit_t)
                self.refresh_one(st)
                return True
            try:
                done = finish_interrupted_commit(
                    st.path, exc.approved_head, list(exc.approved_paths), t_,
                    exc.approved_tree, exc.approved_ref, exc.reflog_action)
            except subprocess.TimeoutExpired:
                self.message = t("commit_outcome_unknown")
                self.refresh_one(st)
                return True
            except (CommitSafetyError, GitReadError, OSError):
                # Der eigentliche `git commit` war bereits gestartet und ist in
                # den Timeout gelaufen. Jede danach misslungene Beweisprüfung
                # lässt den Ausgang offen; sie darf niemals zu "Commit failed"
                # und damit zu einem riskanten Wiederholungsversuch verleiten.
                self.message = t("commit_outcome_unknown")
                self.refresh_one(st)
                return True
            self.message = t("commit_timeout_done" if done else "commit_timeout_none",
                             s=commit_t)
            self.refresh_one(st)
            return True
        finally:
            # Tasten, die während der Wartezeit gedrückt wurden, würden sonst
            # anschließend als Kommandos ausgeführt (⏎ aus Ungeduld z.B.).
            curses.flushinp()
        if r.returncode != 0:
            self.message = t("commit_failed", e=r.stderr.strip()[:120])
            self.refresh_one(st)
            return True
        self.refresh_one(st)
        undo = commit_undo_command(
            r.approved_head, r.committed_head, r.approved_ref)
        self.message = t("committed_in", rel=st.rel, undo=undo)
        return True

    # -- Hauptschleife -------------------------------------------------------

    def dispatch_action(self, key: str) -> None:
        """Einen Buchstabenbefehl ausführen.

        Bewusst von der Hauptschleife getrennt: So liegt jede Aktion, die Git
        aufruft, hinter genau einer Absicherung gegen Timeouts (siehe `run`).
        """
        if key == "C":
            self.action_commit_wizard()
        elif key == "R":
            self.reload(fetch=True)
        elif key == "P":
            self.action_sync_push()
        elif key == "G":
            self.action_github_push()
        elif key == "H":
            self.action_git_help()
        elif key == "I":
            self.action_repo_info()
        elif key == "A":
            self.action_file_changes()
        elif key == "M":
            # Ansicht wechseln — die Auswahl bleibt auf demselben Repo, damit
            # man in der Übersicht suchen und im Detail weiterarbeiten kann.
            self.view_mode = "detail" if self.view_mode == "compact" else "compact"
        elif key == "S":
            self.action_stash_show()
        elif key in self.cfg["apps"]:
            self.action_open_app(key)

    def run(self):
        curses.curs_set(0)
        self.reload()
        while True:
            self.draw()
            ch = self.scr.getch()
            self.message = ""
            st = self.current()
            # Tab schaltet den Fokus zwischen Repo-Liste und Protokoll um; im
            # Protokoll bedeuten die Cursortasten dann scrollen statt auswählen.
            if ch == 9:
                self.focus = "log" if self.focus == "repos" else "repos"
                if self.focus == "log" and self.log_selected is None:
                    # Erster Besuch: beim neuesten Befehl anfangen.
                    self.log_selected = max(0, len(COMMAND_LOG) - 1)
                continue
            if self.focus == "log":
                last = max(0, len(COMMAND_LOG) - 1)
                current = self.log_selected or 0
                if ch == curses.KEY_UP:
                    self.log_selected = max(0, current - 1)
                    continue
                if ch == curses.KEY_DOWN:
                    self.log_selected = min(last, current + 1)
                    continue
                if ch in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                    step = max(1, self.log_height(self.scr.getmaxyx()[0]) - 1)
                    self.log_selected = max(0, min(
                        last, current + (step if ch == curses.KEY_NPAGE else -step)))
                    continue
                if ch == 27:
                    self.focus = "repos"
                    continue
            # Cursor-/Sondertasten zuerst; Buchstaben danach case-insensitiv.
            if ch == curses.KEY_UP:
                self.selected = max(0, self.selected - 1)
                continue
            elif ch == curses.KEY_DOWN:
                self.selected = min(len(self.statuses) - 1, self.selected + 1)
                continue
            elif ch == curses.KEY_RIGHT and st:
                if self.view_mode == "compact":
                    # Kompakt: eine Spalte weiter statt aufklappen.
                    self.selected = self.compact_step(+1)
                else:
                    self.expanded.add(st.rel)
                continue
            elif ch == curses.KEY_LEFT and st:
                if self.view_mode == "compact":
                    self.selected = self.compact_step(-1)
                else:
                    self.expanded.discard(st.rel)
                continue
            elif ch in (10, 13, curses.KEY_ENTER):
                if self.action_cd_and_quit():
                    return
                continue
            elif ch == 27:  # Esc
                return

            # Buchstaben-Kürzel: groß ODER klein akzeptieren (F wie f).
            try:
                key = chr(ch).upper()
            except ValueError:
                continue
            if key == "Q":
                return
            # Jeder Git-Aufruf kann hängen bleiben (langsamer Hook, langsames Netz).
            # Ein Timeout ist dann eine Auskunft und kein Grund, die Übersicht zu
            # beenden — vorher stieg gmf an dieser Stelle mit einem Traceback aus.
            try:
                self.dispatch_action(key)
            except subprocess.TimeoutExpired as exc:
                self.handle_action_timeout(exc)

    def handle_action_timeout(self, exc: subprocess.TimeoutExpired) -> None:
        """Nach einem Aktions-Timeout melden UND das Repo defensiv neu einlesen.

        Eine Aktion kann vor dem hängenden Schritt schon mutiert haben — etwa
        ein Commit, dessen post-commit-Hook dann in den Timeout läuft. Der
        aktionsinterne refresh_one() wird wegen der Ausnahme nie erreicht; ohne
        das Neu-Einlesen hier arbeitete die TUI mit altem Branch-, Datei- oder
        Stash-Zustand weiter und böte darauf falsche Folgeaktionen an.
        (collect_status fängt eigene Fehler intern ab und liefert schlimmstenfalls
        einen Fehlerstatus — es kann diesen Handler nicht erneut sprengen.)
        """
        self.message = timeout_message(exc)
        curses.flushinp()
        st = self.current()
        if st:
            self.refresh_one(st)


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(C_RED, curses.COLOR_RED, -1)
    curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
    curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)
    # Rot in der markierten Zeile: heller Text AUF Rot statt Rot als Hintergrund
    # mit schwarzem Text (siehe selected_pair).
    curses.init_pair(C_SEL, curses.COLOR_WHITE, curses.COLOR_RED)


# ---------------------------------------------------------------------------
# Demo-Sandbox (für `--demo`, Screenshots und risikofreies Ausprobieren)
# ---------------------------------------------------------------------------

# Feste Zeitstempel für alle Demo-Commits. Erst dadurch sind die Commit-IDs auf jedem
# Rechner und in jedem Lauf dieselben — und damit auch die Commit-IDs in den
# Demo-Repos und Vorschauen. Ohne das wäre der Bild-Check
# `docs/make-screens.py --check` nicht zu gewinnen: jede Sekunde eine andere ID.
DEMO_DATE = "2026-01-02T10:00:00+00:00"


def _demo_env() -> dict[str, str]:
    """Umgebung für jeden git-Aufruf der Demo-Sandbox.

    Sie wird von allen GIT_*-Variablen befreit: GIT_AUTHOR_NAME, GIT_COMMITTER_*,
    GIT_CONFIG_* oder GIT_DEFAULT_HASH der aufrufenden Shell würden sonst
    Identität, Config oder Hashformat übersteuern — und damit die
    maschinenunabhängigen Demo-Commit-IDs brechen, auf denen der Bild-Check
    (`docs/make-screens.py --check`) beruht. GIT_DIR wäre sogar schlimmer als
    das: Es schlägt `-C <repo>` durch, der Aufruf landete also in einem ganz
    anderen Repo. Identität und Datum stehen deshalb hier explizit; globale und
    System-Gitconfig bleiben außen vor.
    """
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env.update(GIT_AUTHOR_DATE=DEMO_DATE, GIT_COMMITTER_DATE=DEMO_DATE,
               GIT_AUTHOR_NAME="Demo", GIT_AUTHOR_EMAIL="demo@example.invalid",
               GIT_COMMITTER_NAME="Demo", GIT_COMMITTER_EMAIL="demo@example.invalid",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


def _dgit(repo: Path, *args: str) -> None:
    """git-Aufruf in der Demo-Sandbox; wirft bei Fehler (Sandbox muss sauber bauen)."""
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, env=_demo_env())


def _dgit_conflict(repo: Path, *args: str) -> None:
    """Demo-git-Aufruf, dessen erwartetes Ergebnis ein Konflikt ist (Exit 1).

    Bruder von `_dgit()`: dieselbe bereinigte Umgebung, aber `check=False`.
    Genau Exit 1 geht durch — das ist der Konflikt, den die Demo zeigen will.
    Exit 0 (kein Konflikt entstanden) und jeder andere Code sind ein Fehler und
    fliegen auf, statt still ein falsches Demo-Bild zu bauen.
    """
    result = subprocess.run(["git", "-C", str(repo), *args], check=False,
                            capture_output=True, text=True, env=_demo_env())
    if result.returncode != 1:
        raise subprocess.CalledProcessError(result.returncode, result.args,
                                            result.stdout, result.stderr)


def _demo_repo(root: Path, name: str, branch: str = "main") -> tuple[Path, Path]:
    """Neues Repo mit eigenem bare-"origin"-Remote + Erst-Commit, gepusht, upstream=origin/<branch>.

    `branch` ist der einzige Branch des Repos — so lassen sich auch Repos mit
    `master`, `develop` oder `feature/...` zeigen, ohne Sonderfälle im Aufrufer.
    """
    bare = root / "_remotes" / f"{name}.git"
    bare.mkdir(parents=True)
    _dgit(bare, "init", "-q", "--bare")
    repo = root / name
    repo.mkdir()
    _dgit(repo, "init", "-q", "-b", branch)
    _dgit(repo, "config", "user.email", "demo@example.invalid")
    _dgit(repo, "config", "user.name", "Demo")
    _dgit(repo, "config", "commit.gpgsign", "false")
    # Globale gitignore/Hooks ausblenden, sonst hängt das Demo-Ergebnis an der
    # Maschine (eine globale .DS_Store-Regel würde z.B. eine Zeile verschlucken).
    _dgit(repo, "config", "core.excludesFile", "/dev/null")
    _dgit(repo, "config", "core.hooksPath", "/dev/null")
    (repo / "README.md").write_text(f"# {name}\n")
    _dgit(repo, "add", "README.md")
    _dgit(repo, "commit", "-qm", "initial commit")
    _dgit(repo, "remote", "add", "origin", str(bare))
    _dgit(repo, "push", "-q", "-u", "origin", branch)
    return repo, bare


def _demo_commit(repo: Path, fname: str, content: str, msg: str) -> None:
    (repo / fname).write_text(content)
    _dgit(repo, "add", fname)
    _dgit(repo, "commit", "-qm", msg)


def _demo_extra_remote(root: Path, repo: Path, remote: str, branch: str = "main",
                       url: str | None = None) -> None:
    """Zweites Remote (z.B. `github`, `backup`) mit aktuellem Stand anlegen.

    Es wird immer gegen ein lokales bare-Repo gepusht (kein Netz). `url` stellt die
    Adresse danach auf eine Beispieladresse um — damit zeigt die Demo z.B. die echte
    GitHub-Sicherheitsklasse, ohne je ins Netz zu gehen.
    """
    bare = root / "_remotes" / f"{repo.name}-{remote}.git"
    bare.mkdir(parents=True)
    _dgit(bare, "init", "-q", "--bare")
    _dgit(repo, "remote", "add", remote, str(bare))
    _dgit(repo, "push", "-q", remote, branch)
    if url:
        _dgit(repo, "remote", "set-url", remote, url)


def _demo_ahead(repo: Path, n: int, branch: str = "main") -> None:
    """n Commits erzeugen, die auf origin fehlen (Repo ist 'voraus')."""
    for i in range(n):
        _demo_commit(repo, "work.txt", f"step {i}\n", f"feat: step {i}")


def _demo_behind(repo: Path, n: int) -> None:
    """n Commits auf origin schieben und lokal zurücksetzen (Repo ist 'zurück')."""
    for i in range(n):
        _demo_commit(repo, "upstream.txt", f"remote step {i}\n", f"chore: remote step {i}")
    _dgit(repo, "push", "-q", "origin", "HEAD")
    _dgit(repo, "reset", "--hard", "-q", f"HEAD~{n}")


def build_demo_sandbox(base: Path) -> Path:
    """Wegwerf-Sandbox mit Fake-Repos in allen Zuständen (Screenshots/Ausprobieren).

    Nutzt lokale bare-Repos als Remotes (kein Netz). Deckt ab: sauber & synchron,
    modified/untracked, ahead/behind/auseinandergelaufen, Merge-Konflikt + Stash, nur
    Stash, kein Sync-Remote, mehrere Remotes (origin/backup/github), Branches abseits
    von main (master, develop, feature/…, release/…) und den 'fremder Upstream'-Hinweis
    (↑n github).
    """
    root = base / "gmf-demo"
    root.mkdir(parents=True, exist_ok=True)

    # 1) sauber & synchron, aber 3 Commits vor 'github' (cyaner Upstream-Badge)
    repo, _ = _demo_repo(root, "webshop-frontend")
    gh = root / "_remotes" / "webshop-frontend-github.git"
    gh.mkdir(parents=True)
    _dgit(gh, "init", "-q", "--bare")
    _dgit(repo, "remote", "add", "github", str(gh))
    _dgit(repo, "push", "-q", "github", "main")               # github/main = Basis
    # Nach dem lokalen Aufbau nur die URL auf eine harmlose Beispieladresse
    # umstellen. So zeigt die Demo die echte GitHub-Sicherheitsklasse, ohne Netz.
    _dgit(repo, "remote", "set-url", "github",
          "https://github.com/example/webshop-frontend.git")
    for i in range(3):
        _demo_commit(repo, "app.js", f"// build {i}\n", f"feat: change {i}")
    _dgit(repo, "push", "-q", "origin", "main")               # origin synchron
    _dgit(repo, "branch", "--set-upstream-to=github/main", "main")

    # 2) modified + untracked
    repo, _ = _demo_repo(root, "api-gateway")
    (repo / "README.md").write_text("# api-gateway\n\nlocal change\n")
    (repo / "server.py").write_text("print('wip')\n")
    (repo / "notes.txt").write_text("todo\n")

    # 3) mehrere untracked
    repo, _ = _demo_repo(root, "dotfiles")
    (repo / "install.sh").write_text("#!/bin/sh\n")
    (repo / ".DS_Store").write_text("junk\n")
    (repo / "debug.log").write_text("log\n")

    # 4) 2 Commits vor dem Sync-Remote (ungepusht), Baum sauber
    repo, _ = _demo_repo(root, "blog-astro")
    for i in range(2):
        _demo_commit(repo, "post.md", f"# post {i}\n", f"post: entry {i}")

    # 5) 1 Commit hinter dem Sync-Remote
    repo, _ = _demo_repo(root, "invoice-parser")
    _demo_commit(repo, "parser.py", "v2\n", "fix: parsing")
    _dgit(repo, "push", "-q", "origin", "main")               # origin voraus
    _dgit(repo, "reset", "--hard", "-q", "HEAD~1")            # lokal 1 zurück

    # 6) Merge-Konflikt (2 Dateien) + erhaltener Stash
    repo, _ = _demo_repo(root, "ml-experiments")
    _demo_commit(repo, "a.txt", "a-base\n", "add a")
    _demo_commit(repo, "b.txt", "b-base\n", "add b")
    _dgit(repo, "push", "-q", "origin", "main")
    (repo / "a.txt").write_text("a-stash\n")
    (repo / "b.txt").write_text("b-stash\n")
    _dgit(repo, "stash")                                       # Stash: a/b geändert
    (repo / "a.txt").write_text("a-head\n")
    (repo / "b.txt").write_text("b-head\n")
    _dgit(repo, "commit", "-qam", "conflicting change")
    _dgit(repo, "push", "-q", "origin", "main")
    _dgit_conflict(repo, "stash", "pop")       # erzeugt Konflikt, behält Stash

    # 7) nur ein Stash (Baum sonst sauber)
    repo, _ = _demo_repo(root, "game-jam")
    (repo / "README.md").write_text("# game-jam\n\nunfinished\n")
    _dgit(repo, "stash")

    # 8) sauber & synchron (schlichtes Grün)
    _demo_repo(root, "notes-vault")

    # 10) master statt main, sauber & synchron
    _demo_repo(root, "legacy-cms", branch="master")

    # 11) master + lokale Änderung
    repo, _ = _demo_repo(root, "payroll-tool", branch="master")
    (repo / "README.md").write_text("# payroll-tool\n\nrate table 2026\n")

    # 12) auseinandergelaufen: 1 zurück und 2 voraus
    repo, _ = _demo_repo(root, "site-generator")
    _demo_behind(repo, 1)
    _demo_ahead(repo, 2)

    # 13) eigener Branch 'develop', 3 Commits voraus
    repo, _ = _demo_repo(root, "iot-firmware", branch="develop")
    _demo_ahead(repo, 3)

    # 14) Feature-Branch mit Slash im Namen + untracked
    repo, _ = _demo_repo(root, "data-pipeline", branch="feature/etl-rewrite")
    (repo / "sketch.py").write_text("# draft\n")

    # 15) drei Remotes (origin, backup, github), sauber
    repo, _ = _demo_repo(root, "docs-portal")
    _demo_extra_remote(root, repo, "backup")
    _demo_extra_remote(root, repo, "github",
                       url="https://github.com/example/docs-portal.git")

    # 16) Backup-Remote + 2 Commits zurück
    repo, _ = _demo_repo(root, "photo-sorter")
    _demo_extra_remote(root, repo, "backup")
    _demo_behind(repo, 2)

    # 17) fremder Upstream (github) UND lokale Änderung
    repo, _ = _demo_repo(root, "auth-service")
    _demo_extra_remote(root, repo, "github",
                       url="https://github.com/example/auth-service.git")
    _demo_ahead(repo, 2)
    _dgit(repo, "push", "-q", "origin", "main")
    _dgit(repo, "branch", "--set-upstream-to=github/main", "main")
    (repo / "token.py").write_text("SECRET = None\n")

    # 18) zwei Stashes, Baum sonst sauber
    repo, _ = _demo_repo(root, "recipe-app")
    for i in range(2):
        (repo / "README.md").write_text(f"# recipe-app\n\ndraft {i}\n")
        _dgit(repo, "stash")

    # 19) Release-Branch, sauber & synchron
    _demo_repo(root, "monorepo-sandbox", branch="release/2.1")

    # 20) viele Commits voraus
    repo, _ = _demo_repo(root, "chess-engine")
    _demo_ahead(repo, 5)

    # 21) deutlich zurück
    repo, _ = _demo_repo(root, "portfolio-site")
    _demo_behind(repo, 3)

    # 22) master + Backup-Remote + Stash
    repo, _ = _demo_repo(root, "budget-tracker", branch="master")
    _demo_extra_remote(root, repo, "backup", branch="master")
    (repo / "README.md").write_text("# budget-tracker\n\nwip\n")
    _dgit(repo, "stash")

    # 23) viele untracked Dateien
    repo, _ = _demo_repo(root, "sensor-logs")
    for i in range(4):
        (repo / f"run-{i}.csv").write_text("t,v\n")

    # 24) Konflikt auf master (zweiter Konfliktfall, andere Farbe im Kopf)
    repo, _ = _demo_repo(root, "test-harness", branch="master")
    _demo_commit(repo, "case.txt", "base\n", "add case")
    _dgit(repo, "push", "-q", "origin", "master")
    (repo / "case.txt").write_text("stashed\n")
    _dgit(repo, "stash")
    (repo / "case.txt").write_text("head\n")
    _dgit(repo, "commit", "-qam", "conflicting change")
    _dgit(repo, "push", "-q", "origin", "master")
    _dgit_conflict(repo, "stash", "pop")       # erzeugt Konflikt, behält Stash

    # 25) Änderung + Stash gleichzeitig
    repo, _ = _demo_repo(root, "vpn-config")
    (repo / "README.md").write_text("# vpn-config\n\nold\n")
    _dgit(repo, "stash")
    (repo / "peers.conf").write_text("[Peer]\n")

    # 26) voraus + untracked
    repo, _ = _demo_repo(root, "cli-toolkit")
    _demo_ahead(repo, 1)
    (repo / "TODO.md").write_text("- ship\n")

    # 27) sauber & synchron auf develop
    _demo_repo(root, "wiki-export", branch="develop")

    # 9) gar kein Remote
    repo = root / "scratchpad"
    repo.mkdir()
    _dgit(repo, "init", "-q", "-b", "main")
    _dgit(repo, "config", "user.email", "demo@example.invalid")
    _dgit(repo, "config", "user.name", "Demo")
    _dgit(repo, "config", "commit.gpgsign", "false")
    _dgit(repo, "config", "core.excludesFile", "/dev/null")
    _dgit(repo, "config", "core.hooksPath", "/dev/null")
    (repo / "idea.md").write_text("# scratch\n")
    _dgit(repo, "add", "idea.md")
    _dgit(repo, "commit", "-qm", "initial commit")

    return root


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fast TUI overview of every Git repo below the current directory.")
    ap.add_argument("root", nargs="?", default=".",
                    help="start directory for the repo scan (default: current dir)")
    ap.add_argument("--list", action="store_true",
                    help="non-interactive colored text list")
    ap.add_argument("--json", action="store_true",
                    help="non-interactive JSON output (machine-readable)")
    ap.add_argument("--fetch", action="store_true",
                    help="`git fetch` each repo before output (with --list/--json)")
    ap.add_argument("--diff", metavar="HOST[:PATH]",
                    help="compare this machine's repos with HOST over ssh and print "
                         "only the differences. Branches, index, and working trees stay "
                         "untouched; config.json may be created and --fetch updates safe "
                         "remote-tracking refs. Needs `ssh HOST` to work; "
                         "gitmaster_flash need NOT be installed there. PATH overrides "
                         "the directory scanned on the other side.")
    ap.add_argument("--lang", choices=["en", "de"],
                    help="UI language (overrides config; default: auto from $LANG)")
    ap.add_argument("--demo", action="store_true",
                    help="build a throwaway sandbox of fake repos and run on it")
    ap.add_argument("--cd-file", metavar="FILE",
                    help="(internal, for the gmf wrapper) write the ⏎ target path here")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    global UI_LANG

    if args.demo:
        # Demo ignoriert die persönliche Config bewusst: generische Apps + Sprache,
        # damit Screenshots reproduzierbar und neutral sind.
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        # Der Bildgenerator beendet einen festhängenden Capture nach 90 Sekunden.
        # Sein Demo-Commit muss vorher selbst timeouten, damit `run_git()` dessen
        # eigene Git-/Hook-Prozessgruppe zuverlässig tötet, bevor die TUI endet.
        if os.environ.get("GMF_SCREEN_CAPTURE") == "1":
            cfg["commit_timeout"] = min(cfg["commit_timeout"], 45)
        cfg["apps"] = {k.upper(): v for k, v in cfg["apps"].items()}
        UI_LANG = resolve_lang(cfg, args.lang)
        sandbox = build_demo_sandbox(Path(tempfile.mkdtemp(prefix="gmf-demo-")))
        # Auch die anschließend in der TUI ausgelösten Demo-Aktionen müssen die
        # feste Identität und Zeit verwenden. `_dgit()` bindet nur den Aufbau;
        # ohne diese Prozessumgebung bekäme der Screenshot-Commit bei jedem Lauf
        # eine andere OID und `make-screens.py --check` wäre nicht reproduzierbar.
        for key in [key for key in os.environ if key.startswith("GIT_")]:
            os.environ.pop(key, None)
        os.environ.update({key: value for key, value in _demo_env().items()
                           if key.startswith("GIT_")})
        print(t("demo_built", p=sandbox), file=sys.stderr)
        root = sandbox
    else:
        cfg = load_config()
        UI_LANG = resolve_lang(cfg, args.lang)
        root = Path(args.root).expanduser().resolve()
        if not root.is_dir():
            print(t("not_a_dir", p=root), file=sys.stderr)
            return 1

    if args.diff:
        return run_diff(args.diff, root, cfg, fetch=args.fetch, as_json=args.json)

    if args.list or args.json or not sys.stdout.isatty():
        statuses = collect_all(root, cfg, fetch=args.fetch)
        if args.json:
            # Objekt statt nacktem Array (seit 0.6.0): nur so lassen sich Version und
            # Wurzel mitgeben — beim Vergleich zweier Macs muss erkennbar sein, ob
            # dieselbe Fassung dahintersteht. Die Repos liegen unter "repos".
            print(json.dumps({"version": __version__, "root": str(root),
                              "repos": [status_dict(s) for s in statuses]},
                             indent=2, ensure_ascii=False))
        else:
            print_list(statuses, root)
        # Exit-Code 1, wenn irgendein Repo Aufmerksamkeit braucht (skriptbar).
        return 0 if all(s.clean_and_synced for s in statuses) else 1

    def _run(stdscr):
        init_colors()
        TUI(stdscr, root, cfg, args.cd_file).run()

    curses.wrapper(_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
