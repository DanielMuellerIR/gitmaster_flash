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
  C     commit helper: suggests what to commit and what to .gitignore
  P     safely push the current branch to the private sync remote
  L     safely fast-forward the current branch from the private sync remote
  G     guarded GitHub push (preview + typed confirmation; branch only, no tags)
  H     explain the Git safety rules
  I     show repository details, remote addresses, and clickable GitHub URLs
  U     apply the latest stash (git stash pop, with confirmation)
  S     view the latest stash as a diff (read-only, scrollable)
  D     drop the latest stash (git stash drop, with confirmation)
  R     reload everything incl. `git fetch --all` (shows progress)
  Q     quit

Non-interactive: with --list / --json (or no TTY) it prints the overview as text
or JSON (machine-readable). Exit code 1 if any repo needs attention.

Two machines: `--diff HOST` compares this machine's repos with another one over
ssh and prints only the differences (read-only, never changes anything). The only
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
import posixpath
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.15.2"

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
    # `git commit` führt den pre-commit-Hook des Repos aus — und der startet in
    # vielen Projekten Linter oder Tests, die deutlich länger als zehn Sekunden
    # brauchen. Mit dem kurzen git_timeout wäre jeder solche Commit chancenlos.
    "commit_timeout": 120,
}

# Muster für die Commit-Hilfe: Dateien, die typischerweise in .gitignore gehören.
# (basename_oder_teil, ist_verzeichnis, gitignore_zeile)
IGNORE_RULES = [
    ("node_modules", True, "node_modules/"),
    ("__pycache__", True, "__pycache__/"),
    (".venv", True, ".venv/"),
    ("venv", True, "venv/"),
    ("dist", True, "dist/"),
    ("build", True, "build/"),
    (".idea", True, ".idea/"),
    (".pytest_cache", True, ".pytest_cache/"),
    (".mypy_cache", True, ".mypy_cache/"),
    (".ruff_cache", True, ".ruff_cache/"),
    (".DS_Store", False, ".DS_Store"),
    ("Thumbs.db", False, "Thumbs.db"),
    (".env", False, ".env"),
]
IGNORE_SUFFIXES = {".pyc": "*.pyc", ".log": "*.log", ".tmp": "*.tmp"}


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
    "diff_ssh_failed": {"en": "Cannot reach {h}: {e}", "de": "{h} nicht erreichbar: {e}"},
    "diff_ssh_exit": {"en": "ssh exited with code {code}",
                      "de": "ssh endete mit Code {code}"},
    "diff_ssh_no_output": {"en": "no output", "de": "keine Ausgabe"},
    "diff_ssh_bad_json": {"en": "unreadable JSON", "de": "unlesbares JSON"},
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
    "diff_repo_field": {
        "en": "DRIFT  {rel}: {field} is {va} {a}, {vb} {b}",
        "de": "DRIFT  {rel}: {field} {a}={va}, {b}={vb}"},
    "diff_remote_security": {
        "en": "DRIFT  {rel}: security/endpoint identity for {r} differs",
        "de": "DRIFT  {rel}: Sicherheit/Ziel-Identität für {r} unterscheidet sich"},
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
    "stash_row_hint": {"en": "(U pop · S preview · D drop)",
                       "de": "(U anwenden · S Vorschau · D verwerfen)"},
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
    "f2": {"en": " {apps} · A changes · C commit · U stash pop · S stash view · D stash drop",
           "de": " {apps} · A Änderungen · C Commit · U Stash pop · S Stash-Blick · D Stash weg"},
    "f3": {"en": " R fetch all · P sync push · L sync pull · G GitHub push · Q quit",
           "de": " R fetch all · P Sync-Push · L Sync-Pull · G GitHub-Push · Q Beenden"},
    # Kompakte Ansicht und Protokollbereich
    "compact_more": {"en": "columns {a}-{b}/{n}", "de": "Spalten {a}-{b}/{n}"},
    "log_pane_title": {"en": " Commands", "de": " Befehle"},
    "log_pane_hint": {"en": "  (Tab to scroll)", "de": "  (Tab zum Scrollen)"},
    "log_pane_focus": {"en": "  ↑/↓ scroll · Tab back to the list",
                       "de": "  ↑/↓ scrollen · Tab zurück zur Liste"},
    "yesno": {"en": "  (Y/N)", "de": "  (J/N)"},
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
    "resolve_conflicts_first": {
        "en": "Resolve the merge conflicts first (open the repo with an app key), "
              "then press U again.",
        "de": "Erst die Merge-Konflikte auflösen (App-Taste öffnet das Repo), "
              "dann erneut U drücken."},
    "confirm_pop": {"en": "Apply latest stash in '{rel}' (git stash pop)?",
                    "de": "Neuesten Stash in '{rel}' anwenden (git stash pop)?"},
    "cancelled": {"en": "Cancelled.", "de": "Abgebrochen."},
    "stash_applied": {"en": "Stash applied in {rel}.", "de": "Stash angewendet in {rel}."},
    "stash_conflict": {
        "en": "Stash created {n} merge conflict(s) — the stash is kept. "
              "Open the repo with an app key and resolve.",
        "de": "Stash erzeugte {n} Merge-Konflikt(e) — Stash bleibt erhalten. "
              "Repo mit einer App-Taste öffnen und auflösen."},
    "stash_pop_failed": {"en": "stash pop failed: {e}", "de": "stash pop fehlgeschlagen: {e}"},
    "empty_diff": {"en": "(empty diff)", "de": "(leerer Diff)"},
    "stash_preview_failed": {"en": "Stash preview failed: {e}",
                              "de": "Stash-Vorschau fehlgeschlagen: {e}"},
    "stash_preview_empty": {
        "en": "(stash exists, but Git produced no displayable patch)",
        "de": "(Stash vorhanden, aber Git erzeugte keinen darstellbaren Patch)"},
    "stash_preview_title": {"en": "Stash preview · {rel} · {s}",
                            "de": "Stash-Vorschau · {rel} · {s}"},
    "confirm_drop": {"en": "Drop latest stash in '{rel}' PERMANENTLY "
                           "(git stash drop)? Cannot be undone.",
                     "de": "Neuesten Stash in '{rel}' ENDGÜLTIG verwerfen "
                           "(git stash drop)? Nicht rückgängig machbar."},
    "drop_cancelled": {"en": "Cancelled — stash kept.",
                       "de": "Abgebrochen — Stash bleibt erhalten."},
    "stash_dropped": {"en": "Stash dropped in {rel}.", "de": "Stash verworfen in {rel}."},
    "stash_drop_failed": {"en": "stash drop failed: {e}",
                          "de": "stash drop fehlgeschlagen: {e}"},
    # Pager
    "pager_footer": {"en": " ↑/↓ scroll · Q/Esc close · line {a}-{b} / {n}",
                     "de": " ↑/↓ scrollen · Q/Esc schließen · Zeile {a}-{b} / {n}"},
    # Commit-Hilfe
    "commit_title": {"en": "Commit helper · {rel} — review, then ⏎",
                     "de": "Commit-Hilfe · {rel} — Vorschlag prüfen, dann ⏎"},
    "to_gitignore": {"en": "→ .gitignore ({p})", "de": "→ .gitignore ({p})"},
    "do_commit": {"en": "✔ commit", "de": "✔ committen"},
    "do_skip": {"en": "✘ skip", "de": "✘ auslassen"},
    "commit_footer": {"en": " ␣ commit on/off · i gitignore on/off · ⏎ next · Esc cancel",
                      "de": " ␣ committen an/aus · i gitignore an/aus · ⏎ weiter · Esc abbrechen"},
    "commit_cancelled": {"en": "Commit helper cancelled.", "de": "Commit-Hilfe abgebrochen."},
    "nothing_selected": {"en": "Nothing selected.", "de": "Nichts ausgewählt."},
    "commit_in": {"en": "Commit in {rel}", "de": "Commit in {rel}"},
    "new_in_gitignore": {"en": "New in .gitignore:", "de": "Neu in .gitignore:"},
    "more_entries": {"en": "… and {n} more", "de": "… und {n} weitere"},
    "to_commit_n": {"en": "To commit: {n} file(s)", "de": "Zu committen: {n} Datei(en)"},
    "recent_msgs": {"en": "Recent commit messages (style reference):",
                    "de": "Letzte Commit-Messages (Stil-Vorlage):"},
    "commit_msg_prompt": {"en": "Commit message: ", "de": "Commit-Message: "},
    "empty_msg": {"en": "Empty message — cancelled.", "de": "Leere Message — abgebrochen."},
    "git_add_failed": {"en": "git add failed: {e}", "de": "git add fehlgeschlagen: {e}"},
    "commit_failed": {"en": "Commit failed: {e}", "de": "Commit fehlgeschlagen: {e}"},
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
    # Der Rückgängig-Befehl steht bewusst in der Meldung: Wer gerade committet hat,
    # soll nicht suchen müssen, wie er es zurücknimmt.
    "committed_in": {"en": "Committed in {rel}. Undo: git reset --soft HEAD~1",
                     "de": "Committet in {rel}. Rückgängig: git reset --soft HEAD~1"},
    "confirm_push": {"en": "Push {n} commit(s) to {r} now?",
                     "de": "Jetzt {n} Commit(s) zu {r} pushen?"},
    "committed_pushed": {"en": "Committed & pushed ({r}).", "de": "Committet & gepusht ({r})."},
    "push_failed": {"en": "Push failed (Git exit code {code}).",
                    "de": "Push fehlgeschlagen (Git-Exit-Code {code})."},
    "pull_failed": {"en": "Fast-forward failed (Git exit code {code}).",
                    "de": "Fast-forward fehlgeschlagen (Git-Exit-Code {code})."},
    "nothing_to_commit": {"en": "Nothing to commit in this repo.",
                          "de": "Nichts zu committen in diesem Repo."},
    # Sichere Push-/Pull-Hilfe
    "no_sync_for_action": {"en": "No sync remote is configured for this repository.",
                           "de": "Für dieses Repo ist kein Sync-Remote konfiguriert."},
    "public_simple_block": {
        "en": "The sync remote is public. Use G for the guarded GitHub preview.",
        "de": "Der Sync-Remote ist öffentlich. Nutze G für die geschützte GitHub-Vorschau."},
    "transfer_fetch_failed": {"en": "Fetch from {r} failed (Git exit code {code}).",
                              "de": "Fetch von {r} fehlgeschlagen (Git-Exit-Code {code})."},
    # Kurz halten: diese Meldung erscheint auch als Badge in der Repo-Zeile und
    # wird dort auf die Terminalbreite abgeschnitten. Die Langfassung steht im README.
    "transfer_auth_missing": {
        "en": "{r} needs a login (no credential helper or SSH key).",
        "de": "{r} braucht einen Login (kein Credential-Helper/SSH-Key)."},
    "fetch_remote_failed": {
        "en": "Fetch from {r} failed; press I to check the cause.",
        "de": "Fetch von {r} fehlgeschlagen; mit I die Ursache prüfen."},
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
    "nothing_to_pull": {"en": "Nothing to pull from {r}.", "de": "Nichts von {r} zu pullen."},
    "confirm_sync_push": {"en": "Push {n} commit(s) to the private sync remote {r}?",
                          "de": "{n} Commit(s) zum privaten Sync-Remote {r} pushen?"},
    "confirm_sync_pull": {"en": "Fast-forward {n} commit(s) from the private sync remote {r}?",
                          "de": "{n} Commit(s) per Fast-forward vom privaten Sync-Remote {r} holen?"},
    "sync_pushed": {"en": "Pushed current branch to {r} (no tags).",
                    "de": "Aktuellen Branch zu {r} gepusht (keine Tags)."},
    "sync_pulled": {"en": "Fast-forwarded current branch from {r}.",
                    "de": "Aktuellen Branch per Fast-forward von {r} geholt."},
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
              "L  Pull only from the private sync remote by fast-forward.\n"
              "   Never merges or rebases and refuses dirty/divergent repositories.\n\n"
              "G  Guarded GitHub push. Shows outgoing commits and file names first.\n"
              "   Requires typing PUSH <remote>; pins source and target OIDs and sends no tags.\n"
              "   New or unrelated GitHub branches remain terminal-only special cases.\n\n"
              "R  Fetches all remotes in all repositories; it does not change working trees.",
        "de": "P  Nur den aktuellen Branch zum privaten Sync-Remote pushen.\n"
              "   Verlangt einen sauberen Tree, fetcht zuerst und blockiert Rückstand/Divergenz.\n\n"
              "L  Nur per Fast-forward vom privaten Sync-Remote holen.\n"
              "   Führt nie Merge oder Rebase aus und verweigert dirty/divergente Repos.\n\n"
              "G  Geschützter GitHub-Push mit Vorschau von Commits und Dateinamen.\n"
              "   Verlangt PUSH <Remote>; pinnt Quell-/Ziel-OID und sendet keine Tags.\n"
              "   Neue oder unverbundene GitHub-Branches bleiben Terminal-Sonderfälle.\n\n"
              "R  Fetcht alle Remotes aller Repos; Working Trees bleiben unverändert."},
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
        "en": " T test remote (does it still exist?) · X remove remote (local config only)",
        "de": " T Remote prüfen (existiert es noch?) · X Remote entfernen (nur lokale Config)"},
    "info_footer_actions_branch": {
        "en": " X delete branch (only if merged; commits stay reachable)",
        "de": " X Branch löschen (nur wenn gemergt; Commits bleiben erreichbar)"},
    "info_no_remotes": {"en": "This repository has no remote.",
                        "de": "Dieses Repo hat kein Remote."},
    "info_nothing_selected": {"en": "Nothing selected.", "de": "Nichts ausgewählt."},
    "info_check_remote_only": {"en": "T tests remotes; a branch is local anyway.",
                               "de": "T prüft Remotes; ein Branch ist ohnehin lokal."},
    # Branch löschen (X auf einem Branch)
    "branch_delete_title": {"en": "Delete branch · {b}", "de": "Branch löschen · {b}"},
    "branch_is_current": {"en": "{b} is the current branch — switch branches first.",
                          "de": "{b} ist der aktuelle Branch — erst wechseln."},
    "branch_not_merged": {
        "en": "{b} is not merged into HEAD; gmf deletes merged branches only "
              "(terminal: git branch -D {b}).",
        "de": "{b} ist nicht in HEAD gemergt; gmf löscht nur gemergte Branches "
              "(Terminal: git branch -D {b})."},
    "branch_effect_pointer": {
        "en": "· only the branch pointer {b} disappears from .git/config and refs",
        "de": "· es verschwindet nur der Branch-Zeiger {b} aus Config und Refs"},
    "branch_effect_merged": {
        "en": "· its commits are already in HEAD, so nothing is lost",
        "de": "· seine Commits stecken schon in HEAD, es geht also nichts verloren"},
    "branch_effect_remote": {
        "en": "· a branch of the same name on a remote is NOT touched",
        "de": "· ein gleichnamiger Branch auf einem Remote bleibt unberührt"},
    "branch_effect_safe": {
        "en": "· files, stashes and other branches stay untouched",
        "de": "· Dateien, Stashes und andere Branches bleiben unberührt"},
    "branch_delete_confirm": {"en": "Delete branch {b} now?",
                              "de": "Branch {b} jetzt löschen?"},
    "branch_deleted": {"en": "Deleted branch {b} (was {oid}).",
                       "de": "Branch {b} gelöscht (war {oid})."},
    "branch_delete_failed": {"en": "Deleting {b} failed: {e}",
                             "de": "Löschen von {b} fehlgeschlagen: {e}"},
    "branch_delete_cancelled": {"en": "No branch was deleted.",
                                "de": "Es wurde kein Branch gelöscht."},
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
    "check_unknown": {"en": "{r}: unclear result — {e}", "de": "{r}: unklares Ergebnis — {e}"},
    # Stichworte für die Repo-Zeile (der ganze Satz steht auf der Info-Seite)
    "short_gone": {"en": "repository gone", "de": "Repo weg"},
    "short_auth": {"en": "login missing", "de": "Login fehlt"},
    "short_hostkey": {"en": "host key unknown", "de": "Hostschlüssel unbekannt"},
    "short_dns": {"en": "host not found", "de": "Host nicht gefunden"},
    "short_unreachable": {"en": "no connection", "de": "keine Verbindung"},
    "short_server": {"en": "server error", "de": "Serverfehler"},
    "short_timeout": {"en": "no answer", "de": "keine Antwort"},
    "short_unknown": {"en": "fetch failed", "de": "Fetch fehlgeschlagen"},
    # Remote entfernen (X)
    "remove_title": {"en": "Remove remote · {r}", "de": "Remote entfernen · {r}"},
    "remove_what_happens": {"en": "What this does:", "de": "Was dabei passiert:"},
    "remove_effect_config": {
        "en": "· the [remote \"{r}\"] section disappears from .git/config",
        "de": "· der Abschnitt [remote \"{r}\"] verschwindet aus .git/config"},
    "remove_effect_refs": {
        "en": "· the remote-tracking branches refs/remotes/{r}/* are deleted",
        "de": "· die Remote-Tracking-Branches refs/remotes/{r}/* werden gelöscht"},
    "remove_effect_upstream": {
        "en": "· a local branch tracking {r} loses its upstream setting",
        "de": "· ein lokaler Branch mit Upstream auf {r} verliert diese Verknüpfung"},
    "remove_effect_safe": {
        "en": "· commits, files, branches and stashes stay untouched — nothing is sent",
        "de": "· Commits, Dateien, Branches und Stashes bleiben unberührt — nichts wird gesendet"},
    "remove_effect_server": {
        "en": "· nothing changes on the server; this is purely local",
        "de": "· auf dem Server ändert sich nichts; das ist rein lokal"},
    "remove_undo": {"en": "Undo (same URL again):", "de": "Rückgängig (URL wieder eintragen):"},
    "remove_command": {"en": "Command:", "de": "Befehl:"},
    "remove_sync_warning": {
        "en": "Careful: {r} is the sync remote here — P and L stop working for this repo.",
        "de": "Achtung: {r} ist hier der Sync-Remote — P und L funktionieren danach nicht mehr."},
    "remove_confirm": {"en": "Remove remote {r} now?", "de": "Remote {r} jetzt entfernen?"},
    "remove_done": {"en": "Removed remote {r}.", "de": "Remote {r} entfernt."},
    "remove_failed": {"en": "Removing {r} failed (Git exit code {code}).",
                      "de": "Entfernen von {r} fehlgeschlagen (Git-Exit-Code {code})."},
    "remove_cancelled": {"en": "Nothing was removed.", "de": "Es wurde nichts entfernt."},
    # Befehlsprotokoll
    "cmdlog_title": {"en": "Commands this session ran",
                     "de": "In dieser Sitzung ausgeführte Befehle"},
    "cmdlog_cancelled": {"en": "not run — cancelled", "de": "nicht ausgeführt — abgebrochen"},
    "cmdlog_empty": {
        "en": "(none yet — actions like C, P, L, G, U, D and X are listed here)",
        "de": "(noch keine — Aktionen wie C, P, L, G, U, D und X stehen hier)"},
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
    conflicts: int = 0            # ungemergte Dateien (Merge-Konflikt, z.B. nach stash pop)
    files: list = field(default_factory=list)   # [(Buchstabe M/D/U/C, Pfad), ...]
    stashes: list = field(default_factory=list)  # ["stash@{0} WIP ...", ...]
    error: str = ""
    # Die Repo-Zeile bekommt das Stichwort (`error`), die Info-Seite den ganzen
    # Satz (`error_long`) und Gits eigenen Wortlaut als Beweis (`error_detail`).
    error_long: str = ""
    error_detail: str = ""

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
    push_fingerprints: list[str] = field(default_factory=list)
    target_mismatch: bool = False
    multiple_pushurls: bool = False
    fetch_failed: bool = False

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
    target_oid: str = ""

    @property
    def ready(self) -> bool:
        return self.reason == "ready"

    def approval_signature(self) -> tuple:
        return (self.branch, self.head_oid, self.index_oid, self.worktree_fingerprint,
                self.fetch_fingerprint, self.push_fingerprint, self.target_oid,
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


@dataclass
class RemoteConfig:
    name: str
    fetch_urls: list[str]
    push_urls: list[str]
    fetch_targets: list[RemoteTarget]
    push_targets: list[RemoteTarget]

    @property
    def transfer_safe(self) -> bool:
        return (len(self.fetch_targets) == 1 and len(self.push_targets) == 1
                and self.fetch_targets[0] == self.push_targets[0])


# Zwei-Buchstaben-Codes, die einen ungemergten Zustand (Merge-Konflikt) bedeuten.
# git status meldet solche Dateien z.B. nach einem `stash pop` mit Konflikt.
UNMERGED_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def parse_porcelain(output: str) -> tuple[int, int, int, int, list]:
    """NUL-getrenntes ``git status --porcelain=v1 -z`` auswerten.

    -> (modified, deleted, untracked, conflicts, dateien).
    Vereinfachung fürs Auge: Konflikt = C, Untracked = U, Gelöschtes = D, jede
    andere Änderung (modified/added/renamed/…) = M. Konflikte werden ZUERST
    geprüft, sonst würde z.B. `UD` fälschlich als Löschung zählen.

    ``-z`` ist für die Commit-Hilfe entscheidend: Ohne diese Option setzt Git
    Pfade mit Umlauten oder Steuerzeichen in Anführungszeichen und maskiert sie.
    Diese Anzeigeform ist kein gültiger Pfad für ein späteres ``git add``.
    Rename-/Copy-Einträge besitzen bei ``-z`` ein zweites Feld mit dem alten
    Namen; für Anzeige und Staging brauchen wir den ersten, neuen Namen.
    """
    m = d = u = c = 0
    files = []
    fields = output.split("\0")
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if not record:
            continue
        xy, path = record[:2], record[3:]
        if "R" in xy or "C" in xy:
            # Bei -z folgt nach dem Zielpfad noch der Quellpfad.
            i += 1
        if xy in UNMERGED_CODES:
            c += 1
            files.append(("C", path))
        elif xy == "??":
            u += 1
            files.append(("U", path))
        elif "D" in xy:
            d += 1
            files.append(("D", path))
        else:
            m += 1
            files.append(("M", path))
    return m, d, u, c, files


def suggested_ignore(path: str) -> str | None:
    """Liefert die passende .gitignore-Zeile, wenn die Datei typischer Müll ist."""
    parts = path.rstrip("/").split("/")
    basename = parts[-1]
    for name, is_dir, pattern in IGNORE_RULES:
        if is_dir and name in parts:
            return pattern
        if not is_dir and basename == name:
            return pattern
    for suffix, pattern in IGNORE_SUFFIXES.items():
        if basename.endswith(suffix):
            return pattern
    return None


class CommitSafetyError(RuntimeError):
    pass


def _path_signature(path: Path) -> tuple | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_mode,
            info.st_size, info.st_mtime_ns)


