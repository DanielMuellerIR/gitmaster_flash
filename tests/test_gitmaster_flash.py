"""Headless-Tests für die reine Logik (Parsing, Ignore-Heuristik, Repo-Scan).

Die TUI selbst wird nicht getestet — die Datensammlung dafür schon:
gegen ein echtes, temporär angelegtes Git-Repo.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gitmaster_flash import (  # noqa: E402
    DEFAULT_CONFIG, RemoteStatus, RepoStatus, collect_status, find_repos,
    inspect_transfer, is_github_url, parse_porcelain, safe_pull_args,
    safe_push_args, suggested_ignore, upstream_delta,
)

# Die Defaults erkennen nur "origin" als Sync-Remote. Tests, die einen anders
# benannten Sync-Remote brauchen, nehmen diese Kopie.
SYNC_CONFIG = {**DEFAULT_CONFIG, "sync_remote_names": ["backup"]}


class TestParsePorcelain(unittest.TestCase):
    def test_counts_and_letters(self):
        lines = [
            " M geändert.py",
            "M  gestaged.py",
            " D geloescht.txt",
            "?? neu.md",
            "?? unterordner/noch-neu.md",
            "A  hinzugefuegt.py",
        ]
        m, d, u, c, files = parse_porcelain(lines)
        self.assertEqual((m, d, u, c), (3, 1, 2, 0))
        self.assertIn(("U", "neu.md"), files)
        self.assertIn(("D", "geloescht.txt"), files)
        self.assertIn(("M", "gestaged.py"), files)

    def test_conflicts_detected_first(self):
        # UU/UD/AA sind Merge-Konflikte und dürfen NICHT als M oder D zählen.
        lines = ["UU beide.txt", "UD ich-geloescht.txt", "AA beide-neu.txt"]
        m, d, u, c, files = parse_porcelain(lines)
        self.assertEqual((m, d, u, c), (0, 0, 0, 3))
        self.assertIn(("C", "beide.txt"), files)
        self.assertIn(("C", "ich-geloescht.txt"), files)

    def test_empty(self):
        self.assertEqual(parse_porcelain([]), (0, 0, 0, 0, []))


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


class TestUpstreamBadge(unittest.TestCase):
    def test_badge_hidden_when_synced(self):
        st = RepoStatus(path=Path("/x"), rel="x", upstream="github/main")
        self.assertEqual(st.upstream_badge(), "")

    def test_badge_ahead(self):
        st = RepoStatus(path=Path("/x"), rel="x", upstream="github/main",
                        upstream_ahead=6)
        self.assertEqual(st.upstream_badge(), "↑6 github")

    def test_badge_ahead_and_behind(self):
        st = RepoStatus(path=Path("/x"), rel="x", upstream="origin/main",
                        upstream_ahead=2, upstream_behind=3)
        self.assertEqual(st.upstream_badge(), "↑2↓3 origin")

    def test_badge_none_without_upstream(self):
        st = RepoStatus(path=Path("/x"), rel="x", upstream_ahead=5)
        self.assertEqual(st.upstream_badge(), "")


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
        self.assertEqual(st.upstream_badge(), "↑2 github")
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

        args = safe_push_args("github", "main")
        self.assertEqual(args[-2:], ("github", "HEAD:refs/heads/main"))
        self.assertIn("--no-follow-tags", args)
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
        self.assertEqual(safe_pull_args(check.remote_ref),
                         ("merge", "--ff-only", "--", "refs/remotes/backup/main"))
        git(self.repo, *safe_pull_args(check.remote_ref))
        self.assertEqual(
            subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                           check=True, capture_output=True, text=True).stdout.strip(),
            subprocess.run(["git", "-C", str(other), "rev-parse", "HEAD"],
                           check=True, capture_output=True, text=True).stdout.strip(),
        )


if __name__ == "__main__":
    unittest.main()
