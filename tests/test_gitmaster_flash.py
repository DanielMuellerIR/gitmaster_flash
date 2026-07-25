"""Headless-Tests für die reine Logik (Parsing, Ignore-Heuristik, Repo-Scan).

Die TUI selbst wird nicht getestet — die Datensammlung dafür schon:
gegen ein echtes, temporär angelegtes Git-Repo.
"""

import json
import importlib.util
import curses
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gitmaster_flash as gmf_module  # noqa: E402

from gitmaster_flash import (  # noqa: E402
    DEFAULT_CONFIG, __version__, CommitSafetyError, RemoteStatus, RepoStatus, TUI,
    build_info_view, check_remote, classify_remote_check, collect_status, find_repos,
    canonical_remote_target, cell_width, commit_selected, diff_status, _remote_root,
    detect_sync_remote, display_remote_url, fetch_remote_status, file_diff,
    inspect_transfer, is_github_url, pad_cells, parse_porcelain, read_branches,
    remote_check_message, repo_info_lines, safe_pull_args, stash_preview, status_dict,
    safe_push_args, suggested_ignore, terminal_text, truncate_cells,
    update_gitignore_atomic, upstream_delta,
)

# Die Defaults erkennen nur "origin" als Sync-Remote. Tests, die einen anders
# benannten Sync-Remote brauchen, nehmen diese Kopie.
SYNC_CONFIG = {**DEFAULT_CONFIG, "sync_remote_names": ["backup"]}


class TestParsePorcelain(unittest.TestCase):
    def test_counts_and_letters(self):
        output = "\0".join([
            " M geändert.py",
            "M  gestaged.py",
            " D geloescht.txt",
            "?? neu.md",
            "?? unterordner/noch-neu.md",
            "A  hinzugefuegt.py",
            "",
        ])
        m, d, u, c, files = parse_porcelain(output)
        self.assertEqual((m, d, u, c), (3, 1, 2, 0))
        self.assertIn(("U", "neu.md"), files)
        self.assertIn(("D", "geloescht.txt"), files)
        self.assertIn(("M", "gestaged.py"), files)

    def test_conflicts_detected_first(self):
        # UU/UD/AA sind Merge-Konflikte und dürfen NICHT als M oder D zählen.
        output = "UU beide.txt\0UD ich-geloescht.txt\0AA beide-neu.txt\0"
        m, d, u, c, files = parse_porcelain(output)
        self.assertEqual((m, d, u, c), (0, 0, 0, 3))
        self.assertIn(("C", "beide.txt"), files)
        self.assertIn(("C", "ich-geloescht.txt"), files)

    def test_unicode_and_control_characters_remain_literal(self):
        output = "?? übungen/Abendsession 2026-07-21.pdf\0?? zeile\numbruch.txt\0"
        _, _, untracked, _, files = parse_porcelain(output)
        self.assertEqual(untracked, 2)
        self.assertIn(("U", "übungen/Abendsession 2026-07-21.pdf"), files)
        self.assertIn(("U", "zeile\numbruch.txt"), files)

    def test_rename_uses_destination_and_consumes_source(self):
        output = "R  neu ü.txt\0alt ü.txt\0 M danach.txt\0"
        modified, _, _, _, files = parse_porcelain(output)
        self.assertEqual(modified, 2)
        self.assertEqual(files, [("M", "neu ü.txt"), ("M", "danach.txt")])

    def test_empty(self):
        self.assertEqual(parse_porcelain(""), (0, 0, 0, 0, []))


class TestSuggestedIgnore(unittest.TestCase):
    def test_typical_junk(self):
        self.assertEqual(suggested_ignore(".DS_Store"), ".DS_Store")
        self.assertEqual(suggested_ignore("sub/.DS_Store"), ".DS_Store")
        self.assertEqual(suggested_ignore("node_modules/foo/bar.js"), "node_modules/")
        self.assertEqual(suggested_ignore("app/__pycache__/x.pyc"), "__pycache__/")
        self.assertEqual(suggested_ignore("debug.log"), "*.log")
        self.assertEqual(suggested_ignore(".env"), ".env")

    def test_real_files_not_ignored(self):
        self.assertIsNone(suggested_ignore("README.md"))
        self.assertIsNone(suggested_ignore("src/main.py"))
        self.assertIsNone(suggested_ignore("notes/env-setup.md"))


class TestRemoteBadges(unittest.TestCase):
    def test_synced_remote_is_still_named(self):
        remote = RemoteStatus("minipc", is_sync=True, branch_exists=True)
        self.assertEqual(remote.badge(), "minipc")

    def test_delta_and_missing_branch(self):
        self.assertEqual(RemoteStatus("github", public=True, branch_exists=True,
                                      ahead=2, behind=3).badge(), "↑2↓3 github")
        self.assertEqual(RemoteStatus("archive").badge(), "? archive")

    def test_github_url_detection_is_name_independent(self):
        self.assertTrue(is_github_url("git@github.com:example/demo.git"))
        self.assertTrue(is_github_url("https://github.com/example/demo.git"))
        self.assertFalse(is_github_url("ssh://internal.example/demo.git"))


class TestRemoteUrlDisplay(unittest.TestCase):
    def test_query_and_fragment_are_not_shown(self):
        separator = chr(58) + chr(47) * 2
        base = "https" + separator + "github.com/example/demo.git"
        shown = display_remote_url(base + "?redacted=true#private")
        self.assertEqual(shown, base)
        self.assertNotIn("redacted", shown)

    def test_ssh_user_is_preserved(self):
        separator = chr(58) + chr(47) * 2
        address = "ssh" + separator + "git@github.com/example/demo.git"
        self.assertEqual(display_remote_url(address), address)

    def test_local_file_url_remains_visible(self):
        separator = chr(58) + chr(47) * 2
        address = "file" + separator + "/tmp/example.git"
        self.assertEqual(display_remote_url(address), address)


class TestSeveritySort(unittest.TestCase):
    def test_dirty_before_clean(self):
        dirty = RepoStatus(path=Path("/x"), rel="x", modified=1)
        clean = RepoStatus(path=Path("/y"), rel="y")
        self.assertLess(dirty.severity(), clean.severity())

    def test_clean_and_synced_property(self):
        st = RepoStatus(path=Path("/x"), rel="x")
        self.assertTrue(st.clean_and_synced)
        st.stashes = ["stash@{0} WIP"]
        self.assertFalse(st.clean_and_synced)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


