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
  U     apply the latest stash (git stash pop, with confirmation)
  S     view the latest stash as a diff (read-only, scrollable)
  D     drop the latest stash (git stash drop, with confirmation)
  R     reload everything incl. `git fetch` (shows progress)
  Q     quit

Non-interactive: with --list / --json (or no TTY) it prints the overview as text
or JSON (machine-readable). Exit code 1 if any repo needs attention.

Try it risk-free: `gitmaster_flash.py --demo` builds a throwaway sandbox of fake
repos in every state and opens the UI on it (also used for the README screenshots).

Config: ~/.config/gitmaster_flash/config.json (created on first run). Configurable:
app keys, the sync-remote match, scan exclusions, and UI language (en/de).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import curses
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.4.0"

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
    # Timeout in Sekunden für einzelne git-Aufrufe (fetch darf länger).
    "git_timeout": 10,
    "fetch_timeout": 30,
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
    # Footer
    "f1": {"en": " ↑/↓ select · → expand · ← collapse · ⏎ cd & quit",
           "de": " ↑/↓ wählen · → aufklappen · ← zuklappen · ⏎ cd & Exit"},
    "f2": {"en": " {apps} · C commit · U stash pop · R fetch all",
           "de": " {apps} · C Commit · U Stash pop · R fetch all"},
    "f3": {"en": " Q quit · S stash preview · D stash drop",
           "de": " Q Beenden · S Stash-Vorschau · D Stash verwerfen"},
    "yesno": {"en": "  (Y/N)", "de": "  (J/N)"},
    # Apps
    "app_not_found": {"en": "App not found: {p} (edit config.json)",
                      "de": "App nicht gefunden: {p} (config.json anpassen)"},
    "app_opened": {"en": "Opened {name}: {rel}", "de": "{name} geöffnet: {rel}"},
    "app_open_failed": {"en": "Failed to open {name}: {e}",
                        "de": "{name} öffnen fehlgeschlagen: {e}"},
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
    "to_commit_n": {"en": "To commit: {n} file(s)", "de": "Zu committen: {n} Datei(en)"},
    "recent_msgs": {"en": "Recent commit messages (style reference):",
                    "de": "Letzte Commit-Messages (Stil-Vorlage):"},
    "commit_msg_prompt": {"en": "Commit message: ", "de": "Commit-Message: "},
    "empty_msg": {"en": "Empty message — cancelled.", "de": "Leere Message — abgebrochen."},
    "git_add_failed": {"en": "git add failed: {e}", "de": "git add fehlgeschlagen: {e}"},
    "commit_failed": {"en": "Commit failed: {e}", "de": "Commit fehlgeschlagen: {e}"},
    "committed_in": {"en": "Committed in {rel}.", "de": "Committet in {rel}."},
    "confirm_push": {"en": "Push {n} commit(s) to {r} now?",
                     "de": "Jetzt {n} Commit(s) zu {r} pushen?"},
    "committed_pushed": {"en": "Committed & pushed ({r}).", "de": "Committet & gepusht ({r})."},
    "push_failed": {"en": "Push failed: {e}", "de": "Push fehlgeschlagen: {e}"},
    "nothing_to_commit": {"en": "Nothing to commit in this repo.",
                          "de": "Nichts zu committen in diesem Repo."},
    # main
    "not_a_dir": {"en": "Not a directory: {p}", "de": "Kein Ordner: {p}"},
    "git_timeout": {"en": "git timeout", "de": "git-Timeout"},
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
    modified: int = 0
    deleted: int = 0
    untracked: int = 0
    conflicts: int = 0            # ungemergte Dateien (Merge-Konflikt, z.B. nach stash pop)
    files: list = field(default_factory=list)   # [(Buchstabe M/D/U/C, Pfad), ...]
    stashes: list = field(default_factory=list)  # ["stash@{0} WIP ...", ...]
    error: str = ""

    @property
    def dirty(self) -> bool:
        return bool(self.modified or self.deleted or self.untracked or self.conflicts)

    @property
    def clean_and_synced(self) -> bool:
        return (not self.dirty and not self.stashes and self.ahead == 0
                and self.behind == 0 and self.remote_state == "ok")

    def upstream_badge(self) -> str:
        """Kurzhinweis zum fremden Upstream, z.B. '↑6 github' — oder '' wenn nichts
        offen ist. Rein informativ (blockiert den Sync-Status nicht)."""
        if not self.upstream or (not self.upstream_ahead and not self.upstream_behind):
            return ""
        remote = self.upstream.split("/", 1)[0]
        arrows = ""
        if self.upstream_ahead:
            arrows += f"↑{self.upstream_ahead}"
        if self.upstream_behind:
            arrows += f"↓{self.upstream_behind}"
        return f"{arrows} {remote}"

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