def update_gitignore_atomic(repo: Path, patterns: list[str]) -> bool:
    """Append unique rules without following symlinks or overwriting a raced target."""
    repo = repo.resolve()
    target = repo / ".gitignore"
    before = _path_signature(target)
    existing_text = ""
    if before is not None:
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CommitSafetyError("target is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags)
        except OSError as e:
            raise CommitSafetyError(str(e)) from e
        try:
            with os.fdopen(fd, "r") as f:
                existing_text = f.read()
        except (OSError, UnicodeError) as e:
            raise CommitSafetyError(str(e)) from e
    existing = set(existing_text.splitlines())
    new_lines = [pattern for pattern in patterns if pattern not in existing]
    if not new_lines:
        return False
    updated = existing_text
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated += "\n".join(new_lines) + "\n"

    fd, tmp_name = tempfile.mkstemp(prefix=".gitignore.gmf.", dir=repo)
    tmp = Path(tmp_name)
    try:
        mode = stat.S_IMODE(target.lstat().st_mode) if before is not None else 0o644
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(updated)
            f.flush()
            os.fsync(f.fileno())
        if _path_signature(target) != before:
            raise CommitSafetyError("target changed during update")
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return True


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


def commit_selected(repo: Path, paths: list[str], message: str, timeout: int,
                    commit_timeout: int | None = None) -> subprocess.CompletedProcess:
    """Commit exactly paths through a temporary index; preserve the user's index bytes.

    `timeout` gilt für die schnellen Vorbereitungsschritte (Index lesen, Baum
    schreiben). `commit_timeout` gilt nur für den Commit selbst, weil dort der
    pre-commit-Hook des Repos läuft; ohne Angabe bleibt es beim selben Wert.
    """
    if not paths:
        raise CommitSafetyError("no approved paths")
    if has_unmerged_entries(repo, timeout):
        raise CommitSafetyError("merge conflicts exist")
    real_before = _real_index_signature(repo, timeout)
    approved = set(paths)
    with tempfile.TemporaryDirectory(prefix="gmf-index-") as temp:
        index_path = str(Path(temp) / "index")
        env = dict(os.environ, GIT_INDEX_FILE=index_path)
        _required_git(repo, "read-tree", "HEAD", timeout=timeout, env=env)
        staged = run_git_logged(repo, "add", "--", *paths, timeout=timeout, env=env)
        if staged.returncode != 0:
            return staged
        names = _required_git(repo, "diff", "--cached", "--name-only", "-z", "--",
                              timeout=timeout, env=env)
        actual = {path for path in names.stdout.split("\0") if path}
        if actual != approved:
            raise CommitSafetyError("temporary index differs from approved paths")
        tree_before = _required_git(repo, "write-tree", timeout=timeout, env=env).stdout.strip()
        if has_unmerged_entries(repo, timeout):
            raise CommitSafetyError("merge conflicts appeared before commit")
        if _real_index_signature(repo, timeout) != real_before:
            raise CommitSafetyError("Git index changed during approval")
        # Stage once more and compare the complete tree to catch worktree races.
        _required_git(repo, "add", "--", *paths, timeout=timeout, env=env)
        tree_after = _required_git(repo, "write-tree", timeout=timeout, env=env).stdout.strip()
        if tree_after != tree_before:
            raise CommitSafetyError("approved files changed during commit preparation")
        result = run_git_logged(
            repo, "commit", "-m", message, env=env,
            timeout=timeout if commit_timeout is None else commit_timeout)
    if _real_index_signature(repo, timeout) != real_before:
        raise CommitSafetyError("Git changed the real index unexpectedly")
    if result.returncode == 0:
        adopt_commit_in_real_index(repo, paths, timeout)
    return result


def adopt_commit_in_real_index(repo: Path, paths: list[str], timeout: int) -> None:
    """Den echten Index für die committeten Pfade auf den neuen HEAD nachziehen.

    Ohne diesen Schritt bleibt der echte Index auf dem Stand von vor dem Commit
    stehen: Er zeigt für die eben committete Datei noch den alten Inhalt. `git
    status` vergleicht Arbeitsbaum und Index gegen HEAD und meldet die Datei
    deshalb weiter als geändert (`MM`) — obwohl der Commit einwandfrei ist. Genau
    das tut auch Git selbst nach einem `git commit -- <pfad>`.

    `git reset` klingt nach mehr, als es hier ist: In der Pfad-Form (mit `--`)
    fasst es ausschließlich diese Index-Einträge an — nie den Arbeitsbaum, nie
    einen Commit und nie die übrigen, bewusst gestageten Änderungen.
    """
    # Ein Fehler hier darf den bereits geschriebenen Commit nicht entwerten. Er
    # steht im Befehlsprotokoll (H) mit seinem Exit-Code; schlimmstenfalls sieht
    # die Datei bis zum nächsten `git add`/`git reset` weiter geändert aus.
    run_git_logged(repo, "reset", "-q", "HEAD", "--", *paths, timeout=timeout)


def current_head(repo: Path, timeout: int) -> str | None:
    """Commit-ID von HEAD — oder None, wenn sie sich nicht lesen lässt.

    Wird gebraucht, um nach einem abgebrochenen Commit zu unterscheiden, ob er noch
    zustande kam. Ein frisches Repo ohne Commits hat kein HEAD: auch dann None.
    """
    try:
        r = run_git(repo, "rev-parse", "HEAD", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def stash_preview(repo: Path, timeout: int) -> tuple[bool, str]:
    """Return a complete stash patch, including untracked and binary contents."""
    r = run_git(repo, "stash", "show", "-p", "--binary", "--include-untracked",
                "stash@{0}", timeout=timeout)
    if r.returncode != 0:
        detail = (r.stderr or "Git exit %d" % r.returncode).strip()[:240]
        return False, detail
    return True, r.stdout


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
    "LC_ALL": "C",                  # Meldungen bleiben stabil englisch (s.u.)
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


def classify_remote_check(result: subprocess.CompletedProcess) -> str:
    """Warum ist der Zugriff auf das Remote gescheitert?

    Liefert "dns", "unreachable", "server", "hostkey", "auth", "gone" oder
    "unknown". Reine Textauswertung von Gits Meldung (deshalb LC_ALL=C).
    """
    text = ((result.stderr or "") + "\n" + (result.stdout or "")).lower()
    for cause, markers in REMOTE_CHECK_CAUSES:
        if any(marker in text for marker in markers):
            return cause
    return "unknown"


def credentials_missing(result: subprocess.CompletedProcess) -> bool:
    """Fehlen wirklich Zugangsdaten? (Ein unbekannter Hostschlüssel ist etwas anderes.)"""
    return classify_remote_check(result) == "auth"


def last_error_line(result: subprocess.CompletedProcess) -> str:
    """Die aussagekräftigste Fehlerzeile von Git — der Beleg für die Ursache.

    `fetch --all` schließt mit der Sammelzeile "error: could not fetch <name>" ab,
    die nichts erklärt. Die eigentliche Ursache steht davor, deshalb werden solche
    Sammelzeilen übersprungen.
    """
    lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
    detailed = [line for line in lines if "could not fetch" not in line]
    return (detailed or lines or [""])[-1]


def check_remote(repo: Path, name: str, timeout: int) -> tuple[str, int, str]:
    """Existiert das Remote-Repo unter seiner Adresse — und wenn nicht, warum?

    `git ls-remote` fragt nur die Ref-Liste ab: es überträgt keine Objekte, ändert
    lokal nichts und ist damit der harmloseste echte Zugriffstest.
    Rückgabe: (Ergebnis, Anzahl Refs, letzte Fehlerzeile). Ergebnis ist "ok",
    "empty", "timeout" oder eine Ursache aus classify_remote_check().
    """
    try:
        r = run_git_logged(repo, "ls-remote", "--heads", "--", name, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", 0, ""
    if r.returncode == 0:
        refs = [line for line in r.stdout.splitlines() if line.strip()]
        return ("ok" if refs else "empty"), len(refs), ""
    detail = next((line.strip() for line in reversed((r.stderr or "").splitlines())
                   if line.strip()), "")
    return classify_remote_check(r), 0, detail[:160]


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
    if outcome in ("dns", "unreachable", "server", "hostkey", "auth", "gone"):
        return t("check_" + outcome, r=name)
    return t("check_unknown", r=name, e=detail or outcome)


def failed_fetch_remotes(result: subprocess.CompletedProcess) -> list[str]:
    """Namen der Remotes, die Git in `fetch --all` als gescheitert meldet.

    Git schreibt pro erfolglosem Remote eine Zeile "error: could not fetch <name>";
    dank LC_ALL=C ist dieser Text stabil. Findet sich nichts, bleibt die Liste leer
    und der Aufrufer nennt eben nur den Sammelbegriff.
    """
    names = []
    for line in (result.stderr or "").splitlines():
        _, sep, rest = line.partition("could not fetch ")
        if sep and rest.strip():
            names.append(rest.strip())
    return names


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
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, AttributeError):
        # Prozess schon weg, oder eine Plattform ohne Prozessgruppen: dann wenigstens
        # das direkte Kind beenden.
        proc.kill()


def run_git(repo: Path, *args: str, timeout: int = 10,
            env: dict | None = None) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo), *args]
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(env or os.environ, **NONINTERACTIVE_GIT_ENV),
        stdin=subprocess.DEVNULL,
        # Eigene Session = kein kontrollierendes Terminal. Damit kommt auch ein
        # von Git gestartetes ssh nicht mehr an unser /dev/tty, um dort nach einer
        # Passphrase zu fragen; der ssh-agent funktioniert davon unberührt weiter.
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            try:
                # Jetzt sind alle Schreiber tot, das Einsammeln der Reste ist kurz.
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            raise subprocess.TimeoutExpired(cmd, timeout, output=out,
                                            stderr=err) from None
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