class TestAgainstRealRepo(unittest.TestCase):
    """Integration: temporäres Repo anlegen und den Status-Sammler prüfen."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "demo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "a.md").write_text("hallo\n")
        git(self.repo, "add", "a.md")
        git(self.repo, "commit", "-qm", "erster Commit")

    def tearDown(self):
        self.tmp.cleanup()

    def test_find_repos(self):
        (self.root / "kein-repo").mkdir()
        repos = find_repos(self.root, DEFAULT_CONFIG["skip_dirs"])
        self.assertEqual(repos, [self.repo])

    def test_status_dirty_untracked_stash(self):
        (self.repo / "a.md").write_text("geändert\n")
        (self.repo / "neu.txt").write_text("neu\n")
        git(self.repo, "stash", "--include-untracked")
        (self.repo / "wieder.txt").write_text("x\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.rel, "demo")
        self.assertEqual(st.untracked, 1)          # wieder.txt
        self.assertEqual(len(st.stashes), 1)       # der Stash von oben
        self.assertEqual(st.remote_state, "no-remote")
        self.assertFalse(st.clean_and_synced)

    def test_unicode_path_from_status_can_be_staged(self):
        # Der reale Fehlerfall: Git maskierte den Umlaut ohne -z als
        # "\\303\\274bungen/..."; GMF reichte diese Anzeige an git add weiter.
        dirname = "u\u0308bungen"
        filename = "Abendsession2026-07-21-aufgaben.pdf"
        (self.repo / dirname).mkdir()
        (self.repo / dirname / "bestehend.txt").write_text("schon getrackt\n")
        git(self.repo, "add", "--", f"{dirname}/bestehend.txt")
        git(self.repo, "commit", "-qm", "Übungsordner anlegen")
        (self.repo / dirname / filename).write_bytes(b"test")

        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.untracked, 1)
        path = st.files[0][1]
        self.assertEqual(unicodedata.normalize("NFC", path), f"übungen/{filename}")

        # Derselbe unveränderte Wert, den der Commit-Assistent nutzt, muss ein
        # gültiger Pathspec sein. Das testet Gits echte macOS-Normalisierung mit.
        git(self.repo, "add", "--", path)
        tracked = subprocess.run(
            ["git", "-C", str(self.repo), "ls-files", "--error-unmatch", "--", path],
            capture_output=True, text=True,
        )
        self.assertEqual(tracked.returncode, 0, tracked.stderr)

    def test_stash_pop_conflict_keeps_stash(self):
        # Ein Stash, dessen Änderungen mit dem inzwischen committeten Stand
        # kollidieren -> pop erzeugt einen Konflikt,
        # der Stash bleibt erhalten. Das Tool muss das als Konflikt (C) erkennen,
        # nicht als simple Modifikation (M).
        (self.repo / "a.md").write_text("stash-variante\n")
        git(self.repo, "stash")                              # Stash mit Änderung an a.md
        (self.repo / "a.md").write_text("andere-variante\n")  # kollidierende Änderung
        git(self.repo, "commit", "-qam", "kollidierender Commit")
        # pop schlägt fehl (Konflikt); Rückgabecode != 0, Stash bleibt
        res = subprocess.run(["git", "-C", str(self.repo), "stash", "pop"],
                             capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.conflicts, 1)
        self.assertEqual(st.modified, 0)
        self.assertEqual(len(st.stashes), 1)   # Stash NICHT verloren
        self.assertTrue(st.dirty)

    def test_status_clean_without_remote(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertFalse(st.dirty)
        # sauber, aber ohne Sync-Remote -> nicht "synchron"
        self.assertFalse(st.clean_and_synced)
        self.assertEqual(st.branch, "main" if st.branch == "main" else st.branch)

    def test_repo_info_contains_clickable_github_url_and_useful_details(self):
        git(self.repo, "tag", "v1.0")
        git(self.repo, "remote", "add", "origin",
            "https://github.com/example/demo.git")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        lines = repo_info_lines(st, DEFAULT_CONFIG)
        text = "\n".join(lines)

        self.assertRegex(text, rf"Path: +{re.escape(str(self.repo))}")
        self.assertRegex(text, rf"Branch: +{st.branch}")
        self.assertIn("HEAD:", text)
        self.assertIn("Last commit:", text)
        self.assertRegex(text, r"Working tree: +clean")
        self.assertRegex(text, r"Tags at HEAD: +v1\.0")
        self.assertIn("origin [sync, GitHub]", text)
        # Identische Fetch- und Push-Adresse steht in einer Zeile, nicht zweimal.
        self.assertRegex(text, r"fetch\+push: +https://github\.com/example/demo\.git")
        self.assertNotIn("\n    push:", text)
        self.assertRegex(text, r"web: +https://github\.com/example/demo")
        # Die Commit-Betreffzeile steht direkt unter "Last commit".
        subject_index = next(i for i, line in enumerate(lines) if "erster Commit" in line)
        self.assertIn("Last commit:", lines[subject_index - 1])

    def test_info_values_start_in_the_same_column(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/example/demo.git")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        lines = repo_info_lines(st, DEFAULT_CONFIG)

        def value_columns(candidates):
            """Spalte, in der der Wert beginnt — für jede Label/Wert-Zeile."""
            columns = set()
            for line in candidates:
                label, _, value = line.partition(":")
                if value.strip():
                    columns.add(len(line) - len(value.lstrip()))
            return columns

        head = [line for line in lines if not line.startswith(" ")]
        details = [line for line in lines if line.startswith("    ")]
        self.assertEqual(len(value_columns(head)), 1, head)
        self.assertEqual(len(value_columns(details)), 1, details)


class TestUpstreamDeltaTwoRemotes(unittest.TestCase):
    """Zwei Remotes: Der Branch trackt einen NICHT-Sync-Remote (github) und ist
    ihm voraus, ist aber mit dem Sync-Remote (backup) synchron. Genau dieser Fall
    soll sauber gelten und trotzdem den Zusatz-Badge zeigen."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Zwei bare-Repos als Remotes.
        self.gh = self.root / "github.git"
        self.bk = self.root / "backup.git"
        for bare in (self.gh, self.bk):
            bare.mkdir()
            git(bare, "init", "-q", "--bare")
        self.repo = self.root / "demo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        git(self.repo, "remote", "add", "github", str(self.gh))
        git(self.repo, "remote", "add", "backup", str(self.bk))
        (self.repo / "a.md").write_text("1\n")
        git(self.repo, "add", "a.md")
        git(self.repo, "commit", "-qm", "c1")
        git(self.repo, "push", "-q", "github", "main")
        git(self.repo, "push", "-q", "backup", "main")
        # main trackt github/main, nicht den Sync-Remote.
        git(self.repo, "branch", "--set-upstream-to=github/main", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ahead_of_upstream_but_synced_with_sync_remote(self):
        # Zwei weitere Commits, nur zum Sync-Remote gepusht -> 2 voraus ggü. github.
        for n in (2, 3):
            (self.repo / "a.md").write_text(f"{n}\n")
            git(self.repo, "commit", "-qam", f"c{n}")
        git(self.repo, "push", "-q", "backup", "main")

        up, ahead, behind = upstream_delta(self.repo, "backup", SYNC_CONFIG)
        self.assertEqual(up, "github/main")
        self.assertEqual((ahead, behind), (2, 0))

        st = collect_status(self.repo, self.root, SYNC_CONFIG)
        self.assertEqual(st.remote, "backup")
        self.assertEqual((st.ahead, st.behind), (0, 0))     # mit backup synchron
        self.assertEqual(st.upstream, "github/main")
        self.assertEqual(st.upstream_ahead, 2)
        self.assertTrue(st.clean_and_synced)                # gilt trotzdem als sauber

        # Alle Remotes erscheinen, auch der synchrone Backup-Remote. Nach URL-
        # Klassifikation steht GitHub unabhängig vom Namen ganz rechts.
        git(self.repo, "remote", "set-url", "github",
            "https://github.com/example/demo.git")
        st = collect_status(self.repo, self.root, SYNC_CONFIG)
        self.assertEqual([r.name for r in st.remotes], ["backup", "github"])
        self.assertEqual([r.badge() for r in st.remotes], ["backup", "↑2 github"])
        self.assertFalse(st.remotes[0].public)
        self.assertTrue(st.remotes[-1].public)

    def test_mixed_fetch_and_push_url_is_visible_and_blockable(self):
        git(self.repo, "remote", "set-url", "github",
            "https://github.com/example/demo.git")
        git(self.repo, "remote", "set-url", "--push", "github", str(self.gh))
        st = collect_status(self.repo, self.root, SYNC_CONFIG)
        public = next(r for r in st.remotes if r.name == "github")
        self.assertTrue(public.public)
        self.assertTrue(public.mixed_public)

    def test_no_badge_when_upstream_is_sync_remote(self):
        # Trackt der Branch den Sync-Remote selbst, gibt es keinen Zusatz-Badge.
        git(self.repo, "branch", "--set-upstream-to=backup/main", "main")
        up, ahead, behind = upstream_delta(self.repo, "backup", SYNC_CONFIG)
        self.assertIsNone(up)

    def test_push_preflight_and_explicit_safe_refspec(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        self.assertEqual(check.ahead, 1)
        self.assertEqual(check.commits[0].split(" ", 1)[1], "c2")
        self.assertEqual(check.files, ["M\ta.md"])
        self.assertEqual(check.branch, "main")
        self.assertEqual(check.head_oid,
                         subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                        check=True, capture_output=True, text=True).stdout.strip())
        self.assertTrue(check.index_oid)
        self.assertTrue(check.worktree_fingerprint)
        self.assertEqual(check.fetch_fingerprint, check.push_fingerprint)
        self.assertTrue(check.target_oid)

        args = safe_push_args("github", "main", check.head_oid, check.target_oid)
        self.assertEqual(args[-2:], ("github", f"{check.head_oid}:refs/heads/main"))
        self.assertIn("--no-follow-tags", args)
        self.assertIn(f"--force-with-lease=refs/heads/main:{check.target_oid}", args)
        self.assertNotIn("--force", args)
        self.assertNotIn("--tags", args)
        git(self.repo, *args)
        self.assertEqual(
            subprocess.run(["git", f"--git-dir={self.gh}", "rev-parse", "main"],
                           check=True, capture_output=True, text=True).stdout.strip(),
            subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                           check=True, capture_output=True, text=True).stdout.strip(),
        )

    def test_push_preflight_blocks_dirty_and_new_remote_branch(self):
        (self.repo / "dirty.txt").write_text("x\n")
        self.assertEqual(
            inspect_transfer(self.repo, "github", "main", "push").reason,
            "dirty",
        )
        (self.repo / "dirty.txt").unlink()
        git(self.repo, "checkout", "-qb", "new-branch")
        self.assertEqual(
            inspect_transfer(self.repo, "github", "new-branch", "push").reason,
            "missing-branch",
        )

    def test_push_preflight_distinguishes_failed_ref_read_from_missing_branch(self):
        original = gmf_module.run_git

        def fail_show_ref(repo, *args, **kwargs):
            if args and args[0] == "show-ref":
                return subprocess.CompletedProcess([], 128, "", "broken refs")
            return original(repo, *args, **kwargs)

        with mock.patch("gitmaster_flash.run_git", side_effect=fail_show_ref):
            check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertEqual(check.reason, "inspect-failed")

    def test_target_lease_prevents_recreating_branch_deleted_after_approval(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        git(self.gh, "update-ref", "-d", "refs/heads/main")
        result = subprocess.run(
            ["git", "-C", str(self.repo),
             *safe_push_args("github", "main", check.head_oid, check.target_oid)],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        missing = subprocess.run(["git", f"--git-dir={self.gh}", "show-ref", "--verify",
                                  "--quiet", "refs/heads/main"])
        self.assertNotEqual(missing.returncode, 0)

    def test_pull_preflight_allows_only_fast_forward(self):
        other = self.root / "other"
        git(self.bk, "symbolic-ref", "HEAD", "refs/heads/main")
        subprocess.run(["git", "clone", "-q", str(self.bk), str(other)], check=True)
        git(other, "config", "user.email", "t@example.invalid")
        git(other, "config", "user.name", "T")
        (other / "remote.md").write_text("remote\n")
        git(other, "add", "remote.md")
        git(other, "commit", "-qm", "remote")
        git(other, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "backup")

        check = inspect_transfer(self.repo, "backup", "main", "pull")
        self.assertTrue(check.ready)
        self.assertEqual((check.ahead, check.behind), (0, 1))
        self.assertEqual(safe_pull_args(check.target_oid),
                         ("merge", "--ff-only", "--", check.target_oid))
        git(self.repo, *safe_pull_args(check.target_oid))
        self.assertEqual(
            subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                           check=True, capture_output=True, text=True).stdout.strip(),
            subprocess.run(["git", "-C", str(other), "rev-parse", "HEAD"],
                           check=True, capture_output=True, text=True).stdout.strip(),
        )


def _repo(rel, branch="main", remotes=(), modified=0, untracked=0):
    # remotes: (name, ahead, behind) oder (name, ahead, behind, is_sync)
    return {"rel": rel, "branch": branch, "modified": modified, "untracked": untracked,
            "deleted": 0,
            "remotes": [{"name": r[0], "ahead": r[1], "behind": r[2],
                         "sync": bool(r[3]) if len(r) > 3 else False}
                        for r in remotes]}


def _side(version="9.9.9", repos=()):
    return {"version": version, "root": "/x", "repos": list(repos)}


class DiffTests(unittest.TestCase):
    """--diff compares two machines. The split is the point: DRIFT (should be
    identical, isn't -> actionable) vs local (different branch, dirty -> explainable).
    A report that lists everything gets ignored."""

    def test_identical_means_no_output(self):
        s = [_repo("a", remotes=[("origin", 0, 0)])]
        self.assertEqual(diff_status(_side(repos=s), _side(repos=s), "here", "there"), [])

    def test_missing_remote_is_drift(self):
        """The core case: git never transfers remotes, so they drift silently."""
        a = _side(repos=[_repo("x", remotes=[("origin", 0, 0), ("github", 0, 0)])])
        b = _side(repos=[_repo("x", remotes=[("origin", 0, 0)])])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("DRIFT", out[0])
        self.assertIn("github", out[0])
        self.assertIn("here", out[0])

    def test_missing_remote_other_direction(self):
        a = _side(repos=[_repo("x", remotes=[("origin", 0, 0)])])
        b = _side(repos=[_repo("x", remotes=[("origin", 0, 0), ("github", 0, 0)])])
        out = diff_status(a, b, "here", "there")
        self.assertIn("there", out[0])

    def test_differing_remote_state_is_drift(self):
        a = _side(repos=[_repo("x", remotes=[("github", 4, 2)])])
        b = _side(repos=[_repo("x", remotes=[("github", 0, 0)])])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("DRIFT", out[0])
        # Die eigene Maschine steht ohne Praeposition da ("here", nicht "on here").
        self.assertNotIn("on here", out[0])
        self.assertIn("on there", out[0])

    def test_sync_remote_equally_behind_is_shown(self):
        """Beide Rechner gleichauf, aber gemeinsam hinter dem Sync-Remote: das ist
        im reinen Zwei-Rechner-Vergleich unsichtbar, aber genau die Zahl, die
        interessiert (wie weit hinter dem Hub?). -> eigene SYNC-Zeile."""
        s = [_repo("x", remotes=[("origin", 0, 2, True)])]
        out = diff_status(_side(repos=s), _side(repos=s), "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("SYNC", out[0])
        self.assertNotIn("DRIFT", out[0])

    def test_sync_remote_in_sync_stays_silent(self):
        s = [_repo("x", remotes=[("origin", 0, 0, True)])]
        self.assertEqual(diff_status(_side(repos=s), _side(repos=s), "here", "there"), [])

    def test_offline_side_does_not_fake_drift(self):
        # fetch_failed haengt am Netz des jeweiligen Rechners und darf den Vergleich
        # nicht beeinflussen — sonst meldete jede Offline-Seite lauter Unterschiede.
        a = _side(repos=[_repo("x", remotes=[("github", 0, 0)])])
        b = _side(repos=[_repo("x", remotes=[("github", 0, 0)])])
        a["repos"][0]["remotes"][0]["fetch_failed"] = True
        self.assertEqual(diff_status(a, b, "here", "there"), [])

    def test_nonsync_remote_equally_behind_stays_silent(self):
        # Fuer Nicht-Sync-Remotes (z.B. github) bleibt gleicher Stand = kein Report.
        s = [_repo("x", remotes=[("github", 0, 2)])]
        self.assertEqual(diff_status(_side(repos=s), _side(repos=s), "here", "there"), [])

    def test_sync_remote_reported_before_other_remotes(self):
        a = _side(repos=[_repo("x", remotes=[("github", 4, 0), ("origin", 1, 0, True)])])
        b = _side(repos=[_repo("x", remotes=[("github", 0, 0), ("origin", 0, 0, True)])])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 2)
        self.assertIn("origin", out[0])   # Sync-Remote zuerst, github danach
        self.assertIn("github", out[1])

    def test_different_branch_is_local_not_drift(self):
        a = _side(repos=[_repo("x", branch="main")])
        b = _side(repos=[_repo("x", branch="feature")])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertNotIn("DRIFT", out[0])

    def test_dirty_is_local_not_drift(self):
        a = _side(repos=[_repo("x", modified=2, untracked=1)])
        b = _side(repos=[_repo("x")])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertNotIn("DRIFT", out[0])
        self.assertIn("3", out[0])

    def test_repo_only_on_one_side(self):
        a = _side(repos=[_repo("here-only"), _repo("both")])
        b = _side(repos=[_repo("both")])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("here-only", out[0])

    def test_version_mismatch_is_flagged_first(self):
        out = diff_status(_side("1.0.0"), _side("2.0.0"), "here", "there")
        self.assertTrue(out[0].startswith("!"))

    def test_errors_conflicts_stashes_and_remote_state_are_compared(self):
        broken = _repo("x")
        broken.update(error="git failed", conflicts=2, stashes=1, remote_state="error")
        clean = _repo("x")
        clean.update(error="", conflicts=0, stashes=0, remote_state="ok")
        out = diff_status(_side(repos=[broken]), _side(repos=[clean]), "here", "there")
        joined = "\n".join(out)
        for field in ("error", "conflicts", "stashes", "remote_state"):
            self.assertIn(field, joined)

    def test_remote_endpoint_fingerprint_drift_is_compared(self):
        a = _repo("x", remotes=[("origin", 0, 0, True)])
        b = _repo("x", remotes=[("origin", 0, 0, True)])
        a["remotes"][0]["fetch_fingerprint"] = "aaa"
        b["remotes"][0]["fetch_fingerprint"] = "bbb"
        out = diff_status(_side(repos=[a]), _side(repos=[b]), "here", "there")
        self.assertTrue(any("security/endpoint" in line for line in out))

    def test_remote_root_uses_remote_home(self):
        """Home dirs differ between machines (/Users/anna vs /home/bob) — the path
        must be resolved against the REMOTE $HOME, not pasted absolutely."""
        r = _remote_root(Path.home() / "git", None)
        self.assertEqual(r, "~/git")
        self.assertIn("git", r)
        self.assertNotIn(str(Path.home()), r)

    def test_remote_root_explicit_path_wins(self):
        self.assertEqual(_remote_root(Path.home() / "git", "/srv/code"), "/srv/code")

    def test_remote_root_outside_home_stays_absolute(self):
        self.assertEqual(_remote_root(Path("/srv/code"), None), "/srv/code")


class RemoteSecurityTests(unittest.TestCase):
    def test_endpoint_fingerprint_omits_credentials_query_and_git_suffix(self):
        a = canonical_remote_target("https://user:secret@example.com/org/repo.git?token=x")
        b = canonical_remote_target("https://example.com/org/repo")
        self.assertEqual(a, b)
        self.assertEqual(a.host, "example.com")
        self.assertNotIn("secret", a.fingerprint)

    def test_github_host_must_match_exactly(self):
        self.assertTrue(is_github_url("ssh://git@github.com/org/repo.git"))
        self.assertFalse(is_github_url("ssh://github.com.attacker.invalid/org/repo.git"))
        self.assertFalse(is_github_url("https://example.invalid/github.com/org/repo"))

    def test_sync_host_must_match_exactly(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "mirror",
                "ssh://trusted.example.attacker.invalid/org/repo.git")
            cfg = {**DEFAULT_CONFIG, "sync_remote_names": [],
                   "sync_remote_hosts": ["trusted.example"]}
            self.assertIsNone(detect_sync_remote(repo, cfg))

    def test_same_class_different_push_target_is_blocked_and_fingerprinted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a")
            git(repo, "add", "a"); git(repo, "commit", "-qm", "base")
            git(repo, "remote", "add", "origin", "https://github.com/example/one.git")
            git(repo, "remote", "set-url", "--push", "origin",
                "https://github.com/example/two.git")
            st = collect_status(repo, root, DEFAULT_CONFIG)
            remote = st.remotes[0]
            self.assertTrue(remote.target_mismatch)
            self.assertFalse(remote.transfer_safe)
            payload = status_dict(st)["remotes"][0]
            self.assertTrue(payload["fetch_fingerprint"])
            self.assertTrue(payload["push_fingerprints"])
            self.assertNotIn("github.com", json.dumps(payload))

    def test_multiple_pushurls_are_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bare = root / "bare.git"; bare.mkdir(); git(bare, "init", "-q", "--bare")
            repo = root / "repo"; repo.mkdir(); git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a"); git(repo, "add", "a"); git(repo, "commit", "-qm", "base")
            git(repo, "remote", "add", "origin", str(bare))
            git(repo, "push", "-qu", "origin", "main")
            git(repo, "remote", "set-url", "--add", "--push", "origin", str(bare))
            git(repo, "remote", "set-url", "--add", "--push", "origin", str(root / "other.git"))
            self.assertEqual(inspect_transfer(repo, "origin", "main", "push").reason,
                             "remote-unsafe")


class CommitSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"; self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "include.txt").write_text("base\n")
        (self.repo / "skip.txt").write_text("base\n")
        git(self.repo, "add", "include.txt", "skip.txt")
        git(self.repo, "commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def test_temporary_index_commits_only_approved_path_and_preserves_real_index(self):
        (self.repo / "include.txt").write_text("approved\n")
        (self.repo / "skip.txt").write_text("staged but excluded\n")
        git(self.repo, "add", "skip.txt")
        index = self.repo / ".git" / "index"
        index_before = index.read_bytes()
        r = commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        changed = subprocess.run(
            ["git", "-C", str(self.repo), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(changed, ["include.txt"])
        self.assertEqual(index.read_bytes(), index_before)
        self.assertIn("skip.txt", subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--cached", "--name-only"],
            check=True, capture_output=True, text=True).stdout)

    def test_conflicts_block_commit(self):
        git(self.repo, "checkout", "-qb", "other")
        (self.repo / "include.txt").write_text("other\n"); git(self.repo, "commit", "-qam", "other")
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "include.txt").write_text("main\n"); git(self.repo, "commit", "-qam", "main")
        merge = subprocess.run(["git", "-C", str(self.repo), "merge", "other"],
                               capture_output=True, text=True)
        self.assertNotEqual(merge.returncode, 0)
        with self.assertRaises(CommitSafetyError):
            commit_selected(self.repo, ["include.txt"], "must fail", 10)

    def test_gitignore_symlink_is_refused_without_touching_target(self):
        outside = Path(self.tmp.name) / "outside"
        outside.write_text("keep\n")
        (self.repo / ".gitignore").symlink_to(outside)
        with self.assertRaises(CommitSafetyError):
            update_gitignore_atomic(self.repo, ["*.log"])
        self.assertEqual(outside.read_text(), "keep\n")

    def test_gitignore_write_is_atomic_and_unique(self):
        (self.repo / ".gitignore").write_text("*.tmp\n")
        self.assertTrue(update_gitignore_atomic(self.repo, ["*.tmp", "*.log"]))
        self.assertEqual((self.repo / ".gitignore").read_text(), "*.tmp\n*.log\n")

    def test_gitignore_race_is_refused(self):
        with mock.patch("gitmaster_flash._path_signature",
                        side_effect=[None, (1, 2, stat.S_IFREG, 0o100644, 0, 1)]):
            with self.assertRaisesRegex(CommitSafetyError, "changed during update"):
                update_gitignore_atomic(self.repo, ["*.log"])
        self.assertFalse((self.repo / ".gitignore").exists())


class StashAndReadFailureTests(unittest.TestCase):
    def test_stash_preview_includes_untracked_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp); git(repo, "init", "-q")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "base").write_text("base\n"); git(repo, "add", "base"); git(repo, "commit", "-qm", "base")
            (repo / "untracked.bin").write_bytes(bytes(range(256)) * 2)
            git(repo, "stash", "push", "-qu")
            ok, preview = stash_preview(repo, 10)
            self.assertTrue(ok)
            self.assertIn("untracked.bin", preview)
            self.assertIn("GIT binary patch", preview)

    def test_broken_index_sets_error_instead_of_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            git(repo, "init", "-q"); git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a"); git(repo, "add", "a"); git(repo, "commit", "-qm", "base")
            (repo / ".git" / "index").write_bytes(b"broken")
            st = collect_status(repo, root, DEFAULT_CONFIG)
            self.assertTrue(st.error)
            self.assertEqual(st.remote_state, "error")
            self.assertFalse(st.clean_and_synced)


