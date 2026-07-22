"""Headless-Tests für die reine Logik (Parsing, Ignore-Heuristik, Repo-Scan).

Die TUI selbst wird nicht getestet — die Datensammlung dafür schon:
gegen ein echtes, temporär angelegtes Git-Repo.
"""

import json
import importlib.util
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gitmaster_flash as gmf_module  # noqa: E402

from gitmaster_flash import (  # noqa: E402
    DEFAULT_CONFIG, __version__, CommitSafetyError, RemoteStatus, RepoStatus, TUI,
    collect_status, find_repos,
    canonical_remote_target, cell_width, commit_selected, diff_status, _remote_root,
    detect_sync_remote, fetch_remote_status, inspect_transfer, is_github_url, pad_cells,
    parse_porcelain, safe_pull_args, stash_preview, status_dict,
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


class DisplayAndIntegrationSafetyTests(unittest.TestCase):
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