# Protokoll der Befehle, die diese Sitzung bewusst abgesetzt hat (Reihenfolge = Verlauf).
# Zweck: Wer gmf benutzt, soll die Git-Syntax nebenbei mitlesen können, statt sie zu
# erraten. Nur ausgelöste Aktionen landen hier — die Lesebefehle des Repo-Scans würden
# das Protokoll unbrauchbar zumüllen.
COMMAND_LOG: list[str] = []
COMMAND_LOG_MAX = 200


def format_git_command(args: tuple[str, ...] | list[str]) -> str:
    """Den Befehl so schreiben, wie man ihn im Repo-Ordner selbst eintippen würde."""
    return "git " + " ".join(shlex.quote(a) for a in args)


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
    except subprocess.TimeoutExpired:
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
    # Der Aufruf sieht immer so aus: ["git", "-C", "<repo>", "<unterbefehl>", …]
    name = cmd[3] if len(cmd) > 3 else "git"
    return t("action_timeout", cmd=name, s=int(exc.timeout or 0))


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


def canonical_remote_target(url: str, repo: Path | None = None) -> RemoteTarget:
    """Credential-free host/repository identity for URL, SCP, and local syntax."""
    raw = url.strip()
    host = "local"
    port = None
    path = raw
    if "://" not in raw and ":" in raw and not raw.startswith(("/", "./", "../", "~")):
        hostpart, path = raw.split(":", 1)
        if "/" not in hostpart:
            host = _normal_host(hostpart.rsplit("@", 1)[-1])
    elif urllib.parse.urlsplit(raw).scheme:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme == "file":
            path = urllib.parse.unquote(parsed.path)
        else:
            host = _normal_host(parsed.hostname or "")
            port = parsed.port
            path = urllib.parse.unquote(parsed.path)
    if host == "local":
        local = Path(path).expanduser()
        if not local.is_absolute() and repo is not None:
            local = repo / local
        repo_id = str(local.resolve(strict=False))
        canonical = "local:" + repo_id
    else:
        repo_id = posixpath.normpath("/" + path.lstrip("/"))
        if repo_id.endswith(".git"):
            repo_id = repo_id[:-4]
        default_port = ((raw.startswith("ssh://") and port == 22)
                        or (raw.startswith("https://") and port == 443)
                        or (raw.startswith("http://") and port == 80))
        authority = host if not port or default_port else f"{host}:{port}"
        canonical = authority + ":" + repo_id
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return RemoteTarget(host, repo_id, fingerprint)