class NonInteractiveGitTests(unittest.TestCase):
    """Git darf nie nach Zugangsdaten fragen — sonst zerlegt der Prompt die TUI."""

    @staticmethod
    def _deny_server():
        """Lokaler HTTP-Server, der jede Anfrage mit 401 + Basic-Auth abweist.

        Damit lässt sich der Login-Fall ohne Netz und ohne echten Host testen:
        Git fragt genau hier nach Username/Passwort.
        """
        class Deny(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="test"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Deny)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def _serving(self):
        """Server starten und am Testende wieder abbauen (Socket inklusive)."""
        srv = self._deny_server()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d" % srv.server_address[1]

    def test_fetch_fails_fast_instead_of_asking_for_a_username(self):
        base = self._serving()
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "faux", base + "/x.git")
            started = time.monotonic()
            r = gmf_module.run_git(repo, "fetch", "--prune", "--quiet", "--", "faux",
                                   timeout=30)
            elapsed = time.monotonic() - started
        self.assertNotEqual(r.returncode, 0)
        # Kein Warten auf eine Eingabe, die niemals kommt.
        self.assertLess(elapsed, 15)
        self.assertTrue(gmf_module.credentials_missing(r), r.stderr)

    def _repo_with_denying_remotes(self, root: Path, *names: str) -> Path:
        base = self._serving()
        repo = root / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "t@example.invalid")
        git(repo, "config", "user.name", "T")
        (repo / "a").write_text("a")
        git(repo, "add", "a")
        git(repo, "commit", "-qm", "base")
        for name in names:
            git(repo, "remote", "add", name, "%s/%s.git" % (base, name))
        return repo

    def test_fetch_all_names_every_remote_that_wants_a_login(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repo_with_denying_remotes(root, "faux", "zwei")
            st = collect_status(repo, root, DEFAULT_CONFIG, fetch=True)
        self.assertEqual(st.remote_state, "error")
        self.assertIn("faux", st.error)
        self.assertIn("zwei", st.error)
        self.assertIn("login", st.error.lower())

    def test_fetch_all_names_the_single_remote_too(self):
        # Bei genau einem Remote nennt Git den Namen nicht — die Meldung trotzdem.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repo_with_denying_remotes(root, "faux")
            st = collect_status(repo, root, DEFAULT_CONFIG, fetch=True)
        self.assertEqual(st.remote_state, "error")
        self.assertIn("faux", st.error)
        self.assertIn("login", st.error.lower())

    def test_failing_fetch_marks_the_remote_instead_of_only_the_repo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repo_with_denying_remotes(root, "faux", "zwei")
            # Ein drittes, lokales Remote muss unberührt bleiben.
            spare = root / "spare.git"
            git(root, "init", "-q", "--bare", str(spare))
            git(repo, "remote", "add", "lokal", str(spare))
            st = collect_status(repo, root, DEFAULT_CONFIG, fetch=True)
        failed = {r.name for r in st.remotes if r.fetch_failed}
        self.assertEqual(failed, {"faux", "zwei"})
        badges = {r.name: r.badge() for r in st.remotes}
        self.assertTrue(badges["faux"].startswith("✘"), badges)
        self.assertNotIn("✘", badges["lokal"])
        self.assertTrue(status_dict(st)["remotes"][0].get("fetch_failed") is not None)

    def test_run_git_disables_prompts_and_keeps_caller_env(self):
        recorded = {}

        def fake_run(argv, **kwargs):
            recorded.update(kwargs)
            recorded["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch("gitmaster_flash.subprocess.run", fake_run):
            gmf_module.run_git(Path("/tmp"), "status", env={"GIT_INDEX_FILE": "/tmp/i"})
        env = recorded["env"]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_ASKPASS"], "")
        self.assertEqual(env["LC_ALL"], "C")
        # Der Aufrufer-Env (temporärer Index beim Commit) darf nicht verloren gehen.
        self.assertEqual(env["GIT_INDEX_FILE"], "/tmp/i")
        self.assertEqual(recorded["stdin"], subprocess.DEVNULL)
        self.assertTrue(recorded["start_new_session"])

    def test_credentials_missing_only_for_login_errors(self):
        def result(stderr):
            return subprocess.CompletedProcess(["git"], 128, "", stderr)

        self.assertTrue(gmf_module.credentials_missing(result(
            "fatal: could not read Username for 'https://github.com': "
            "terminal prompts disabled")))
        self.assertTrue(gmf_module.credentials_missing(result(
            "git@github.com: Permission denied (publickey).")))
        self.assertFalse(gmf_module.credentials_missing(result(
            "fatal: couldn't find remote ref main")))

    def test_failed_fetch_remotes_reads_the_names(self):
        r = subprocess.CompletedProcess(["git"], 1, "", (
            "fatal: could not read Username for 'https://github.com': "
            "terminal prompts disabled\n"
            "error: could not fetch github\n"))
        self.assertEqual(gmf_module.failed_fetch_remotes(r), ["github"])
        self.assertEqual(gmf_module.failed_fetch_remotes(
            subprocess.CompletedProcess(["git"], 1, "", "boom\n")), [])


class RemoteCheckTests(unittest.TestCase):
    """`T` auf der Info-Seite: existiert das Remote-Repo — und wenn nicht, warum?"""

    @staticmethod
    def _result(stderr, code=128):
        return subprocess.CompletedProcess(["git"], code, "", stderr)

    def test_causes_are_told_apart(self):
        cases = {
            "gone": "remote: Repository not found.\nfatal: repository 'https://x/y.git' not found",
            "auth": "fatal: could not read Username for 'https://github.com': "
                    "terminal prompts disabled",
            "dns": "ssh: Could not resolve hostname nirgendwo: nodename nor servname "
                   "provided, or not known",
            "unreachable": "ssh: connect to host 10.0.0.9 port 22: Connection refused",
            # Ein unbekannter Hostschlüssel ist KEIN fehlender Login: man muss ihn
            # einmal bestätigen, nicht Zugangsdaten einrichten.
            "hostkey": "Host key verification failed.\n"
                       "fatal: Could not read from remote repository.",
            "server": "fatal: unable to access 'https://x/y.git/': "
                      "The requested URL returned error: 503",
            "unknown": "fatal: something entirely new happened",
        }
        for expected, stderr in cases.items():
            with self.subTest(expected):
                self.assertEqual(classify_remote_check(self._result(stderr)), expected)

    def test_unknown_host_key_is_not_reported_as_a_missing_login(self):
        result = self._result("Host key verification failed.")
        self.assertFalse(gmf_module.credentials_missing(result))
        message = remote_check_message("github", classify_remote_check(result), 0, "", 30)
        self.assertIn("host key", message.lower())
        self.assertNotIn("login", message.lower())

    def test_fetch_failure_reports_the_real_cause_per_remote(self):
        # Genau Daniels Fall: das GitHub-Repo ist weg, der Fetch soll das sagen —
        # nicht pauschal "Login fehlt".
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "base")
            git(repo, "remote", "add", "github", str(root / "auf-github-geloescht.git"))
            st = collect_status(repo, root, DEFAULT_CONFIG, fetch=True)
        # In der Repo-Zeile nur das Stichwort — sonst verdrängt der Satz die Badges.
        self.assertEqual(st.error, "github: repository gone")
        self.assertNotIn("login", st.error.lower())
        # Ganzer Satz und Gits Wortlaut stehen auf der Info-Seite.
        self.assertIn("no repository", st.error_long)
        info = "\n".join(repo_info_lines(st, DEFAULT_CONFIG))
        self.assertIn("no repository", info)
        self.assertTrue(st.error_detail)
        self.assertIn("Git said", info)

    def test_deleted_repo_and_broken_network_do_not_look_alike(self):
        gone = remote_check_message("github", "gone", 0, "", 30)
        offline = remote_check_message("github", "dns", 0, "", 30)
        down = remote_check_message("github", "unreachable", 0, "", 30)
        self.assertNotEqual(gone, offline)
        self.assertNotEqual(gone, down)
        self.assertNotEqual(offline, down)

    def test_check_reports_existing_and_missing_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            origin = root / "origin.git"
            git(root, "init", "-q", "--bare", str(origin))
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "base")
            git(repo, "remote", "add", "origin", str(origin))
            git(repo, "push", "-q", "origin", "HEAD")
            git(repo, "remote", "add", "weg", str(root / "gibt-es-nicht.git"))

            self.assertEqual(check_remote(repo, "origin", 10)[:2], ("ok", 1))
            outcome, _, detail = check_remote(repo, "weg", 10)
        self.assertEqual(outcome, "gone", detail)

    def test_check_of_empty_repository_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            empty = root / "empty.git"
            git(root, "init", "-q", "--bare", str(empty))
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "remote", "add", "leer", str(empty))
            self.assertEqual(check_remote(repo, "leer", 10)[0], "empty")