# Zwei-Buchstaben-Codes, die einen ungemergten Zustand (Merge-Konflikt) bedeuten.
# git status meldet solche Dateien z.B. nach einem `stash pop` mit Konflikt.
UNMERGED_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def parse_porcelain(lines: list[str]) -> tuple[int, int, int, int, list]:
    """`git status --porcelain` auswerten.

    -> (modified, deleted, untracked, conflicts, dateien).
    Vereinfachung fürs Auge: Konflikt = C, Untracked = U, Gelöschtes = D, jede
    andere Änderung (modified/added/renamed/…) = M. Konflikte werden ZUERST
    geprüft, sonst würde z.B. `UD` fälschlich als Löschung zählen.
    """
    m = d = u = c = 0
    files = []
    for line in lines:
        if not line.strip():
            continue
        xy, path = line[:2], line[3:]
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


def run_git(repo: Path, *args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
    )


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


def detect_sync_remote(repo: Path, cfg: dict) -> str | None:
    """Sync-Remote erkennen: bevorzugt per Name, sonst per Host in der URL."""
    r = run_git(repo, "remote", "-v", timeout=cfg["git_timeout"])
    if r.returncode != 0:
        return None
    remotes: dict[str, str] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes[parts[0]] = parts[1]
    for name in cfg["sync_remote_names"]:
        if name in remotes:
            return name
    for name, url in remotes.items():
        if any(host in url for host in cfg["sync_remote_hosts"]):
            return name
    return None


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
    st = RepoStatus(path=repo, rel=rel)
    t_ = cfg["git_timeout"]
    try:
        # Branch (oder detached HEAD)
        r = run_git(repo, "symbolic-ref", "--short", "-q", "HEAD", timeout=t_)
        if r.returncode == 0:
            st.branch = r.stdout.strip()
        else:
            st.branch = "(detached)"
            st.remote_state = "detached"

        # Arbeitsverzeichnis-Zustand
        r = run_git(repo, "status", "--porcelain", timeout=t_)
        st.modified, st.deleted, st.untracked, st.conflicts, st.files = parse_porcelain(
            r.stdout.splitlines())

        # Stashes (leicht zu übersehen — deshalb deutlich anzeigen)
        r = run_git(repo, "stash", "list", "--format=%gd %gs", timeout=t_)
        st.stashes = [l for l in r.stdout.splitlines() if l.strip()]

        # Vergleich mit dem Sync-Remote (auf Basis des letzten fetch-Stands)
        st.remote = detect_sync_remote(repo, cfg)
        if st.remote is None:
            st.remote_state = "no-remote"
            return st
        if fetch:
            run_git(repo, "fetch", st.remote, "--quiet", timeout=cfg["fetch_timeout"])
        if st.remote_state == "detached":
            return st
        ref = f"refs/remotes/{st.remote}/{st.branch}"
        r = run_git(repo, "rev-parse", "-q", "--verify", ref, timeout=t_)
        if r.returncode != 0:
            st.remote_state = "no-branch"
            return st
        r = run_git(repo, "rev-list", "--left-right", "--count",
                    f"HEAD...{ref}", timeout=t_)
        if r.returncode == 0:
            ahead, behind = r.stdout.split()
            st.ahead, st.behind = int(ahead), int(behind)

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
        "modified": st.modified, "deleted": st.deleted, "untracked": st.untracked,
        "conflicts": st.conflicts,
        "stashes": len(st.stashes), "clean_and_synced": st.clean_and_synced,
        "error": st.error,
    }