def read_remote_configs(repo: Path, cfg: dict) -> dict[str, RemoteConfig]:
    """Read every effective fetch/push target; any Git read error is fatal."""
    t_ = cfg["git_timeout"]
    names_r = _required_git(repo, "remote", timeout=t_)
    result = {}
    for name in [line for line in names_r.stdout.splitlines() if line]:
        fetch_r = _required_git(repo, "remote", "get-url", "--all", "--", name,
                                timeout=t_)
        push_r = _required_git(repo, "remote", "get-url", "--push", "--all", "--", name,
                               timeout=t_)
        fetch_urls = [line for line in fetch_r.stdout.splitlines() if line]
        push_urls = [line for line in push_r.stdout.splitlines() if line]
        if not fetch_urls or not push_urls:
            raise GitReadError("remote target is empty")
        result[name] = RemoteConfig(
            name, fetch_urls, push_urls,
            [canonical_remote_target(url, repo) for url in fetch_urls],
            [canonical_remote_target(url, repo) for url in push_urls],
        )
    return result


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


def remote_urls(repo: Path, cfg: dict) -> dict[str, list[str]]:
    """Compatibility helper: all effective URLs, while reads remain fail-closed."""
    return {name: list(dict.fromkeys(remote.fetch_urls + remote.push_urls))
            for name, remote in read_remote_configs(repo, cfg).items()}


def is_github_url(url: str) -> bool:
    """Only an exact github.com host receives the public-push classification."""
    return canonical_remote_target(url).host == "github.com"