class RemoteRemovalAndCommandLogTests(unittest.TestCase):
    """`X` entfernt nur lokale Config — und jede Aktion landet im Befehlsprotokoll."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "a.md").write_text("hallo\n")
        git(self.repo, "add", "a.md")
        git(self.repo, "commit", "-qm", "erster Commit")
        self.origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", str(self.origin))
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-q", "-u", "origin", "HEAD")
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)

    def tearDown(self):
        self.tmp.cleanup()

    def test_removing_a_remote_keeps_commits_and_files(self):
        head_before = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                     capture_output=True, text=True).stdout
        r = gmf_module.run_git_logged(self.repo, "remote", "remove", "origin", timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        config = (self.repo / ".git" / "config").read_text()
        self.assertNotIn('[remote "origin"]', config)
        # Tracking-Refs weg, Commit und Datei unangetastet.
        refs = subprocess.run(["git", "-C", str(self.repo), "for-each-ref",
                               "refs/remotes/origin"], capture_output=True, text=True)
        self.assertEqual(refs.stdout.strip(), "")
        head_after = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                    capture_output=True, text=True).stdout
        self.assertEqual(head_before, head_after)
        self.assertTrue((self.repo / "a.md").exists())
        # Das Remote-Repo selbst bleibt bestehen — entfernt wird nur die Config.
        self.assertTrue(self.origin.exists())

    def test_command_log_shows_the_real_git_syntax(self):
        gmf_module.run_git_logged(self.repo, "remote", "remove", "origin", timeout=10)
        self.assertEqual(len(gmf_module.COMMAND_LOG), 1)
        entry = gmf_module.COMMAND_LOG[0]
        self.assertIn("git remote remove origin", entry)
        self.assertTrue(entry.startswith("✔"), entry)
        self.assertIn("repo", entry)

    def test_failed_command_is_logged_with_exit_code(self):
        gmf_module.run_git_logged(self.repo, "remote", "remove", "gibtsnicht", timeout=10)
        entry = gmf_module.COMMAND_LOG[0]
        self.assertTrue(entry.startswith("✘"), entry)
        self.assertIn("Exit", entry)

    def test_known_fetch_failure_survives_a_local_refresh(self):
        # Nach einer Aktion (oder einem abgebrochenen Dialog) wird nur lokal neu
        # gelesen. Der tote Remote darf dabei nicht scheinbar heil werden.
        git(self.repo, "remote", "add", "github", str(self.root / "geloescht.git"))
        broken = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        self.assertTrue(broken.error)

        class Screen:
            def getmaxyx(self): return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [broken]
        fresh = ui.refresh_one(broken)
        self.assertEqual(fresh.error, broken.error)
        self.assertEqual(fresh.error_detail, broken.error_detail)
        self.assertTrue(any(r.fetch_failed for r in fresh.remotes if r.name == "github"))
        self.assertFalse(fresh.clean_and_synced)

    def test_cancelled_action_is_logged_as_not_run(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        class Screen:
            def erase(self): pass
            def getmaxyx(self): return (30, 100)
            def addstr(self, *a): pass
            def refresh(self): pass

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch.object(TUI, "confirm", return_value=False):
            self.assertFalse(ui._remove_remote(st, "origin"))
        entry = gmf_module.COMMAND_LOG[-1]
        self.assertTrue(entry.startswith("⊘"), entry)
        self.assertIn("git remote remove origin", entry)
        self.assertIn("cancelled", entry)
        # Und der Remote ist wirklich noch da.
        self.assertIn('[remote "origin"]', (self.repo / ".git" / "config").read_text())

    def test_repo_line_keeps_the_remote_badges_visible(self):
        # Die Fehlermeldung teilt sich die Zeile mit den Badges: bleibt sie kurz,
        # ist das ✘ am Remote noch zu sehen.
        git(self.repo, "remote", "add", "github", str(self.root / "geloescht.git"))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        self.assertLess(len(st.error), 40, st.error)
        self.assertIn("✘", "".join(r.badge() for r in st.remotes))

    def test_log_quotes_arguments_so_the_line_can_be_pasted(self):
        self.assertEqual(
            gmf_module.format_git_command(("commit", "-m", "zwei Wörter")),
            "git commit -m 'zwei Wörter'")

    def test_log_keeps_only_the_newest_entries(self):
        for i in range(gmf_module.COMMAND_LOG_MAX + 5):
            gmf_module.log_command(self.repo, ("status", str(i)), 0)
        self.assertEqual(len(gmf_module.COMMAND_LOG), gmf_module.COMMAND_LOG_MAX)
        self.assertIn(str(gmf_module.COMMAND_LOG_MAX + 4), gmf_module.COMMAND_LOG[-1])

    def _info_screen(self, keys):
        """Fake-Screen fuer die Info-Ansicht; liefert die gedrueckten Tasten der Reihe."""
        class Screen:
            def __init__(self):
                self.keys = iter(keys)
                self.written = []

            def erase(self):
                pass

            def getmaxyx(self):
                return (30, 100)

            def addstr(self, y, x, text, *rest):
                self.written.append(text)

            def refresh(self):
                pass

            def getch(self):
                return next(self.keys)

        return Screen()

    def test_tab_selects_the_next_remote_before_acting_on_it(self):
        git(self.repo, "remote", "add", "github", str(self.root / "geloescht.git"))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        ui = TUI(self._info_screen([9, ord("t"), ord("q")]), self.root,
                 DEFAULT_CONFIG, None)
        ui.statuses = [st]
        # origin (Sync) steht an erster Stelle, github zuletzt — Tab muss weiterspringen.
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_repo_info()
        self.assertIn("github", ui.message)
        self.assertIn("no repository", ui.message)

    def test_x_removes_only_after_confirmation(self):
        ui = TUI(self._info_screen([ord("x"), ord("q")]), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [collect_status(self.repo, self.root, DEFAULT_CONFIG)]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch.object(TUI, "confirm", return_value=False):
            ui.action_repo_info()
        self.assertIn('[remote "origin"]', (self.repo / ".git" / "config").read_text())
        self.assertEqual(ui.message, "Nothing was removed.")

        ui = TUI(self._info_screen([ord("x"), ord("q")]), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [collect_status(self.repo, self.root, DEFAULT_CONFIG)]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch.object(TUI, "confirm", return_value=True):
            ui.action_repo_info()
        self.assertNotIn('[remote "origin"]', (self.repo / ".git" / "config").read_text())
        self.assertIn("origin", ui.message)
        self.assertIn("git remote remove origin", "\n".join(gmf_module.COMMAND_LOG))

    def test_info_view_maps_remote_blocks_to_lines(self):
        git(self.repo, "remote", "add", "github", "https://github.com/example/demo.git")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        view = build_info_view(st, DEFAULT_CONFIG)
        self.assertEqual(view.remote_names, ["origin", "github"])
        for name, first, last in view.remote_blocks:
            self.assertIn(name, view.lines[first])
            self.assertLessEqual(first, last)
            # Der Block endet vor dem naechsten Remote-Kopf.
            self.assertTrue(all(not line.startswith("  " + other)
                                for other in view.remote_names if other != name
                                for line in view.lines[first:last + 1]))
        # Der Textmodus bleibt unveraendert nutzbar (CLI/Tests).
        self.assertEqual(repo_info_lines(st, DEFAULT_CONFIG), view.lines)


class BranchAndDiffTests(unittest.TestCase):
    """Branch-Übersicht und Datei-Diffs — beides rein lesend gegen echte Repos."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "a.md").write_text("eins\n")
        git(self.repo, "add", "a.md")
        git(self.repo, "commit", "-qm", "erster Commit")
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)

    def tearDown(self):
        self.tmp.cleanup()

    def test_branches_report_head_merge_state_and_upstream(self):
        git(self.repo, "branch", "fertig")               # gemergt (zeigt auf HEAD)
        git(self.repo, "checkout", "-q", "-b", "offen")
        (self.repo / "b.md").write_text("zwei\n")
        git(self.repo, "add", "b.md")
        git(self.repo, "commit", "-qm", "zweiter Commit")
        git(self.repo, "checkout", "-q", "main")

        branches = {b.name: b for b in read_branches(self.repo, DEFAULT_CONFIG)}
        self.assertEqual(set(branches), {"main", "fertig", "offen"})
        self.assertTrue(branches["main"].is_head)
        self.assertTrue(branches["fertig"].merged)
        self.assertFalse(branches["offen"].merged)
        self.assertEqual(branches["main"].subject, "erster Commit")
        self.assertEqual(branches["main"].upstream, "")

    def test_branch_upstream_delta_is_read(self):
        origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", str(origin))
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "-u", "origin", "main")
        (self.repo / "a.md").write_text("zwei\n")
        git(self.repo, "commit", "-qam", "lokaler Vorsprung")
        branch = next(b for b in read_branches(self.repo, DEFAULT_CONFIG)
                      if b.name == "main")
        self.assertEqual(branch.upstream, "origin/main")
        self.assertEqual((branch.ahead, branch.behind), (1, 0))

    def test_info_page_lists_branches_as_selectable_blocks(self):
        git(self.repo, "branch", "fertig")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        view = build_info_view(st, DEFAULT_CONFIG)
        kinds = {kind for kind, _, _, _ in view.blocks}
        names = [name for kind, name, _, _ in view.blocks if kind == "branch"]
        self.assertIn("branch", kinds)
        self.assertEqual(sorted(names), ["fertig", "main"])
        self.assertIn("Local branches:", view.lines)
        # Auch über Remotes und Branches hinweg steht alles in einer Spalte.
        details = [line for line in view.lines if line.startswith("    ")]
        columns = {len(line) - len(line.partition(":")[2].lstrip()) for line in details}
        self.assertEqual(len(columns), 1, details)

    def test_merged_branch_is_deletable_and_unmerged_one_is_refused(self):
        git(self.repo, "branch", "fertig")
        git(self.repo, "checkout", "-q", "-b", "offen")
        (self.repo / "b.md").write_text("zwei\n")
        git(self.repo, "add", "b.md")
        git(self.repo, "commit", "-qm", "zweiter Commit")
        git(self.repo, "checkout", "-q", "main")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        class Screen:
            def erase(self): pass
            def getmaxyx(self): return (30, 100)
            def addstr(self, *a): pass
            def refresh(self): pass
            def getch(self): return ord("q")

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            # Nicht gemergt: gar kein Dialog, sondern eine Erklärung.
            self.assertFalse(ui._delete_branch(st, "offen"))
            self.assertIn("not merged", ui.message)
            # Aktueller Branch: ebenfalls tabu.
            self.assertFalse(ui._delete_branch(st, "main"))
            self.assertIn("current branch", ui.message)
            # Gemergt: nur nach Bestätigung.
            with mock.patch.object(TUI, "confirm", return_value=False):
                self.assertFalse(ui._delete_branch(st, "fertig"))
            self.assertIn("fertig", [b.name for b in read_branches(self.repo,
                                                                   DEFAULT_CONFIG)])
            with mock.patch.object(TUI, "confirm", return_value=True):
                self.assertTrue(ui._delete_branch(st, "fertig"))
        self.assertNotIn("fertig", [b.name for b in read_branches(self.repo,
                                                                  DEFAULT_CONFIG)])
        self.assertIn("git branch -d fertig", "\n".join(gmf_module.COMMAND_LOG))
        # Der Commit ist über main weiterhin erreichbar — nichts ist verloren.
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "log", "--oneline"],
                                        capture_output=True, text=True).returncode, 0)

    def test_file_diff_covers_modified_deleted_and_untracked(self):
        (self.repo / "a.md").write_text("geändert\n")
        (self.repo / "neu.txt").write_text("frisch\n")
        (self.repo / "weg.md").write_text("x\n")
        git(self.repo, "add", "weg.md")
        git(self.repo, "commit", "-qm", "zweite Datei")
        (self.repo / "weg.md").unlink()

        ok, diff = file_diff(self.repo, "M", "a.md", 10)
        self.assertTrue(ok)
        self.assertIn("-eins", diff)
        self.assertIn("+geändert", diff)

        ok, diff = file_diff(self.repo, "U", "neu.txt", 10)
        self.assertTrue(ok, diff)
        self.assertIn("+frisch", diff)

        ok, diff = file_diff(self.repo, "D", "weg.md", 10)
        self.assertTrue(ok)
        self.assertIn("-x", diff)

    def test_changes_view_shows_diff_for_the_selected_file(self):
        (self.repo / "a.md").write_text("geändert\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        shown = {}

        class Screen:
            def __init__(self):
                self.keys = iter([10, ord("q")])   # ⏎ Diff, dann zurück
            def erase(self): pass
            def getmaxyx(self): return (30, 100)
            def addstr(self, *a): pass
            def refresh(self): pass
            def getch(self): return next(self.keys)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch.object(TUI, "show_pager",
                                  side_effect=lambda title, lines: shown.update(
                                      title=title, lines=lines)):
            ui.action_file_changes()
        self.assertIn("a.md", shown["title"])
        self.assertTrue(any(line.startswith("+geändert") for line in shown["lines"]))

    def test_app_open_is_refused_over_ssh(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        cfg = {**DEFAULT_CONFIG, "apps": {"E": {"name": "Editor", "path": "/"}}}
        ui = TUI(None, self.root, cfg, None)
        ui.statuses = [st]
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "1.2.3.4 1 5.6.7.8 22"}), \
                mock.patch("gitmaster_flash.subprocess.run") as run:
            ui.action_open_app("E")
        run.assert_not_called()
        self.assertIn("SSH", ui.message)


