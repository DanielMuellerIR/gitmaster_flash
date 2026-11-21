import curses
import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import get_args
from unittest import mock

from gmf_test_support import FakeScreen, git

import gitmaster_flash as gmf_module
from gitmaster_flash import (
    DEFAULT_CONFIG,
    TUI,
    RemoteStatus,
    build_info_view,
    canonical_remote_target,
    check_remote,
    classify_remote_check,
    collect_status,
    detect_sync_remote,
    display_remote_url,
    inspect_transfer,
    is_github_url,
    read_branches,
    remote_check_message,
    repo_info_lines,
    safe_push_args,
    status_dict,
    upstream_delta,
)

SYNC_CONFIG = {**DEFAULT_CONFIG, "sync_remote_names": ["backup"]}


class TestRemoteBadges(unittest.TestCase):
    def test_synced_remote_is_still_named(self):
        remote = RemoteStatus("backup", is_sync=True, branch_exists=True)
        self.assertEqual(remote.badge(), "backup")

    def test_delta_and_missing_branch(self):
        self.assertEqual(
            RemoteStatus("github", public=True, branch_exists=True, ahead=2, behind=3).badge(),
            "↑2↓3 github",
        )
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

    def test_non_ssh_userinfo_is_fully_redacted(self):
        separator = chr(58) + chr(47) * 2
        address = "ftp" + separator + "access-token@example.invalid/repo.git"
        shown = display_remote_url(address)
        self.assertEqual(shown, "ftp" + separator + "example.invalid/repo.git")
        self.assertNotIn("access-token", shown)
        error = gmf_module.redact_remote_error("fatal: " + address)
        self.assertNotIn("access-token", error)

    def test_local_file_url_remains_visible(self):
        separator = chr(58) + chr(47) * 2
        address = "file" + separator + "/tmp/example.git"
        self.assertEqual(display_remote_url(address), address)