def collect_remote_statuses(repo: Path, branch: str, sync_remote: str | None,
                            cfg: dict,
                            configs: dict[str, RemoteConfig] | None = None,
                            fetch_failed: set[str] | None = None) -> list[RemoteStatus]:
    """Alle Remotes samt Branch-Delta lesen; öffentliche Remotes immer zuletzt.

    `fetch_failed` sind die Namen der Remotes, deren Fetch gerade scheiterte; sie
    werden markiert, damit die Zeile sie rot zeigt statt einen veralteten Stand
    als aktuell auszugeben.
    """
    states: list[RemoteStatus] = []
    configs = configs if configs is not None else read_remote_configs(repo, cfg)
    failed = fetch_failed or set()
    for name, remote in configs.items():
        targets = remote.fetch_targets + remote.push_targets
        public_classes = {target.host == "github.com" for target in targets}
        state = RemoteStatus(
            name=name,
            public=True in public_classes,
            mixed_public=len(public_classes) > 1,
            is_sync=name == sync_remote,
            fetch_fingerprint=(remote.fetch_targets[0].fingerprint
                               if len(remote.fetch_targets) == 1 else ""),
            push_fingerprints=[target.fingerprint for target in remote.push_targets],
            target_mismatch=not remote.transfer_safe,
            multiple_pushurls=len(remote.push_targets) != 1,
            fetch_failed=name in failed,
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


def read_branches(repo: Path, cfg: dict) -> list[BranchInfo]:
    """Alle lokalen Branches mit Stand, Upstream und Merge-Zustand lesen.

    Zwei Git-Aufrufe reichen: einer für die Daten, einer für die Frage, welche
    Branches vollständig in HEAD stecken (nur solche darf `X` löschen).
    """
    t_ = cfg["git_timeout"]
    fields = ("%(HEAD)", "%(refname:short)", "%(upstream:short)", "%(upstream:track)",
              "%(objectname:short)", "%(committerdate:short)", "%(contents:subject)")
    r = run_git(repo, "branch", "--format=" + "%00".join(fields), timeout=t_)
    if r.returncode != 0:
        return []
    merged_r = run_git(repo, "branch", "--merged", "HEAD",
                       "--format=%(refname:short)", timeout=t_)
    merged = {line.strip() for line in merged_r.stdout.splitlines() if line.strip()}
    branches = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\0")
        if len(parts) < 7:
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


def file_diff(repo: Path, code: str, path: str, timeout: int) -> tuple[bool, str]:
    """Diff einer einzelnen Datei, ohne Index oder Arbeitsbaum anzufassen.

    Unversionierte Dateien kennt `git diff` nicht — sie werden über `--no-index`
    gegen /dev/null gezeigt, damit auch neue Dateien sichtbar sind.
    """
    if code == "U":
        r = run_git(repo, "diff", "--no-index", "--", os.devnull, path, timeout=timeout)
    elif run_git(repo, "rev-parse", "--verify", "-q", "HEAD",
                 timeout=timeout).returncode == 0:
        # Gegen HEAD, damit gestagte UND ungestagte Änderungen zusammen erscheinen.
        r = run_git(repo, "diff", "HEAD", "--", path, timeout=timeout)
    else:
        r = run_git(repo, "diff", "--cached", "--", path, timeout=timeout)
    # `git diff` meldet mit Unterschieden je nach Modus 0 oder 1 — beides ist Erfolg.
    if r.returncode in (0, 1):
        return True, r.stdout
    return False, (r.stderr or "").strip()[:240]


def display_remote_url(url: str) -> str:
    """Remote-Adresse für die lokale Anzeige, aber ohne eingebettete Secrets."""
    raw = url.strip()
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
        # Bei SSH ist der Benutzer (meist "git") Teil der hilfreichen Adresse.
        # Bei HTTP(S) kann genau dieses Feld dagegen ein Personal Access Token sein.
        user = ""
        if parsed.scheme.lower() not in ("http", "https") and parsed.username:
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
        if target.host != "github.com":
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
    # Beide sind auswählbar, weil beide lokal aufräumbar sind.
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

    Genau wie bei Remotes sammeln sich hier Reste an (abgeschlossene Features, alte
    Experimente), die niemand sieht, weil man immer nur den aktuellen Branch
    betrachtet. Deshalb stehen sie auf der Info-Seite und sind mit `X` löschbar.
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
                     timeout: int = 10) -> TransferCheck:
    """Prüft Push/Pull, ohne etwas zu verändern.

    Bewusst eng: sauberer Tree, vorhandener Remote-Branch und verwandte,
    fast-forward-fähige History. Neue Branches und Divergenzen gehören ins
    Terminal, wo der Mensch den Sonderfall ausdrücklich auflöst.
    """
    if branch in ("?", "(detached)"):
        return TransferCheck("detached")
    cfg = {**DEFAULT_CONFIG, "git_timeout": timeout}
    try:
        configs = read_remote_configs(repo, cfg)
        remote_cfg = configs.get(remote)
        if remote_cfg is None or not remote_cfg.transfer_safe:
            return TransferCheck("remote-unsafe")
        branch_r = _required_git(repo, "symbolic-ref", "--short", "-q", "HEAD",
                                 timeout=timeout)
        current_branch = branch_r.stdout.strip()
        if current_branch != branch:
            return TransferCheck("inspect-failed")
        head = _required_git(repo, "rev-parse", "--verify", "HEAD", timeout=timeout).stdout.strip()
        index_oid = _required_git(repo, "write-tree", timeout=timeout).stdout.strip()
        dirty = _required_git(repo, "status", "--porcelain=v1", "-z", timeout=timeout)
    except (GitReadError, ValueError):
        return TransferCheck("inspect-failed")
    worktree_fingerprint = hashlib.sha256(dirty.stdout.encode("utf-8")).hexdigest()
    if dirty.stdout:
        return TransferCheck("dirty")
    ref = f"refs/remotes/{remote}/{branch}"
    exists = run_git(repo, "show-ref", "--verify", "--quiet", ref, timeout=timeout)
    if exists.returncode == 1:
        return TransferCheck("missing-branch", remote_ref=ref)
    if exists.returncode != 0:
        return TransferCheck("inspect-failed", remote_ref=ref)
    try:
        target_oid = _required_git(repo, "rev-parse", "--verify", ref,
                                   timeout=timeout).stdout.strip()
    except GitReadError:
        return TransferCheck("inspect-failed", remote_ref=ref)
    delta = run_git(repo, "rev-list", "--left-right", "--count",
                    f"{head}...{target_oid}", timeout=timeout)
    if delta.returncode != 0 or len(delta.stdout.split()) != 2:
        return TransferCheck("inspect-failed", remote_ref=ref)
    ahead_s, behind_s = delta.stdout.split()
    ahead, behind = int(ahead_s), int(behind_s)
    if ahead and behind:
        return TransferCheck("divergent", ahead=ahead, behind=behind, remote_ref=ref)
    if action == "push":
        if behind:
            return TransferCheck("behind", ahead=ahead, behind=behind, remote_ref=ref)
        if not ahead:
            return TransferCheck("nothing-push", remote_ref=ref)
        commits_r = run_git(repo, "log", "--oneline", "--no-decorate",
                            f"{target_oid}..{head}", timeout=timeout)
        files_r = run_git(repo, "diff", "--name-status", f"{target_oid}..{head}",
                          timeout=timeout)
        if commits_r.returncode != 0 or files_r.returncode != 0:
            return TransferCheck("inspect-failed", ahead, behind, ref)
        return TransferCheck(
            "ready", ahead=ahead, behind=behind, remote_ref=ref,
            commits=[line for line in commits_r.stdout.splitlines() if line.strip()],
            files=[line for line in files_r.stdout.splitlines() if line.strip()],
            branch=current_branch, head_oid=head, index_oid=index_oid,
            worktree_fingerprint=worktree_fingerprint,
            fetch_fingerprint=remote_cfg.fetch_targets[0].fingerprint,
            push_fingerprint=remote_cfg.push_targets[0].fingerprint,
            target_oid=target_oid,
        )
    if action == "pull":
        if ahead:
            return TransferCheck("nothing-pull", ahead, behind, ref)
        if not behind:
            return TransferCheck("nothing-pull", remote_ref=ref)
        return TransferCheck(
            "ready", ahead=ahead, behind=behind, remote_ref=ref,
            branch=current_branch, head_oid=head, index_oid=index_oid,
            worktree_fingerprint=worktree_fingerprint,
            fetch_fingerprint=remote_cfg.fetch_targets[0].fingerprint,
            push_fingerprint=remote_cfg.push_targets[0].fingerprint,
            target_oid=target_oid,
        )
    raise ValueError(f"unknown transfer action: {action}")


def safe_push_args(remote: str, branch: str, source_oid: str,
                   target_oid: str) -> tuple[str, ...]:
    """Push approved OID only, leased to the approved target OID, without tags."""
    lease = f"--force-with-lease=refs/heads/{branch}:{target_oid}"
    return ("push", "--porcelain", "--no-follow-tags", lease, "--", remote,
            f"{source_oid}:refs/heads/{branch}")


def safe_pull_args(target_oid: str) -> tuple[str, ...]:
    """Pull ohne Fetch-Konfigurationsmagie: nur lokaler Fast-forward-Merge."""
    return ("merge", "--ff-only", "--", target_oid)


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

        # Arbeitsverzeichnis-Zustand
        r = _required_git(repo, "status", "--porcelain=v1", "-z", timeout=t_)
        st.modified, st.deleted, st.untracked, st.conflicts, st.files = parse_porcelain(
            r.stdout)

        # Stashes (leicht zu übersehen — deshalb deutlich anzeigen)
        r = _required_git(repo, "stash", "list", "--format=%gd %gs", timeout=t_)
        st.stashes = [l for l in r.stdout.splitlines() if l.strip()]

        # Vergleich mit ALLEN Remotes (auf Basis des letzten fetch-Stands).
        configs = read_remote_configs(repo, cfg)
        st.remote = detect_sync_remote(repo, cfg, configs)
        failed_remotes: set[str] = set()
        if fetch:
            # R aktualisiert nicht nur alle Repos, sondern je Repo auch alle Remotes.
            # Fetch verändert weder Branch noch Working Tree.
            try:
                fetched = run_git(repo, "fetch", "--all", "--prune", "--quiet",
                                  timeout=cfg["fetch_timeout"])
            except subprocess.TimeoutExpired:
                st.error = t("git_timeout")
                st.remote_state = "error"
                failed_remotes = set(configs)
            else:
                if fetched.returncode != 0:
                    # Ein gescheiterter Fetch ist ein Problem EINZELNER Remotes (Repo
                    # gelöscht, kein Netz, Login fehlt) — nicht des Repos. Deshalb die
                    # betroffenen Namen merken und rot markieren. Bei nur einem Remote
                    # nennt Git keinen Namen (es verhält sich dann wie ein einfaches
                    # `fetch`), deshalb der Fallback auf alle konfigurierten.
                    failed_remotes = set(failed_fetch_remotes(fetched)) or set(configs)
                    names = ", ".join(sorted(failed_remotes)) or "--all"
                    # Dieselbe Ursachenanalyse wie bei der T-Prüfung: die Zeile soll
                    # sagen, was zu tun ist, statt jeden Fehler "Login" zu nennen.
                    cause = classify_remote_check(fetched)
                    st.error = remote_failure_short(names, cause)
                    # Der ganze Satz und Gits eigener Wortlaut stehen auf der
                    # Info-Seite; sie beweisen die Ursache auch auf fremden Rechnern.
                    st.error_long = remote_check_message(
                        names, cause, 0, last_error_line(fetched),
                        cfg["fetch_timeout"])
                    st.error_detail = last_error_line(fetched)
                    st.remote_state = "error"
            # Auch nach einem Teilfehler sind vorhandene Remotes und ihre zuletzt
            # bekannten Tracking-Refs wertvoll. Ohne sie sähe ein Auth-Fehler wie
            # ein gelöschtes Remote aus und erzeugte irreführende DRIFT-Zeilen.
            configs = read_remote_configs(repo, cfg)
            st.remote = detect_sync_remote(repo, cfg, configs)
        st.remotes = collect_remote_statuses(repo, st.branch, st.remote, cfg, configs,
                                             fetch_failed=failed_remotes)
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