def print_list(statuses: list[RepoStatus]) -> None:
    green, red, yellow, cyan, reset = (
        "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m")
    for st in statuses:
        badge = st.upstream_badge()
        badge_txt = f"  {cyan}{badge}{reset}" if badge else ""
        if st.clean_and_synced:
            print(f"{green}✔ {st.rel}{reset}{badge_txt}")
            continue
        bits = []
        if st.error:
            bits.append(f"{red}{t('error_prefix', e=st.error)}{reset}")
        if st.ahead:
            bits.append(f"{yellow}↑{st.ahead}{reset}")
        if st.behind:
            bits.append(f"{red}↓{st.behind}{reset}")
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
        if badge:
            bits.append(f"{cyan}{badge}{reset}")
        print(f"{red}✘{reset} {st.rel}  {' '.join(bits)}")


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------

# Farb-Paar-Nummern
C_GREEN, C_RED, C_YELLOW, C_DIM, C_SEL, C_CYAN = 1, 2, 3, 4, 5, 6


def safe_addstr(win, y, x, text, attr=0):
    """addstr, das am Bildschirmrand nicht crasht."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    try:
        win.addstr(y, x, text[: w - x - 1], attr)
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

    # -- Datenbeschaffung ---------------------------------------------------

    def reload(self, fetch: bool = False):
        label = t("fetching") if fetch else t("reading")

        def progress(done, total):
            self.scr.erase()
            safe_addstr(self.scr, 1, 2, f"{label} … {done}/{total}",
                        curses.color_pair(C_YELLOW))
            self.scr.refresh()

        progress(0, 0)
        self.statuses = collect_all(self.root, self.cfg, fetch, progress)
        self.selected = min(self.selected, max(0, len(self.statuses) - 1))

    def refresh_one(self, st: RepoStatus):
        """Nur ein Repo neu einlesen (nach commit/stash), Sortierung beibehalten."""
        new = collect_status(st.path, self.root, self.cfg)
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
        x += len(name) + 2

        def part(text, pair):
            nonlocal x
            safe_addstr(self.scr, y, x, text, sel | curses.color_pair(pair))
            x += len(text) + 1

        if st.error:
            part(t("error_prefix", e=st.error), C_RED)
            return
        if st.clean_and_synced:
            part(t("clean_synced"), C_GREEN)
        else:
            if st.ahead:
                part(f"↑{st.ahead}", C_YELLOW)
            if st.behind:
                part(f"↓{st.behind}", C_RED)
            if st.ahead == 0 and st.behind == 0 and st.remote_state == "ok":
                part("=", C_GREEN)
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
        # Fremder Upstream (z.B. github): cyan, auch neben einem grünen ✔ sichtbar.
        badge = st.upstream_badge()
        if badge:
            part(badge, C_CYAN)
        part(f"[{st.branch}]", C_DIM)

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        dirty = sum(1 for s in self.statuses if not s.clean_and_synced)
        tail = t("hdr_review", n=dirty) if dirty else t("hdr_clean")
        head = (f" gitmaster_flash · {self.root} · "
                f"{len(self.statuses)} {t('hdr_repos')} · {tail}")
        safe_addstr(self.scr, 0, 0, head.ljust(w - 1), curses.A_BOLD)

        rows = self.build_rows()
        # Zeile des ausgewählten Repos finden, damit sie sichtbar bleibt
        sel_row = next((i for i, r in enumerate(rows)
                        if r[0] == "repo" and r[1] == self.selected), 0)
        body_h = h - 5  # Kopf + Feedback + 3 Footerzeilen
        if sel_row < self.offset:
            self.offset = sel_row
        if sel_row >= self.offset + body_h:
            self.offset = sel_row - body_h + 1

        y = 1
        for row in rows[self.offset:self.offset + body_h]:
            kind = row[0]
            if kind == "repo":
                self.draw_repo_line(y, self.statuses[row[1]], row[1] == self.selected)
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

    def prompt_line(self, y: int, prompt: str) -> str | None:
        """Einzeilige Texteingabe; Esc bricht ab, ⏎ bestätigt."""
        buf: list[str] = []
        while True:
            h, w = self.scr.getmaxyx()
            safe_addstr(self.scr, y, 1, (prompt + "".join(buf)).ljust(w - 2),
                        curses.A_BOLD)
            self.scr.move(min(y, h - 1), min(1 + len(prompt) + len(buf), w - 2))
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
        r = run_git(st.path, "stash", "pop", timeout=self.cfg["git_timeout"])
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
        r = run_git(st.path, "stash", "show", "-p", "stash@{0}",
                    timeout=self.cfg["git_timeout"])
        text = r.stdout if r.returncode == 0 else (r.stderr or t("empty_diff"))
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
        r = run_git(st.path, "stash", "drop", "stash@{0}",
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

    # -- Commit-Hilfe --------------------------------------------------------

    def action_commit_wizard(self):
        st = self.current()
        if not st:
            return
        if not st.files:
            self.message = t("nothing_to_commit")
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
                mark = curses.A_REVERSE if i == sel else 0
                if it["ignore"]:
                    label, pair = t("to_gitignore", p=it["pattern"]), C_YELLOW
                elif it["include"]:
                    label, pair = t("do_commit"), C_GREEN
                else:
                    label, pair = t("do_skip"), C_DIM
                safe_addstr(self.scr, y, 1,
                            f"{it['code']}  {it['path']:<{max(10, w - 40)}.{w - 40}} {label}",
                            mark | curses.color_pair(pair))
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
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        safe_addstr(self.scr, 0, 0, (" " + t("commit_in", rel=st.rel)).ljust(w - 1),
                    curses.A_BOLD)
        y = 2
        if to_ignore:
            safe_addstr(self.scr, y, 1, t("new_in_gitignore"), curses.color_pair(C_YELLOW))
            y += 1
            for pat in to_ignore:
                safe_addstr(self.scr, y, 3, pat, curses.color_pair(C_YELLOW))
                y += 1
            y += 1
        safe_addstr(self.scr, y, 1, t("to_commit_n", n=len(to_commit)),
                    curses.color_pair(C_GREEN))
        y += 2
        # Stil-Vorlage: die letzten Commit-Messages des Repos
        r = run_git(st.path, "log", "-5", "--format=%s", timeout=self.cfg["git_timeout"])
        recent = [l for l in r.stdout.splitlines() if l.strip()]
        if recent:
            safe_addstr(self.scr, y, 1, t("recent_msgs"), curses.color_pair(C_DIM))
            y += 1
            for msg in recent:
                safe_addstr(self.scr, y, 3, f"· {msg}", curses.color_pair(C_DIM))
                y += 1
        y += 1
        msg = self.prompt_line(y, t("commit_msg_prompt"))
        if msg is None:
            return False  # Esc -> zurück zur Dateiauswahl
        if not msg:
            self.message = t("empty_msg")
            return True

        # Ausführen: .gitignore ergänzen (ohne Duplikate), stagen, committen.
        t_ = self.cfg["git_timeout"]
        if to_ignore:
            gi = st.path / ".gitignore"
            existing = set(gi.read_text().splitlines()) if gi.exists() else set()
            new_lines = [p for p in to_ignore if p not in existing]
            if new_lines:
                with gi.open("a") as f:
                    if existing and not gi.read_text().endswith("\n"):
                        f.write("\n")
                    f.write("\n".join(new_lines) + "\n")
            run_git(st.path, "add", "--", ".gitignore", timeout=t_)
        if to_commit:
            r = run_git(st.path, "add", "--", *to_commit, timeout=t_)
            if r.returncode != 0:
                self.message = t("git_add_failed", e=r.stderr.strip()[:120])
                return True
        r = run_git(st.path, "commit", "-m", msg, timeout=t_)
        if r.returncode != 0:
            self.message = t("commit_failed", e=r.stderr.strip()[:120])
            return True
        new = self.refresh_one(st)
        self.message = t("committed_in", rel=st.rel)
        # Push nur anbieten, wenn ein Sync-Remote da ist und wir nicht hinterher sind.
        if new.remote and new.behind == 0 and new.ahead > 0:
            self.draw()
            if self.confirm(t("confirm_push", n=new.ahead, r=new.remote)):
                r = run_git(st.path, "push", new.remote, timeout=self.cfg["fetch_timeout"])
                if r.returncode == 0:
                    self.message = t("committed_pushed", r=new.remote)
                else:
                    self.message = t("push_failed", e=r.stderr.strip()[:120])
                self.refresh_one(new)
        return True

    # -- Hauptschleife -------------------------------------------------------

    def run(self):
        curses.curs_set(0)
        self.reload()
        while True:
            self.draw()
            ch = self.scr.getch()
            self.message = ""
            st = self.current()
            # Cursor-/Sondertasten zuerst; Buchstaben danach case-insensitiv.
            if ch == curses.KEY_UP:
                self.selected = max(0, self.selected - 1)
                continue
            elif ch == curses.KEY_DOWN:
                self.selected = min(len(self.statuses) - 1, self.selected + 1)
                continue
            elif ch == curses.KEY_RIGHT and st:
                self.expanded.add(st.rel)
                continue
            elif ch == curses.KEY_LEFT and st:
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
            elif key == "C":
                self.action_commit_wizard()
            elif key == "U":
                self.action_stash_pop()
            elif key == "R":
                self.reload(fetch=True)
            elif key == "S":
                self.action_stash_show()
            elif key == "D":
                self.action_stash_drop()
            elif key in self.cfg["apps"]:
                self.action_open_app(key)


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(C_RED, curses.COLOR_RED, -1)
    curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
    curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)


# ---------------------------------------------------------------------------
# Demo-Sandbox (für `--demo`, Screenshots und risikofreies Ausprobieren)
# ---------------------------------------------------------------------------

def _dgit(repo: Path, *args: str) -> None:
    """git-Aufruf in der Demo-Sandbox; wirft bei Fehler (Sandbox muss sauber bauen)."""
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _demo_repo(root: Path, name: str) -> tuple[Path, Path]:
    """Neues Repo mit eigenem bare-"origin"-Remote + Erst-Commit, gepusht, upstream=origin/main."""
    bare = root / "_remotes" / f"{name}.git"
    bare.mkdir(parents=True)
    _dgit(bare, "init", "-q", "--bare")
    repo = root / name
    repo.mkdir()
    _dgit(repo, "init", "-q", "-b", "main")
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
    _dgit(repo, "push", "-q", "-u", "origin", "main")
    return repo, bare


def _demo_commit(repo: Path, fname: str, content: str, msg: str) -> None:
    (repo / fname).write_text(content)
    _dgit(repo, "add", fname)
    _dgit(repo, "commit", "-qm", msg)


def build_demo_sandbox(base: Path) -> Path:
    """Wegwerf-Sandbox mit Fake-Repos in allen Zuständen (Screenshots/Ausprobieren).

    Nutzt lokale bare-Repos als Remotes (kein Netz). Deckt ab: sauber & synchron,
    modified/untracked, ahead/behind Sync-Remote, Merge-Konflikt + Stash, nur Stash,
    kein Sync-Remote und ein 'fremder Upstream'-Hinweis (↑n github).
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
        root = Path(args.root).resolve()
        if not root.is_dir():
            print(t("not_a_dir", p=root), file=sys.stderr)
            return 1

    if args.list or args.json or not sys.stdout.isatty():
        statuses = collect_all(root, cfg, fetch=args.fetch)
        if args.json:
            print(json.dumps([status_dict(s) for s in statuses],
                             indent=2, ensure_ascii=False))
        else:
            print_list(statuses)
        # Exit-Code 1, wenn irgendein Repo Aufmerksamkeit braucht (skriptbar).
        return 0 if all(s.clean_and_synced for s in statuses) else 1

    def _run(stdscr):
        init_colors()
        TUI(stdscr, root, cfg, args.cd_file).run()

    curses.wrapper(_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