class CompactViewTests(unittest.TestCase):
    """Kompakte Übersicht: Layout, Marken und Navigation — alles headless."""

    @staticmethod
    def _status(rel, **kw):
        kw.setdefault("remote_state", "ok")
        return RepoStatus(path=Path("/tmp") / rel, rel=rel, **kw)

    def test_marks_show_the_most_urgent_state(self):
        cases = [
            (self._status("a", error="kaputt"), "✘"),
            (self._status("b", conflicts=1), "⚠"),
            (self._status("c", modified=2), "●"),
            (self._status("d", stashes=["stash@{0} x"]), "⚑"),
            (self._status("e", ahead=2), "↑2"),
            (self._status("f", behind=1), "↓1"),
            (self._status("g", remote_state="no-remote"), "?"),
            (self._status("h"), "✔"),
        ]
        for st, expected in cases:
            with self.subTest(expected):
                self.assertEqual(gmf_module.compact_mark(st)[0], expected)
        # Ein toter Remote schlägt auch ohne st.error durch.
        st = self._status("i", remotes=[RemoteStatus(name="github", fetch_failed=True)])
        self.assertEqual(gmf_module.compact_mark(st)[0], "✘")

    def test_layout_keeps_three_columns_despite_one_long_name(self):
        rows, columns, width = gmf_module.compact_layout(24, 110, 20, 40)
        self.assertGreaterEqual(columns, 3)
        self.assertLessEqual(width * columns, 110)
        self.assertEqual(rows, math.ceil(24 / columns))
        # Schmales Fenster: lieber weniger Spalten als unlesbare Namen.
        _, narrow_columns, narrow_width = gmf_module.compact_layout(24, 40, 20, 40)
        self.assertGreaterEqual(narrow_width, gmf_module.COMPACT_MIN_WIDTH)
        self.assertLessEqual(narrow_columns, 3)

    def test_layout_fills_columns_like_ls(self):
        rows, columns, _ = gmf_module.compact_layout(9, 68, 10, 20)
        self.assertEqual((rows, columns), (3, 3))
        # Erste Spalte = erste drei Einträge (spaltenweise gefüllt).
        self.assertEqual(gmf_module.compact_position(0, rows), (0, 0))
        self.assertEqual(gmf_module.compact_position(2, rows), (2, 0))
        self.assertEqual(gmf_module.compact_position(3, rows), (0, 1))
        self.assertEqual(gmf_module.compact_position(8, rows), (2, 2))

    def test_long_names_are_shortened_visibly(self):
        self.assertEqual(gmf_module.ellipsize("kurz", 10), "kurz")
        shortened = gmf_module.ellipsize("firefox-tabs-save-and-restore", 12)
        self.assertEqual(cell_width(shortened), 12)
        self.assertTrue(shortened.endswith("…"))

    def _ui(self, count, keys=(), width=100, height=30):
        class Screen:
            def __init__(self):
                self.keys = iter(list(keys) + [ord("q")])
                self.lines = []

            def erase(self): pass
            def clear(self): self.lines.clear()
            def getmaxyx(self): return (height, width)
            def addstr(self, y, x, text, *rest): self.lines.append((y, x, text))
            def refresh(self): pass
            def getch(self): return next(self.keys)

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.statuses = [self._status(f"repo-{i:02d}") for i in range(count)]
        return ui

    def test_view_switch_keeps_the_selected_repository(self):
        ui = self._ui(24)
        ui.view_mode = "compact"
        ui.selected = 17
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.scr.keys = iter([ord("m"), ord("q")])
            ui.run()
        self.assertEqual(ui.view_mode, "detail")
        self.assertEqual(ui.selected, 17)
        self.assertEqual(ui.current().rel, "repo-17")

    def test_left_right_move_by_one_column(self):
        ui = self._ui(24)
        ui.view_mode = "compact"
        ui.selected = 0
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.scr.keys = iter([curses.KEY_RIGHT, ord("q")])
            ui.run()
        rows, _, _ = ui.compact_geometry(max(1, 30 - 5 - ui.log_height(30)), 100)
        self.assertEqual(ui.selected, rows)
        # Und zurück.
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.scr.keys = iter([curses.KEY_LEFT, ord("q")])
            ui.run()
        self.assertEqual(ui.selected, 0)

    def test_many_repositories_start_compact(self):
        for count, expected in ((25, "compact"), (5, "detail")):
            with self.subTest(count=count):
                ui = self._ui(0)
                ui.statuses = []
                with mock.patch("gitmaster_flash.collect_all",
                                return_value=[self._status(f"r{i}")
                                              for i in range(count)]), \
                        mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                        mock.patch("gitmaster_flash.safe_addstr"):
                    ui.reload()
                self.assertEqual(ui.view_mode, expected)

    def test_only_the_focused_pane_shows_a_selection_bar(self):
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for i in range(6):
            gmf_module.log_command(Path("/tmp/repo"), ("status", str(i)), 0)
        class Recorder(list):
            pass

        for focus, expect_repo_bar in (("repos", True), ("log", False)):
            with self.subTest(focus):
                ui = self._ui(9, height=26, width=100)
                ui.view_mode = "compact"
                ui.focus = focus
                ui.log_selected = 2
                marked = Recorder()

                def addstr(y, x, text, attr=0, _marked=marked):
                    _marked.append((text, bool(attr & curses.A_REVERSE)))

                ui.scr.addstr = addstr
                with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
                    ui.draw()
                repo_bar = any(mark for text, mark in marked if "repo-00" in text)
                log_bar = any(mark for text, mark in marked if "git status 2" in text)
                self.assertEqual(repo_bar, expect_repo_bar, marked)
                self.assertEqual(log_bar, not expect_repo_bar, marked)

    def test_log_selection_and_repo_selection_survive_switching(self):
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for i in range(10):
            gmf_module.log_command(Path("/tmp/repo"), ("status", str(i)), 0)
        ui = self._ui(9, height=26, width=100)
        ui.selected = 4
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            # Tab nach unten, zweimal hoch, Tab zurück, einmal runter, wieder Tab.
            ui.scr.keys = iter([9, curses.KEY_UP, curses.KEY_UP, 9,
                                curses.KEY_DOWN, 9, ord("q")])
            ui.run()
        self.assertEqual(ui.focus, "log")
        self.assertEqual(ui.log_selected, 7)   # 9 (neuester) minus zwei nach oben
        self.assertEqual(ui.selected, 5)       # oben eins weiter, blieb erhalten

    def test_first_visit_to_the_log_starts_at_the_newest_entry(self):
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for i in range(4):
            gmf_module.log_command(Path("/tmp/repo"), ("status", str(i)), 0)
        ui = self._ui(3)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.scr.keys = iter([9, ord("q")])
            ui.run()
        self.assertEqual(ui.log_selected, 3)

    def test_tab_moves_focus_to_the_log_and_arrows_then_scroll(self):
        ui = self._ui(5)
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for i in range(30):
            gmf_module.log_command(Path("/tmp/repo"), ("status", str(i)), 0)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.scr.keys = iter([9, curses.KEY_UP, curses.KEY_UP, ord("q")])
            ui.run()
        self.assertEqual(ui.focus, "log")
        self.assertEqual(ui.selected, 0)      # Auswahl blieb unberührt
        self.assertGreater(ui.log_height(30), 3)   # im Fokus größer

    def test_layout_does_not_overlap_and_fits_the_window(self):
        """Kein Bereich darf in einen anderen hineinzeichnen (h=26, w=100)."""
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for i in range(5):
            gmf_module.log_command(Path("/tmp/repo"), ("status", str(i)), 0)
        for mode in ("compact", "detail"):
            with self.subTest(mode):
                ui = self._ui(24, height=26, width=100)
                ui.view_mode = mode
                with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
                    ui.draw()
                calls = ui.scr.lines
                self.assertTrue(all(0 <= y < 26 for y, _, _ in calls), calls)
                repo_ys = {y for y, _, text in calls
                           if any(name in text for name in ("repo-0", "repo-1"))}
                log_ys = {y for y, _, text in calls
                          if "Befehle" in text or "Commands" in text
                          or "git status" in text}
                footer_ys = {y for y, _, text in calls
                             if any(marker in text for marker in
                                    ("M view", "M Ansicht", "A changes",
                                     "A Änderungen", "Q quit", "Q Beenden"))}
                self.assertTrue(repo_ys and log_ys and footer_ys, calls)
                self.assertLess(max(repo_ys), min(log_ys))
                self.assertLess(max(log_ys), min(footer_ys))
                # Die drei Fußzeilen stehen ganz unten und nirgends sonst.
                self.assertEqual(sorted(footer_ys), [23, 24, 25])

    def test_compact_uses_the_leftover_height_for_the_log(self):
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        ui = self._ui(6, height=30, width=100)      # 6 Repos = 2 Zeilen bei 3 Spalten
        ui.view_mode = "compact"
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        log_header = next(y for y, _, text in ui.scr.lines
                          if "Befehle" in text or "Commands" in text)
        # Der Protokollkopf rückt direkt unter die kurze Liste, statt unten zu kleben.
        self.assertLessEqual(log_header, 5)

    def test_log_pane_is_always_drawn_with_at_least_three_lines(self):
        ui = self._ui(3)
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        gmf_module.log_command(Path("/tmp/repo"), ("remote", "remove", "github"), 0)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        drawn = "\n".join(text for _, _, text in ui.scr.lines)
        self.assertIn("git remote remove github", drawn)
        self.assertEqual(ui.log_height(30), 3)
        self.assertEqual(ui.log_height(12), 0)     # winziges Fenster: Repos gehen vor