def carry_fetch_failure(old: RepoStatus, new: RepoStatus) -> None:
    """Einen bekannten Fetch-Fehler in den frisch gelesenen Zustand übernehmen.

    `collect_status` ohne `fetch` geht nur über lokale Daten und weiß deshalb
    nichts von einem toten Remote. Ohne diese Übernahme sähe ein Repo nach jeder
    Aktion (Commit, Stash, abgebrochener Dialog) plötzlich sauber aus, obwohl der
    letzte Fetch gescheitert ist — der Fehler verschwände scheinbar von selbst.
    Ein neuer Fetch überschreibt das ordnungsgemäß.
    """
    failed = {remote.name for remote in old.remotes if remote.fetch_failed}
    if not failed and not old.error:
        return
    for remote in new.remotes:
        if remote.name in failed:
            remote.fetch_failed = True
    if old.error and not new.error:
        new.error = old.error
        new.error_long = old.error_long
        new.error_detail = old.error_detail
        if new.remote_state == "ok":
            new.remote_state = "error"


def collect_all(root: Path, cfg: dict, fetch: bool = False,
                progress=None) -> list[RepoStatus]:
    """Alle Repos parallel einsammeln; optional Fortschritts-Callback (done, total)."""
    repos = find_repos(root, cfg["skip_dirs"])
    results: list[RepoStatus] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
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
        "path": str(st.path), "rel": st.rel, "branch": st.branch,
        "remote": st.remote, "remote_state": st.remote_state,
        "ahead": st.ahead, "behind": st.behind,
        "upstream": st.upstream,
        "upstream_ahead": st.upstream_ahead, "upstream_behind": st.upstream_behind,
        "remotes": [
            {"name": r.name, "public": r.public, "mixed_public": r.mixed_public,
             "sync": r.is_sync,
             "branch_exists": r.branch_exists, "ahead": r.ahead, "behind": r.behind,
             "fetch_fingerprint": r.fetch_fingerprint,
             "push_fingerprints": r.push_fingerprints,
             "target_mismatch": r.target_mismatch,
             "multiple_pushurls": r.multiple_pushurls,
             # Bewusst NICHT im --diff-Vergleich: dieser Zustand hängt am Netz des
             # jeweiligen Rechners, sonst meldete eine Offline-Seite lauter Drift.
             "fetch_failed": r.fetch_failed}
            for r in st.remotes
        ],
        "modified": st.modified, "deleted": st.deleted, "untracked": st.untracked,
        "conflicts": st.conflicts,
        "stashes": len(st.stashes), "clean_and_synced": st.clean_and_synced,
        "error": st.error,
        "error_long": st.error_long,
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