class GithubWebUrlTests(unittest.TestCase):
    """Die anklickbare Web-Adresse darf die Info-Ansicht nie beenden."""

    def test_a_ref_byte_without_utf8_meaning_does_not_end_the_view(self):
        """`urllib.parse.quote()` lehnt Surrogate ab — hier ohne Rueckfallebene.

        Git erlaubt in einer Remote-Adresse Bytes ohne UTF-8-Bedeutung, die
        Leser dekodieren sie mit surrogateescape. `display_remote_url()` faengt
        das seit jeher ab; `github_web_urls()` tat es nicht, der Aufruf steht
        ausserhalb des try/except von `build_info_view()`, und
        `dispatch_action()` faengt nur TimeoutExpired: Ein Druck auf `I`
        beendete gmf mit einem Traceback (Review-Fund 2026-09-10).
        """
        broken = gmf_module.canonical_remote_target(
            "https://github.com/org/" + os.fsdecode(b"re\xffpo") + ".git"
        )
        remote = gmf_module.RemoteConfig(
            name="github",
            fetch_urls=["x"],
            push_urls=["x"],
            fetch_targets=[broken],
            push_targets=[broken],
        )
        self.assertTrue(broken.is_github)

        self.assertEqual(gmf_module.github_web_urls(remote), [])

    def test_a_normal_github_address_still_becomes_a_web_url(self):
        """Gegenprobe: Die Rueckfallebene darf den Normalfall nicht schlucken."""
        target = gmf_module.canonical_remote_target("git@github.com:example/demo.git")
        remote = gmf_module.RemoteConfig(
            name="github",
            fetch_urls=["x"],
            push_urls=["x"],
            fetch_targets=[target],
            push_targets=[target],
        )
        self.assertEqual(gmf_module.github_web_urls(remote), ["https://github.com/example/demo"])

    def test_percent_escapes_in_the_github_path_are_not_encoded_twice(self):
        for name, expected in (
            ("my%2Erepo", "my%2Erepo"),
            ("my%252Erepo", "my%252Erepo"),
            ("my%repo", "my%25repo"),
        ):
            with self.subTest(name=name):
                target = gmf_module.canonical_remote_target(
                    f"https://github.com/example/{name}.git"
                )
                remote = gmf_module.RemoteConfig(
                    name="github",
                    fetch_urls=[],
                    push_urls=[],
                    fetch_targets=[target],
                    push_targets=[],
                )
                self.assertEqual(
                    gmf_module.github_web_urls(remote), [f"https://github.com/example/{expected}"]
                )


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
        self.assertEqual((st.ahead, st.behind), (0, 0))  # mit backup synchron
        self.assertEqual(st.upstream, "github/main")
        self.assertEqual(st.upstream_ahead, 2)
        self.assertTrue(st.clean_and_synced)  # gilt trotzdem als sauber

        # Alle Remotes erscheinen, auch der synchrone Backup-Remote. Nach URL-
        # Klassifikation steht GitHub unabhängig vom Namen ganz rechts.
        git(self.repo, "remote", "set-url", "github", "https://github.com/example/demo.git")
        st = collect_status(self.repo, self.root, SYNC_CONFIG)
        self.assertEqual([r.name for r in st.remotes], ["backup", "github"])
        self.assertEqual([r.badge() for r in st.remotes], ["backup", "GitHub ≠ History ≠"])
        self.assertFalse(st.remotes[0].public)
        self.assertTrue(st.remotes[-1].public)

    def test_mixed_fetch_and_push_url_is_visible_and_blockable(self):
        git(self.repo, "remote", "set-url", "github", "https://github.com/example/demo.git")
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

    def test_ambiguous_upstream_tag_does_not_change_the_remote_or_counts(self):
        git(self.repo, "commit", "--allow-empty", "-qm", "ahead")
        git(self.repo, "tag", "github/main")
        self.assertEqual(upstream_delta(self.repo, "github", DEFAULT_CONFIG), (None, 0, 0))
        self.assertEqual(upstream_delta(self.repo, "backup", SYNC_CONFIG), ("github/main", 1, 0))

    def test_unicode_line_separator_in_a_remote_name_remains_part_of_the_ref(self):
        name = "mirror\u2028remote"
        git(self.repo, "remote", "rename", "github", name)
        git(self.repo, "commit", "--allow-empty", "-qm", "ahead")
        self.assertEqual(upstream_delta(self.repo, "backup", SYNC_CONFIG), (name + "/main", 1, 0))
        branches = read_branches(self.repo, DEFAULT_CONFIG, strict=True)
        self.assertEqual([(b.name, b.upstream_remote) for b in branches], [("main", name)])

    def test_push_preflight_and_explicit_safe_refspec(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        self.assertEqual(check.ahead, 1)
        self.assertEqual(check.commits[0].split(" ", 1)[1], "c2")
        self.assertEqual(check.files, ["M\ta.md"])
        self.assertEqual(check.branch, "main")
        self.assertEqual(
            check.head_oid,
            subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
        )
        self.assertTrue(check.index_oid)
        self.assertTrue(check.worktree_fingerprint)
        self.assertEqual(check.fetch_fingerprint, check.push_fingerprint)
        self.assertTrue(check.target_oid)

        args = safe_push_args(check.transfer_url, "main", check.head_oid, check.target_oid)
        self.assertRegex(args[-2], r"^gmf-pin-[0-9a-f]{32}://approved$")
        self.assertEqual(args[-1], f"{check.head_oid}:refs/heads/main")
        self.assertIn(str(self.gh), gmf_module.format_git_command(args))
        self.assertIn("--no-follow-tags", args)
        self.assertIn("--recurse-submodules=no", args)
        self.assertIn(f"--force-with-lease=refs/heads/main:{check.target_oid}", args)
        self.assertNotIn("--force", args)
        self.assertNotIn("--tags", args)
        pushed = gmf_module.run_git(self.repo, *args, timeout=10)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(
            subprocess.run(
                ["git", f"--git-dir={self.gh}", "rev-parse", "main"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
        )

    def test_fetch_observes_tracking_ref_before_requesting_the_remote_branch(self):
        remote = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["github"]
        tracked = git(self.repo, "rev-parse", "refs/remotes/github/main")
        calls = []

        def read(repo, *args, **kwargs):
            calls.append(args)
            output = {
                "rev-parse": tracked + "\n",
                "ls-remote": tracked + "\trefs/heads/main\n",
                "cat-file": "",
            }[args[0]]
            return subprocess.CompletedProcess(args, 0, output, "")

        with (
            mock.patch.object(gmf_module, "fetch_remote_block_reason", return_value=None),
            mock.patch.object(gmf_module, "read_remote_config_or_none", return_value=remote),
            mock.patch.object(gmf_module, "run_git", side_effect=read),
            mock.patch.object(
                gmf_module,
                "run_git_logged",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as logged,
        ):
            result = gmf_module.fetch_remote_safely(self.repo, remote, "main", 10)

        self.assertEqual(result.returncode, 0)
        self.assertEqual([args[0] for args in calls], ["rev-parse", "ls-remote", "cat-file"])
        self.assertEqual(logged.call_args.args[-3:], ("refs/remotes/github/main", tracked, tracked))

    def test_inherited_git_namespace_cannot_hide_a_safe_fetch(self):
        before = git(self.repo, "rev-parse", "HEAD")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", before],
            input="remote tip\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git(self.repo, "push", "-q", str(self.gh), f"{remote_tip}:refs/heads/main")
        remote = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["github"]

        with mock.patch.dict(os.environ, {"GIT_NAMESPACE": "review-ns"}):
            fetched = gmf_module.fetch_remote_safely(self.repo, remote, "main", 10)

        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "refs/remotes/github/main"), remote_tip)

    def test_a_ref_that_only_ends_in_the_branch_name_is_not_the_branch(self):
        """`ls-remote <muster>` trifft das ENDE eines Refs, nicht seinen Namen.

        Git vergleicht das Muster ab dem Anfang ODER ab einem Schraegstrich,
        und `--refs` laesst Tags stehen. Ein Remote ohne `refs/heads/main`,
        aber mit dem Tag `refs/tags/refs/heads/main`, lieferte deshalb genau
        eine Zeile — und gmf schrieb deren fremde Objekt-ID in den
        Tracking-Ref, ohne den Ref-Namen je anzusehen (an git 2.54
        nachgemessen). Richtig ist: Der Branch ist weg, also gehoert der
        Tracking-Ref weg.
        """
        stranger = git(self.repo, "rev-parse", "HEAD")
        git(self.gh, "tag", "refs/heads/main", "main")
        git(self.gh, "symbolic-ref", "HEAD", "refs/heads/other")
        git(self.gh, "branch", "other", "main")
        git(self.gh, "update-ref", "-d", "refs/heads/main")
        remote = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["github"]

        fetched = gmf_module.fetch_remote_safely(self.repo, remote, "main", 10)

        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        missing = gmf_module.run_git(
            self.repo, "rev-parse", "--verify", "-q", "refs/remotes/github/main", timeout=10
        )
        self.assertEqual(missing.returncode, 1, missing.stdout)
        self.assertNotEqual(missing.stdout.strip(), stranger)

    def test_cancelled_scan_still_rolls_back_tracking_if_the_remote_changes(self):
        ref = "refs/remotes/github/main"
        old_oid = git(self.repo, "rev-parse", ref)
        git(self.repo, "commit", "--allow-empty", "-qm", "remote update")
        git(self.repo, "push", "-q", "github", "main")
        new_oid = git(self.repo, "rev-parse", ref)
        git(self.repo, "update-ref", ref, old_oid, new_oid)
        approved = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["github"]
        scope = gmf_module.GitCallScope()
        run = gmf_module.run_git_logged

        def cancel_after_tracking_update(repo, *args, **kwargs):
            result = run(repo, *args, **kwargs)
            if "update-ref" in args and not scope.cancelled.is_set():
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(git(repo, "rev-parse", ref), new_oid)
                git(repo, "remote", "set-url", "github", str(self.bk))
                scope.cancel()
            return result

        with mock.patch.object(
            gmf_module, "run_git_logged", side_effect=cancel_after_tracking_update
        ):
            with self.assertRaises(gmf_module.RemoteConfigChangedError):
                scope.call(gmf_module.fetch_remote_safely, self.repo, approved, "main", 10)
        self.assertTrue(scope.cancelled.is_set())
        self.assertEqual(git(self.repo, "rev-parse", ref), old_oid)
        self.assertFalse(scope.processes)

    def test_another_ref_ending_in_the_branch_name_does_not_block_the_fetch(self):
        """Ein zweiter Treffer des Musters ist kein mehrdeutiger Branch.

        Solange nur die ZEILENZAHL geprueft wurde, machte ein Branch namens
        `x/refs/heads/main` auf der Gegenseite jeden Fetch dieses Remotes
        dauerhaft unmoeglich ("ambiguous remote branch"), obwohl
        `refs/heads/main` unveraendert da war.
        """
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        head = git(self.repo, "rev-parse", "HEAD")
        decoy = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", head],
            input="decoy\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git(self.repo, "push", "-q", str(self.gh), f"{decoy}:refs/heads/x/refs/heads/main")
        remote = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["github"]

        fetched = gmf_module.fetch_remote_safely(self.repo, remote, "main", 10)

        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertEqual(
            git(self.repo, "rev-parse", "refs/remotes/github/main"),
            git(self.gh, "rev-parse", "refs/heads/main"),
        )
        self.assertNotEqual(git(self.repo, "rev-parse", "refs/remotes/github/main"), decoy)

    def test_inherited_git_namespace_cannot_redirect_a_safe_push(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")

        with mock.patch.dict(os.environ, {"GIT_NAMESPACE": "review-ns"}):
            check = inspect_transfer(self.repo, "github", "main", "push")
            self.assertTrue(check.ready, check.reason)
            pushed = gmf_module.run_git(
                self.repo,
                *safe_push_args(check.transfer_url, check.branch, check.head_oid, check.target_oid),
                timeout=10,
            )

        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(
            subprocess.run(
                ["git", f"--git-dir={self.gh}", "rev-parse", "main"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            check.head_oid,
        )

    def test_successful_url_push_advances_the_tracking_ref_with_oid_cas(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        pushed = gmf_module.run_git(
            self.repo,
            *safe_push_args(check.transfer_url, check.branch, check.head_oid, check.target_oid),
            timeout=10,
        )
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        observed = git(self.repo, "rev-parse", check.remote_ref)
        self.assertIn(observed, (check.target_oid, check.head_oid))
        # Git kann einen zur URL passenden Tracking-Ref schon selbst nachziehen.
        # Für die eigene CAS den noch ausstehenden Zustand ausdrücklich herstellen.
        git(self.repo, "update-ref", "--no-deref", check.remote_ref, check.target_oid, observed)

        self.assertEqual(gmf_module.update_tracking_after_push(self.repo, check, 10), "ok")

        self.assertEqual(git(self.repo, "rev-parse", check.remote_ref), check.head_oid)
        self.assertEqual(
            inspect_transfer(self.repo, "github", "main", "push").reason, "nothing-push"
        )

    def test_tracking_update_never_dereferences_a_symbolic_ref(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        git(self.repo, "branch", "victim", check.target_oid)
        git(self.repo, "symbolic-ref", check.remote_ref, "refs/heads/victim")

        updated = gmf_module.update_tracking_after_push(self.repo, check, 10)

        self.assertEqual(updated, "ok")
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/victim"), check.target_oid)
        self.assertEqual(git(self.repo, "rev-parse", check.remote_ref), check.head_oid)
        symbolic = subprocess.run(
            ["git", "-C", str(self.repo), "symbolic-ref", "-q", check.remote_ref],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(symbolic.returncode, 0)

    def test_fetch_and_tracking_cas_do_not_run_reference_hooks(self):
        marker = self.root / "reference-hook-ran"
        hook = self.repo / ".git" / "hooks" / "reference-transaction"
        hook.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$1\" >> {shlex.quote(str(marker))}\n"
            f"cat >> {shlex.quote(str(marker))}\n"
        )
        hook.chmod(0o755)
        before = git(self.repo, "rev-parse", "HEAD")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", before],
            input="remote update\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git(self.repo, "push", "-q", str(self.gh), f"{remote_tip}:refs/heads/main")
        marker.unlink(missing_ok=True)  # nur den folgenden gmf-Fetch messen
        config = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["github"]

        fetched = gmf_module.fetch_remote_safely(self.repo, config, "main", 10)
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")

        # Der Nachzug nach einem URL-gebundenen Push benutzt denselben
        # hookfreien, nicht dereferenzierenden Ref-CAS.
        git(self.repo, "merge", "--ff-only", "-q", "github/main")
        (self.repo / "a.md").write_text("local push\n")
        git(self.repo, "commit", "-qam", "local push")
        marker.unlink(missing_ok=True)  # der normale Benutzer-Commit darf Hooks nutzen
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready, check.reason)
        pushed = gmf_module.run_git(
            self.repo,
            *safe_push_args(check.transfer_url, check.branch, check.head_oid, check.target_oid),
            timeout=10,
        )
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(gmf_module.update_tracking_after_push(self.repo, check, 10), "ok")
        self.assertFalse(marker.exists())

    def test_tracking_ref_is_rolled_back_if_remote_config_changes_after_push(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        original = gmf_module.run_git_logged
        changed = False

        def change_config_before_update(repo, *args, **kwargs):
            nonlocal changed
            if "update-ref" in args and not changed:
                changed = True
                git(self.repo, "remote", "set-url", "github", str(self.bk))
                git(self.repo, "remote", "set-url", "--push", "github", str(self.bk))
            return original(repo, *args, **kwargs)

        with mock.patch.object(
            gmf_module, "run_git_logged", side_effect=change_config_before_update
        ):
            updated = gmf_module.update_tracking_after_push(self.repo, check, 10)

        self.assertTrue(changed)
        # Nicht bloss "nicht ok": Am Tracking-Ref hat sich nichts geaendert,
        # die geaenderte Remote-Konfiguration ist der Grund. Die Oberflaeche
        # nannte dafuer lange denselben Satz wie fuer einen fremden Zugriff.
        self.assertEqual(updated, "changed")
        self.assertEqual(git(self.repo, "rev-parse", check.remote_ref), check.target_oid)

    def test_failed_tracking_rollback_reports_the_observed_outcome(self):
        for text in ("2\n", "3\n"):
            (self.repo / "a.md").write_text(text)
            git(self.repo, "commit", "-qam", "c" + text.strip())
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        foreign = git(self.repo, "rev-parse", "HEAD~1")
        original = gmf_module.run_git_logged
        config_file = self.repo / ".git" / "config"
        config_text = config_file.read_text()
        lock = self.repo / ".git" / (check.remote_ref + ".lock")
        for cause, expected in (("foreign", "raced"), ("lock", "unknown")):
            with self.subTest(cause=cause):
                config_file.write_text(config_text)
                git(self.repo, "update-ref", check.remote_ref, check.target_oid)
                results = []

                def interfere(repo, *args, **kwargs):
                    if not results:
                        git(self.repo, "remote", "set-url", "github", str(self.bk))
                    elif cause == "foreign":
                        git(self.repo, "update-ref", check.remote_ref, foreign)
                    else:
                        lock.write_text("test lock\n")
                    result = original(repo, *args, **kwargs)
                    results.append(result.returncode)
                    return result

                try:
                    with mock.patch.object(gmf_module, "run_git_logged", side_effect=interfere):
                        outcome = gmf_module.update_tracking_after_push(self.repo, check, 10)
                finally:
                    lock.unlink(missing_ok=True)
                self.assertEqual(results[0], 0)
                self.assertNotEqual(results[1], 0)
                self.assertEqual(outcome, expected)
                self.assertEqual(
                    git(self.repo, "rev-parse", check.remote_ref),
                    foreign if cause == "foreign" else check.head_oid,
                )

    def test_tracking_cas_failure_never_accepts_a_changed_remote_config(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)

        def concurrent_fetch_and_config_change(_repo, *_args, **_kwargs):
            git(self.repo, "update-ref", check.remote_ref, check.head_oid)
            git(self.repo, "remote", "set-url", "github", str(self.bk))
            git(self.repo, "remote", "set-url", "--push", "github", str(self.bk))
            return subprocess.CompletedProcess([], 1, "", "changed")

        with mock.patch.object(
            gmf_module, "run_git_logged", side_effect=concurrent_fetch_and_config_change
        ):
            updated = gmf_module.update_tracking_after_push(self.repo, check, 10)

        self.assertEqual(updated, "changed")

    def test_a_foreign_tracking_ref_change_is_named_as_such(self):
        """Gegenprobe: Genau EIN Fall heisst wirklich "parallel geaendert".

        Alle vier Ausgaenge lieferten bis 2026-09-03 ein `False`, und die
        Oberflaeche nannte dafuer immer den fremden Zugriff als Grund. Hier ist
        er der richtige — und die Faelle "Konfiguration geaendert" und
        "Ausgang unklar" sind daneben getestet.
        """
        for text in ("2\n", "3\n"):
            (self.repo / "a.md").write_text(text)
            git(self.repo, "commit", "-qam", "c" + text.strip())
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready, check.reason)
        # Ein anderer Prozess setzt den Tracking-Ref auf einen dritten Stand:
        # weder unser Push-Ziel noch der beim Preflight gesehene Wert.
        fremd = git(self.repo, "rev-parse", "HEAD~1")
        self.assertNotIn(fremd, (check.head_oid, check.target_oid))
        git(self.repo, "update-ref", check.remote_ref, fremd)

        self.assertEqual(gmf_module.update_tracking_after_push(self.repo, check, 10), "raced")
        # Der fremde Stand bleibt unangetastet.
        self.assertEqual(git(self.repo, "rev-parse", check.remote_ref), fremd)

    def test_public_preview_keeps_files_added_then_deleted_in_outgoing_history(self):
        secret = self.repo / "secret.env"
        secret.write_text("TOKEN=not-for-publication\n")
        git(self.repo, "add", "secret.env")
        git(self.repo, "commit", "-qm", "add private draft")
        secret.unlink()
        git(self.repo, "add", "-u")
        git(self.repo, "commit", "-qm", "remove private draft")

        check = inspect_transfer(self.repo, "github", "main", "push")

        self.assertTrue(check.ready)
        self.assertEqual(check.ahead, 2)
        self.assertTrue(any(line.endswith("\tsecret.env") for line in check.files), check.files)

    def test_public_preview_never_hides_an_outgoing_gitlink_update(self):
        nested = self.repo / "vendor" / "sub"
        nested.mkdir(parents=True)
        git(nested, "init", "-q", "-b", "main")
        git(nested, "config", "user.email", "t@example.invalid")
        git(nested, "config", "user.name", "T")
        (nested / "module.txt").write_text("one\n")
        git(nested, "add", "module.txt")
        git(nested, "commit", "-qm", "module base")
        git(self.repo, "add", "vendor/sub")
        git(self.repo, "commit", "-qm", "add module pointer")
        git(self.repo, "config", "diff.ignoreSubmodules", "all")
        git(self.repo, "config", "submodule.vendor/sub.ignore", "all")

        check = inspect_transfer(self.repo, "github", "main", "push")

        self.assertTrue(check.ready)
        self.assertTrue(any(line.endswith("\tvendor/sub") for line in check.files), check.files)

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

    def test_transfer_preflight_cannot_hide_untracked_files_via_config(self):
        git(self.repo, "config", "status.showUntrackedFiles", "no")
        (self.repo / "untracked-secret.txt").write_text("not approved\n")

        self.assertEqual(inspect_transfer(self.repo, "github", "main", "push").reason, "dirty")

    def test_scan_and_transfer_cannot_hide_a_dirty_submodule_via_config(self):
        source = self.root / "module-source"
        source.mkdir()
        git(source, "init", "-qb", "main")
        (source / "module.txt").write_text("base\n")
        git(source, "add", "module.txt")
        git(
            source,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-qm",
            "module base",
        )
        git(
            self.repo, "-c", "protocol.file.allow=always", "submodule", "add", str(source), "module"
        )
        git(self.repo, "commit", "-qm", "add module")
        git(self.repo, "push", "-q", "backup", "main:main")
        git(self.repo, "branch", "incoming")
        git(self.repo, "switch", "-q", "incoming")
        git(self.repo, "commit", "--allow-empty", "-qm", "incoming change")
        git(self.repo, "push", "-q", "backup", "incoming:main")
        git(self.repo, "switch", "-q", "main")
        git(self.repo, "config", "diff.ignoreSubmodules", "all")
        module_file = self.repo / "module" / "module.txt"

        def transfer(remote, action):
            return inspect_transfer(self.repo, remote, "main", action)

        for remote, action in (("github", "push"), ("backup", "pull")):
            with self.subTest(action=action):
                self.assertTrue(transfer(remote, action).ready)
                module_file.write_text("dirty\n")
                status = collect_status(self.repo, self.root, SYNC_CONFIG)
                self.assertTrue(status.dirty)
                self.assertEqual(status.modified, 1)
                self.assertEqual(transfer(remote, action).reason, "dirty")
                with self.assertRaises(gmf_module.GitReadError):
                    gmf_module.stash_target(self.repo, 10, clean=True)
                module_file.write_text("base\n")

                original = gmf_module.run_git

                def modify_before_final_status(repo, *args, **kwargs):
                    if args[:2] == ("log", "--oneline"):
                        module_file.write_text("dirty during preflight\n")
                    return original(repo, *args, **kwargs)

                with mock.patch.object(
                    gmf_module, "run_git", side_effect=modify_before_final_status
                ):
                    self.assertEqual(transfer(remote, action).reason, "inspect-failed")
                module_file.write_text("base\n")

    def test_transfer_preflight_binds_the_expected_public_class(self):
        git(self.repo, "remote", "set-url", "github", "https://github.com/example/demo.git")
        self.assertEqual(
            inspect_transfer(self.repo, "github", "main", "push", expected_public=False).reason,
            "remote-unsafe",
        )
        self.assertEqual(
            inspect_transfer(self.repo, "backup", "main", "push", expected_public=True).reason,
            "remote-unsafe",
        )

    def test_transfer_preflight_fails_closed_on_an_unresolvable_local_url(self):
        git(self.repo, "remote", "set-url", "github", "~gmf-user-that-does-not-exist/repo.git")

        check = inspect_transfer(self.repo, "github", "main", "push")

        self.assertEqual(check.reason, "remote-unsafe")

    def test_transfer_preflight_handles_a_non_utf8_dirty_filename(self):
        original = gmf_module.run_git

        def non_utf8_status(repo, *args, **kwargs):
            if args[:2] == ("status", "--porcelain=v1"):
                return subprocess.CompletedProcess(["git", *args], 0, "?? dirty-\udcff\0", "")
            return original(repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git", side_effect=non_utf8_status):
            check = inspect_transfer(self.repo, "github", "main", "push")

        self.assertEqual(check.reason, "dirty")

    def test_transfer_preflight_rejects_a_checkout_between_ref_and_oid_reads(self):
        (self.repo / "a.md").write_text("main ahead\n")
        git(self.repo, "commit", "-qam", "main ahead")
        git(self.repo, "branch", "other", "HEAD^")
        original = gmf_module.run_git
        switched = False

        def race(repo, *args, **kwargs):
            nonlocal switched
            result = original(repo, *args, **kwargs)
            if args[:3] == ("symbolic-ref", "-q", "HEAD") and not switched:
                switched = True
                git(self.repo, "switch", "-q", "other")
            return result

        with mock.patch.object(gmf_module, "run_git", side_effect=race):
            check = inspect_transfer(self.repo, "github", "main", "push")

        self.assertTrue(switched)
        self.assertEqual(check.reason, "inspect-failed")

    def test_inherited_graft_cannot_turn_divergence_into_a_safe_push(self):
        base = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (self.repo / "a.md").write_text("local\n")
        git(self.repo, "commit", "-qam", "local")
        local = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", base + "^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        remote = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", base, "-m", "remote"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git(self.repo, "update-ref", "refs/remotes/github/main", remote)
        graft = self.root / "grafts"
        graft.write_text(f"{local} {remote}\n")
        inherited = {**os.environ, "GIT_GRAFT_FILE": str(graft)}
        fooled = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "rev-list",
                "--left-right",
                "--count",
                f"{local}...{remote}",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=inherited,
        ).stdout.split()
        self.assertEqual(fooled, ["1", "0"])

        with mock.patch.dict(os.environ, {"GIT_GRAFT_FILE": str(graft)}):
            self.assertEqual(
                inspect_transfer(self.repo, "github", "main", "push").reason, "divergent"
            )

    def test_repository_graft_cannot_turn_divergence_into_a_safe_push(self):
        base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "a.md").write_text("local\n")
        git(self.repo, "commit", "-qam", "local")
        local = git(self.repo, "rev-parse", "HEAD")
        tree = git(self.repo, "rev-parse", base + "^{tree}")
        remote = git(self.repo, "commit-tree", tree, "-p", base, "-m", "remote")
        git(self.repo, "update-ref", "refs/remotes/github/main", remote)
        grafts = self.repo / ".git" / "info" / "grafts"
        grafts.write_text(f"{local} {remote}\n")
        fooled = git(
            self.repo, "rev-list", "--left-right", "--count", f"{local}...{remote}"
        ).split()
        self.assertEqual(fooled, ["1", "0"])

        self.assertEqual(inspect_transfer(self.repo, "github", "main", "push").reason, "divergent")

    def test_transfer_rejects_a_refspec_that_maps_dev_to_main(self):
        git(self.repo, "config", "--unset-all", "remote.github.fetch")
        git(
            self.repo,
            "config",
            "--add",
            "remote.github.fetch",
            "+refs/heads/dev:refs/remotes/github/main",
        )
        before = git(self.repo, "rev-parse", "HEAD")

        check = inspect_transfer(self.repo, "github", "main", "push")

        self.assertEqual(check.reason, "remote-unsafe")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)

    def test_push_stays_bound_to_the_approved_url_after_config_changes(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        backup_before = git(self.bk, "rev-parse", "refs/heads/main")
        git(self.repo, "remote", "set-url", "--push", "github", str(self.bk))
        git(self.repo, "config", f"url.{self.bk}.pushInsteadOf", check.transfer_url)

        pushed = gmf_module.run_git(
            self.repo,
            *safe_push_args(check.transfer_url, check.branch, check.head_oid, check.target_oid),
            timeout=10,
        )
        self.assertEqual(pushed.returncode, 0, pushed.stderr)

        self.assertEqual(git(self.gh, "rev-parse", "refs/heads/main"), check.head_oid)
        self.assertEqual(git(self.bk, "rev-parse", "refs/heads/main"), backup_before)

    def test_pinned_push_handles_equals_and_beats_an_exact_rewrite_rule(self):
        destination = self.root / "archive=copy.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.gh), str(destination)],
            check=True,
            capture_output=True,
            text=True,
        )
        before = git(destination, "rev-parse", "refs/heads/main")
        backup_before = git(self.bk, "rev-parse", "refs/heads/main")
        (self.repo / "a.md").write_text("equals-safe\n")
        git(self.repo, "commit", "-qam", "equals-safe")
        head = git(self.repo, "rev-parse", "HEAD")
        # Eine gleich lange, bereits konfigurierte Regel darf nicht gewinnen.
        git(self.repo, "config", f"url.{self.bk}.insteadOf", str(destination))

        args = safe_push_args(str(destination), "main", head, before)
        pushed = gmf_module.run_git(self.repo, *args, timeout=10)

        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(git(destination, "rev-parse", "main"), head)
        self.assertEqual(git(self.bk, "rev-parse", "main"), backup_before)

    def test_safe_push_does_not_run_a_pre_push_hook(self):
        marker = self.root / "pre-push-ran"
        hook = self.repo / ".git" / "hooks" / "pre-push"
        git(self.repo, "tag", "not-approved")
        hook.write_text(
            "#!/bin/sh\n"
            f"printf ran > {shlex.quote(str(marker))}\n"
            'git push "$2" refs/tags/not-approved\n'
        )
        hook.chmod(0o755)
        (self.repo / "a.md").write_text("hook-safe\n")
        git(self.repo, "commit", "-qam", "hook-safe")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)

        pushed = gmf_module.run_git(
            self.repo,
            *safe_push_args(check.transfer_url, check.branch, check.head_oid, check.target_oid),
            timeout=10,
        )

        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertFalse(marker.exists())
        tag = subprocess.run(
            [
                "git",
                f"--git-dir={self.gh}",
                "show-ref",
                "--verify",
                "--quiet",
                "refs/tags/not-approved",
            ]
        )
        self.assertNotEqual(tag.returncode, 0)

    def test_safe_push_clears_push_options_and_disables_signing(self):
        option_marker = self.root / "received-push-options"
        gpg_marker = self.root / "gpg-ran"
        receive_hook = self.gh / "hooks" / "pre-receive"
        receive_hook.write_text(
            "#!/bin/sh\n"
            f"printf '%s:%s\\n' \"${{GIT_PUSH_OPTION_COUNT-unset}}\" "
            f'"${{GIT_PUSH_OPTION_0-unset}}" > '
            f"{shlex.quote(str(option_marker))}\n"
        )
        receive_hook.chmod(0o755)
        git(self.gh, "config", "receive.advertisePushOptions", "true")
        git(self.gh, "config", "receive.certNonceSeed", "test-seed")
        fake_gpg = self.root / "fake-gpg"
        fake_gpg.write_text(f"#!/bin/sh\nprintf ran > {shlex.quote(str(gpg_marker))}\nexit 1\n")
        fake_gpg.chmod(0o755)
        git(self.repo, "config", "push.pushOption", "deploy=all")
        git(self.repo, "config", "push.gpgSign", "true")
        git(self.repo, "config", "gpg.program", str(fake_gpg))
        (self.repo / "a.md").write_text("push config safe\n")
        git(self.repo, "commit", "-qam", "push config safe")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready, check.reason)

        args = safe_push_args(check.transfer_url, check.branch, check.head_oid, check.target_oid)
        pushed = gmf_module.run_git(self.repo, *args, timeout=10)

        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(option_marker.read_text(), "0:unset\n")
        self.assertFalse(gpg_marker.exists())
        self.assertIn("push.pushOption=", args)
        self.assertIn("--no-signed", args)

    def test_local_symlink_switch_cannot_change_the_approved_push_target(self):
        link = self.root / "selected.git"
        link.symlink_to(self.gh)
        git(self.repo, "remote", "set-url", "github", str(link))
        git(self.repo, "remote", "set-url", "--push", "github", str(link))
        (self.repo / "a.md").write_text("symlink-safe\n")
        git(self.repo, "commit", "-qam", "symlink-safe")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        self.assertEqual(check.transfer_url, str(self.gh.resolve()))
        backup_before = git(self.bk, "rev-parse", "main")
        link.unlink()
        link.symlink_to(self.bk)

        pushed = gmf_module.run_git(
            self.repo,
            *safe_push_args(check.transfer_url, check.branch, check.head_oid, check.target_oid),
            timeout=10,
        )

        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(git(self.gh, "rev-parse", "main"), check.head_oid)
        self.assertEqual(git(self.bk, "rev-parse", "main"), backup_before)

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
        result = gmf_module.run_git(
            self.repo,
            *safe_push_args(check.transfer_url, "main", check.head_oid, check.target_oid),
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        missing = subprocess.run(
            ["git", f"--git-dir={self.gh}", "show-ref", "--verify", "--quiet", "refs/heads/main"]
        )
        self.assertNotEqual(missing.returncode, 0)


class ShallowCloneTransferTests(unittest.TestCase):
    """In einem flachen Klon (`git clone --depth`) muessen P und G gehen.

    `RAW_OBJECT_ENV` stellt mit `GIT_SHALLOW_FILE=/dev/null` genau die Datei
    blind, in der die Graph-Grenze eines flachen Klons steht. Damit lief
    `rev-list --left-right --count <head>...<target>` ueber die Grenze hinaus in
    fehlende Eltern-Objekte und endete mit Exit 128: `inspect_transfer()`
    meldete `inspect-failed`, und die Oberflaeche sagte nur "Pruefung
    fehlgeschlagen", ohne einen Grund zu nennen. Der Transferpfad benutzt
    deshalb `TRANSFER_OBJECT_ENV` (Replace- und Graft-Sperre, aber die echte
    Shallow-Datei).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        seed = self.root / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        git(seed, "config", "user.email", "t@example.invalid")
        git(seed, "config", "user.name", "T")
        for n in range(5):
            (seed / f"f{n}.txt").write_text(f"{n}\n")
            git(seed, "add", f"f{n}.txt")
            git(seed, "commit", "-qm", f"c{n}")
        self.bare = self.root / "upstream.git"
        git(self.root, "init", "-q", "-b", "main", "--bare", str(self.bare))
        git(seed, "push", "-q", str(self.bare), "main")
        self.repo = self.root / "work"
        git(self.root, "clone", "-q", "--depth", "1", f"file://{self.bare}", str(self.repo))
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        # Ohne diese Datei ist der Klon nicht flach und der Test wertlos.
        self.assertTrue((self.repo / ".git" / "shallow").exists())

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_preflight_sees_the_outgoing_commit_instead_of_failing(self):
        (self.repo / "neu.txt").write_text("neu\n")
        git(self.repo, "add", "neu.txt")
        git(self.repo, "commit", "-qm", "lokal")

        check = inspect_transfer(self.repo, "origin", "main", "push")

        self.assertEqual(check.reason, "ready")
        self.assertEqual((check.ahead, check.behind), (1, 0))
        # Die Vorschau nennt genau den einen ausgehenden Commit und seine Datei.
        self.assertEqual(len(check.commits), 1)
        self.assertIn("lokal", check.commits[0])
        self.assertEqual([line.split("\t")[-1] for line in check.files], ["neu.txt"])

    def test_push_fetch_and_scan_all_work_and_keep_the_shallow_boundary(self):
        (self.repo / "neu.txt").write_text("neu\n")
        git(self.repo, "add", "neu.txt")
        git(self.repo, "commit", "-qm", "lokal")
        check = inspect_transfer(self.repo, "origin", "main", "push")
        self.assertTrue(check.ready, check.reason)

        pushed = gmf_module.run_git(
            self.repo,
            *safe_push_args(check.transfer_url, check.branch, check.head_oid, check.target_oid),
            timeout=30,
            env=gmf_module.TRANSFER_OBJECT_ENV,
        )
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(gmf_module.update_tracking_after_push(self.repo, check, 10), "ok")

        remote = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["origin"]
        fetched = gmf_module.fetch_remote_safely(self.repo, remote, "main", 20)
        self.assertEqual(fetched.returncode, 0, fetched.stderr)

        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.error, "")
        self.assertEqual((st.ahead, st.behind), (0, 0))
        # Der Klon bleibt flach: gmf darf seine Graph-Grenze nicht einreissen.
        self.assertTrue((self.repo / ".git" / "shallow").exists())


class RemoteSecurityTests(unittest.TestCase):
    def test_endpoint_fingerprint_omits_credentials_query_and_git_suffix(self):
        a = canonical_remote_target("https://user:secret@example.com/org/repo.git?token=x")
        b = canonical_remote_target("https://example.com/org/repo")
        self.assertEqual(a, b)
        self.assertEqual(a.host, "example.com")
        self.assertNotIn("secret", a.fingerprint)

    def test_the_default_port_falls_away_in_every_spelling(self):
        """Der Standardport hing an der Rohschreibweise der Adresse.

        `default_port` verglich `raw.startswith("ssh://")`, waehrend der Rest
        der Funktion das kleingeschriebene Schema benutzt. `SSH://host:22/x`
        galt dadurch als anderes Ziel als `ssh://host/x`, und eine voellig
        gewoehnliche Konfiguration war ohne Not blockiert. `git+ssh`, `ssh+git`
        und `git://` kannte die Liste ueberhaupt nicht.
        """
        for plain, loud in (
            ("ssh://example.com/a/b", "SSH://example.com:22/a/b"),
            ("https://example.com/a/b", "HTTPS://example.com:443/a/b"),
            ("http://example.com/a/b", "HTTP://example.com:80/a/b"),
            ("git+ssh://example.com/a/b", "git+ssh://example.com:22/a/b"),
            ("git://example.com/a/b", "git://example.com:9418/a/b"),
            ("file:///tmp/a/b", "FILE:///tmp/a/b"),
        ):
            with self.subTest(url=loud):
                self.assertEqual(
                    canonical_remote_target(plain).fingerprint,
                    canonical_remote_target(loud).fingerprint,
                )

    def test_a_non_default_port_still_separates_two_targets(self):
        """Gegenprobe: Nur der Standardport faellt weg."""
        self.assertNotEqual(
            canonical_remote_target("ssh://example.com/a/b").fingerprint,
            canonical_remote_target("ssh://example.com:2222/a/b").fingerprint,
        )

    def test_an_option_like_address_never_counts_as_argv_safe(self):
        """Eine Adresse mit fuehrendem "-" liest Git als Option."""
        for url in ("--upload-pack=/bin/sh", "-oProxyCommand=x", "-"):
            with self.subTest(url=url):
                self.assertFalse(gmf_module._argv_safe_remote_url(url))
        self.assertTrue(gmf_module._argv_safe_remote_url("https://example.com/a/b"))

    def test_reserved_percent_escapes_do_not_alias_another_network_path(self):
        encoded_slash = canonical_remote_target("https://example.com/org%2Frepo.git")
        literal_slash = canonical_remote_target("https://example.com/org/repo.git")
        encoded_parent = canonical_remote_target("https://example.com/org/%2e%2e/private.git")
        literal_parent = canonical_remote_target("https://example.com/org/../private.git")
        self.assertNotEqual(encoded_slash, literal_slash)
        self.assertNotEqual(encoded_parent, literal_parent)

    def test_ipv6_host_and_port_have_an_unambiguous_identity(self):
        with_port = canonical_remote_target("ssh://[::1]:2222/repo.git")
        host_text_ending_in_port = canonical_remote_target("https://[::1:2222]/repo.git")
        self.assertNotEqual(with_port, host_text_ending_in_port)

    def test_network_path_segments_and_tilde_semantics_stay_distinct(self):
        self.assertNotEqual(
            canonical_remote_target("https://example.com/org//repo.git"),
            canonical_remote_target("ssh://example.com/org/repo.git"),
        )
        self.assertNotEqual(
            canonical_remote_target("https://example.com/~/repo.git"),
            canonical_remote_target("ssh://example.com/~/repo.git"),
        )
        separator = chr(58) + chr(47) * 2
        alice = "ssh" + separator + "alice@example.com/~/repo.git"
        bob = "ssh" + separator + "bob@example.com/~/repo.git"
        self.assertNotEqual(canonical_remote_target(alice), canonical_remote_target(bob))

    def test_github_host_must_match_exactly(self):
        self.assertTrue(is_github_url("ssh://git@github.com/org/repo.git"))
        self.assertFalse(is_github_url("ssh://github.com.attacker.invalid/org/repo.git"))
        self.assertFalse(is_github_url("https://example.invalid/github.com/org/repo"))

    def test_scp_user_belongs_to_the_identity_of_home_relative_paths(self):
        """Relative SCP-Pfade liegen im Home des SSH-Benutzers.

        alice@host:repo und bob@host:repo sind also verschiedene Repositories —
        gälten sie als identisch, könnte ein "sicherer" Push im Repo des falschen
        Benutzers landen.
        """
        alice = canonical_remote_target("alice@host:repo.git")
        bob = canonical_remote_target("bob@host:repo.git")
        self.assertNotEqual(alice, bob)
        # Derselbe Benutzer, dieselbe Schreibweise: natürlich identisch.
        self.assertEqual(alice, canonical_remote_target("alice@host:repo.git"))
        # Der Benutzername bleibt trotzdem aus der Anzeige-Identität heraus.
        self.assertEqual(alice.repo_id, "/repo")

    def test_scp_relative_and_absolute_paths_stay_distinct(self):
        # host:repo wird im Remote-Home aufgelöst, host:/repo absolut — zwei Ziele.
        relative = canonical_remote_target("host:repo.git")
        absolute = canonical_remote_target("host:/repo.git")
        self.assertNotEqual(relative, absolute)
        # Tilde-Pfade hängen ebenfalls am Benutzer.
        self.assertNotEqual(
            canonical_remote_target("alice@host:~/repo.git"),
            canonical_remote_target("bob@host:~/repo.git"),
        )

    def test_scp_github_form_still_maps_to_the_web_url_path(self):
        # git@github.com:org/repo ist formal home-relativ; die Web-URL-Ableitung
        # (repo_id) muss davon unberührt bleiben.
        target = canonical_remote_target("git@github.com:example/demo.git")
        self.assertTrue(target.is_github)
        self.assertEqual(target.repo_id, "/example/demo")

    def test_scp_git_user_matches_the_https_form_of_the_same_repo(self):
        """Fetch per HTTPS, Push per SSH — der Standardfall bei GitHub & Co.

        Der virtuelle SSH-Benutzer "git" hat kein privates Home-Verzeichnis:
        sein SCP-Pfad ist derselbe Namensraum wie der HTTPS-Pfad. Vorher galten
        beide URLs als verschiedene Ziele (transfer_safe False), und P/G
        verweigerten die Übertragung, obwohl sie dasselbe Repo meinen.
        """
        ssh = canonical_remote_target("git@github.com:example/demo.git")
        https = canonical_remote_target("https://github.com/example/demo.git")
        self.assertEqual(ssh, https)
        # Ein ausdrückliches "~" bleibt benutzerabhängig — auch beim Benutzer git.
        self.assertNotEqual(
            canonical_remote_target("git@host:~/repo.git"),
            canonical_remote_target("host:/repo.git"),
        )
        # Andere Benutzernamen bleiben home-relativ und damit eigene Ziele.
        self.assertNotEqual(
            canonical_remote_target("alice@host:repo.git"),
            canonical_remote_target("https://host/repo.git"),
        )

    def test_https_fetch_with_ssh_push_is_transfer_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "base")
            git(repo, "remote", "add", "origin", "https://github.com/example/one.git")
            git(repo, "remote", "set-url", "--push", "origin", "git@github.com:example/one.git")
            st = collect_status(repo, root, DEFAULT_CONFIG)
            remote = st.remotes[0]
            self.assertFalse(remote.target_mismatch)
            self.assertTrue(remote.transfer_safe)

    def test_sync_host_must_match_exactly(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(
                repo,
                "remote",
                "add",
                "mirror",
                "ssh://trusted.example.attacker.invalid/org/repo.git",
            )
            cfg = {
                **DEFAULT_CONFIG,
                "sync_remote_names": [],
                "sync_remote_hosts": ["trusted.example"],
            }
            self.assertIsNone(detect_sync_remote(repo, cfg))

    def test_same_class_different_push_target_is_blocked_and_fingerprinted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "base")
            git(repo, "remote", "add", "origin", "https://github.com/example/one.git")
            git(repo, "remote", "set-url", "--push", "origin", "https://github.com/example/two.git")
            st = collect_status(repo, root, DEFAULT_CONFIG)
            remote = st.remotes[0]
            self.assertTrue(remote.target_mismatch)
            self.assertFalse(remote.transfer_safe)
            payload = status_dict(st)["remotes"][0]
            self.assertTrue(payload["fetch_fingerprint"])
            self.assertEqual(payload["fetch_fingerprints"], [payload["fetch_fingerprint"]])
            self.assertTrue(payload["push_fingerprints"])
            self.assertNotIn("github.com", json.dumps(payload))

    def test_multiple_pushurls_are_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bare = root / "bare.git"
            bare.mkdir()
            git(bare, "init", "-q", "--bare")
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "base")
            git(repo, "remote", "add", "origin", str(bare))
            git(repo, "push", "-qu", "origin", "main")
            git(repo, "remote", "set-url", "--add", "--push", "origin", str(bare))
            git(repo, "remote", "set-url", "--add", "--push", "origin", str(root / "other.git"))
            self.assertEqual(
                inspect_transfer(repo, "origin", "main", "push").reason, "remote-unsafe"
            )


class HostNamedLocalTests(unittest.TestCase):
    """Ein Remote-Host, der `local` heißt, ist kein lokaler Ordner.

    `local` war bis 2026-09-03 der Merkwert für Pfad-Remotes und stand dafür im
    Feld `host` — dabei ist `local` ein völlig gültiger Hostname, etwa als
    ssh-Alias in ~/.ssh/config. Ein Remote `ssh://local/srv/repo.git` galt
    dadurch als lokaler Ordner: gmf holte und pushte in das Verzeichnis
    `/srv/repo.git` DIESES Rechners, während Git selbst den Server angesprochen
    hätte. Ein als zielgebunden bestätigter Push landete also woanders.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.bare = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", str(self.bare))
        self.repo = self.root / "work"
        git(self.root, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "a.txt").write_text("a\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-qm", "eins")
        git(self.repo, "remote", "add", "origin", str(self.bare))
        git(self.repo, "push", "-q", "-u", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        # Erst jetzt auf den Netz-Host umstellen: Der Tracking-Ref steht, der
        # Vergleich unten kommt daher ohne Netz aus.
        self.url = "https://local" + str(self.bare)
        git(self.repo, "remote", "set-url", "origin", self.url)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "zwei")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_network_host_named_local_is_not_a_path(self):
        target = canonical_remote_target("ssh://local/srv/repo.git")
        self.assertFalse(target.is_local)
        self.assertEqual(target.host, "local")
        # Und es ist ein ANDERES Ziel als der gleichnamige Ordner hier.
        self.assertNotEqual(target.fingerprint, canonical_remote_target("/srv/repo").fingerprint)

    def test_a_path_remote_carries_no_host_at_all(self):
        for spelling in (str(self.bare), "file://" + str(self.bare)):
            target = canonical_remote_target(spelling)
            self.assertTrue(target.is_local, spelling)
            self.assertEqual(target.host, "", spelling)
            self.assertFalse(target.is_github, spelling)

    def test_transfer_addresses_stay_the_configured_url(self):
        remote = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["origin"]
        self.assertEqual(gmf_module.remote_fetch_url(remote), self.url)
        self.assertEqual(gmf_module.remote_push_url(remote), self.url)
        check = inspect_transfer(self.repo, "origin", "main", "push")
        self.assertEqual(check.reason, "ready")
        # Der entscheidende Punkt: kein Dateipfad. Vorher stand hier
        # str(self.bare) — der Push wäre in diesen Ordner gelaufen.
        self.assertEqual(check.transfer_url, self.url)
        self.assertNotEqual(check.transfer_url, str(self.bare))

    def test_a_real_path_remote_still_uses_its_resolved_path(self):
        """Gegenprobe: Pfad-Remotes müssen weiterhin absolut aufgelöst werden."""
        git(self.repo, "remote", "set-url", "origin", "../origin.git")
        remote = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["origin"]
        self.assertTrue(remote.fetch_targets[0].is_local)
        self.assertEqual(gmf_module.remote_fetch_url(remote), str(self.bare))
        self.assertEqual(
            inspect_transfer(self.repo, "origin", "main", "push").transfer_url, str(self.bare)
        )

    def test_an_empty_configured_sync_host_matches_nothing(self):
        """Ein leerer Eintrag darf nicht jedes hostlose Pfad-Remote treffen."""
        git(self.repo, "remote", "set-url", "origin", str(self.bare))
        cfg = {**DEFAULT_CONFIG, "sync_remote_names": [], "sync_remote_hosts": [""]}
        self.assertIsNone(detect_sync_remote(self.repo, cfg))
        cfg = {**DEFAULT_CONFIG, "sync_remote_names": [], "sync_remote_hosts": ["local"]}
        self.assertIsNone(detect_sync_remote(self.repo, cfg))
        git(self.repo, "remote", "set-url", "origin", self.url)
        self.assertEqual(detect_sync_remote(self.repo, cfg), "origin")


class RemoteCheckTests(unittest.TestCase):
    """`T` auf der Info-Seite: existiert das Remote-Repo — und wenn nicht, warum?"""

    @staticmethod
    def _result(stderr, code=128):
        return subprocess.CompletedProcess(["git"], code, "", stderr)

    def test_unreadable_remote_config_counts_as_a_missing_remote(self):
        """Fünf Sicherheitsentscheidungen hängen an dieser einen Antwort.

        Ist die Config noch dieselbe wie bei der Freigabe? Hängt der Login am
        Schlüsselbund? Darf gefetcht werden? Eine Config, die gerade nicht
        lesbar ist, darf keine davon stützen — egal, woran das Lesen scheitert.
        """
        errors = (
            subprocess.TimeoutExpired(["git"], 1),
            gmf_module.GitReadError("git config kaputt"),
            OSError("kein Dateideskriptor"),
            ValueError("krumme Ausgabe"),
            RuntimeError("unklarer Zustand"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(gmf_module, "read_remote_configs", side_effect=error):
                    self.assertIsNone(
                        gmf_module.read_remote_config_or_none(
                            Path("/tmp"), DEFAULT_CONFIG, "origin"
                        )
                    )

    def test_timeout_config_changes_only_the_git_timeout(self):
        cfg = gmf_module.timeout_config(7)
        self.assertEqual(cfg["git_timeout"], 7)
        self.assertEqual(
            {key: value for key, value in cfg.items() if key != "git_timeout"},
            {key: value for key, value in DEFAULT_CONFIG.items() if key != "git_timeout"},
        )
        # Die Vorlage darf dabei nicht verändert werden: sie ist prozessweit.
        self.assertEqual(DEFAULT_CONFIG["git_timeout"], 10)

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
        # `keychain_session` festnageln: In einer Sitzung ohne Schlüsselbund
        # (ssh, cron) würde der "auth"-Fall sonst als "nokeychain" landen und
        # der Test hinge an der Umgebung statt an der Meldung.
        with mock.patch.object(gmf_module, "keychain_session", return_value=True):
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
        # Konkreter Regressionfall: Das GitHub-Repo ist weg, der Fetch soll das sagen —
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

    def test_check_never_starts_an_unknown_remote_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            marker = root / "helper-ran"
            binary = root / "git-remote-evil"
            binary.write_text('#!/bin/sh\nprintf ran > "$GMF_HELPER_MARKER"\nexit 1\n')
            binary.chmod(0o755)
            git(repo, "remote", "add", "origin", "evil://example/repo")
            env = dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ["PATH"],
                GMF_HELPER_MARKER=str(marker),
            )

            with mock.patch.dict(os.environ, env, clear=True):
                outcome = check_remote(repo, "origin", 10)[0]

            self.assertEqual(outcome, "unsafe_url")
            self.assertFalse(marker.exists())


class RemoteAndCommandLogTests(unittest.TestCase):
    """Remote-Schutz, Info-Ansicht und Befehlsprotokoll gegen echte Repos."""

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

    def test_fetch_blocks_refspecs_that_target_local_branches(self):
        git(self.repo, "branch", "victim")
        before = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "refs/heads/victim"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git(self.repo, "config", "--unset-all", "remote.origin.fetch")
        git(self.repo, "config", "--add", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        remote = next(item for item in status.remotes if item.name == "origin")
        self.assertEqual(remote.fetch_outcome, "unsafe_refspec")
        after = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "refs/heads/victim"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(after, before)
        self.assertFalse(any("git fetch" in line for line in gmf_module.COMMAND_LOG))

    def test_fetch_requires_a_tracking_destination_not_only_fetch_head(self):
        git(self.repo, "config", "--unset-all", "remote.origin.fetch")
        git(self.repo, "config", "--add", "remote.origin.fetch", "refs/heads/main")

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        remote = next(item for item in status.remotes if item.name == "origin")
        self.assertEqual(remote.fetch_outcome, "unsafe_refspec")
        self.assertFalse(any("git fetch" in line for line in gmf_module.COMMAND_LOG))

    def test_fetch_rejects_mismatched_refspec_wildcards(self):
        for refspec in (
            "refs/heads/main:refs/remotes/origin/*",
            "refs/heads/*:refs/remotes/origin/main",
        ):
            with self.subTest(refspec=refspec):
                remote = gmf_module.RemoteConfig(
                    "origin",
                    [str(self.origin)],
                    [str(self.origin)],
                    [],
                    [],
                    settings=[("fetch", refspec)],
                )
                self.assertFalse(gmf_module.fetch_refspecs_safe(remote))
                self.assertFalse(gmf_module.fetch_maps_branch_exactly(remote, "main"))

    def test_fetch_rejects_invalid_ref_components(self):
        for refspec in (
            "+refs/heads/foo..bar:refs/remotes/origin/main",
            "+refs/heads/.hidden:refs/remotes/origin/main",
            "+refs/heads/topic.lock:refs/remotes/origin/main",
        ):
            with self.subTest(refspec=refspec):
                remote = gmf_module.RemoteConfig(
                    "origin",
                    [str(self.origin)],
                    [str(self.origin)],
                    [],
                    [],
                    settings=[("fetch", refspec)],
                )
                self.assertFalse(gmf_module.fetch_refspecs_safe(remote))

    def test_fetch_never_places_a_secret_remote_url_in_argv(self):
        secret_url = "https://" + "user:" + "credential@" + "example.invalid/repo.git?access=query"
        remote = gmf_module.RemoteConfig(
            "origin",
            [secret_url],
            [secret_url],
            [],
            [],
            settings=[
                ("fetch", "+refs/heads/*:refs/remotes/origin/*"),
            ],
        )
        self.assertIsNone(gmf_module.approved_fetch_args(remote, "main", "a" * 40))

    def test_fetch_reports_a_secret_remote_url_as_unsafe(self):
        secret_url = "https://" + "user:credential@" + "example.invalid/repo.git"
        git(self.repo, "remote", "set-url", "origin", secret_url)

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        remote = next(item for item in status.remotes if item.name == "origin")
        self.assertEqual(remote.fetch_outcome, "unsafe_url")
        self.assertNotIn("credential", status.error_long)

        class Screen:
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [status]
        self.assertIsNone(ui._fetch_remote(status, "origin"))
        self.assertIn("blocked", ui.message.lower())
        self.assertNotIn("credential", ui.message)

    def test_tracking_update_timeout_is_reported_as_unknown_fetch_outcome(self):
        config = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["origin"]
        branch = git(self.repo, "symbolic-ref", "--short", "HEAD")
        original = gmf_module.run_git_logged

        def timeout_update(repo, *args, **kwargs):
            if "update-ref" in args:
                raise subprocess.TimeoutExpired(["git", "-C", str(repo), "update-ref"], 1)
            return original(repo, *args, **kwargs)

        with (
            mock.patch.object(gmf_module, "run_git_logged", side_effect=timeout_update),
            self.assertRaises(subprocess.TimeoutExpired) as caught,
        ):
            gmf_module.fetch_remote_safely(self.repo, config, branch, 10)

        self.assertEqual(caught.exception.cmd[-1], "fetch")
        self.assertIn("unknown", gmf_module.timeout_message(caught.exception).lower())

    def test_tracking_update_oserror_is_reported_as_unknown_fetch_outcome(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        original = gmf_module.run_git_logged

        def fail_update(repo, *args, **kwargs):
            if "update-ref" in args:
                raise OSError("cannot start update-ref")
            return original(repo, *args, **kwargs)

        class Screen(FakeScreen):
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with (
            mock.patch.object(gmf_module, "run_git_logged", side_effect=fail_update),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            result = ui._fetch_remote(st, "origin")

        self.assertIsNone(result)
        self.assertEqual(ui.message, gmf_module.t("fetch_outcome_unknown", r="origin"))
        self.assertIsNot(ui.statuses[0], st)

    def test_a_remote_that_vanished_is_not_blamed_for_an_unsafe_refspec(self):
        """Eine nicht belegbare Remote-Config ist keine unsichere Refspec.

        `read_remote_config_or_none()` liefert `None` bei jedem Lesefehler UND
        fuer ein geloeschtes Remote. Beides sind gewoehnliche Zustaende; die
        TUI meldete dafuer "unsichere Fetch-Refspec" und beschuldigte damit
        eine voellig gewoehnliche Konfiguration — dieselbe Verwechslung, die
        der `detached`-Fall schon einmal hatte (Review-Fund 2026-09-10).
        """
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        class Screen(FakeScreen):
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        # Das Remote verschwindet zwischen Anzeige und Tastendruck.
        git(self.repo, "remote", "remove", "origin")
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            result = ui._fetch_remote(st, "origin")

        self.assertIsNone(result)
        self.assertEqual(
            ui.message,
            remote_check_message("origin", "changed", 0, "", DEFAULT_CONFIG["fetch_timeout"]),
        )
        self.assertNotEqual(
            ui.message,
            remote_check_message(
                "origin", "unsafe_refspec", 0, "", DEFAULT_CONFIG["fetch_timeout"]
            ),
        )

    def test_one_invalid_remote_does_not_hide_or_block_the_safe_remote(self):
        git(self.repo, "remote", "add", "broken", "https://example.invalid:not-a-port/repo.git")

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        origin = next(item for item in status.remotes if item.name == "origin")
        broken = next(item for item in status.remotes if item.name == "broken")
        self.assertFalse(origin.fetch_failed)
        self.assertTrue(broken.fetch_failed)
        self.assertEqual(broken.fetch_outcome, "unsafe_url")

    def test_one_fetch_oserror_does_not_skip_the_next_remote(self):
        branch = git(self.repo, "symbolic-ref", "--short", "HEAD")
        before = git(self.repo, "rev-parse", "HEAD")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = git(self.repo, "commit-tree", tree, "-p", before, "-m", "remote tip")
        git(self.repo, "push", "-q", str(self.origin), f"{remote_tip}:refs/heads/{branch}")
        backup = self.root / "backup.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.origin), str(backup)],
            check=True,
            capture_output=True,
            text=True,
        )
        git(self.repo, "remote", "add", "backup", str(backup))
        original = gmf_module.fetch_remote_safely
        fetched_names = []

        def fetch_then_fail(repo, remote, current_branch, timeout):
            result = original(repo, remote, current_branch, timeout)
            fetched_names.append(remote.name)
            if remote.name == "origin":
                self.assertEqual(result.returncode, 0, result.stderr)
                raise OSError("lost fetch result")
            return result

        with mock.patch.object(gmf_module, "fetch_remote_safely", side_effect=fetch_then_fail):
            status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        self.assertEqual(set(fetched_names), {"origin", "backup"})
        remotes = {remote.name: remote for remote in status.remotes}
        self.assertEqual(remotes["origin"].fetch_outcome, "outcome_unknown")
        self.assertIn("may already have changed", remotes["origin"].fetch_error_long)
        self.assertTrue(remotes["origin"].fetch_failed)
        self.assertFalse(remotes["backup"].fetch_failed)
        self.assertEqual(git(self.repo, "rev-parse", f"refs/remotes/origin/{branch}"), remote_tip)
        payload = status_dict(status)
        origin_payload = next(remote for remote in payload["remotes"] if remote["name"] == "origin")
        self.assertEqual(origin_payload["fetch_outcome"], "outcome_unknown")
        self.assertIn("origin", [item.name for item in status.remotes])

    def test_invalid_push_url_does_not_block_a_safe_fetch(self):
        branch = git(self.repo, "symbolic-ref", "--short", "HEAD")
        head = git(self.repo, "rev-parse", "HEAD")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", head],
            input="remote tip\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        git(self.repo, "push", "-q", str(self.origin), f"{remote_tip}:refs/heads/{branch}")
        git(
            self.repo,
            "remote",
            "set-url",
            "--push",
            "origin",
            "https://example.invalid:not-a-port/repo.git",
        )

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        remote = next(item for item in status.remotes if item.name == "origin")

        self.assertFalse(remote.fetch_failed)
        self.assertTrue(remote.fetch_url_safe)
        self.assertFalse(remote.push_url_safe)
        self.assertEqual(git(self.repo, "rev-parse", f"refs/remotes/origin/{branch}"), remote_tip)
        self.assertEqual(
            inspect_transfer(self.repo, "origin", branch, "push").reason, "remote-unsafe"
        )

    def test_url_execution_safety_is_serialized_without_the_url(self):
        unsafe_url = "https://" + "token@" + "example.invalid/repo.git"
        git(self.repo, "remote", "set-url", "origin", unsafe_url)

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        payload = status_dict(status)
        remote = next(item for item in payload["remotes"] if item["name"] == "origin")

        self.assertFalse(remote["fetch_url_safe"])
        self.assertFalse(remote["push_url_safe"])
        self.assertNotIn("token", json.dumps(payload))

    def test_fetch_uses_the_approved_refspec_after_a_config_race(self):
        before = gmf_module.current_head(self.repo, 10)
        git(self.repo, "branch", "victim", before)
        (self.repo / "remote.txt").write_text("new\n")
        git(self.repo, "add", "remote.txt")
        git(self.repo, "commit", "-qm", "remote victim")
        git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/victim")
        approved_remote_tip = git(self.repo, "rev-parse", "HEAD")
        redirected = self.root / "redirected.git"
        git(self.root, "init", "-q", "--bare", str(redirected))
        original = gmf_module.run_git_logged
        raced = False

        def race(target_repo, *args, **kwargs):
            nonlocal raced
            if "fetch" in args and not raced:
                raced = True
                git(
                    self.repo,
                    "config",
                    "--replace-all",
                    "remote.origin.fetch",
                    "+refs/heads/*:refs/heads/*",
                )
                git(self.repo, "remote", "set-url", "origin", str(redirected))
            return original(target_repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git_logged", side_effect=race):
            status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        self.assertTrue(raced)
        remote = next(item for item in status.remotes if item.name == "origin")
        self.assertTrue(remote.fetch_failed)
        self.assertEqual(remote.fetch_outcome, "changed")
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "refs/heads/victim"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            before,
        )
        self.assertEqual(
            git(self.repo, "rev-parse", "refs/remotes/origin/victim"), approved_remote_tip
        )

    def test_fetch_cannot_rewrite_the_approved_url_a_second_time(self):
        original_parent = self.root / "configured"
        safe_parent = self.root / "approved"
        redirected_parent = self.root / "redirected"
        for parent in (original_parent, safe_parent, redirected_parent):
            parent.mkdir()
        safe = safe_parent / "repo.git"
        redirected = redirected_parent / "repo.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.origin), str(safe)], check=True)
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.origin), str(redirected)], check=True
        )
        branch = git(self.repo, "symbolic-ref", "--short", "HEAD")
        head = git(self.repo, "rev-parse", "HEAD")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        approved_oid = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", head],
            input="approved remote tip\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git(self.repo, "push", "-q", str(safe), f"{approved_oid}:refs/heads/{branch}")
        git(self.repo, "remote", "set-url", "origin", str(original_parent / "repo.git"))
        git(self.repo, "config", f"url.{safe_parent}/.insteadOf", str(original_parent) + "/")
        git(self.repo, "config", f"url.{redirected_parent}/.insteadOf", str(safe_parent) + "/")
        config = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["origin"]
        self.assertEqual(config.fetch_urls, [str(safe)])

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        remote = next(item for item in status.remotes if item.name == "origin")
        self.assertFalse(remote.fetch_failed)
        self.assertEqual(
            git(self.repo, "rev-parse", f"refs/remotes/origin/{branch}"),
            git(safe, "rev-parse", f"refs/heads/{branch}"),
        )
        self.assertNotEqual(
            git(self.repo, "rev-parse", f"refs/remotes/origin/{branch}"),
            git(redirected, "rev-parse", f"refs/heads/{branch}"),
        )

    def test_command_log_shows_the_real_git_syntax(self):
        gmf_module.run_git_logged(self.repo, "config", "gmf.command-log-test", "value", timeout=10)
        self.assertEqual(len(gmf_module.COMMAND_LOG), 1)
        entry = gmf_module.COMMAND_LOG[0]
        self.assertIn("git config gmf.command-log-test value", entry)
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
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [broken]
        fresh = ui.refresh_one(broken)
        self.assertEqual(fresh.error, broken.error)
        self.assertEqual(fresh.error_detail, broken.error_detail)
        self.assertTrue(any(r.fetch_failed for r in fresh.remotes if r.name == "github"))
        self.assertFalse(fresh.clean_and_synced)

    def test_successful_refetch_clears_the_known_failure(self):
        """Nach einem bewiesen erfolgreichen Fetch darf der alte Fehler nicht
        bis zum kompletten Reload weiterleuchten."""
        git(self.repo, "remote", "add", "github", str(self.root / "geloescht.git"))
        broken = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        self.assertTrue(broken.error)
        self.assertTrue(broken.fetch_error)
        fresh = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        gmf_module.carry_fetch_failure(broken, fresh, refetched="github")
        self.assertEqual(fresh.error, "")
        self.assertFalse(any(r.fetch_failed for r in fresh.remotes))

    def test_fetch_failure_is_not_carried_to_a_new_url_with_the_same_name(self):
        git(self.repo, "remote", "add", "github", str(self.root / "missing.git"))
        broken = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        self.assertTrue(any(r.fetch_failed for r in broken.remotes if r.name == "github"))
        git(self.repo, "remote", "set-url", "github", str(self.origin))
        fresh = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        gmf_module.carry_fetch_failure(broken, fresh)

        self.assertFalse(any(r.fetch_failed for r in fresh.remotes if r.name == "github"))
        self.assertEqual(fresh.error, "")

    def test_local_read_error_is_not_carried_over(self):
        """Nur Fetch-Fehler überleben einen lokalen Refresh: ein reparierter
        Index-/Lesefehler muss nach dem Neu-Einlesen verschwinden."""
        old = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        old.error = "cannot read Git index: kaputt"
        old.remote_state = "error"  # fetch_error bleibt False
        fresh = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        gmf_module.carry_fetch_failure(old, fresh)
        self.assertEqual(fresh.error, "")
        self.assertEqual(fresh.remote_state, "ok")

    def test_interactive_fetch_appears_in_the_command_log(self):
        # R bzw. --fetch ist eine bewusst ausgelöste Aktion und gehört ins
        # Protokoll — sonst erklärt das Protokoll genau die eine Netz-Aktion nicht.
        collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        fetch_logs = [line for line in gmf_module.COMMAND_LOG if " fetch " in line]
        self.assertEqual(len(fetch_logs), 1, gmf_module.COMMAND_LOG)
        logged = fetch_logs[0].split(": ", 1)[1]
        argv = shlex.split(logged)
        self.assertIn("fetch", argv)
        self.assertIn("--no-tags", argv)
        self.assertIn("--no-prune-tags", argv)
        self.assertIn("--no-recurse-submodules", argv)
        self.assertIn("--refmap=", argv)
        self.assertIn("--no-write-fetch-head", argv)

    def test_nonzero_push_exit_is_reported_as_unknown_not_unpublished(self):
        remote = gmf_module.RemoteStatus(name="origin", branch_exists=True)
        st = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remote="origin", remotes=[remote]
        )
        check = gmf_module.TransferCheck(
            "ready",
            ahead=1,
            remote_ref="refs/remotes/origin/main",
            branch="main",
            head_oid="a" * 40,
            target_oid="b" * 40,
            transfer_url=str(self.origin),
        )

        class Screen:
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        failed = subprocess.CompletedProcess(["git", "push"], 1, "", "rejected")
        with (
            mock.patch.object(ui, "_fetch_remote", return_value=st),
            mock.patch.object(gmf_module, "inspect_transfer", return_value=check),
            mock.patch.object(ui, "confirm", return_value=True),
            mock.patch.object(gmf_module, "run_git_logged", return_value=failed),
            mock.patch.object(gmf_module, "credentials_missing", return_value=False),
            mock.patch.object(ui, "refresh_one", return_value=st),
        ):
            ui.action_sync_push()

        self.assertEqual(ui.message, gmf_module.t("push_outcome_unknown", code=1))

    def test_oserror_after_real_push_is_reported_as_unknown(self):
        (self.repo / "a.md").write_text("pushed\n")
        git(self.repo, "commit", "-qam", "outgoing")
        branch_ref = git(self.repo, "symbolic-ref", "HEAD")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        original = gmf_module.run_git

        def push_then_lose_result(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if "push" in args:
                self.assertEqual(result.returncode, 0, result.stderr)
                raise OSError("lost push result")
            return result

        class Screen(FakeScreen):
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with (
            mock.patch.object(ui, "confirm", return_value=True),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch.object(gmf_module, "run_git", side_effect=push_then_lose_result),
        ):
            ui.action_sync_push()

        self.assertEqual(ui.message, gmf_module.t("push_io_unknown"))
        self.assertEqual(
            git(self.origin, "rev-parse", branch_ref), git(self.repo, "rev-parse", "HEAD")
        )
        self.assertTrue(
            any(line.startswith("…") and " push " in line for line in gmf_module.COMMAND_LOG)
        )

    def test_cancelled_private_push_is_translated_in_both_languages(self):
        remote = gmf_module.RemoteStatus(name="origin", branch_exists=True)
        st = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remote="origin", remotes=[remote]
        )
        check = gmf_module.TransferCheck(
            "ready",
            ahead=1,
            branch="main",
            head_oid="a" * 40,
            target_oid="b" * 40,
            transfer_url=str(self.origin),
        )

        class Screen:
            def getmaxyx(self):
                return (30, 100)

        for lang, expected in (("en", "Cancelled."), ("de", "Abgebrochen.")):
            with self.subTest(lang=lang):
                ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
                ui.statuses = [st]
                with (
                    mock.patch.object(gmf_module, "UI_LANG", lang),
                    mock.patch.object(ui, "_fetch_remote", return_value=st),
                    mock.patch.object(gmf_module, "inspect_transfer", return_value=check),
                    mock.patch.object(ui, "confirm", return_value=False),
                ):
                    ui.action_sync_push()
                self.assertEqual(ui.message, expected)

    def test_private_push_blocks_a_branch_change_during_the_first_fetch(self):
        remote = gmf_module.RemoteStatus(name="origin", branch_exists=True)
        shown = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remote="origin", remotes=[remote]
        )
        fresh = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="other", remote="origin", remotes=[remote]
        )

        class Screen:
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [shown]
        with (
            mock.patch.object(ui, "_fetch_remote", return_value=fresh),
            mock.patch.object(gmf_module, "inspect_transfer") as inspect,
            mock.patch.object(ui, "confirm") as confirm,
        ):
            ui.action_sync_push()

        self.assertEqual(ui.message, gmf_module.t("transfer_changed"))
        inspect.assert_not_called()
        confirm.assert_not_called()

    def test_private_push_blocks_a_remote_change_since_the_list_scan(self):
        shown_remote = gmf_module.RemoteStatus(
            name="origin",
            branch_exists=True,
            fetch_fingerprints=["target-a"],
            push_fingerprints=["target-a"],
            fetch_refspecs_safe=True,
            branch_mapping_safe=True,
        )
        fresh_remote = gmf_module.RemoteStatus(
            name="origin",
            branch_exists=True,
            fetch_fingerprints=["target-b"],
            push_fingerprints=["target-b"],
            fetch_refspecs_safe=True,
            branch_mapping_safe=True,
        )
        shown = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remote="origin", remotes=[shown_remote]
        )
        fresh = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remote="origin", remotes=[fresh_remote]
        )

        class Screen:
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [shown]
        with (
            mock.patch.object(ui, "_fetch_remote", return_value=fresh),
            mock.patch.object(gmf_module, "inspect_transfer") as inspect,
        ):
            ui.action_sync_push()

        self.assertEqual(ui.message, gmf_module.t("transfer_changed"))
        inspect.assert_not_called()

    def test_public_push_blocks_a_remote_change_since_the_list_scan(self):
        shown_remote = gmf_module.RemoteStatus(
            name="github",
            public=True,
            branch_exists=True,
            fetch_fingerprints=["target-a"],
            push_fingerprints=["target-a"],
            fetch_refspecs_safe=True,
            branch_mapping_safe=True,
        )
        fresh_remote = gmf_module.RemoteStatus(
            name="github",
            public=True,
            branch_exists=True,
            fetch_fingerprints=["target-b"],
            push_fingerprints=["target-b"],
            fetch_refspecs_safe=True,
            branch_mapping_safe=True,
        )
        shown = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remotes=[shown_remote]
        )
        fresh = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remotes=[fresh_remote]
        )

        class Screen:
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [shown]
        with (
            mock.patch.object(ui, "_fetch_remote", return_value=fresh),
            mock.patch.object(gmf_module, "inspect_transfer") as inspect,
        ):
            ui.action_github_push()

        self.assertEqual(ui.message, gmf_module.t("github_changed"))
        inspect.assert_not_called()

    def _public_push_fixture(self):
        remote = gmf_module.RemoteStatus(
            name="github",
            public=True,
            branch_exists=True,
            fetch_fingerprints=["target-a"],
            push_fingerprints=["target-a"],
            fetch_refspecs_safe=True,
            branch_mapping_safe=True,
        )
        st = gmf_module.RepoStatus(path=self.repo, rel="repo", branch="main", remotes=[remote])
        check = gmf_module.TransferCheck(
            "ready",
            ahead=1,
            remote_ref="refs/remotes/github/main",
            branch="main",
            head_oid="a" * 40,
            target_oid="b" * 40,
            transfer_url="https://github.com/example/repo.git",
            commits=["abc1234 add feature"],
            files=["src/app.py"],
        )

        class Screen:
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        return ui, st, check

    def test_public_push_asks_inside_the_preview_and_pushes_on_yes(self):
        ui, st, check = self._public_push_fixture()
        asked = {}

        def preview(title, lines, question):
            asked.update(title=title, lines=lines, question=question)
            return True

        pushed = subprocess.CompletedProcess(["git", "push"], 0, "", "")
        with (
            mock.patch.object(ui, "_fetch_remote", return_value=st),
            mock.patch.object(gmf_module, "inspect_transfer", return_value=check),
            mock.patch.object(ui, "confirm_in_pager", side_effect=preview),
            mock.patch.object(ui, "prompt_line") as typed,
            mock.patch.object(gmf_module, "run_git_logged", return_value=pushed) as run,
            mock.patch.object(gmf_module, "update_tracking_after_push", return_value="ok"),
            mock.patch.object(ui, "refresh_one", return_value=st),
        ):
            ui.action_github_push()

        self.assertEqual(ui.message, gmf_module.t("github_pushed", r="github"))
        # Kein getippter Satz mehr: Die Rückfrage steht in der Vorschau selbst.
        typed.assert_not_called()
        self.assertEqual(
            asked["title"], gmf_module.t("github_preview", rel="repo", r="github", b="main")
        )
        self.assertEqual(asked["question"], gmf_module.t("github_confirm", r="github"))
        self.assertIn("abc1234 add feature", asked["lines"])
        self.assertIn("src/app.py", asked["lines"])
        run.assert_called_once()
        self.assertIn("push", run.call_args.args)

    def test_public_push_is_cancelled_when_the_preview_question_is_declined(self):
        ui, st, check = self._public_push_fixture()
        with (
            mock.patch.object(ui, "_fetch_remote", return_value=st),
            mock.patch.object(gmf_module, "inspect_transfer", return_value=check),
            mock.patch.object(ui, "confirm_in_pager", return_value=False),
            mock.patch.object(gmf_module, "run_git_logged") as run,
            mock.patch.object(gmf_module, "log_cancelled") as cancelled,
        ):
            ui.action_github_push()

        self.assertEqual(ui.message, gmf_module.t("github_cancelled"))
        run.assert_not_called()
        cancelled.assert_called_once()

    def test_both_push_paths_abort_when_the_approval_changes_before_the_push(self):
        """Der zweite Fetch direkt vor dem Push ist der Schutz gegen ein
        veraltetes Ja.

        Bis 2026-08-29 stand diese Pruefung zweimal im Code — einmal je
        Push-Weg. Geprueft war bisher nur der Abgleich VOR der Rueckfrage;
        dieser hier haengt nun an derselben Methode fuer beide Wege und faellt
        auf, wenn einer der beiden sie verliert.
        """
        first = gmf_module.TransferCheck(
            "ready",
            ahead=1,
            remote_ref="refs/remotes/x/main",
            branch="main",
            head_oid="a" * 40,
            target_oid="b" * 40,
            transfer_url="https://example.invalid/repo.git",
            commits=["abc1234 add feature"],
            files=["src/app.py"],
        )
        # Zwischen Rueckfrage und Push ist ein weiterer Commit dazugekommen:
        # dieselbe Uebertragung, aber nicht mehr die freigegebene.
        # `dataclasses.replace` statt `copy.replace`: Letzteres gibt es erst ab
        # Python 3.13, und install.sh faehrt diese Suite als Selbsttest auf der
        # Maschine des Anwenders.
        second = dataclasses.replace(first, head_oid="c" * 40)
        self.assertNotEqual(first.approval_signature(), second.approval_signature())

        class Screen:
            def getmaxyx(self):
                return (30, 100)

        cases = (
            ("action_sync_push", "transfer_changed", "origin", False, "confirm"),
            ("action_github_push", "github_changed", "github", True, "confirm_in_pager"),
        )
        for action, message_key, name, public, ask in cases:
            with self.subTest(weg=action):
                remote = gmf_module.RemoteStatus(
                    name=name,
                    public=public,
                    branch_exists=True,
                    fetch_fingerprints=["target-a"],
                    push_fingerprints=["target-a"],
                    fetch_refspecs_safe=True,
                    branch_mapping_safe=True,
                )
                st = gmf_module.RepoStatus(
                    path=self.repo,
                    rel="repo",
                    branch="main",
                    remote=None if public else name,
                    remotes=[remote],
                )
                ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
                ui.statuses = [st]
                with (
                    mock.patch.object(ui, "_fetch_remote", return_value=st),
                    mock.patch.object(gmf_module, "inspect_transfer", side_effect=[first, second]),
                    mock.patch.object(ui, ask, return_value=True),
                    mock.patch.object(gmf_module, "run_git_logged") as run,
                ):
                    getattr(ui, action)()

                self.assertEqual(ui.message, gmf_module.t(message_key))
                run.assert_not_called()

    def test_successful_push_keeps_success_visible_when_tracking_update_times_out(self):
        remote = gmf_module.RemoteStatus(name="origin", branch_exists=True)
        st = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remote="origin", remotes=[remote]
        )
        config = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["origin"]
        check = gmf_module.TransferCheck(
            "ready",
            ahead=1,
            remote_ref="refs/remotes/origin/main",
            branch="main",
            head_oid="a" * 40,
            target_oid="b" * 40,
            transfer_url=str(self.origin),
            remote_name="origin",
            remote_config_signature=(
                tuple(config.fetch_urls),
                tuple(config.push_urls),
                tuple(config.settings),
            ),
        )

        class Screen:
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        pushed = subprocess.CompletedProcess(["git", "push"], 0, "", "")
        timeout = subprocess.TimeoutExpired(["git", "update-ref"], 10)
        with (
            mock.patch.object(ui, "_fetch_remote", return_value=st),
            mock.patch.object(gmf_module, "inspect_transfer", return_value=check),
            mock.patch.object(ui, "confirm", return_value=True),
            mock.patch.object(gmf_module, "run_git_logged", side_effect=[pushed, timeout]),
            mock.patch.object(ui, "refresh_one", return_value=st),
        ):
            ui.action_sync_push()

        # Der Ausgang der eigenen Ref-CAS ist nach dem Timeout nicht belegt —
        # "der Ref aenderte sich parallel" waere eine andere Aussage.
        self.assertEqual(ui.message, gmf_module.t("push_tracking_unknown"))

    def test_repo_line_with_error_still_shows_branch_and_remotes(self):
        """Der Fehlertext darf Branch und Remote-Badges nicht verdrängen —
        gerade dann muss sichtbar sein, WELCHES Remote das ✘ trägt."""
        git(self.repo, "remote", "add", "github", str(self.root / "geloescht.git"))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        self.assertTrue(st.error)

        class Screen:
            def __init__(self):
                self.texts = []

            def getmaxyx(self):
                return (30, 200)

            def addstr(self, y, x, text, attr=0):
                self.texts.append(text)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw_repo_line(0, st, False)
        drawn = " ".join(ui.scr.texts)
        self.assertIn(st.error, drawn)
        self.assertIn("[" + st.branch + "]", drawn)
        self.assertIn("github", drawn)
        self.assertIn("origin", drawn)

    def test_action_timeout_rereads_the_repo(self):
        """Eine Aktion kann vor dem hängenden Schritt schon mutiert haben —
        nach dem Timeout muss die TUI den echten Zustand neu einlesen."""

        class Screen:
            def getmaxyx(self):
                return (30, 100)

        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        exc = subprocess.TimeoutExpired(["git", "-C", str(self.repo), "merge"], 7)
        with mock.patch("gitmaster_flash.curses.flushinp"):
            ui.handle_action_timeout(exc)
        self.assertIsNot(ui.statuses[0], st)
        self.assertIn("merge", ui.message)

    def test_info_view_paging_survives_the_redraw(self):
        """Der Footer verspricht freie Seitennavigation: ein PgDn-Sprung darf im
        nächsten Zeichendurchlauf nicht auf den gewählten Block zurückschnappen."""
        for i in range(8):
            git(self.repo, "branch", f"zweig-{i}")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        view = build_info_view(st, DEFAULT_CONFIG)
        body_h = 30 - 4
        self.assertGreater(len(view.lines), body_h)  # Paging hat etwas zu tun
        expected_top = min(len(view.lines) - body_h, body_h)

        class Screen(FakeScreen):
            def __init__(self):
                self.keys = iter([curses.KEY_NPAGE, ord("q")])
                self.frames = []

            def erase(self):
                self.frames.append([])

            def getmaxyx(self):
                return (30, 100)

            def addstr(self, y, x, text, attr=0):
                if self.frames:
                    self.frames[-1].append((y, text))

            def getch(self):
                return next(self.keys)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_repo_info()
        last_frame = ui.scr.frames[-1]
        first_body_line = next(text for y, text in last_frame if y == 1)
        self.assertEqual(first_body_line, view.lines[expected_top])

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
            "git commit -m 'zwei Wörter'",
        )

    @unittest.skipUnless(shutil.which("zsh"), "zsh unavailable")
    def test_log_preserves_control_characters_as_executable_zsh_escapes(self):
        name = "line\ncontrol\x1b\u0085.txt"
        (self.repo / name).write_text("content\n")
        command = gmf_module.format_git_command(("add", "--", name))

        self.assertNotIn("\n", command)
        self.assertNotIn("\x1b", command)
        added = subprocess.run(
            ["zsh", "-fc", command], cwd=self.repo, capture_output=True, text=True
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        cached = subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--cached", "--name-only", "-z"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn(name, cached.stdout.split("\0"))

    def test_log_keeps_only_the_newest_entries(self):
        for i in range(gmf_module.COMMAND_LOG_MAX + 5):
            gmf_module.log_command(self.repo, ("status", str(i)), 0)
        self.assertEqual(len(gmf_module.COMMAND_LOG), gmf_module.COMMAND_LOG_MAX)
        self.assertIn(str(gmf_module.COMMAND_LOG_MAX + 4), gmf_module.COMMAND_LOG[-1])

    def _info_screen(self, keys):
        """Fake-Screen fuer die Info-Ansicht; liefert die gedrueckten Tasten der Reihe."""

        class Screen(FakeScreen):
            def __init__(self):
                self.keys = iter(keys)
                self.written = []

            def getmaxyx(self):
                return (30, 100)

            def addstr(self, y, x, text, *rest):
                self.written.append(text)

            def getch(self):
                return next(self.keys)

        return Screen()

    def test_tab_selects_the_next_remote_before_acting_on_it(self):
        git(self.repo, "remote", "add", "github", str(self.root / "geloescht.git"))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        ui = TUI(self._info_screen([9, ord("t"), ord("q")]), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        # origin (Sync) steht an erster Stelle, github zuletzt — Tab muss weiterspringen.
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_repo_info()
        self.assertIn("github", ui.message)
        self.assertIn("no repository", ui.message)

    def _remove_remote_ui(self, keys, answer=True):
        """Info-Ansicht mit gefälschter Pager-Rückfrage; sammelt Titel, Zeilen, Frage.

        `answer` ist die Antwort auf die Rückfrage — oder ein Callable, das
        zwischen Vorschau und Antwort noch etwas am Repo verändern darf.
        """
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        ui = TUI(self._info_screen(keys), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        asked = {}

        def preview(title, lines, question):
            asked.update(title=title, lines=lines, question=question)
            return answer() if callable(answer) else answer

        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch.object(ui, "confirm_in_pager", side_effect=preview),
        ):
            ui.action_repo_info()
        return ui, asked

    def _commit_only_under(self, ref: str) -> str:
        """Einen Commit anlegen, der danach nur noch unter `ref` erreichbar ist."""
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "nur dort")
        oid = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        git(self.repo, "update-ref", ref, oid)
        return oid

    def test_custom_fetch_namespace_blocks_removal_and_keeps_last_ref(self):
        git(self.repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/archive/*")
        oid = self._commit_only_under("refs/remotes/archive/topic")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        ui = TUI(self._info_screen([]), self.root, DEFAULT_CONFIG, None)
        with mock.patch.object(ui, "confirm_in_pager") as confirm:
            self.assertFalse(ui._remove_remote(st, "origin"))
        confirm.assert_not_called()
        self.assertEqual(git(self.repo, "rev-parse", "refs/remotes/archive/topic"), oid)
        self.assertEqual(git(self.repo, "remote"), "origin")

    def test_shared_fetch_namespace_blocks_inaccurate_removal_preview(self):
        git(self.repo, "remote", "add", "other", "https://example.com/other.git")
        git(self.repo, "config", "remote.other.fetch", "+refs/heads/*:refs/remotes/origin/*")
        with self.assertRaises(gmf_module.GitReadError):
            gmf_module.remote_removal_facts(self.repo, "origin", DEFAULT_CONFIG)

    def test_nested_remote_names_block_the_removal_preview(self):
        for name in ("team", "team/fork"):
            git(self.repo, "config", f"remote.{name}.url", f"https://example.invalid/{name}.git")
            git(self.repo, "config", f"remote.{name}.fetch", f"+refs/heads/*:refs/remotes/{name}/*")
        for name in ("team", "team/fork"):
            with self.subTest(name=name), self.assertRaises(gmf_module.GitReadError):
                gmf_module.remote_removal_facts(self.repo, name, DEFAULT_CONFIG)

    def test_unicode_line_separator_survives_full_scan_and_removal_preview(self):
        name = "mirror\u2028remote"
        git(self.repo, "remote", "add", name, "https://example.com/demo.git")
        ref = f"refs/remotes/{name}/topic\u2028part"
        git(self.repo, "update-ref", ref, "HEAD")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertIn(name, [remote.name for remote in st.remotes])
        facts = gmf_module.remote_removal_facts(self.repo, name, DEFAULT_CONFIG)
        self.assertEqual(facts.tracking_refs, [ref])
        self.assertEqual(facts.ref_snapshot[0][0], ref)

    def test_remote_removal_facts_count_what_only_the_remote_still_holds(self):
        git(self.repo, "remote", "add", "github", "https://github.com/example/demo.git")
        only_there = self._commit_only_under("refs/remotes/github/feature")
        git(self.repo, "update-ref", "refs/remotes/github/main", "HEAD")
        git(self.repo, "symbolic-ref", "refs/remotes/github/HEAD", "refs/remotes/github/main")
        git(self.repo, "branch", "--track", "tracker", "refs/remotes/github/main")

        facts = gmf_module.remote_removal_facts(self.repo, "github", DEFAULT_CONFIG)

        # Die HEAD-Symref ist kein Branch und zählt nicht mit.
        self.assertEqual(
            facts.tracking_refs, ["refs/remotes/github/feature", "refs/remotes/github/main"]
        )
        self.assertEqual(facts.tracking_branches, ["tracker"])
        self.assertEqual(facts.orphan_commits, 1)
        self.assertEqual(facts.remote.fetch_urls, ["https://github.com/example/demo.git"])
        # Sobald ein lokaler Branch den Commit hält, ist nichts mehr verwaist.
        git(self.repo, "branch", "keep", only_there)
        self.assertEqual(
            gmf_module.remote_removal_facts(self.repo, "github", DEFAULT_CONFIG).orphan_commits, 0
        )
        self.assertIsNone(gmf_module.remote_removal_facts(self.repo, "nirgends", DEFAULT_CONFIG))

    def test_remote_removal_counts_the_real_graph_despite_replace_refs(self):
        git(self.repo, "remote", "add", "github", "https://github.com/example/demo.git")
        base = git(self.repo, "rev-parse", "HEAD")
        for message in ("eins", "zwei"):
            git(self.repo, "commit", "-q", "--allow-empty", "-m", message)
        tip = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/github/topic", tip)
        git(self.repo, "reset", "-q", "--hard", base)
        git(self.repo, "replace", tip, base)

        facts = gmf_module.remote_removal_facts(self.repo, "github", DEFAULT_CONFIG)

        self.assertEqual(facts.orphan_commits, 2)

    def test_remote_removal_tracks_upstreams_even_with_an_ambiguous_tag(self):
        git(self.repo, "remote", "add", "github", "https://github.com/example/demo.git")
        git(self.repo, "update-ref", "refs/remotes/github/main", "HEAD")
        git(self.repo, "branch", "--track", "tracker", "refs/remotes/github/main")
        git(self.repo, "tag", "github/main")

        facts = gmf_module.remote_removal_facts(self.repo, "github", DEFAULT_CONFIG)

        self.assertEqual(facts.tracking_branches, ["tracker"])

    def test_x_removes_the_selected_remote_after_a_typed_confirmation(self):
        git(self.repo, "remote", "add", "github", "https://github.com/example/demo.git")
        git(self.repo, "update-ref", "refs/remotes/github/main", "HEAD")
        # Tab wählt github (origin steht als Sync-Remote vorn), X öffnet den Dialog.
        ui, asked = self._remove_remote_ui([9, ord("x"), ord("q")])

        after = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual([remote.name for remote in after.remotes], ["origin"])
        self.assertEqual(ui.message, gmf_module.t("remove_done", r="github"))
        self.assertIn("✔ repo: git remote remove github", gmf_module.COMMAND_LOG)
        # Die Tracking-Refs sind mit dem Remote weg — genau das, wovor der
        # Dialog warnt.
        self.assertEqual(git(self.repo, "for-each-ref", "refs/remotes/github/"), "")
        self.assertEqual(asked["title"], gmf_module.t("remove_title", rel="repo", r="github"))
        self.assertEqual(asked["question"], gmf_module.t("remove_confirm", r="github"))
        lines = asked["lines"]
        self.assertIn(
            "fetch+push: https://github.com/example/demo.git", [line.lower() for line in lines]
        )
        self.assertIn(gmf_module.t("remove_effect_refs", r="github", n=1), lines)
        self.assertIn(gmf_module.t("remove_no_orphans"), lines)
        for line in gmf_module.t("remove_irreversible").splitlines():
            self.assertIn(line, lines)
        self.assertNotIn(gmf_module.t("remove_sync_warning", r="github"), lines)
        self.assertEqual(lines[-1], gmf_module.t("remove_command") + " git remote remove github")

    def test_x_dialog_counts_the_commits_that_would_lose_their_last_reference(self):
        git(self.repo, "remote", "add", "github", "https://github.com/example/demo.git")
        self._commit_only_under("refs/remotes/github/feature")
        # Der Tracking-Branch hängt an einem anderen Ref des Remotes; `feature`
        # bleibt der einzige Halter seines Commits.
        git(self.repo, "update-ref", "refs/remotes/github/main", "HEAD")
        git(self.repo, "branch", "--track", "tracker", "refs/remotes/github/main")

        ui, asked = self._remove_remote_ui([9, ord("x"), ord("q")], answer=False)

        lines = asked["lines"]
        for line in gmf_module.t("remove_orphans", n=1).splitlines():
            self.assertIn(line, lines)
        self.assertNotIn(gmf_module.t("remove_no_orphans"), lines)
        self.assertIn(gmf_module.t("remove_effect_upstream", r="github", b="tracker"), lines)
        self.assertEqual(ui.message, gmf_module.t("remove_cancelled"))

    def test_x_warns_about_the_sync_remote_and_declining_removes_nothing(self):
        ui, asked = self._remove_remote_ui([ord("x"), ord("q")], answer=False)

        after = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual([remote.name for remote in after.remotes], ["origin"])
        self.assertEqual(ui.message, gmf_module.t("remove_cancelled"))
        self.assertIn(gmf_module.t("remove_sync_warning", r="origin"), asked["lines"])
        self.assertTrue(
            any(
                line.startswith("⊘ repo: git remote remove origin")
                for line in gmf_module.COMMAND_LOG
            )
        )
        self.assertFalse(
            any(line.startswith("✔") and "remote remove" in line for line in gmf_module.COMMAND_LOG)
        )

    def test_x_on_a_branch_block_removes_nothing(self):
        git(self.repo, "branch", "keep-me")
        # Shift-Tab vom ersten Block aus landet auf dem letzten: einem Branch.
        ui, asked = self._remove_remote_ui([curses.KEY_BTAB, ord("x"), ord("q")])

        self.assertEqual(asked, {})
        self.assertEqual(
            [
                remote.name
                for remote in collect_status(self.repo, self.root, DEFAULT_CONFIG).remotes
            ],
            ["origin"],
        )
        self.assertIn(
            "keep-me", {branch.name for branch in read_branches(self.repo, DEFAULT_CONFIG)}
        )
        self.assertFalse(
            any("remote remove" in line or "branch -d" in line for line in gmf_module.COMMAND_LOG)
        )
        self.assertIn(gmf_module.t("info_remove_remote_only"), ui.scr.written)

    def test_x_aborts_when_the_remote_changes_between_preview_and_yes(self):
        git(self.repo, "remote", "add", "github", "https://github.com/example/demo.git")

        def change_then_agree():
            # Ein Terminal nebenan verschiebt das Remote, während die Frage offen ist.
            git(self.repo, "remote", "set-url", "github", "https://github.com/example/other.git")
            return True

        ui, asked = self._remove_remote_ui([9, ord("x"), ord("q")], answer=change_then_agree)

        self.assertEqual(ui.message, gmf_module.t("remove_changed"))
        self.assertEqual(
            [
                remote.name
                for remote in collect_status(self.repo, self.root, DEFAULT_CONFIG).remotes
            ],
            ["origin", "github"],
        )
        self.assertTrue(
            any(
                line.startswith("⊘ repo: git remote remove github")
                for line in gmf_module.COMMAND_LOG
            )
        )
        self.assertFalse(any(line.startswith("✔") for line in gmf_module.COMMAND_LOG))

    def test_x_aborts_when_a_fetch_adds_tracking_refs_before_the_yes(self):
        git(self.repo, "remote", "add", "github", "https://github.com/example/demo.git")

        def fetch_lands_then_agree():
            git(self.repo, "update-ref", "refs/remotes/github/late", "HEAD")
            return True

        ui, _ = self._remove_remote_ui([9, ord("x"), ord("q")], answer=fetch_lands_then_agree)

        self.assertEqual(ui.message, gmf_module.t("remove_changed"))
        self.assertEqual(
            git(self.repo, "for-each-ref", "--format=%(refname)", "refs/remotes/github/"),
            "refs/remotes/github/late",
        )

    def test_x_aborts_when_a_fetch_moves_an_existing_tracking_ref(self):
        git(self.repo, "remote", "add", "github", "https://github.com/example/demo.git")
        git(self.repo, "update-ref", "refs/remotes/github/main", "HEAD")

        def fetch_moves_ref_then_agree():
            # Gleicher Refname und gleiche Orphan-Zahl, aber ein anderer Commit:
            # Genau dieser Wechsel muss die alte Bestaetigung ungueltig machen.
            git(self.repo, "commit", "-q", "--allow-empty", "-m", "spaeter")
            git(self.repo, "update-ref", "refs/remotes/github/main", "HEAD")
            return True

        ui, _ = self._remove_remote_ui([9, ord("x"), ord("q")], answer=fetch_moves_ref_then_agree)

        self.assertEqual(ui.message, gmf_module.t("remove_changed"))
        self.assertIn(
            "github",
            [
                remote.name
                for remote in collect_status(self.repo, self.root, DEFAULT_CONFIG).remotes
            ],
        )
        self.assertEqual(
            git(self.repo, "rev-parse", "refs/remotes/github/main"),
            git(self.repo, "rev-parse", "HEAD"),
        )

    def test_info_view_maps_remote_blocks_to_lines(self):
        git(self.repo, "remote", "add", "github", "https://github.com/example/demo.git")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        view = build_info_view(st, DEFAULT_CONFIG)
        self.assertEqual(view.remote_names, ["origin", "github"])
        for name, first, last in view.remote_blocks:
            self.assertIn(name, view.lines[first])
            self.assertLessEqual(first, last)
            # Der Block endet vor dem naechsten Remote-Kopf.
            self.assertTrue(
                all(
                    not line.startswith("  " + other)
                    for other in view.remote_names
                    if other != name
                    for line in view.lines[first : last + 1]
                )
            )
        # Der Textmodus bleibt unveraendert nutzbar (CLI/Tests).
        self.assertEqual(repo_info_lines(st, DEFAULT_CONFIG), view.lines)


class RepoTransportOverrideTests(unittest.TestCase):
    """Transporteinstellungen aus der Config DES REPOS sperren die Übertragung.

    `core.gitProxy` (für `git://`), `http.proxy` samt seiner adressgebundenen
    Form und ein `http.sslVerify = false` entscheiden, wohin eine Übertragung
    wirklich geht beziehungsweise ob die geprüfte Adresse überhaupt
    nachgewiesen wird — dieselbe Frage wie bei `core.sshCommand`. Anders als
    dort lässt sich keine davon für einen einzelnen Aufruf ausschalten:
    `core.gitproxy` ist MEHRWERTIG (ein zusätzlicher Wert stellt sich nur
    daneben, das Skript des Repos lief trotzdem), und ein erzwungenes leeres
    `http.proxy` schaltete auch den echten Firmen-Proxy aus der globalen Config
    ab. Deshalb wird abgelehnt statt gegengesteuert. Global gesetzt bleibt
    alles erlaubt: Dort hat der Mensch es selbst hingeschrieben.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.global_config = self.root / "gitconfig"
        self.global_config.write_text("")
        self.included = self.root / "extra.cfg"
        self.marker = self.root / "proxy-ran"
        self.proxy = self.root / "proxy.sh"
        self.proxy.write_text(f"#!/bin/sh\nprintf ran >> {shlex.quote(str(self.marker))}\nexit 7\n")
        self.proxy.chmod(0o755)
        self.bare = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", str(self.bare))
        self.repo = self.root / "work"
        git(self.root, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "a.txt").write_text("a\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-qm", "eins")
        git(self.repo, "remote", "add", "origin", str(self.bare))
        git(self.repo, "push", "-q", "-u", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        (self.repo / "a.txt").write_text("b\n")
        git(self.repo, "commit", "-qam", "zwei")
        self.env = mock.patch.dict(
            os.environ, {"GIT_CONFIG_GLOBAL": str(self.global_config), "GIT_CONFIG_NOSYSTEM": "1"}
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _remote(self):
        return gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["origin"]

    def _reasons(self) -> tuple:
        return (
            gmf_module.repo_transport_override(self.repo, 10),
            gmf_module.fetch_remote_block_reason(self.repo, self._remote(), "main", 10),
            inspect_transfer(self.repo, "origin", "main", "push", 10).reason,
            check_remote(self.repo, "origin", 10)[0],
        )

    def _append(self, text: str) -> None:
        with (self.repo / ".git" / "config").open("a") as handle:
            handle.write(text)

    def test_an_untouched_repository_transfers_normally(self):
        self.assertEqual(self._reasons(), (None, None, "ready", "ok"))

    def test_the_repo_config_blocks_fetch_push_and_check(self):
        git(self.repo, "config", "core.gitProxy", str(self.proxy))
        self.assertEqual(
            self._reasons(),
            (
                "core.gitProxy",
                ("unsafe_transport", "core.gitProxy"),
                "unsafe-transport",
                "unsafe_transport",
            ),
        )

    def test_an_http_proxy_in_the_repo_config_blocks_too(self):
        self._append("[http]\n\tproxy = http://127.0.0.1:9\n")
        self.assertEqual(
            self._reasons(),
            (
                "http.proxy",
                ("unsafe_transport", "http.proxy"),
                "unsafe-transport",
                "unsafe_transport",
            ),
        )

    def test_the_address_bound_form_of_http_proxy_is_no_back_door(self):
        """`http.<url>.proxy` tut dasselbe, nur für eine bestimmte Adresse."""
        self._append('[http "https://example.com/"]\n\tproxy = http://127.0.0.1:9\n')
        self.assertEqual(gmf_module.repo_transport_override(self.repo, 10), "http.proxy")

    def test_sslverify_false_in_the_repo_config_blocks(self):
        """Ohne Nachweis der Gegenstelle ist die geprüfte Adresse austauschbar."""
        self._append("[http]\n\tsslVerify = false\n")
        self.assertEqual(
            self._reasons(),
            (
                "http.sslVerify=false",
                ("unsafe_transport", "http.sslVerify=false"),
                "unsafe-transport",
                "unsafe_transport",
            ),
        )

    def test_sslverify_true_is_the_normal_case_and_never_blocks(self):
        """Gegenprobe: Hier ist nur der Wert `false` ein Grund, nicht der Schlüssel."""
        self._append("[http]\n\tsslVerify = true\n")
        self.assertEqual(self._reasons(), (None, None, "ready", "ok"))
        # Auch die adressgebundene Form mit `true` bleibt erlaubt.
        self._append('[http "https://example.com/"]\n\tsslVerify = true\n')
        self.assertIsNone(gmf_module.repo_transport_override(self.repo, 10))

    def test_an_unparsable_boolean_counts_as_not_provable(self):
        self._append("[http]\n\tsslVerify = vielleicht\n")
        self.assertEqual(gmf_module.repo_transport_override(self.repo, 10), "http.sslVerify=false")

    def test_the_same_settings_from_the_global_config_stay_allowed(self):
        """Gegenprobe: Global hat der Mensch sie selbst hingeschrieben.

        Ohne diese Grenze wäre die Regel keine Aussage über das Repo, sondern
        ein Verbot der Einstellungen — und jeder Rechner hinter einem echten
        Firmen-Proxy könnte nichts mehr übertragen.
        """
        self.global_config.write_text(
            f"[core]\n\tgitProxy = {self.proxy}\n"
            "[http]\n\tproxy = http://firmenproxy:3128\n\tsslVerify = false\n"
        )
        self.assertEqual(self._reasons(), (None, None, "ready", "ok"))

    def test_outer_config_includes_stay_allowed(self):
        """Explizite Scopes müssen dieselben Includes wie die Gesamtlesung sehen."""
        system = self.root / "system.cfg"
        self.included.write_text("[http]\n proxy = http://proxy.example:8080\n sslVerify = false\n")
        for scope in (self.global_config, system):
            for condition in (
                "include",
                f'includeIf "gitdir:{self.repo}/.git"',
                'includeIf "onbranch:main"',
            ):
                with self.subTest(scope=scope.name, condition=condition):
                    self.global_config.write_text("")
                    system.write_text("")
                    scope.write_text(f"[{condition}]\n path = {self.included}\n")
                    with mock.patch.dict(
                        os.environ, {"GIT_CONFIG_SYSTEM": str(system), "GIT_CONFIG_NOSYSTEM": "0"}
                    ):
                        self.assertEqual(self._reasons(), (None, None, "ready", "ok"))
                        # Derselbe Eintrag aus dem Repo bleibt verboten.
                        git(self.repo, "config", "http.proxy", "http://local.example:9")
                        self.assertEqual(
                            gmf_module.repo_transport_override(self.repo, 10), "http.proxy"
                        )
                        git(self.repo, "config", "--unset-all", "http.proxy")

    def test_disabled_system_config_does_not_block_a_safe_repo(self):
        system = self.root / "system.cfg"
        system.write_text("[http]\n proxy = http://proxy.example:8080\n sslVerify = false\n")
        for disabled in ("1", "true", "yes", "on", "2"):
            with (
                self.subTest(disabled=disabled),
                mock.patch.dict(
                    os.environ, {"GIT_CONFIG_SYSTEM": str(system), "GIT_CONFIG_NOSYSTEM": disabled}
                ),
            ):
                self.assertIsNone(gmf_module.repo_transport_override(self.repo, 10))
                git(self.repo, "config", "http.proxy", "http://repo.example:9")
                self.assertEqual(gmf_module.repo_transport_override(self.repo, 10), "http.proxy")
                git(self.repo, "config", "--unset-all", "http.proxy")

    def test_a_value_reached_through_an_include_is_caught_too(self):
        """`include.path` aus `.git/config` ist genauso repo-seitig.

        Eine Prüfung nur auf `git config --local` übersieht diesen Weg: Der
        Wert steht dann in einer anderen Datei, und die Entscheidung, sie
        einzubinden, traf die Repo-Config.
        """
        self.included.write_text(f"[core]\n\tgitProxy = {self.proxy}\n")
        self._append(f"[include]\n\tpath = {self.included}\n")
        self.assertEqual(
            self._reasons(),
            (
                "core.gitProxy",
                ("unsafe_transport", "core.gitProxy"),
                "unsafe-transport",
                "unsafe_transport",
            ),
        )

    def test_a_global_value_plus_a_repo_value_is_still_blocked(self):
        self.global_config.write_text(
            f"[core]\n\tgitProxy = {self.proxy}\n[http]\n\tproxy = http://firmenproxy:3128\n"
        )
        git(self.repo, "config", "core.gitProxy", str(self.proxy))
        self.assertEqual(gmf_module.repo_transport_override(self.repo, 10), "core.gitProxy")
        git(self.repo, "config", "--unset-all", "core.gitProxy")
        self._append("[http]\n\tproxy = http://127.0.0.1:9\n")
        self.assertEqual(gmf_module.repo_transport_override(self.repo, 10), "http.proxy")

    def test_the_scan_says_so_and_never_starts_the_proxy(self):
        git(self.repo, "remote", "set-url", "origin", "git://127.0.0.1/repo.git")
        git(self.repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        git(self.repo, "config", "core.gitProxy", str(self.proxy))

        st = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        self.assertEqual(
            [(r.name, r.fetch_outcome) for r in st.remotes], [("origin", "unsafe_transport")]
        )
        self.assertEqual(st.error, "origin: " + gmf_module.TR["short_unsafe_transport"]["en"])
        self.assertFalse(self.marker.exists(), "das Proxy-Skript des Repos entschied über das Ziel")

    def test_the_message_names_the_one_setting_in_both_languages(self):
        """Nicht alle drei aufzaehlen: Gesetzt ist genau eine davon."""
        for lang in ("en", "de"):
            for setting in ("core.gitProxy", "http.proxy", "http.sslVerify=false"):
                with self.subTest(lang=lang, setting=setting):
                    with mock.patch.object(gmf_module, "UI_LANG", lang):
                        message = remote_check_message("origin", "unsafe_transport", 0, setting, 30)
                    self.assertIn(setting, message)
                    self.assertIn("origin", message)
                    for other in ("core.gitProxy", "http.proxy"):
                        if other not in setting:
                            self.assertNotIn(other, message)

    def test_an_unreadable_config_counts_as_not_provable(self):
        """Fail closed: Ohne lesbare Config lässt sich nichts belegen."""
        broken = subprocess.CompletedProcess(["git", "config"], 128, "", "boom")
        with mock.patch.object(gmf_module, "run_git", return_value=broken):
            self.assertEqual(gmf_module.repo_transport_override(self.repo, 10), "core.gitProxy")

    def test_unknown_or_incomplete_config_scope_is_not_trusted(self):
        for output in ("future\0core.gitproxy\nvalue\0", "local\0", "global\0\0"):
            with self.subTest(output=output):
                result = subprocess.CompletedProcess(["git", "config"], 0, output, "")
                with mock.patch.object(gmf_module, "run_git", return_value=result):
                    self.assertEqual(
                        gmf_module.repo_transport_override(self.repo, 10), "core.gitProxy"
                    )

    def test_a_proxy_value_never_leaves_the_check(self):
        """Eine Proxy-Adresse kann Zugangsdaten tragen.

        Sie darf deshalb weder in einer Meldung noch im Befehlsprotokoll
        landen. Der Vergleich sieht bei den Proxy-Einträgen nur die
        Schlüsselnamen an; nur die Wahrheitswerte von `sslVerify` liest er,
        und die normalisiert Git vorher auf `true`/`false`.
        """
        geheim = "http://benutzer:s3cr3t@proxy.example:3128"
        self._append(f"[http]\n\tproxy = {geheim}\n")
        gmf_module.COMMAND_LOG.clear()
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        spuren = "\n".join(
            [
                st.error,
                st.error_long,
                st.error_detail,
                *gmf_module.COMMAND_LOG,
                json.dumps(gmf_module.status_dict(st)),
            ]
        )
        self.assertNotIn("s3cr3t", spuren)
        self.assertNotIn("proxy.example", spuren)


class DetachedHeadFetchTests(unittest.TestCase):
    """Ein detached HEAD ist ein gewöhnlicher Zustand, kein Sicherheitsproblem.

    Ohne ausgecheckten Branch gibt es keinen Tracking-Ref, den ein Fetch
    aktualisieren könnte. Bis 2026-09-03 warf `fetch_remote_block_reason()`
    diesen Fall mit einer wirklich unsicheren Refspec zusammen: `R` (und
    `--fetch`) meldeten für jedes Remote „unsichere Fetch-Refspec", färbten das
    Repo rot und ersetzten den ehrlichen Zustand `detached` durch `error` —
    eine Falschaussage über eine völlig gewöhnliche Konfiguration.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        bare = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", str(bare))
        self.repo = self.root / "work"
        git(self.root, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "a.txt").write_text("a\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-qm", "eins")
        git(self.repo, "remote", "add", "origin", str(bare))
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "checkout", "-q", "--detach", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def test_setup_really_is_detached_with_an_ordinary_refspec(self):
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.repo), "symbolic-ref", "-q", "HEAD"], capture_output=True
            ).returncode,
            1,
        )
        self.assertEqual(
            git(self.repo, "config", "remote.origin.fetch"), "+refs/heads/*:refs/remotes/origin/*"
        )

    def test_block_reason_names_the_missing_branch_not_the_refspec(self):
        remote = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["origin"]
        # Die Refspec selbst ist einwandfrei — nur der Branch fehlt.
        self.assertTrue(gmf_module.fetch_refspecs_safe(remote))
        self.assertEqual(
            gmf_module.fetch_remote_block_reason(self.repo, remote, "(detached)", 10),
            ("detached", ""),
        )
        self.assertEqual(
            gmf_module.fetch_remote_block_reason(self.repo, remote, "?", 10), ("detached", "")
        )

    def test_message_for_the_detached_case_does_not_accuse_the_refspec(self):
        message = remote_check_message("origin", "detached", 0, "", 30)
        self.assertNotIn("refspec", message.lower())
        self.assertIn("origin", message)
        self.assertIn("detached", message.lower())
        self.assertNotEqual(message, remote_check_message("origin", "unsafe_refspec", 0, "", 30))

    def test_fetch_scan_leaves_the_detached_state_intact(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        self.assertEqual(st.branch, "(detached)")
        self.assertEqual(st.error, "")
        self.assertEqual(st.remote_state, "detached")
        self.assertFalse(st.fetch_error)
        self.assertEqual(
            [(r.name, r.fetch_failed, r.fetch_outcome) for r in st.remotes], [("origin", False, "")]
        )

    def test_a_truly_unsafe_refspec_is_still_blocked_as_such(self):
        """Gegenprobe: Der neue Zweig darf keine echte Gefahr durchlassen."""
        git(self.repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/fremd/*")
        remote = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["origin"]
        self.assertEqual(
            gmf_module.fetch_remote_block_reason(self.repo, remote, "main", 10),
            ("unsafe_refspec", ""),
        )
        git(self.repo, "checkout", "-q", "main")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        self.assertTrue(st.fetch_error)
        self.assertEqual([r.fetch_outcome for r in st.remotes], ["unsafe_refspec"])


class TransferMessageTests(unittest.TestCase):
    """Jeder Ablehnungsgrund von `inspect_transfer()` bekommt einen Satz.

    `_transfer_message()` wurde von keinem Test ausgefuehrt. Wird ein Grund
    umbenannt, faellt sie stumm auf `return check.reason` durch und der
    Anwender liest ein internes Stichwort statt einer Auskunft.
    """

    def test_every_reason_becomes_a_sentence(self):
        ui = TUI(None, Path("/tmp"), DEFAULT_CONFIG, None)
        reasons = set(get_args(gmf_module.TransferReason)) - {"ready"}
        self.assertIn("dirty", reasons)
        for reason in sorted(reasons):
            with self.subTest(reason=reason):
                check = gmf_module.TransferCheck(reason, ahead=2, behind=3)
                message = ui._transfer_message(check, "origin", "main")
                self.assertNotEqual(message, reason)
                self.assertTrue(message.strip())

    def test_the_blocked_transport_message_names_the_one_setting(self):
        """Der Beleg kommt aus dem Preflight, nicht aus einer zweiten Messung.

        `inspect_transfer()` verwarf den Namen der gesetzten Einstellung, und
        die Meldung las ihn mit einem zweiten `repo_transport_override()`
        nach — neun weitere `git config`-Aufrufe, und aenderte sich die Config
        dazwischen, zaehlte sie alle drei Einstellungen auf statt der einen.
        """
        ui = TUI(None, Path("/tmp"), DEFAULT_CONFIG, None)
        blocked = gmf_module.TransferCheck("unsafe-transport", evidence="http.sslVerify=false")
        named = ui._transfer_message(blocked, "origin", "main")
        self.assertIn("http.sslVerify=false", named)
        self.assertNotIn("core.gitProxy", named)
        # Ohne Beleg bleibt die vollstaendige Aufzaehlung die Rueckfallebene.
        every = ui._transfer_message(gmf_module.TransferCheck("unsafe-transport"), "origin", "main")
        self.assertIn("core.gitProxy", every)