class DisplayAndIntegrationSafetyTests(unittest.TestCase):
    def test_i_key_opens_repo_info_case_insensitively(self):
        class Screen:
            def __init__(self):
                self.keys = iter((ord("i"), ord("Q")))

            def getch(self):
                return next(self.keys)

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        with mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(ui, "reload"), \
                mock.patch.object(ui, "draw"), \
                mock.patch.object(ui, "action_repo_info") as action:
            ui.run()
        action.assert_called_once_with()

    def test_terminal_controls_are_visible_and_width_is_cell_aware(self):
        escaped = terminal_text("name\n\x1b[31m\x85")
        self.assertNotIn("\n", escaped)
        self.assertNotIn("\x1b", escaped)
        self.assertIn("\\n", escaped)
        self.assertIn("\\x1b", escaped)
        self.assertEqual(cell_width("a\u0308界✔"), 5)
        self.assertEqual(truncate_cells("界x", 2), "界")
        self.assertEqual(cell_width(pad_cells("界", 4)), 4)

    def test_commit_picker_survives_39_columns(self):
        class Screen:
            def erase(self): pass
            def getmaxyx(self): return (10, 39)
            def addstr(self, *args): pass
            def refresh(self): pass
            def getch(self): return 27

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.statuses = [RepoStatus(path=Path("/tmp/repo"), rel="r",
                                  files=[("M", "very-long-name.txt")], modified=1)]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_commit_wizard()
        self.assertEqual(ui.message, "Commit helper cancelled.")

    def test_remote_root_is_one_shell_argument(self):
        root = "~/My Projects/x;touch PWNED"
        completed = subprocess.CompletedProcess([], 0, stdout='{"version":"x","repos":[]}', stderr="")
        with mock.patch("gitmaster_flash.subprocess.run", return_value=completed) as run:
            fetch_remote_status("example", root, fetch=False)
        command = run.call_args.args[0][-1]
        self.assertEqual(shlex.split(command)[-1], root)
        self.assertEqual(shlex.split(command).count(root), 1)

    def test_remote_json_with_attention_exit_is_accepted(self):
        """Exit 1 ist bei --json ein Befund, kein fehlgeschlagener SSH-Aufruf."""
        payload = {"version": "x", "repos": [{"rel": "needs-attention"}]}
        completed = subprocess.CompletedProcess(
            [], 1, stdout=json.dumps(payload), stderr="")
        with mock.patch("gitmaster_flash.subprocess.run", return_value=completed):
            self.assertEqual(fetch_remote_status("example", "~/git", fetch=True), payload)

    def test_remote_process_failure_without_stderr_reports_exit_code(self):
        completed = subprocess.CompletedProcess([], 255, stdout="", stderr="")
        with mock.patch("gitmaster_flash.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "255"):
                fetch_remote_status("example", "~/git", fetch=False)

    def test_screenshot_settle_and_owned_tmpdir_are_wired(self):
        source = Path(__file__).resolve().parents[1] / "docs" / "make-screens.py"
        spec = importlib.util.spec_from_file_location("gmf_make_screens_test", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        owned = []

        def fake_render(args, keys, settle, tmpdir):
            self.assertEqual(settle, 0.123)
            self.assertTrue(Path(tmpdir).is_dir())
            owned.append(Path(tmpdir))
            return []

        with mock.patch.object(module, "_render_in_pty", side_effect=fake_render):
            self.assertEqual(module.render_in_pty(["--version"], settle=0.123), [])
        self.assertEqual(len(owned), 1)
        self.assertFalse(owned[0].exists())
        self.assertNotIn('glob("gmf-demo-', source.read_text())

    @unittest.skipUnless(shutil.which("zsh"), "zsh unavailable")
    def test_installer_quotes_weird_clone_path(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo = base / 'My Projects; "quoted"'
            repo.mkdir()
            shutil.copy2(source / "install.sh", repo / "install.sh")
            shutil.copy2(source / "gmf.zsh", repo / "gmf.zsh")
            fakebin = base / "bin"; fakebin.mkdir()
            fake_python = fakebin / "python3"
            fake_python.write_text("#!/bin/sh\nexit 0\n"); fake_python.chmod(0o755)
            zdot = base / "zdot"; zdot.mkdir()
            env = dict(os.environ, HOME=str(base / "home"), ZDOTDIR=str(zdot),
                       PATH=str(fakebin) + os.pathsep + os.environ["PATH"])
            result = subprocess.run(["zsh", str(repo / "install.sh")], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            sourced = subprocess.run(
                ["zsh", "-fc", 'source "$1"; print -r -- "$GMF_SCRIPT"', "zsh", str(zdot / ".zshrc")],
                env=env, capture_output=True, text=True)
            self.assertEqual(sourced.returncode, 0, sourced.stderr)
            self.assertEqual(Path(sourced.stdout.strip()).resolve(),
                             (repo / "gitmaster_flash.py").resolve())


class VersionInOutputTests(unittest.TestCase):
    """Die Version muss in JEDER Ausgabe stehen (seit 0.6.0).

    Zweck: Wer zwei Ausgaben von verschiedenen Macs vergleicht, muss sehen, ob
    dieselbe Fassung dahintersteckt — sonst haelt man einen Versionsunterschied fuer
    einen echten Repo-Unterschied. Betrifft auch eine laufende Instanz, die den Code
    von ihrem Start zeigt, waehrend der Fleet-Sync im Hintergrund schon aktualisiert hat.
    """

    SCRIPT = str(Path(__file__).resolve().parent.parent / "gitmaster_flash.py")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = Path(cls.tmp.name) / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "f.txt").write_text("x")
        git(repo, "add", "f.txt")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_gmf(self, *args):
        return subprocess.run([sys.executable, self.SCRIPT, *args, self.tmp.name],
                              capture_output=True, text=True)

    def test_version_flag(self):
        r = subprocess.run([sys.executable, self.SCRIPT, "--version"],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), __version__)

    def test_list_kopfzeile_nennt_version(self):
        out = self.run_gmf("--list").stdout
        self.assertIn(f"gitmaster_flash {__version__}", out.splitlines()[0])

    def test_json_traegt_version_und_root(self):
        d = json.loads(self.run_gmf("--json").stdout)
        self.assertEqual(d["version"], __version__)
        # resolve() auf beiden Seiten: auf macOS ist /var ein Symlink auf /private/var,
        # das Tool loest den Pfad auf — beide meinen dasselbe Verzeichnis.
        self.assertEqual(Path(d["root"]).resolve(), Path(self.tmp.name).resolve())
        self.assertIsInstance(d["repos"], list)

    def test_json_repo_felder_unveraendert(self):
        """Das Wrapper-Objekt darf die Repo-Eintraege selbst nicht veraendert haben."""
        d = json.loads(self.run_gmf("--json").stdout)
        self.assertTrue(d["repos"])
        for feld in ("rel", "path", "branch", "clean_and_synced", "remotes"):
            self.assertIn(feld, d["repos"][0])


if __name__ == "__main__":
    unittest.main()