def fetch_remote_status(host: str, root: str, *, fetch: bool) -> dict:
    """Run this very script on `host` over ssh and return its --json output."""
    remote_args = ["python3", "-", "--json"]
    if fetch:
        remote_args.append("--fetch")
    remote_args.append(root)
    # ssh passes one remote command string to the remote shell. Quote every argv
    # element here; root remains exactly one argument even with spaces/metacharacters.
    inner = shlex.join(remote_args)
    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host, inner],
                stdin=fh, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(t("diff_ssh_failed", h=host, e=str(e)[:100]))
    # `--json` behält den normalen CLI-Exit-Code bei: 1 bedeutet, dass mindestens
    # ein Repo Aufmerksamkeit braucht. Das JSON ist trotzdem vollständig und muss
    # für den Rechnervergleich ausgewertet werden. Nur echte SSH-/Prozessfehler
    # (Exit-Codes außerhalb 0/1) machen die Gegenstelle unerreichbar.
    if r.returncode not in (0, 1):
        last = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
        raise RuntimeError(t("diff_ssh_failed", h=host,
                             e=(last[-1][:120] if last else
                                t("diff_ssh_exit", code=r.returncode))))
    if not (r.stdout or "").strip():
        last = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
        raise RuntimeError(t("diff_ssh_failed", h=host,
                             e=(last[-1][:120] if last else t("diff_ssh_no_output"))))
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(t("diff_ssh_failed", h=host, e=t("diff_ssh_bad_json")))


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
    tree). A report that lists everything gets ignored."""
    out = []
    if here.get("version") != there.get("version"):
        out.append(t("diff_version", a=here_name, va=here.get("version"),
                     b=there_name, vb=there.get("version")))
    here_at, there_at = _loc(here_name), _loc(there_name)
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
            security_fields = ("public", "mixed_public", "sync", "branch_exists",
                               "fetch_fingerprint", "push_fingerprints",
                               "target_mismatch", "multiple_pushurls")
            if tuple(pa.get(k) for k in security_fields) != tuple(
                    pb.get(k) for k in security_fields):
                out.append(t("diff_remote_security", rel=rel, r=rn))
            sa = (pa.get("ahead"), pa.get("behind"))
            sb = (pb.get("ahead"), pb.get("behind"))
            if sa != sb:
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
        for field_name in ("error", "conflicts", "stashes", "remote_state"):
            va, vb = x.get(field_name), y.get(field_name)
            if va != vb:
                out.append(t("diff_repo_field", rel=rel, field=field_name,
                             a=here_at, va=va, b=there_at, vb=vb))
        for name, r in ((here_at, x), (there_at, y)):
            n = (r.get("modified") or 0) + (r.get("untracked") or 0) + (r.get("deleted") or 0)
            if n:
                out.append(t("diff_dirty", rel=rel, m=name, n=n))
    return [terminal_text(line) for line in out]


def run_diff(spec: str, root: Path, cfg: dict, *, fetch: bool, as_json: bool) -> int:
    """--diff HOST[:PATH]: compare this machine with `HOST`. Read-only."""
    host, _, path = spec.partition(":")
    if not host:
        print(t("diff_need_host"), file=sys.stderr)
        return 2
    try:
        there = fetch_remote_status(host, _remote_root(root, path or None), fetch=fetch)
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
        else:
            out.append(ch)
    return "".join(out)


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

    def refresh_one(self, st: RepoStatus):
        """Nur ein Repo neu einlesen (nach commit/stash), Sortierung beibehalten."""
        new = collect_status(st.path, self.root, self.cfg)
        carry_fetch_failure(st, new)
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
                for code, path in st.files:
                    rows.append(("file", i, code, path))
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
            part(t("error_prefix", e=st.error), C_RED)
            return
        if st.clean_and_synced:
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
            # sind — der frei bleibende Platz darunter gehört dem Protokoll.
            rows, _, _ = self.compact_geometry(body_h, w)
            body_h = min(body_h, rows)
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
        rows, _, _ = self.compact_geometry(body_h, w)
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

    def compact_geometry(self, body_h: int, w: int) -> tuple[int, int, int]:
        """Zeilen, Spalten und Spaltenbreite der kompakten Ansicht."""
        cells = compact_cells(self.statuses)
        longest = max((COMPACT_MARK_WIDTH + 1 + cell_width(name)
                       for _, name, _ in cells), default=12)
        return compact_layout(len(cells), w, body_h, longest + 1)

    def draw_compact(self, top: int, body_h: int, w: int) -> None:
        """Kompakte Ansicht: Marke + Name, spaltenweise wie `ls`."""
        cells = compact_cells(self.statuses)
        rows, columns, column_width = self.compact_geometry(body_h, w)
        # Zeilen zuerst leeren: sonst bleiben rechts Reste des vorigen Bildes
        # stehen (die Ladeanzeige ist breiter als eine kurze Repo-Spalte).
        for row in range(rows):
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
        if total_columns > columns:
            # Ohne diesen Hinweis wirkt die Liste abgeschnitten statt scrollbar.
            safe_addstr(self.scr, top + body_h - 1, max(1, w - 22),
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

    def confirm(self, question: str) -> bool:
        h, w = self.scr.getmaxyx()
        safe_addstr(self.scr, h - 4, 1, (question + t("yesno")).ljust(w - 2),
                    curses.color_pair(C_YELLOW) | curses.A_BOLD)
        self.scr.refresh()
        while True:
            ch = self.scr.getch()
            if ch in (ord("j"), ord("J"), ord("y"), ord("Y")):
                return True
            if ch in (ord("n"), ord("N"), 27):
                return False

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
            print(f"\ncd {st.path}")
            print(t("cd_hint"))
        return True

    def action_stash_pop(self):
        st = self.current()
        if not st or not st.stashes:
            self.message = t("no_stash")
            return
        # Auf einen bereits konfliktbehafteten Baum lässt sich nicht poppen
        # (git: „konnte Index nicht schreiben"). Erst die Konflikte auflösen.
        if st.conflicts:
            self.message = t("resolve_conflicts_first")
            return
        if not self.confirm(t("confirm_pop", rel=st.rel)):
            self.message = t("cancelled")
            return
        r = run_git_logged(st.path, "stash", "pop", timeout=self.cfg["git_timeout"])
        new = self.refresh_one(st)
        if r.returncode == 0:
            self.message = t("stash_applied", rel=st.rel)
        elif new.conflicts:
            # Git hat den Stash mit Konfliktmarkern eingespielt und ihn ABSICHTLICH
            # behalten — nichts geht verloren. Konflikte müssen von Hand gelöst werden.
            self.message = t("stash_conflict", n=new.conflicts)
        else:
            self.message = t("stash_pop_failed", e=r.stderr.strip()[:120])

    def action_stash_show(self):
        """Neuesten Stash als Diff anzeigen (read-only), scrollbar. Für den
        Fall redundanter Alt-Stashes: erst schauen, dann entscheiden."""
        st = self.current()
        if not st or not st.stashes:
            self.message = t("no_stash")
            return
        ok, preview = stash_preview(st.path, self.cfg["git_timeout"])
        if not ok:
            text = t("stash_preview_failed", e=preview)
        else:
            text = preview or t("stash_preview_empty")
        title = t("stash_preview_title", rel=st.rel, s=st.stashes[0])
        self.show_pager(title, text.splitlines())

    def action_stash_drop(self):
        """Neuesten Stash endgültig verwerfen (destruktiv -> Rückfrage)."""
        st = self.current()
        if not st or not st.stashes:
            self.message = t("no_stash")
            return
        if not self.confirm(t("confirm_drop", rel=st.rel)):
            self.message = t("drop_cancelled")
            return
        r = run_git_logged(st.path, "stash", "drop", "stash@{0}",
                           timeout=self.cfg["git_timeout"])
        if r.returncode == 0:
            self.message = t("stash_dropped", rel=st.rel)
        else:
            self.message = t("stash_drop_failed", e=r.stderr.strip()[:120])
        self.refresh_one(st)

    def show_pager(self, title: str, lines: list[str]):
        """Einfacher scrollbarer Textbetrachter (↑/↓/PgUp/PgDn, q/Esc schließt)."""
        top = 0
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            safe_addstr(self.scr, 0, 0, (" " + title).ljust(w - 1), curses.A_BOLD)
            body_h = h - 2
            for y, line in enumerate(lines[top:top + body_h], start=1):
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
            b = min(len(lines), top + body_h)
            safe_addstr(self.scr, h - 1, 0,
                        t("pager_footer", a=a, b=b, n=len(lines)).ljust(w - 1),
                        curses.color_pair(C_DIM) | curses.A_REVERSE)
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (ord("q"), ord("Q"), 27):
                return
            elif ch == curses.KEY_UP:
                top = max(0, top - 1)
            elif ch == curses.KEY_DOWN:
                top = min(max(0, len(lines) - body_h), top + 1)
            elif ch == curses.KEY_NPAGE:
                top = min(max(0, len(lines) - body_h), top + body_h)
            elif ch == curses.KEY_PPAGE:
                top = max(0, top - body_h)

    # -- Sichere Push-/Pull-Aktionen ---------------------------------------

    @staticmethod
    def _remote(st: RepoStatus, name: str | None) -> RemoteStatus | None:
        return next((remote for remote in st.remotes if remote.name == name), None)

    def _fetch_remote(self, st: RepoStatus, remote: str) -> RepoStatus | None:
        r = run_git_logged(st.path, "fetch", "--prune", "--quiet", "--", remote,
                           timeout=self.cfg["fetch_timeout"])
        if r.returncode != 0:
            self.message = (t("transfer_auth_missing", r=remote)
                            if credentials_missing(r)
                            else t("transfer_fetch_failed", r=remote,
                                   code=r.returncode))
            return None
        return self.refresh_one(st)

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
        if check.reason == "nothing-pull":
            return t("nothing_to_pull", r=remote)
        return check.reason

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
        check = inspect_transfer(fresh.path, remote.name, fresh.branch, "push",
                                 self.cfg["git_timeout"])
        if not check.ready:
            self.message = self._transfer_message(check, remote.name, fresh.branch)
            return
        if not self.confirm(t("confirm_sync_push", n=check.ahead, r=remote.name)):
            self.message = t("cancelled")
            return
        newest = self._fetch_remote(fresh, remote.name)
        if not newest:
            return
        final = inspect_transfer(newest.path, remote.name, newest.branch, "push",
                                 self.cfg["git_timeout"])
        if not final.ready or final.approval_signature() != check.approval_signature():
            self.message = t("transfer_changed")
            return
        r = run_git_logged(newest.path, *safe_push_args(
                               remote.name, check.branch, check.head_oid,
                               check.target_oid),
                           timeout=self.cfg["fetch_timeout"])
        self.refresh_one(newest)
        if r.returncode == 0:
            self.message = t("sync_pushed", r=remote.name)
        elif credentials_missing(r):
            self.message = t("transfer_auth_missing", r=remote.name)
        else:
            self.message = t("push_failed", code=r.returncode)

    def action_sync_pull(self):
        """Einfacher Pull = Fetch + lokaler --ff-only-Merge vom privaten Sync."""
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
        check = inspect_transfer(fresh.path, remote.name, fresh.branch, "pull",
                                 self.cfg["git_timeout"])
        if not check.ready:
            self.message = self._transfer_message(check, remote.name, fresh.branch)
            return
        if not self.confirm(t("confirm_sync_pull", n=check.behind, r=remote.name)):
            self.message = t("cancelled")
            return
        newest = self._fetch_remote(fresh, remote.name)
        if not newest:
            return
        final = inspect_transfer(newest.path, remote.name, newest.branch, "pull",
                                 self.cfg["git_timeout"])
        if not final.ready or final.approval_signature() != check.approval_signature():
            self.message = t("transfer_changed")
            return
        r = run_git_logged(newest.path, *safe_pull_args(check.target_oid),
                           timeout=self.cfg["git_timeout"])
        self.refresh_one(newest)
        if r.returncode == 0:
            self.message = t("sync_pulled", r=remote.name)
        else:
            self.message = t("pull_failed", code=r.returncode)

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
        check = inspect_transfer(fresh.path, remote.name, fresh.branch, "push",
                                 self.cfg["git_timeout"])
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
            self.message = t("github_cancelled")
            return

        # Unmittelbar vor dem öffentlichen Push erneut fetchen. Ändert sich der
        # ausgehende Satz seit der Vorschau, wird nicht mit veralteter Freigabe gepusht.
        newest = self._fetch_remote(fresh, remote.name)
        if not newest:
            return
        final = inspect_transfer(newest.path, remote.name, newest.branch, "push",
                                 self.cfg["git_timeout"])
        if (not final.ready
                or final.approval_signature() != check.approval_signature()):
            self.message = t("github_changed")
            return
        r = run_git_logged(newest.path, *safe_push_args(
                               remote.name, check.branch, check.head_oid,
                               check.target_oid),
                           timeout=self.cfg["fetch_timeout"])
        self.refresh_one(newest)
        if r.returncode == 0:
            self.message = t("github_pushed", r=remote.name)
        elif credentials_missing(r):
            self.message = t("transfer_auth_missing", r=remote.name)
        else:
            self.message = t("push_failed", code=r.returncode)

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
        """Geänderte Dateien durchgehen und einzeln als Diff ansehen.

        Die Liste zeigt, WAS sich geändert hat — bisher stand dort nur, DASS sich
        etwas geändert hat. Rein lesend: `git diff` fasst weder Index noch Baum an.
        """
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
                code, path = st.files[index]
                pair = {"M": C_RED, "D": C_RED, "U": C_YELLOW, "C": C_RED}[code]
                label = t("conflict_label") if code == "C" else ""
                safe_addstr(self.scr, y, 1, f"{code}  {label}{path}",
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
                code, path = st.files[sel]
                ok, text = file_diff(st.path, code, path, self.cfg["git_timeout"])
                if not ok:
                    self.message = t("diff_failed", p=path, e=text)
                    return
                self.show_pager(t("diff_title", p=terminal_text(path)),
                                (text or t("diff_empty")).splitlines())

    # -- Repo-Info mit Remote- und Branch-Auswahl ---------------------------

    def action_repo_info(self):
        """Repo-Details; Remotes und Branches sind auswählbar (T prüfen, X entfernen)."""
        st = self.current()
        if not st:
            return
        fresh = self.refresh_one(st)
        view = build_info_view(fresh, self.cfg)
        selected = 0        # Index in view.blocks (Remotes und Branches)
        top = 0             # erste sichtbare Zeile
        note = ""           # Ergebnis der letzten Prüfung/Aktion
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            title = t("repo_info_title", rel=terminal_text(fresh.rel))
            safe_addstr(self.scr, 0, 0, (" " + title).ljust(w - 1), curses.A_BOLD)
            body_h = max(1, h - 4)
            selected = min(selected, max(0, len(view.blocks) - 1))
            block = view.blocks[selected] if view.blocks else None
            if block:
                # Der gewählte Block soll immer komplett sichtbar sein.
                _, _, first, last = block
                if first < top:
                    top = first
                if last >= top + body_h:
                    top = min(first, max(0, last - body_h + 1))
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
            elif ch in (ord("x"), ord("X")):
                if not block:
                    note = t("info_nothing_selected")
                    continue
                kind, name = block[0], block[1]
                removed = (self._remove_remote(fresh, name) if kind == "remote"
                           else self._delete_branch(fresh, name))
                if removed:
                    # Die Liste hat sich geändert: Ansicht neu aufbauen.
                    fresh = self.refresh_one(fresh)
                    view = build_info_view(fresh, self.cfg)
                    selected, top = 0, 0
                note = self.message

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

    def _remove_remote(self, st: RepoStatus, name: str) -> bool:
        """Remote nach ausführlicher Erklärung und Bestätigung aus der Config nehmen."""
        try:
            configs = read_remote_configs(st.path, self.cfg)
        except Exception as exc:
            self.message = t("info_remote_error", e=terminal_text(exc))
            return False
        remote = configs.get(name)
        if remote is None:
            self.message = t("info_no_remotes")
            return False
        command = format_git_command(("remote", "remove", name))
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        safe_addstr(self.scr, 0, 0, (" " + t("remove_title", r=name)).ljust(w - 1),
                    curses.A_BOLD)
        y = 2
        for url in remote.fetch_urls:
            safe_addstr(self.scr, y, 1,
                        f"{t('info_fetch_url')}: {display_remote_url(url)}")
            y += 1
        y += 1
        safe_addstr(self.scr, y, 1, t("remove_what_happens"), curses.A_BOLD)
        y += 1
        for key in ("remove_effect_config", "remove_effect_refs",
                    "remove_effect_upstream", "remove_effect_safe",
                    "remove_effect_server"):
            safe_addstr(self.scr, y, 3, t(key, r=name))
            y += 1
        y += 1
        if st.remote == name:
            safe_addstr(self.scr, y, 1, t("remove_sync_warning", r=name),
                        curses.color_pair(C_RED) | curses.A_BOLD)
            y += 2
        safe_addstr(self.scr, y, 1, t("remove_undo"), curses.color_pair(C_DIM))
        y += 1
        undo = format_git_command(("remote", "add", name, remote.fetch_urls[0]))
        safe_addstr(self.scr, y, 3, terminal_text(undo), curses.color_pair(C_DIM))
        y += 2
        safe_addstr(self.scr, y, 1, t("remove_command"), curses.A_BOLD)
        safe_addstr(self.scr, y, 1 + cell_width(t("remove_command")) + 1, command,
                    curses.color_pair(C_CYAN) | curses.A_BOLD)
        self.scr.refresh()
        if not self.confirm(t("remove_confirm", r=name)):
            log_cancelled(st.path, ("remote", "remove", name))
            self.message = t("remove_cancelled")
            return False
        r = run_git_logged(st.path, "remote", "remove", name,
                           timeout=self.cfg["git_timeout"])
        if r.returncode != 0:
            self.message = t("remove_failed", r=name, code=r.returncode)
            return False
        self.message = t("remove_done", r=name)
        return True

    def _delete_branch(self, st: RepoStatus, name: str) -> bool:
        """Lokalen Branch löschen — nur gemergte, und nur nach Erklärung."""
        branch = next((b for b in read_branches(st.path, self.cfg) if b.name == name),
                      None)
        if branch is None:
            self.message = t("info_nothing_selected")
            return False
        if branch.is_head:
            self.message = t("branch_is_current", b=name)
            return False
        if not branch.merged:
            # `git branch -d` würde das ohnehin verweigern. Lieber vorher ehrlich
            # sagen, warum — und wie es im Terminal bewusst doch geht.
            self.message = t("branch_not_merged", b=name)
            return False
        command = format_git_command(("branch", "-d", name))
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        safe_addstr(self.scr, 0, 0, (" " + t("branch_delete_title", b=name)).ljust(w - 1),
                    curses.A_BOLD)
        y = 2
        for line in aligned_rows([
                (t("info_branch_commit"),
                 f"{branch.oid} · {branch.date} · {terminal_text(branch.subject)}"),
                (t("info_upstream"), terminal_text(branch.upstream) or t("none_label")),
        ], indent=" "):
            safe_addstr(self.scr, y, 0, line)
            y += 1
        y += 1
        safe_addstr(self.scr, y, 1, t("remove_what_happens"), curses.A_BOLD)
        y += 1
        for key in ("branch_effect_pointer", "branch_effect_merged",
                    "branch_effect_remote", "branch_effect_safe"):
            safe_addstr(self.scr, y, 3, t(key, b=name))
            y += 1
        y += 2
        safe_addstr(self.scr, y, 1, t("remove_undo"), curses.color_pair(C_DIM))
        y += 1
        undo = format_git_command(("branch", name, branch.oid))
        safe_addstr(self.scr, y, 3, terminal_text(undo), curses.color_pair(C_DIM))
        y += 2
        safe_addstr(self.scr, y, 1, t("remove_command"), curses.A_BOLD)
        safe_addstr(self.scr, y, 1 + cell_width(t("remove_command")) + 1, command,
                    curses.color_pair(C_CYAN) | curses.A_BOLD)
        self.scr.refresh()
        if not self.confirm(t("branch_delete_confirm", b=name)):
            log_cancelled(st.path, ("branch", "-d", name))
            self.message = t("branch_delete_cancelled")
            return False
        r = run_git_logged(st.path, "branch", "-d", name,
                           timeout=self.cfg["git_timeout"])
        if r.returncode != 0:
            self.message = t("branch_delete_failed", b=name,
                             e=last_error_line(r)[:100])
            return False
        self.message = t("branch_deleted", b=name, oid=branch.oid)
        return True

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
        # Jede Datei bekommt einen Vorschlag: committen oder gitignoren.
        items = []
        for code, path in st.files:
            pattern = suggested_ignore(path)
            items.append({"code": code, "path": path,
                          "ignore": pattern is not None, "pattern": pattern,
                          "include": pattern is None})
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
                if it["ignore"]:
                    label, pair = t("to_gitignore", p=it["pattern"]), C_YELLOW
                elif it["include"]:
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
                items[sel]["include"] = not items[sel]["include"]
                if items[sel]["include"]:
                    items[sel]["ignore"] = False
            elif ch == ord("i"):
                it = items[sel]
                it["ignore"] = not it["ignore"]
                if it["ignore"]:
                    it["include"] = False
                    it["pattern"] = it["pattern"] or it["path"]
            elif ch in (10, 13, curses.KEY_ENTER):
                if self._commit_step2(st, items):
                    return
            elif ch == 27:
                self.message = t("commit_cancelled")
                return

    def _commit_step2(self, st: RepoStatus, items: list) -> bool:
        """Schritt 2: letzte Commit-Messages zeigen, Message erfragen, ausführen."""
        to_commit = [it["path"] for it in items if it["include"]]
        to_ignore = sorted({it["pattern"] for it in items if it["ignore"] and it["pattern"]})
        if not to_commit and not to_ignore:
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
        # Layout von unten her planen: die Eingabezeile muss sichtbar bleiben. Sonst
        # schiebt eine lange .gitignore- oder Message-Liste sie aus dem Bild und man
        # tippt blind.
        prompt_y = max(4, h - 2)
        y = 2
        if to_ignore:
            shown_ignore = to_ignore[:5]
            safe_addstr(self.scr, y, 1, t("new_in_gitignore"), curses.color_pair(C_YELLOW))
            y += 1
            for pat in shown_ignore:
                safe_addstr(self.scr, y, 3, pat, curses.color_pair(C_YELLOW))
                y += 1
            if len(to_ignore) > len(shown_ignore):
                safe_addstr(self.scr, y, 3,
                            t("more_entries", n=len(to_ignore) - len(shown_ignore)),
                            curses.color_pair(C_YELLOW))
                y += 1
            y += 1
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

        # Ausführen: .gitignore atomar ergänzen; exakt freigegebene Pfade über
        # einen temporären Index committen. Der echte Benutzer-Index bleibt erhalten.
        t_ = self.cfg["git_timeout"]
        commit_t = self.cfg["commit_timeout"]
        head_before = current_head(st.path, t_)
        try:
            ignore_changed = update_gitignore_atomic(st.path, to_ignore) if to_ignore else False
            approved = list(dict.fromkeys(to_commit + ([".gitignore"] if ignore_changed else [])))
            if not approved:
                self.message = t("nothing_selected")
                return True
            # Ab hier kann es dauern: `git commit` führt den pre-commit-Hook des
            # Repos aus, der oft Linter oder Tests startet.
            self.show_busy(t("commit_running", s=commit_t))
            r = commit_selected(st.path, approved, msg, t_, commit_t)
        except (CommitSafetyError, GitReadError, OSError) as e:
            self.message = t("commit_failed", e=str(e)[:120])
            return True
        except subprocess.TimeoutExpired:
            # Git und der von ihm gestartete Hook wurden beendet. Ob der Commit
            # vorher noch fertig wurde, weiß nur das Repo selbst — deshalb den
            # HEAD vergleichen, statt zu raten.
            done = current_head(st.path, t_) not in (None, head_before)
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
            return True
        new = self.refresh_one(st)
        self.message = t("committed_in", rel=st.rel)
        # Nach einem Commit denselben abgesicherten privaten Sync-Push anbieten wie P.
        # Ein öffentlicher `origin` kann dadurch nie über die alte Kurzstrecke rutschen.
        if new.remote and new.behind == 0 and new.ahead > 0:
            self.action_sync_push()
        return True

    # -- Hauptschleife -------------------------------------------------------

    def dispatch_action(self, key: str) -> None:
        """Einen Buchstabenbefehl ausführen.

        Bewusst von der Hauptschleife getrennt: So liegt jede Aktion, die Git
        aufruft, hinter genau einer Absicherung gegen Timeouts (siehe `run`).
        """
        if key == "C":
            self.action_commit_wizard()
        elif key == "U":
            self.action_stash_pop()
        elif key == "R":
            self.reload(fetch=True)
        elif key == "P":
            self.action_sync_push()
        elif key == "L":
            self.action_sync_pull()
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
        elif key == "D":
            self.action_stash_drop()
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
                self.message = timeout_message(exc)
                curses.flushinp()


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
# Rechner und in jedem Lauf dieselben — und damit auch die Befehle, die die Sandbox in
# das Protokoll schreibt (`git merge --ff-only -- <id>`). Ohne das wäre der Bild-Check
# `docs/make-screens.py --check` nicht zu gewinnen: jede Sekunde eine andere ID.
DEMO_DATE = "2026-01-02T10:00:00+00:00"


def _dgit(repo: Path, *args: str) -> None:
    """git-Aufruf in der Demo-Sandbox; wirft bei Fehler (Sandbox muss sauber bauen)."""
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True,
                   env={**os.environ, "GIT_AUTHOR_DATE": DEMO_DATE,
                        "GIT_COMMITTER_DATE": DEMO_DATE})


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

    # 3) mehrere untracked (inkl. typischem gitignore-Kandidat)
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
    subprocess.run(["git", "-C", str(repo), "stash", "pop"],  # erzeugt Konflikt, behält Stash
                   capture_output=True, text=True)

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
    subprocess.run(["git", "-C", str(repo), "stash", "pop"],
                   capture_output=True, text=True)

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
                         "only the differences (read-only). Needs `ssh HOST` to work; "
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
        cfg["apps"] = {k.upper(): v for k, v in cfg["apps"].items()}
        UI_LANG = resolve_lang(cfg, args.lang)
        sandbox = build_demo_sandbox(Path(tempfile.mkdtemp(prefix="gmf-demo-")))
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
