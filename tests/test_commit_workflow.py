import curses
import os
import shlex
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from gmf_test_support import FakeScreen, git

import gitmaster_flash as gmf_module
from gitmaster_flash import (
    DEFAULT_CONFIG,
    TUI,
    ChangedFile,
    CommitSafetyError,
    RepoStatus,
    collect_status,
    commit_selected,
    inspect_transfer,
    safe_push_args,
)


class CommitSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "include.txt").write_text("base\n")
        (self.repo / "skip.txt").write_text("base\n")
        git(self.repo, "add", "include.txt", "skip.txt")
        git(self.repo, "commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def _status(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_temporary_index_commits_only_approved_path_and_keeps_other_staging(self):
        (self.repo / "include.txt").write_text("approved\n")
        (self.repo / "skip.txt").write_text("staged but excluded\n")
        git(self.repo, "add", "skip.txt")
        r = commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        changed = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertEqual(changed, ["include.txt"])
        # Das bewusste Staging einer NICHT committeten Datei bleibt erhalten …
        self.assertIn(
            "skip.txt",
            subprocess.run(
                ["git", "-C", str(self.repo), "diff", "--cached", "--name-only"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout,
        )
        # … die committete Datei dagegen ist erledigt und verschwindet aus dem Status.
        self.assertNotIn("include.txt", self._status())

    def test_magic_filename_cannot_expand_into_foreign_staging(self):
        magic = ":(glob)*.txt"
        matching = "matching.txt"
        (self.repo / magic).write_text("base magic\n")
        (self.repo / matching).write_text("base matching\n")
        git(self.repo, "add", "--", magic, matching)
        git(self.repo, "commit", "-qm", "magic names")
        (self.repo / magic).write_text("approved magic\n")
        (self.repo / matching).write_text("foreign staging\n")
        git(self.repo, "add", "--", matching)

        result = commit_selected(self.repo, [magic], "selected", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        changed = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertEqual(changed, [magic])
        cached = subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--cached", "--name-only"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertEqual(cached, [matching])

    def test_existing_head_replace_ref_cannot_change_the_approved_base_tree(self):
        head = gmf_module.current_head(self.repo, 10)
        temp_index = Path(self.tmp.name) / "replacement-index"
        env = {**os.environ, "GIT_INDEX_FILE": str(temp_index)}
        subprocess.run(
            ["git", "-C", str(self.repo), "read-tree", head],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
        (self.repo / "foreign.txt").write_text("foreign\n")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "--", "foreign.txt"],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
        replacement_tree = subprocess.run(
            ["git", "-C", str(self.repo), "write-tree"],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        ).stdout.strip()
        replacement = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", replacement_tree, "-m", "replacement"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (self.repo / "foreign.txt").unlink()
        git(self.repo, "replace", head, replacement)
        (self.repo / "include.txt").write_text("approved\n")

        result = commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        tree_paths = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "core.useReplaceRefs=false",
                "ls-tree",
                "-r",
                "--name-only",
                result.committed_head,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertNotIn("foreign.txt", tree_paths)

    def test_commit_helper_refuses_a_conflict_free_merge(self):
        """Ein selektiver Baum darf keinen Merge mit unvollständigem Inhalt abschließen."""
        git(self.repo, "switch", "-qc", "feature")
        (self.repo / "feature-a.txt").write_text("a\n")
        (self.repo / "feature-b.txt").write_text("b\n")
        git(self.repo, "add", "feature-a.txt", "feature-b.txt")
        git(self.repo, "commit", "-qm", "feature")
        git(self.repo, "switch", "-q", "main")
        git(self.repo, "merge", "--no-ff", "--no-commit", "feature")
        head_before = gmf_module.current_head(self.repo, 10)
        index_before = (self.repo / ".git" / "index").read_bytes()

        with self.assertRaisesRegex(CommitSafetyError, "merge|sequencer"):
            commit_selected(self.repo, ["feature-a.txt"], "partial merge", 10)

        self.assertEqual(gmf_module.current_head(self.repo, 10), head_before)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index_before)

    def test_committed_file_is_clean_afterwards(self):
        """Der echte Index muss den Commit übernehmen, sonst lügt die Anzeige.

        Vorher blieb er auf dem Stand von vor dem Commit stehen: `git status` meldete
        die Datei als `MM`, obwohl Arbeitsbaum und HEAD längst identisch waren — in
        gmf sah es aus, als wäre der Commit gar nicht passiert.
        """
        head_before = gmf_module.current_head(self.repo, 10)
        (self.repo / "include.txt").write_text("approved\n")
        r = commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.committed_head, gmf_module.current_head(self.repo, 10))
        self.assertEqual(
            gmf_module.commit_undo_command(head_before, r.committed_head, r.approved_ref),
            f"git -c core.hooksPath=/dev/null update-ref --no-deref "
            f"refs/heads/main "
            f"{head_before} {r.committed_head}",
        )
        self.assertEqual(r.approved_head, head_before)
        self.assertEqual(r.approved_ref, "refs/heads/main")
        self.assertEqual(self._status(), "")
        st = collect_status(self.repo, self.repo.parent, DEFAULT_CONFIG)
        self.assertEqual((st.modified, st.deleted, st.untracked), (0, 0, 0))

    def test_real_index_adoption_refuses_a_moved_head(self):
        committed = gmf_module.current_head(self.repo, 10)
        (self.repo / "later.txt").write_text("later\n")
        git(self.repo, "add", "later.txt")
        git(self.repo, "commit", "-qm", "later")
        index_before = (self.repo / ".git" / "index").read_bytes()
        signature = gmf_module._real_index_signature(self.repo, 10)

        with self.assertRaisesRegex(CommitSafetyError, "HEAD changed"):
            gmf_module.adopt_commit_in_real_index(
                self.repo, ["include.txt"], committed, "refs/heads/main", signature, 10
            )

        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index_before)

    def test_a_killed_adoption_leaves_no_lock_file_behind_in_git(self):
        """Git legt neben JEDEN Index eine `.lock`; nach SIGKILL bleibt sie.

        Der temporaere Index der Index-Uebernahme liegt in `.git/` selbst (der
        des Commits in einem TemporaryDirectory, das mit ihm verschwindet).
        Das `finally` entfernte nur den Index, nie seine Lockdatei — nach
        jedem Adoptions-Timeout blieb also eine Datei im `.git` des Benutzers
        stehen, bei jedem Versuch eine weitere (Review-Fund 2026-09-10).
        """
        committed = gmf_module.current_head(self.repo, 10)
        signature = gmf_module._real_index_signature(self.repo, 10)
        original = gmf_module.run_git

        def leave_a_lock(repo, *args, **kwargs):
            temp = (kwargs.get("env") or {}).get("GIT_INDEX_FILE")
            if temp and "reset" in args:
                # So sieht der Arbeitsbaum aus, wenn Git per SIGKILL endet,
                # bevor es seine eigene Lockdatei aufraeumen konnte.
                Path(temp + ".lock").write_text("gmf test\n")
                raise subprocess.TimeoutExpired(["git", "reset"], 1)
            return original(repo, *args, **kwargs)

        with (
            mock.patch.object(gmf_module, "run_git", side_effect=leave_a_lock),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            gmf_module.adopt_commit_in_real_index(
                self.repo, ["include.txt"], committed, "refs/heads/main", signature, 10
            )

        leftovers = sorted(
            p.name for p in (self.repo / ".git").iterdir() if p.name.startswith("gmf-adopt-index-")
        )
        self.assertEqual(leftovers, [])

    def test_commit_undo_refuses_a_branch_that_moved_again(self):
        before = gmf_module.current_head(self.repo, 10)
        (self.repo / "include.txt").write_text("approved\n")
        result = commit_selected(self.repo, ["include.txt"], "selected", 10)
        (self.repo / "later.txt").write_text("later\n")
        git(self.repo, "add", "later.txt")
        git(self.repo, "commit", "-qm", "later")
        moved = gmf_module.current_head(self.repo, 10)

        undo = shlex.split(
            gmf_module.commit_undo_command(
                result.approved_head, result.committed_head, result.approved_ref
            )
        )[1:]
        attempted = subprocess.run(
            ["git", "-C", str(self.repo), *undo], capture_output=True, text=True
        )

        self.assertNotEqual(attempted.returncode, 0)
        self.assertEqual(gmf_module.current_head(self.repo, 10), moved)
        self.assertNotEqual(moved, before)

    def test_commit_undo_stays_bound_to_the_original_branch(self):
        before = gmf_module.current_head(self.repo, 10)
        (self.repo / "include.txt").write_text("approved\n")
        result = commit_selected(self.repo, ["include.txt"], "selected", 10)
        git(self.repo, "branch", "other", result.committed_head)
        git(self.repo, "switch", "-q", "other")

        undo = shlex.split(
            gmf_module.commit_undo_command(
                result.approved_head, result.committed_head, result.approved_ref
            )
        )[1:]
        attempted = subprocess.run(
            ["git", "-C", str(self.repo), *undo], capture_output=True, text=True
        )

        self.assertEqual(attempted.returncode, 0, attempted.stderr)
        self.assertEqual(gmf_module.current_head(self.repo, 10), result.committed_head)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "refs/heads/main"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            before,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "refs/heads/other"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            result.committed_head,
        )

    def test_commit_undo_never_dereferences_a_symbolic_branch(self):
        before = gmf_module.current_head(self.repo, 10)
        (self.repo / "include.txt").write_text("approved\n")
        result = commit_selected(self.repo, ["include.txt"], "selected", 10)
        git(self.repo, "branch", "other", result.committed_head)
        git(self.repo, "symbolic-ref", "refs/heads/main", "refs/heads/other")

        undo = shlex.split(
            gmf_module.commit_undo_command(
                result.approved_head, result.committed_head, result.approved_ref
            )
        )[1:]
        attempted = subprocess.run(
            ["git", "-C", str(self.repo), *undo], capture_output=True, text=True
        )

        self.assertEqual(attempted.returncode, 0, attempted.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/main"), before)
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/other"), result.committed_head)
        symbolic = subprocess.run(
            ["git", "-C", str(self.repo), "symbolic-ref", "-q", "refs/heads/main"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(symbolic.returncode, 0)
        self.assertNotEqual(result.committed_head, before)

    def test_commit_on_detached_head_is_blocked(self):
        git(self.repo, "switch", "--detach", "-q")
        head_before = gmf_module.current_head(self.repo, 10)
        (self.repo / "include.txt").write_text("approved\n")

        with self.assertRaisesRegex(CommitSafetyError, "detached HEAD"):
            commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertEqual(gmf_module.current_head(self.repo, 10), head_before)

    def test_head_read_timeout_never_starts_a_commit(self):
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        calls = []

        def record(target_repo, *args, **kwargs):
            calls.append(args)
            return original(target_repo, *args, **kwargs)

        timeout = subprocess.TimeoutExpired(["git", "rev-parse", "HEAD"], 10)
        with (
            mock.patch.object(gmf_module, "current_head", side_effect=timeout),
            mock.patch.object(gmf_module, "run_git", side_effect=record),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertFalse(any("commit" in args for args in calls))

    def test_commit_rolled_back_if_direct_head_change_targets_another_branch(self):
        before = gmf_module.current_head(self.repo, 10)
        git(self.repo, "branch", "other")
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        switched = False

        def race(target_repo, *args, **kwargs):
            nonlocal switched
            if "commit" in args and not switched:
                switched = True
                # `git switch` wird inzwischen schon vom Index-Lock gesperrt.
                # Ein direkter symbolischer Ref-Wechsel umgeht den Index bewusst
                # und prüft weiterhin den nachgelagerten Branch-CAS-Rollback.
                git(self.repo, "symbolic-ref", "HEAD", "refs/heads/other")
            return original(target_repo, *args, **kwargs)

        with (
            mock.patch.object(gmf_module, "run_git", side_effect=race),
            self.assertRaisesRegex(CommitSafetyError, "another branch.*rolled back"),
        ):
            commit_selected(self.repo, ["include.txt"], "selected", 10)

        for ref in ("refs/heads/main", "refs/heads/other"):
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(self.repo), "rev-parse", ref],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                before,
            )

    def test_merge_cannot_start_between_the_guard_and_commit(self):
        before = gmf_module.current_head(self.repo, 10)
        git(self.repo, "switch", "-qc", "feature")
        (self.repo / "feature.txt").write_text("feature\n")
        git(self.repo, "add", "feature.txt")
        git(self.repo, "commit", "-qm", "feature")
        git(self.repo, "switch", "-q", "main")
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        merge_attempt = None

        def race(target_repo, *args, **kwargs):
            nonlocal merge_attempt
            if "commit" in args and merge_attempt is None:
                merge_attempt = subprocess.run(
                    ["git", "-C", str(self.repo), "merge", "--no-ff", "--no-commit", "feature"],
                    capture_output=True,
                    text=True,
                )
            return original(target_repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git", side_effect=race):
            result = commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(merge_attempt)
        self.assertNotEqual(merge_attempt.returncode, 0)
        self.assertEqual(gmf_module.commit_parents(self.repo, result.committed_head, 10), [before])

    def test_temporary_index_is_built_from_the_approved_oid_not_moving_head(self):
        git(self.repo, "switch", "-qc", "other")
        (self.repo / "foreign.txt").write_text("foreign\n")
        git(self.repo, "add", "foreign.txt")
        git(self.repo, "commit", "-qm", "foreign")
        git(self.repo, "switch", "-q", "main")
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        raced = False

        def race(target_repo, *args, **kwargs):
            nonlocal raced
            if "read-tree" in args and not raced:
                raced = True
                git(self.repo, "symbolic-ref", "HEAD", "refs/heads/other")
                result = original(target_repo, *args, **kwargs)
                git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
                return result
            return original(target_repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git", side_effect=race):
            result = commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertTrue(raced)
        self.assertEqual(result.returncode, 0, result.stderr)
        tree_paths = subprocess.run(
            ["git", "-C", str(self.repo), "ls-tree", "-r", "--name-only", result.committed_head],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertNotIn("foreign.txt", tree_paths)

    def test_index_adoption_blocks_a_checkout_between_check_and_update(self):
        git(self.repo, "branch", "other")
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        switch_attempt = None

        def race(target_repo, *args, **kwargs):
            nonlocal switch_attempt
            if (
                "reset" in args
                and "-q" in args
                and kwargs.get("env", {}).get("GIT_INDEX_FILE")
                and "gmf-adopt-index-" in kwargs["env"]["GIT_INDEX_FILE"]
                and switch_attempt is None
            ):
                switch_attempt = subprocess.run(
                    ["git", "-C", str(self.repo), "switch", "-q", "other"],
                    capture_output=True,
                    text=True,
                )
            return original(target_repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git", side_effect=race):
            result = commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(switch_attempt)
        self.assertNotEqual(switch_attempt.returncode, 0)
        self.assertEqual(gmf_module.current_symbolic_head_ref(self.repo, 10), "refs/heads/main")
        self.assertEqual(self._status(), "")

    def test_checkout_after_commit_does_not_roll_back_the_approved_branch(self):
        before = gmf_module.current_head(self.repo, 10)
        git(self.repo, "branch", "other")
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        committed = None

        def race(target_repo, *args, **kwargs):
            nonlocal committed
            result = original(target_repo, *args, **kwargs)
            if (
                committed is None
                and args[:2] == ("rev-parse", "HEAD")
                and result.returncode == 0
                and subprocess.run(
                    ["git", "-C", str(self.repo), "log", "-1", "--format=%s"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                == "selected"
            ):
                committed = result.stdout.strip()
                git(self.repo, "symbolic-ref", "HEAD", "refs/heads/other")
            return result

        with (
            mock.patch.object(gmf_module, "run_git", side_effect=race),
            self.assertRaisesRegex(
                gmf_module.CommitOutcomeUnknownError, "commit remains on the approved branch"
            ),
        ):
            commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertIsNotNone(committed)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "refs/heads/main"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            committed,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "refs/heads/other"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            before,
        )

    def test_foreign_followup_commit_is_never_rolled_back(self):
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        foreign = None

        def race(target_repo, *args, **kwargs):
            nonlocal foreign
            result = original(target_repo, *args, **kwargs)
            if "commit" in args and result.returncode == 0 and foreign is None:
                own = subprocess.run(
                    ["git", "-C", str(self.repo), "rev-parse", "refs/heads/main"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                tree = subprocess.run(
                    ["git", "-C", str(self.repo), "rev-parse", own + "^{tree}"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                foreign = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.repo),
                        "commit-tree",
                        tree,
                        "-p",
                        own,
                        "-m",
                        "foreign followup",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                git(self.repo, "update-ref", "refs/heads/main", foreign, own)
            return result

        with (
            mock.patch.object(gmf_module, "run_git", side_effect=race),
            self.assertRaisesRegex(gmf_module.CommitOutcomeUnknownError, "branch changed"),
        ):
            commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertIsNotNone(foreign)
        self.assertEqual(gmf_module.current_head(self.repo, 10), foreign)

    def test_post_commit_index_adoption_failure_is_distinguishable(self):
        (self.repo / "include.txt").write_text("approved\n")
        before = gmf_module.current_head(self.repo, 10)
        original = gmf_module._acquire_git_lock

        def block_head_lock(path):
            if path.name == "HEAD.lock":
                raise CommitSafetyError("synthetic HEAD lock")
            return original(path)

        with (
            mock.patch.object(gmf_module, "_acquire_git_lock", side_effect=block_head_lock),
            self.assertRaises(gmf_module.CommitAdoptionError) as raised,
        ):
            commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertNotEqual(gmf_module.current_head(self.repo, 10), before)
        self.assertEqual(raised.exception.committed_head, gmf_module.current_head(self.repo, 10))

    def test_post_commit_index_read_failure_is_an_adoption_failure(self):
        (self.repo / "include.txt").write_text("approved\n")
        before = gmf_module.current_head(self.repo, 10)
        original = gmf_module._real_index_signature
        calls = 0

        def fail_after_commit(repo, timeout):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise CommitSafetyError("synthetic index read failure")
            return original(repo, timeout)

        with (
            mock.patch.object(gmf_module, "_real_index_signature", side_effect=fail_after_commit),
            self.assertRaises(gmf_module.CommitAdoptionError) as raised,
        ):
            commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertNotEqual(gmf_module.current_head(self.repo, 10), before)
        self.assertEqual(raised.exception.committed_head, gmf_module.current_head(self.repo, 10))

    def test_commit_forces_its_single_reflog_proof_when_repo_disables_logs(self):
        git(self.repo, "config", "core.logAllRefUpdates", "false")
        (self.repo / "include.txt").write_text("approved\n")
        # Ein geerbtes GIT_CONFIG_PARAMETERS wird spaeter als die
        # GIT_CONFIG_COUNT-Umgebung ausgewertet. Nur das echte `git -c` des
        # Commit-Aufrufs gewinnt auch gegen diesen fremden Override.
        with mock.patch.dict(
            os.environ, {"GIT_CONFIG_PARAMETERS": "'core.logAllRefUpdates'='false'"}
        ):
            result = commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.committed_head, gmf_module.current_head(self.repo, 10))

    def test_head_reflog_marker_counts_as_an_unknown_commit_outcome(self):
        action = "gmf-detached-hook"
        oid = "a" * 40
        reflog = subprocess.CompletedProcess(
            [], 0, f"{oid}\0HEAD@{{0}}\0{action}: commit (initial): hook\n", ""
        )
        with mock.patch.object(gmf_module, "_required_git", return_value=reflog):
            matches = gmf_module._commit_reflog_matches(self.repo, action, 10)
            with self.assertRaises(CommitSafetyError):
                gmf_module._commit_reflog_proof(self.repo, action, 10)

        self.assertEqual(matches, [(oid, "HEAD")])

    def test_timeout_after_git_reported_success_never_claims_no_commit(self):
        (self.repo / "include.txt").write_text("approved\n")
        before = gmf_module.current_head(self.repo, 10)
        timeout = subprocess.TimeoutExpired(["git", "reflog"], 10)
        with (
            mock.patch.object(gmf_module, "_commit_reflog_proof", side_effect=timeout),
            self.assertRaises(gmf_module.CommitOutcomeUnknownError),
        ):
            commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertNotEqual(gmf_module.current_head(self.repo, 10), before)

    def test_read_error_after_git_reported_success_is_not_commit_failed(self):
        (self.repo / "include.txt").write_text("approved\n")
        before = gmf_module.current_head(self.repo, 10)
        with (
            mock.patch.object(
                gmf_module,
                "_commit_reflog_proof",
                side_effect=gmf_module.GitReadError("broken reflog"),
            ),
            self.assertRaises(gmf_module.CommitOutcomeUnknownError),
        ):
            commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertNotEqual(gmf_module.current_head(self.repo, 10), before)

    def test_older_staged_version_of_the_same_file_does_not_survive(self):
        """Committet wird der Arbeitsbaum-Stand — genau wie bei `git commit -- <pfad>`.

        Eine ältere, gestagete Fassung derselben Datei ist damit überholt; der Index
        zeigt danach den committeten Inhalt und nicht mehr die Zwischenversion.
        """
        (self.repo / "include.txt").write_text("staged version\n")
        git(self.repo, "add", "include.txt")
        (self.repo / "include.txt").write_text("worktree version\n")
        r = commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        for ref in ("HEAD:include.txt", ":include.txt"):
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(self.repo), "show", ref],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "worktree version\n",
            )
        self.assertEqual(self._status(), "")

    def test_rename_commits_move_and_deletion_together(self):
        """Ein approvter Rename (Ziel + Quelle) ergibt einen echten Move-Commit.

        Vorher stagte die Hilfe nur den Zielpfad: der Commit enthielt eine Kopie,
        und die Löschung des alten Namens blieb als schmutziger Rest im Repo.
        """
        git(self.repo, "mv", "include.txt", "renamed.txt")
        st = collect_status(self.repo, self.repo.parent, DEFAULT_CONFIG)
        rename = [entry for entry in st.files if entry.rename_group]
        self.assertEqual({entry.path for entry in rename}, {"renamed.txt", "include.txt"})
        self.assertEqual(len({entry.rename_group for entry in rename}), 1)
        r = commit_selected(self.repo, ["renamed.txt", "include.txt"], "umbenannt", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        changed = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "diff-tree",
                "--no-commit-id",
                "--name-status",
                "-r",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        self.assertEqual(sorted(changed), ["A", "D", "include.txt", "renamed.txt"])
        self.assertEqual(self._status(), "")

    def test_plain_deletion_can_be_committed(self):
        """Löschungen konnte die Hilfe nie committen: das zweite Race-Check-
        Staging fand den Pfad weder im Index noch im Arbeitsbaum und `git add`
        brach mit "pathspec did not match" ab (GitReadError, Exit 128)."""
        (self.repo / "include.txt").unlink()
        r = commit_selected(self.repo, ["include.txt"], "geloescht", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        changed = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "diff-tree",
                "--no-commit-id",
                "--name-status",
                "-r",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        self.assertEqual(changed, ["D", "include.txt"])
        self.assertEqual(self._status(), "")

    def test_initial_commit_in_fresh_repository(self):
        """Die Commit-Hilfe muss auch den allerersten Commit eines Repos können.

        Ohne HEAD scheiterte `git read-tree HEAD` mit GitReadError — ausgerechnet
        beim ersten Commit, den der alte direkte Pfad noch konnte.
        """
        fresh = Path(self.tmp.name) / "frisch"
        fresh.mkdir()
        git(fresh, "init", "-q", "-b", "main")
        git(fresh, "config", "user.email", "t@example.invalid")
        git(fresh, "config", "user.name", "T")
        (fresh / "erste.txt").write_text("hallo\n")
        r = commit_selected(fresh, ["erste.txt"], "initial", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        committed_head = gmf_module.current_head(fresh, 10)
        self.assertEqual(r.committed_head, committed_head)
        log = subprocess.run(
            ["git", "-C", str(fresh), "log", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(log.strip(), "initial")
        status = subprocess.run(
            ["git", "-C", str(fresh), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(status, "")

        undo = gmf_module.commit_undo_command(None, r.committed_head, r.approved_ref)
        git(fresh, *shlex.split(undo)[1:])
        self.assertIsNone(gmf_module.current_head(fresh, 10))
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(fresh), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout,
            "A  erste.txt\n",
        )

    def test_initial_commit_keeps_a_private_index_mode(self):
        fresh = Path(self.tmp.name) / "private-index"
        fresh.mkdir()
        git(fresh, "init", "-q", "-b", "main")
        git(fresh, "config", "user.email", "t@example.invalid")
        git(fresh, "config", "user.name", "T")
        (fresh / "first.txt").write_text("content\n")
        previous_umask = os.umask(0o077)
        try:
            result = commit_selected(fresh, ["first.txt"], "initial", 10)
        finally:
            os.umask(previous_umask)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE((fresh / ".git" / "index").stat().st_mode), 0o600)

    def test_oserror_after_real_commit_is_reported_as_unknown(self):
        before = gmf_module.current_head(self.repo, 10)
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git

        def commit_then_lose_result(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if "commit" in args:
                self.assertEqual(result.returncode, 0, result.stderr)
                raise OSError("lost commit result")
            return result

        with (
            mock.patch.object(gmf_module, "run_git", side_effect=commit_then_lose_result),
            self.assertRaises(gmf_module.CommitOutcomeUnknownError),
        ):
            commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertNotEqual(gmf_module.current_head(self.repo, 10), before)
        self.assertTrue(
            any(line.startswith("…") and "git commit" in line for line in gmf_module.COMMAND_LOG)
        )

    def test_command_log_shows_the_terminal_equivalent_commit(self):
        """Das Protokoll verspricht terminal-ausführbare Zeilen. Ein nacktes
        `git add`/`git commit` liefe dort aber gegen den ECHTEN Index (ohne das
        GIT_INDEX_FILE der Hilfe) — deshalb steht stattdessen der äquivalente,
        pfadbegrenzte Befehl im Protokoll."""
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        (self.repo / "include.txt").write_text("approved\n")
        r = commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        joined = "\n".join(gmf_module.COMMAND_LOG)
        self.assertIn("git commit -m selected -- ':(literal)include.txt'", joined)
        self.assertIn(
            f"git -c core.hooksPath=/dev/null reset -q "
            f"{r.committed_head} -- ':(literal)include.txt'",
            joined,
        )
        self.assertNotIn("git add", joined)

    def test_conflicts_block_commit(self):
        git(self.repo, "checkout", "-qb", "other")
        (self.repo / "include.txt").write_text("other\n")
        git(self.repo, "commit", "-qam", "other")
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "include.txt").write_text("main\n")
        git(self.repo, "commit", "-qam", "main")
        merge = subprocess.run(
            ["git", "-C", str(self.repo), "merge", "other"], capture_output=True, text=True
        )
        self.assertNotEqual(merge.returncode, 0)
        with self.assertRaises(CommitSafetyError):
            commit_selected(self.repo, ["include.txt"], "must fail", 10)

    def test_the_approval_check_measures_against_the_pinned_oid_not_head(self):
        """Die Freigabepruefung darf HEAD nicht ein zweites Mal aufloesen.

        Der temporaere Index startet bewusst vom festgehaltenen `head_before`.
        Die Pruefung danach las aber wieder den beweglichen Namen HEAD: Ein
        Checkout in diesem Fenster liess sie den Baum eines fremden Branches
        messen, und der Abbruch zeigte mit "temporary index differs from
        approved paths" auf die Dateiauswahl statt auf den Checkout
        (Review-Fund 2026-09-10).
        """
        git(self.repo, "checkout", "-q", "-b", "other")
        (self.repo / "fremd.txt").write_text("nur auf other\n")
        git(self.repo, "add", "fremd.txt")
        git(self.repo, "commit", "-qm", "other")
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "include.txt").write_text("neu\n")
        original = gmf_module._stage_approved

        def checkout_between(repo, paths, timeout, env):
            result = original(repo, paths, timeout, env)
            git(self.repo, "checkout", "-q", "other")
            return result

        with (
            mock.patch.object(gmf_module, "_stage_approved", side_effect=checkout_between),
            self.assertRaises(CommitSafetyError) as caught,
        ):
            commit_selected(self.repo, ["include.txt"], "msg", 10)

        # Der Abbruch ist richtig — es lief wirklich ein Checkout. Er darf nur
        # nicht die Dateiauswahl beschuldigen.
        self.assertNotIn("differs from approved paths", str(caught.exception))


class CommitWizardSafetyTests(unittest.TestCase):
    class Screen(FakeScreen):
        def __init__(self, keys=()):
            self.keys = iter(keys)
            self.drawn = []

        def getmaxyx(self):
            return (30, 120)

        def addstr(self, y, x, text, *_a):
            self.drawn.append(text)

        def clrtoeol(self):
            pass

        def getch(self):
            return next(self.keys)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        for name in ("one.txt", "two.txt"):
            (self.repo / name).write_text(name + "\n")
        (self.repo / ".gitignore").write_text("base-rule\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def _staged_then_deleted(self) -> RepoStatus:
        """`git add neu.txt && rm neu.txt` neben einer gewöhnlichen Änderung."""
        (self.repo / "neu.txt").write_text("neu\n")
        git(self.repo, "add", "neu.txt")
        (self.repo / "neu.txt").unlink()
        (self.repo / "one.txt").write_text("geändert\n")
        return collect_status(self.repo, self.root, DEFAULT_CONFIG)

    def test_a_staged_then_deleted_path_is_nothing_against_head(self):
        st = self._staged_then_deleted()
        entries = {f.path: f for f in st.files}
        self.assertEqual(entries["neu.txt"].xy, "AD")
        self.assertFalse(gmf_module.committable_against_head(entries["neu.txt"]))
        self.assertTrue(gmf_module.committable_against_head(entries["one.txt"]))

    def test_committing_such_a_path_contributes_nothing(self):
        """Der Grund für die Ausnahme — hier ausdrücklich festgehalten.

        Der temporäre Index startet aus HEAD; den Pfad gibt es dort nicht, im
        Arbeitsbaum auch nicht. Er trägt also nichts bei. Seit die
        Freigabeprüfung eine Teilmenge statt Gleichheit verlangt, reißt er die
        gewöhnliche Änderung an one.txt nicht mehr mit — der Commit enthält
        genau sie, und die Hilfe zeigt für den Pfad trotzdem, dass es dort
        nichts zu committen gibt.
        """
        self._staged_then_deleted()
        result = commit_selected(self.repo, ["neu.txt", "one.txt"], "test", 10, 20)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            git(self.repo, "show", "--name-only", "--format=", "HEAD").split(), ["one.txt"]
        )

    def test_such_a_path_alone_is_nothing_to_commit(self):
        self._staged_then_deleted()
        with self.assertRaises(CommitSafetyError) as raised:
            commit_selected(self.repo, ["neu.txt"], "test", 10, 20)
        self.assertIn("change nothing against HEAD", str(raised.exception))

    def test_the_wizard_leaves_it_unselected_and_says_why(self):
        st = self._staged_then_deleted()
        # ⏎ committet die Vorauswahl; prompt_line liefert die Message.
        ui = TUI(self.Screen([10]), self.root, DEFAULT_CONFIG, None)
        ui.all_statuses = [st]
        ui.statuses = [st]
        with (
            mock.patch.object(ui, "prompt_line", return_value="nur one.txt"),
            mock.patch.object(ui, "refresh_one", return_value=st),
            mock.patch.object(ui, "show_busy"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.flushinp"),
        ):
            ui.action_commit_wizard()

        self.assertIn(gmf_module.t("committed_in", rel=st.rel, undo="").split(" ")[0], ui.message)
        # Nur one.txt ist committet; neu.txt bleibt als AD im Arbeitsbaum-Zustand.
        committed = git(self.repo, "show", "--name-only", "--format=", "HEAD")
        self.assertEqual(committed.split(), ["one.txt"])
        # Die Zeile sagt auch, WARUM nichts angehakt ist.
        rows = [text for text in ui.scr.drawn if "neu.txt" in text]
        self.assertTrue(rows)
        self.assertIn(gmf_module.t("do_nothing_vs_head"), rows[0])

    def test_the_labels_stay_complete_in_both_languages(self):
        """Die Pfadspalte richtet sich nach der längsten Beschriftung.

        Mit der früheren festen Breite schnitt schon ein 120 Zeichen breites
        Fenster die neue, längere Beschriftung ab.
        """

        class Narrow(CommitWizardSafetyTests.Screen):
            def getmaxyx(self):
                return (30, 80)

        st = self._staged_then_deleted()
        for lang in ("en", "de"):
            with self.subTest(lang=lang):
                gmf_module.set_ui_lang(lang)
                try:
                    ui = TUI(Narrow([27]), self.root, DEFAULT_CONFIG, None)
                    ui.all_statuses = [st]
                    ui.statuses = [st]
                    with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
                        ui.action_commit_wizard()
                    rows = [text for text in ui.scr.drawn if "neu.txt" in text]
                    self.assertIn(gmf_module.t("do_nothing_vs_head"), rows[0])
                    rows = [text for text in ui.scr.drawn if "one.txt" in text]
                    self.assertIn(gmf_module.t("do_commit"), rows[0])
                finally:
                    gmf_module.set_ui_lang("en")

    def test_select_all_skips_what_cannot_be_committed(self):
        st = self._staged_then_deleted()
        items = [
            {
                "code": f.code,
                "path": f.path,
                "include": False,
                "committable": gmf_module.committable_against_head(f),
                "rename_group": f.rename_group,
            }
            for f in st.files
        ]
        for it in items:
            it["include"] = it["committable"]
        self.assertEqual([it["path"] for it in items if it["include"]], ["one.txt"])

    def test_commit_helper_never_changes_gitignore_implicitly(self):
        before = (self.repo / ".gitignore").read_text()
        (self.repo / "debug.log").write_text("log\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "U", "path": "debug.log", "include": False, "rename_group": ""}]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)

        self.assertTrue(ui._commit_step2(st, items))

        self.assertEqual(ui.message, gmf_module.t("nothing_selected"))
        self.assertEqual((self.repo / ".gitignore").read_text(), before)

    def test_timeout_before_git_commit_never_reads_unset_approval_state(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [
            {
                "code": "M",
                "path": "one.txt",
                "ignore": False,
                "pattern": None,
                "include": True,
                "rename_group": "",
            }
        ]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        timeout = subprocess.TimeoutExpired(["git", "symbolic-ref"], 10)
        with (
            mock.patch.object(ui, "prompt_line", return_value="selected"),
            mock.patch.object(ui, "refresh_one", return_value=st),
            mock.patch.object(gmf_module, "current_symbolic_head_ref", side_effect=timeout),
            mock.patch("gitmaster_flash.curses.flushinp"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            self.assertTrue(ui._commit_step2(st, items))

        self.assertEqual(
            ui.message, gmf_module.t("commit_timeout_none", s=DEFAULT_CONFIG["commit_timeout"])
        )

    def test_head_read_error_before_commit_is_reported_inside_the_tui(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [
            {
                "code": "M",
                "path": "one.txt",
                "ignore": False,
                "pattern": None,
                "include": True,
                "rename_group": "",
            }
        ]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with (
            mock.patch.object(ui, "prompt_line", return_value="selected"),
            mock.patch.object(
                gmf_module, "current_head", side_effect=gmf_module.GitReadError("broken HEAD")
            ),
            mock.patch("gitmaster_flash.curses.flushinp"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            self.assertTrue(ui._commit_step2(st, items))
        self.assertIn("broken HEAD", ui.message)

    def test_checkout_during_the_commit_dialog_never_changes_the_target_branch(self):
        git(self.repo, "branch", "other")
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [
            {
                "code": "M",
                "path": "one.txt",
                "ignore": False,
                "pattern": None,
                "include": True,
                "rename_group": "",
            }
        ]
        main_before = git(self.repo, "rev-parse", "refs/heads/main")
        other_before = git(self.repo, "rev-parse", "refs/heads/other")
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        real_commit = gmf_module.commit_selected

        def switch_before_commit(*args, **kwargs):
            git(self.repo, "switch", "-q", "other")
            return real_commit(*args, **kwargs)

        with (
            mock.patch.object(ui, "prompt_line", return_value="selected"),
            mock.patch.object(gmf_module, "commit_selected", side_effect=switch_before_commit),
            mock.patch.object(ui, "show_busy"),
            mock.patch("gitmaster_flash.curses.flushinp"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            self.assertTrue(ui._commit_step2(st, items))

        self.assertIn("HEAD changed after UI approval", ui.message)
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/main"), main_before)
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/other"), other_before)

    def test_existing_commit_with_failed_index_adoption_is_named_clearly(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [
            {
                "code": "M",
                "path": "one.txt",
                "ignore": False,
                "pattern": None,
                "include": True,
                "rename_group": "",
            }
        ]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        error = gmf_module.CommitAdoptionError("a" * 40, OSError("locked"))
        with (
            mock.patch.object(ui, "prompt_line", return_value="selected"),
            mock.patch.object(gmf_module, "commit_selected", side_effect=error),
            mock.patch.object(ui, "show_busy"),
            mock.patch("gitmaster_flash.curses.flushinp"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            self.assertTrue(ui._commit_step2(st, items))
        self.assertIn("aaaaaaaaaaaa", ui.message)
        self.assertIn("index", ui.message.lower())
        self.assertIsNot(ui.statuses[0], st)

    def test_failed_commit_never_creates_an_ignore_rule(self):
        (self.repo / "debug.log").write_text("log\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "U", "path": "debug.log", "include": True, "rename_group": ""}]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        failed = gmf_module.CommitResult(1, "", "hook failed")
        with (
            mock.patch.object(ui, "prompt_line", return_value="logs"),
            mock.patch.object(gmf_module, "commit_selected", return_value=failed),
            mock.patch.object(ui, "show_busy"),
            mock.patch("gitmaster_flash.curses.flushinp"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            self.assertTrue(ui._commit_step2(st, items))

        self.assertEqual((self.repo / ".gitignore").read_text(), "base-rule\n")
        self.assertIsNot(ui.statuses[0], st)

    def test_post_success_timeout_never_says_nothing_was_committed(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [
            {
                "code": "M",
                "path": "one.txt",
                "ignore": False,
                "pattern": None,
                "include": True,
                "rename_group": "",
            }
        ]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        error = gmf_module.CommitOutcomeUnknownError("synthetic timeout")
        with (
            mock.patch.object(ui, "prompt_line", return_value="selected"),
            mock.patch.object(gmf_module, "commit_selected", side_effect=error),
            mock.patch.object(ui, "refresh_one", return_value=st),
            mock.patch.object(ui, "show_busy"),
            mock.patch("gitmaster_flash.curses.flushinp"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            self.assertTrue(ui._commit_step2(st, items))
        self.assertEqual(ui.message, gmf_module.t("commit_outcome_unknown"))
        self.assertNotIn("nothing was committed", ui.message)

    def test_unlock_oserror_after_real_commit_is_outcome_unknown(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [
            {
                "code": "M",
                "path": "one.txt",
                "ignore": False,
                "pattern": None,
                "include": True,
                "rename_group": "",
            }
        ]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        original_release = gmf_module._release_owned_git_lock

        def release_then_fail(path, fd, identity):
            if path.name != "index.lock":
                return original_release(path, fd, identity)
            if fd is not None:
                os.close(fd)
            path.unlink(missing_ok=True)
            raise OSError("lost unlock result")

        with (
            mock.patch.object(ui, "prompt_line", return_value="selected"),
            mock.patch.object(gmf_module, "_release_owned_git_lock", side_effect=release_then_fail),
            mock.patch.object(ui, "show_busy"),
            mock.patch("gitmaster_flash.curses.flushinp"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            self.assertTrue(ui._commit_step2(st, items))

        self.assertEqual(ui.message, gmf_module.t("commit_outcome_unknown"))
        self.assertEqual(git(self.repo, "show", "HEAD:one.txt"), "changed")

    def test_timeout_during_interrupted_commit_head_check_is_unknown(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [
            {
                "code": "M",
                "path": "one.txt",
                "ignore": False,
                "pattern": None,
                "include": True,
                "rename_group": "",
            }
        ]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        commit_timeout = gmf_module.CommitTimeoutError(
            subprocess.TimeoutExpired(["git", "commit"], 120),
            "a" * 40,
            gmf_module.current_head(self.repo, 10),
            "refs/heads/main",
            "gmf test interrupted commit",
        )
        verify_timeout = subprocess.TimeoutExpired(["git", "rev-parse"], 10)
        with (
            mock.patch.object(ui, "prompt_line", return_value="selected"),
            mock.patch.object(gmf_module, "commit_selected", side_effect=commit_timeout),
            mock.patch.object(
                gmf_module,
                "current_head",
                side_effect=[commit_timeout.approved_head, verify_timeout],
            ),
            mock.patch.object(ui, "refresh_one", return_value=st),
            mock.patch.object(ui, "show_busy"),
            mock.patch("gitmaster_flash.curses.flushinp"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            self.assertTrue(ui._commit_step2(st, items))
        self.assertEqual(ui.message, gmf_module.t("commit_outcome_unknown"))

    def test_timeout_with_failed_commit_proof_is_outcome_unknown(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [
            {
                "code": "M",
                "path": "one.txt",
                "ignore": False,
                "pattern": None,
                "include": True,
                "rename_group": "",
            }
        ]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        commit_timeout = gmf_module.CommitTimeoutError(
            subprocess.TimeoutExpired(["git", "commit"], 120),
            "a" * 40,
            gmf_module.current_head(self.repo, 10),
            "refs/heads/main",
            "gmf test interrupted commit",
        )
        with (
            mock.patch.object(ui, "prompt_line", return_value="selected"),
            mock.patch.object(gmf_module, "commit_selected", side_effect=commit_timeout),
            mock.patch.object(
                gmf_module,
                "finish_interrupted_commit",
                side_effect=CommitSafetyError("tree differs"),
            ),
            mock.patch.object(ui, "refresh_one", return_value=st),
            mock.patch.object(ui, "show_busy"),
            mock.patch("gitmaster_flash.curses.flushinp"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            self.assertTrue(ui._commit_step2(st, items))
        self.assertEqual(ui.message, gmf_module.t("commit_outcome_unknown"))

    def test_timeout_with_os_error_during_proof_is_outcome_unknown(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [
            {
                "code": "M",
                "path": "one.txt",
                "ignore": False,
                "pattern": None,
                "include": True,
                "rename_group": "",
            }
        ]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        commit_timeout = gmf_module.CommitTimeoutError(
            subprocess.TimeoutExpired(["git", "commit"], 120),
            "a" * 40,
            gmf_module.current_head(self.repo, 10),
            "refs/heads/main",
            "gmf test interrupted commit",
        )
        with (
            mock.patch.object(ui, "prompt_line", return_value="selected"),
            mock.patch.object(gmf_module, "commit_selected", side_effect=commit_timeout),
            mock.patch.object(
                gmf_module, "finish_interrupted_commit", side_effect=OSError("cannot read repo")
            ),
            mock.patch.object(ui, "refresh_one", return_value=st),
            mock.patch.object(ui, "show_busy"),
            mock.patch("gitmaster_flash.curses.flushinp"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            self.assertTrue(ui._commit_step2(st, items))
        self.assertEqual(ui.message, gmf_module.t("commit_outcome_unknown"))

    def test_rename_pair_toggle_never_affects_a_second_pair(self):
        git(self.repo, "mv", "one.txt", "one-new.txt")
        git(self.repo, "mv", "two.txt", "two-new.txt")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        groups = {entry.rename_group for entry in st.files if entry.rename_group}
        self.assertEqual(len(groups), 2)
        captured = []
        ui = TUI(self.Screen([ord(" "), 10]), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]

        def capture(_status, items):
            captured.extend(dict(item) for item in items)
            return True

        with (
            mock.patch.object(ui, "_commit_step2", side_effect=capture),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            ui.action_commit_wizard()
        first_group = captured[0]["rename_group"]
        first = [item for item in captured if item["rename_group"] == first_group]
        other = [item for item in captured if item["rename_group"] != first_group]
        self.assertEqual(len(first), 2)
        self.assertFalse(any(item["include"] for item in first))
        self.assertTrue(all(item["include"] for item in other))


class TagNamedLikeBranchTests(unittest.TestCase):
    """Ein Tag, der wie der aktuelle Branch heißt, legte gmf lahm.

    `git symbolic-ref --short HEAD` liefert nicht den Branchnamen, sondern den
    EINDEUTIGEN Kurznamen. Gibt es einen gleichnamigen Tag, antwortet Git
    `heads/main`. gmf baute daraus `refs/heads/heads/main`: Der Commit
    scheiterte dauerhaft mit „HEAD changed after UI approval", der Push mit
    „inspect failed", und die Repo-Zeile behauptete, der Branch liege nicht auf
    origin.
    """

    class Screen(FakeScreen):
        def __init__(self, keys=()):
            self.keys = iter(keys)
            self.drawn = []

        def getmaxyx(self):
            return (30, 120)

        def addstr(self, y, x, text, *_a):
            self.drawn.append(text)

        def clrtoeol(self):
            pass

        def getch(self):
            return next(self.keys)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
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
        git(self.repo, "push", "-q", "-u", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "tag", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_git_really_answers_with_the_ambiguous_short_name(self):
        self.assertEqual(git(self.repo, "symbolic-ref", "--short", "-q", "HEAD"), "heads/main")

    def test_the_branch_name_matches_the_real_head_ref(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.branch, "main")
        self.assertEqual(
            "refs/heads/" + st.branch, gmf_module.current_symbolic_head_ref(self.repo, 10)
        )

    def test_the_remote_branch_is_found_instead_of_reported_missing(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.remote_state, "ok")
        self.assertEqual([r.branch_exists for r in st.remotes], [True])

    def test_the_transfer_preflight_reaches_a_real_verdict(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        # Sauberer Baum, kein ausstehender Commit: "nichts zu pushen" ist die
        # richtige Auskunft. Vorher endete schon der Ref-Vergleich in
        # "inspect-failed".
        self.assertEqual(
            inspect_transfer(self.repo, "origin", st.branch, "push", 10).reason, "nothing-push"
        )

    def test_the_commit_helper_actually_commits(self):
        (self.repo / "a.txt").write_text("geändert\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        ui = TUI(self.Screen([10]), self.root, DEFAULT_CONFIG, None)
        ui.all_statuses = [st]
        ui.statuses = [st]
        with (
            mock.patch.object(ui, "prompt_line", return_value="zwei"),
            mock.patch.object(ui, "refresh_one", return_value=st),
            mock.patch.object(ui, "show_busy"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.flushinp"),
        ):
            ui.action_commit_wizard()
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "zwei")


class ApprovedPathsWithoutContributionTests(unittest.TestCase):
    """Ein freigegebener Pfad, der gegenüber HEAD nichts beiträgt.

    Die Freigabeprüfung verlangte Gleichheit von freigegebenen und tatsächlich
    im temporären Index veränderten Pfaden. Damit riss ein einziger solcher
    Pfad den GESAMTEN Commit mit — auch jede gewöhnliche Änderung daneben.
    Betroffen waren `git rm --cached` (Status `D `), ein Submodul mit nur
    schmutzigem Arbeitsbaum (` M`) und die Zielhälfte eines Renames, dessen
    Datei danach gelöscht wurde (`RD`). Die Zusage lautet aber „nichts
    committen, was nicht freigegeben ist" — das ist eine Teilmenge.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        git(self.root, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "a.txt").write_text("a\n")
        (self.repo / "config.local").write_text("c\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def _selection(self) -> tuple[RepoStatus, list[str]]:
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        paths = list(
            dict.fromkeys(f.path for f in st.files if gmf_module.committable_against_head(f))
        )
        return st, paths

    def test_a_path_removed_from_the_index_only_no_longer_blocks_the_commit(self):
        (self.repo / "a.txt").write_text("a2\n")
        git(self.repo, "rm", "-q", "--cached", "config.local")
        st, paths = self._selection()
        self.assertEqual([f.xy for f in st.files if f.path == "config.local"], ["D ", "??"])
        result = commit_selected(self.repo, paths, "test", 10, 30)
        self.assertEqual(result.returncode, 0)
        # Genau das Ergebnis von `git commit -m … -- a.txt config.local`, dem im
        # Protokoll versprochenen gleichwertigen Befehl.
        self.assertEqual(
            git(self.repo, "show", "--name-only", "--format=", "HEAD").split(), ["a.txt"]
        )
        self.assertEqual(git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"), "")

    def test_a_dirty_submodule_no_longer_blocks_the_commit(self):
        upstream = self.root / "sub-origin"
        git(self.root, "init", "-q", "-b", "main", str(upstream))
        git(upstream, "config", "user.email", "t@example.invalid")
        git(upstream, "config", "user.name", "T")
        (upstream / "s.txt").write_text("s\n")
        git(upstream, "add", "s.txt")
        git(upstream, "commit", "-qm", "s")
        git(
            self.repo,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(upstream),
            "sub",
        )
        git(self.repo, "commit", "-qm", "submodul")
        (self.repo / "sub" / "s.txt").write_text("schmutzig\n")
        (self.repo / "a.txt").write_text("a2\n")
        st, paths = self._selection()
        self.assertIn("sub", paths)
        result = commit_selected(self.repo, paths, "test", 10, 30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            git(self.repo, "show", "--name-only", "--format=", "HEAD").split(), ["a.txt"]
        )
        # Das Submodul bleibt unangetastet schmutzig — nichts wurde still mitcommittet.
        self.assertIn("M sub", git(self.repo, "status", "--porcelain=v1"))

    def test_a_rename_whose_target_was_deleted_no_longer_blocks_the_commit(self):
        (self.repo / "alt.txt").write_text("o\n")
        git(self.repo, "add", "alt.txt")
        git(self.repo, "commit", "-qm", "alt")
        git(self.repo, "mv", "alt.txt", "neu.txt")
        (self.repo / "neu.txt").unlink()
        (self.repo / "a.txt").write_text("a2\n")
        st, paths = self._selection()
        self.assertEqual(sorted(f.xy for f in st.files), [" M", "RD", "RD"])
        result = commit_selected(self.repo, paths, "test", 10, 30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            sorted(git(self.repo, "show", "--name-only", "--format=", "HEAD").split()),
            ["a.txt", "alt.txt"],
        )

    def _embed_repository(self) -> None:
        """Ein fremdes Repo MIT Commit — nur dann macht `git add` einen Gitlink."""
        inner = self.repo / "sub"
        git(self.root, "init", "-q", "-b", "main", str(inner))
        git(inner, "config", "user.email", "t@example.invalid")
        git(inner, "config", "user.name", "T")
        (inner / "s.txt").write_text("s\n")
        git(inner, "add", "s.txt")
        git(inner, "commit", "-qm", "s")

    def test_an_embedded_repository_is_visible_but_not_selectable(self):
        self._embed_repository()
        (self.repo / "a.txt").write_text("a2\n")
        st, paths = self._selection()
        self.assertIn("sub/", [f.path for f in st.files])
        self.assertEqual(paths, ["a.txt"])
        result = commit_selected(self.repo, paths, "test", 10, 30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            git(self.repo, "show", "--name-only", "--format=", "HEAD").split(), ["a.txt"]
        )

    def test_a_path_that_pulls_an_unapproved_one_into_the_tree_still_fails(self):
        """Gegenprobe: Die Teilmengenprüfung muss weiterhin greifen.

        `git add -- sub/` macht aus dem fremden Repo einen Submodul-Verweis
        `sub` — ein Pfad, der nicht freigegeben war. Genau davor schützt die
        Prüfung, und genau deshalb ist `sub/` in der Hilfe gesperrt.
        """
        self._embed_repository()
        (self.repo / "a.txt").write_text("a2\n")
        with self.assertRaises(CommitSafetyError) as raised:
            commit_selected(self.repo, ["a.txt", "sub/"], "test", 10, 30)
        self.assertIn("approved paths", str(raised.exception))
        self.assertEqual(git(self.repo, "log", "--format=%s"), "init")

    def test_a_selection_that_changes_nothing_says_exactly_that(self):
        git(self.repo, "rm", "-q", "--cached", "config.local")
        with self.assertRaises(CommitSafetyError) as raised:
            commit_selected(self.repo, ["config.local"], "test", 10, 30)
        self.assertIn("change nothing against HEAD", str(raised.exception))
        self.assertEqual(git(self.repo, "log", "--format=%s"), "init")


class HookInterferenceTests(unittest.TestCase):
    """`git commit` vererbt GIT_INDEX_FILE an seine Hooks.

    Ein pre-commit-Hook kann darüber mit `git add` zusätzliche, bewusst NICHT
    freigegebene Pfade in den temporären Index stagen — genau so landete eine
    abgewählte Datei im Commit. Die Hilfe muss das nach dem Commit erkennen und
    den Commit zurücknehmen, statt Erfolg zu melden.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)

    def tearDown(self):
        self.tmp.cleanup()

    def _install_hook(self, script: str) -> None:
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\n" + script + "\n")
        hook.chmod(0o755)

    def _base_commit(self) -> None:
        (self.repo / "gut.txt").write_text("base\n")
        (self.repo / "geheim.txt").write_text("base\n")
        git(self.repo, "add", "gut.txt", "geheim.txt")
        git(self.repo, "commit", "-qm", "base")

    def test_hook_staged_path_rolls_the_commit_back(self):
        self._base_commit()
        (self.repo / "gut.txt").write_text("freigegeben\n")
        (self.repo / "geheim.txt").write_text("abgewählt\n")
        self._install_hook("git add geheim.txt")
        head_before = gmf_module.current_head(self.repo, 10)
        with self.assertRaisesRegex(CommitSafetyError, "rolled back"):
            commit_selected(self.repo, ["gut.txt"], "nur gut", 10)
        # Der Branch steht wieder auf dem alten Commit; nichts wurde publiziert.
        self.assertEqual(gmf_module.current_head(self.repo, 10), head_before)
        show = subprocess.run(
            ["git", "-C", str(self.repo), "show", "HEAD:geheim.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(show, "base\n")
        # Arbeitsbaum unangetastet: beide Änderungen liegen weiter vor.
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("gut.txt", status)
        self.assertIn("geheim.txt", status)

    def test_commit_rollback_does_not_run_reference_transaction_hook(self):
        self._base_commit()
        marker = Path(self.tmp.name) / "reference-hook-ran"
        armed = Path(self.tmp.name) / "rollback-armed"
        ref_hook = self.repo / ".git" / "hooks" / "reference-transaction"
        ref_hook.write_text(
            "#!/bin/sh\n"
            f"test ! -e {shlex.quote(str(armed))} || "
            f"printf ran >> {shlex.quote(str(marker))}\n"
        )
        ref_hook.chmod(0o755)
        post_commit = self.repo / ".git" / "hooks" / "post-commit"
        post_commit.write_text(f"#!/bin/sh\n: > {shlex.quote(str(armed))}\n")
        post_commit.chmod(0o755)
        (self.repo / "gut.txt").write_text("freigegeben\n")
        (self.repo / "geheim.txt").write_text("abgewählt\n")
        self._install_hook("git add geheim.txt")

        with self.assertRaisesRegex(CommitSafetyError, "rolled back"):
            commit_selected(self.repo, ["gut.txt"], "nur gut", 10)

        self.assertTrue(armed.exists())
        self.assertFalse(marker.exists())

    def test_index_adoption_does_not_run_post_index_change_hook(self):
        self._base_commit()
        marker = Path(self.tmp.name) / "index-hook-ran"
        armed = Path(self.tmp.name) / "adoption-armed"
        post_commit = self.repo / ".git" / "hooks" / "post-commit"
        post_commit.write_text(f"#!/bin/sh\n: > {shlex.quote(str(armed))}\n")
        post_commit.chmod(0o755)
        index_hook = self.repo / ".git" / "hooks" / "post-index-change"
        index_hook.write_text(
            "#!/bin/sh\n"
            f"if test -e {shlex.quote(str(armed))}; then\n"
            f"  printf ran >> {shlex.quote(str(marker))}\n"
            "  git add geheim.txt\n"
            "fi\n"
        )
        index_hook.chmod(0o755)
        (self.repo / "gut.txt").write_text("freigegeben\n")
        (self.repo / "geheim.txt").write_text("bleibt unstaged\n")

        # Belegen, dass dieses Git den Hook beim betroffenen Reset wirklich
        # ausführt; sonst könnte ein bloß ignorierter Hook den Test grün machen.
        probe_index = Path(self.tmp.name) / "probe-index"
        probe_index.write_bytes((self.repo / ".git" / "index").read_bytes())
        armed.touch()
        probe_env = {**os.environ, "GIT_INDEX_FILE": str(probe_index)}
        probe = subprocess.run(
            ["git", "-C", str(self.repo), "reset", "-q", "HEAD", "--", "gut.txt"],
            env=probe_env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertTrue(marker.exists())
        marker.unlink()
        armed.unlink()

        result = commit_selected(self.repo, ["gut.txt"], "nur gut", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only", "--", "geheim.txt"), "")
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain=v1", "--", "geheim.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn(" M geheim.txt", status)

    def test_failed_hook_rollback_reports_an_unknown_existing_commit(self):
        self._base_commit()
        (self.repo / "gut.txt").write_text("freigegeben\n")
        (self.repo / "geheim.txt").write_text("abgewählt\n")
        self._install_hook("git add geheim.txt")
        head_before = gmf_module.current_head(self.repo, 10)
        original = gmf_module.run_git

        def fail_rollback(repo, *args, **kwargs):
            if "update-ref" in args:
                return subprocess.CompletedProcess(["git", *args], 1, "", "simulated lock race")
            return original(repo, *args, **kwargs)

        with (
            mock.patch.object(gmf_module, "run_git_logged", side_effect=fail_rollback),
            self.assertRaises(gmf_module.CommitOutcomeUnknownError),
        ):
            commit_selected(self.repo, ["gut.txt"], "nur gut", 10)

        self.assertNotEqual(gmf_module.current_head(self.repo, 10), head_before)

    def test_wrong_branch_rollback_restores_that_branch_own_parent(self):
        self._base_commit()
        approved_head = git(self.repo, "rev-parse", "HEAD")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        other_parent = git(self.repo, "commit-tree", tree, "-m", "other root")
        committed = git(self.repo, "commit-tree", tree, "-p", other_parent, "-m", "misdirected")
        git(self.repo, "update-ref", "refs/heads/other", committed)

        with self.assertRaisesRegex(CommitSafetyError, "rolled back"):
            gmf_module._verify_hooks_kept_approved_tree(
                self.repo, approved_head, tree, 10, "refs/heads/main", committed, "refs/heads/other"
            )

        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/main"), approved_head)
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/other"), other_parent)

    def test_same_branch_parent_race_preserves_the_foreign_parent(self):
        self._base_commit()
        approved_head = git(self.repo, "rev-parse", "HEAD")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        foreign = git(self.repo, "commit-tree", tree, "-p", approved_head, "-m", "foreign")
        committed = git(self.repo, "commit-tree", tree, "-p", foreign, "-m", "gmf after race")
        git(self.repo, "update-ref", "refs/heads/main", committed)

        with self.assertRaisesRegex(CommitSafetyError, "rolled back"):
            gmf_module._verify_hooks_kept_approved_tree(
                self.repo, approved_head, tree, 10, "refs/heads/main", committed, "refs/heads/main"
            )

        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/main"), foreign)

    def test_replace_ref_cannot_hide_a_hook_changed_tree(self):
        self._base_commit()
        head_before = gmf_module.current_head(self.repo, 10)
        (self.repo / "gut.txt").write_text("freigegeben\n")
        (self.repo / "geheim.txt").write_text("abgewählt\n")
        git(self.repo, "add", "gut.txt")
        approved_tree = subprocess.run(
            ["git", "-C", str(self.repo), "write-tree"], check=True, capture_output=True, text=True
        ).stdout.strip()
        git(self.repo, "reset", "-q", "HEAD", "--", "gut.txt")
        self._install_hook("git add geheim.txt")
        post = self.repo / ".git" / "hooks" / "post-commit"
        post.write_text(
            "#!/bin/sh\n"
            f"replacement=$(printf 'replacement\\n' | git commit-tree {approved_tree} "
            f"-p {head_before})\n"
            'git replace -f "$(git rev-parse HEAD)" "$replacement"\n'
        )
        post.chmod(0o755)

        with self.assertRaisesRegex(CommitSafetyError, "rolled back"):
            commit_selected(self.repo, ["gut.txt"], "nur gut", 10)
        self.assertEqual(gmf_module.current_head(self.repo, 10), head_before)

    def test_hook_cannot_make_the_new_commit_look_parentless_via_shallow(self):
        self._base_commit()
        head_before = gmf_module.current_head(self.repo, 10)
        (self.repo / "gut.txt").write_text("approved\n")
        post = self.repo / ".git" / "hooks" / "post-commit"
        post.write_text('#!/bin/sh\ngit rev-parse HEAD > "$(git rev-parse --git-dir)/shallow"\n')
        post.chmod(0o755)

        result = commit_selected(self.repo, ["gut.txt"], "shallow hook", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/main"), result.committed_head)
        self.assertEqual(
            gmf_module.commit_parents(self.repo, result.committed_head, 10), [head_before]
        )

    def test_replace_ref_for_new_commit_cannot_change_index_adoption(self):
        self._base_commit()
        head_before = gmf_module.current_head(self.repo, 10)
        base_tree = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", head_before + "^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (self.repo / "gut.txt").write_text("freigegeben\n")
        post = self.repo / ".git" / "hooks" / "post-commit"
        post.write_text(
            "#!/bin/sh\n"
            f"replacement=$(printf 'replacement\\n' | git commit-tree {base_tree} "
            f"-p {head_before})\n"
            'git replace -f "$(git rev-parse HEAD)" "$replacement"\n'
        )
        post.chmod(0o755)

        result = commit_selected(self.repo, ["gut.txt"], "nur gut", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        cached = subprocess.run(
            ["git", "-C", str(self.repo), "show", ":gut.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(cached, "freigegeben\n")

    def test_hook_interference_on_initial_commit_restores_unborn_state(self):
        (self.repo / "gut.txt").write_text("a\n")
        (self.repo / "geheim.txt").write_text("b\n")
        self._install_hook("git add geheim.txt")
        with self.assertRaisesRegex(CommitSafetyError, "rolled back"):
            commit_selected(self.repo, ["gut.txt"], "initial", 10)
        # Das Repo ist wieder ohne Commit (unborn branch), nichts ging verloren.
        self.assertIsNone(gmf_module.current_head(self.repo, 10))
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("gut.txt", status)
        self.assertIn("geheim.txt", status)

    def test_harmless_hook_does_not_disturb_the_commit(self):
        self._base_commit()
        (self.repo / "gut.txt").write_text("freigegeben\n")
        self._install_hook("echo pre-commit lief > /dev/null")
        r = commit_selected(self.repo, ["gut.txt"], "mit Hook", 10)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_hook_commit_then_failure_is_reported_as_unknown_outcome(self):
        self._base_commit()
        before = gmf_module.current_head(self.repo, 10)
        (self.repo / "gut.txt").write_text("freigegeben\n")
        self._install_hook("git commit --no-verify -m nested\nexit 1")

        with self.assertRaises(gmf_module.CommitOutcomeUnknownError):
            commit_selected(self.repo, ["gut.txt"], "outer", 10)

        self.assertNotEqual(gmf_module.current_head(self.repo, 10), before)
        subject = subprocess.run(
            ["git", "-C", str(self.repo), "log", "-1", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(subject, "nested")
        changed = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        self.assertEqual(changed, ["gut.txt"])

    def test_hook_commit_on_other_branch_cannot_hide_behind_restored_head(self):
        self._base_commit()
        before = gmf_module.current_head(self.repo, 10)
        (self.repo / "gut.txt").write_text("freigegeben\n")
        self._install_hook(
            "git branch other HEAD\n"
            "git symbolic-ref HEAD refs/heads/other\n"
            "git commit --no-verify -m nested-other\n"
            "git symbolic-ref HEAD refs/heads/main\n"
            "exit 1"
        )

        with self.assertRaises(gmf_module.CommitOutcomeUnknownError):
            commit_selected(self.repo, ["gut.txt"], "outer", 10)

        self.assertEqual(gmf_module.current_head(self.repo, 10), before)
        self.assertNotEqual(git(self.repo, "rev-parse", "refs/heads/other"), before)


class SlowPreCommitHookTests(unittest.TestCase):
    """Ein langsamer pre-commit-Hook ist der Alltagsfall, an dem gmf abstürzte.

    `git commit` führt den Hook des Repos aus; startet der Linter oder Tests, ist der
    kurze `git_timeout` von zehn Sekunden längst um. Früher flog der `TimeoutExpired`
    dann bis in `main()` durch und beendete die TUI mit einem Traceback.

    Die Tests unten erzwingen diesen Timeout über `HOOK_TIMEOUT` statt über den
    Produktionswert `commit_timeout` von 120 Sekunden. Der Wert muss zwei Dinge
    zugleich leisten: kurz genug, um die Suite nicht zu bremsen, und lang genug,
    dass der Hook seine ersten Schritte (Marker schreiben, Fremd-Commit anlegen)
    davor sicher schafft. Gemessen braucht er dafür rund 0,4 Sekunden; mit den
    früheren fest verdrahteten 1 Sekunde blieb unter der Last der vollen Suite
    zu wenig Rand, und drei Tests dieser Klasse schlugen sporadisch fehl
    (2026-08-19: `hook-pids` fehlte, der Fremd-Commit des Hooks entstand nie).
    """

    # Rand gegenüber den gemessenen ~0,4 s Hook-Vorlauf; kostet je Test genau
    # diese Wartezeit, weil der Hook danach absichtlich weiterhängt.
    HOOK_TIMEOUT = 3

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "file.txt").write_text("base\n")
        git(self.repo, "add", "file.txt")
        git(self.repo, "commit", "-qm", "base")
        (self.repo / "file.txt").write_text("changed\n")
        self.marker = Path(self.tmp.name) / "hook-pids"

    def tearDown(self):
        self.tmp.cleanup()

    def _install_hook(self, seconds: float) -> None:
        """pre-commit-Hook, der `seconds` lang beschäftigt ist.

        Er notiert seine eigene PID und die eines Enkelprozesses; daran prüft der
        Test, dass ein Timeout wirklich die ganze Prozessgruppe abräumt.
        """
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            f"sleep {seconds} &\n"
            "child=$!\n"
            f'echo "$$ $child" > {shlex.quote(str(self.marker))}\n'
            "wait $child\n"
        )
        hook.chmod(0o755)

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def test_timeout_kills_hook_and_its_children_and_leaves_no_commit(self):
        self._install_hook(30)
        head_before = gmf_module.current_head(self.repo, 10)
        with self.assertRaises(subprocess.TimeoutExpired):
            commit_selected(self.repo, ["file.txt"], "hängt", 10, commit_timeout=self.HOOK_TIMEOUT)
        hook_pid, child_pid = (int(p) for p in self.marker.read_text().split())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (self._alive(hook_pid) or self._alive(child_pid)):
            time.sleep(0.05)
        self.assertFalse(self._alive(hook_pid), "pre-commit-Hook läuft weiter")
        self.assertFalse(self._alive(child_pid), "Enkelprozess des Hooks läuft weiter")
        self.assertEqual(gmf_module.current_head(self.repo, 10), head_before)
        self.assertFalse((self.repo / ".git" / "index.lock").exists())

    def test_timeout_kills_a_background_child_after_git_has_exited(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            "trap '' HUP\n"
            "sleep 30 &\n"
            f"echo $! > {shlex.quote(str(self.marker))}\n"
            "exit 0\n"
        )
        hook.chmod(0o755)

        with self.assertRaises(subprocess.TimeoutExpired):
            commit_selected(
                self.repo, ["file.txt"], "hintergrund", 10, commit_timeout=self.HOOK_TIMEOUT
            )

        child_pid = int(self.marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self._alive(child_pid):
            time.sleep(0.05)
        self.assertFalse(
            self._alive(child_pid), "Hintergrundprozess des beendeten Hooks läuft weiter"
        )

    def test_commit_timeout_covers_the_hook_while_git_timeout_stays_short(self):
        # Genau der gemeldete Fall: Vorbereitungsschritte sind schnell, nur der
        # Commit selbst braucht wegen des Hooks länger als git_timeout.
        self._install_hook(2)
        r = commit_selected(self.repo, ["file.txt"], "mit Hook", 1, commit_timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(gmf_module.current_head(self.repo, 10), None)

    def test_timeout_message_names_the_subcommand_and_the_limit(self):
        exc = subprocess.TimeoutExpired(["git", "-C", str(self.repo), "commit", "-m", "x"], 42)
        text = gmf_module.timeout_message(exc)
        self.assertIn("commit", text)
        self.assertIn("42", text)

    def test_timeout_message_skips_global_url_binding_options(self):
        push = safe_push_args("/tmp/approved.git", "main", "a" * 40, "b" * 40)
        exc = subprocess.TimeoutExpired(["git", "-C", str(self.repo), *push], 17)
        text = gmf_module.timeout_message(exc)
        self.assertIn("push", text)
        self.assertNotIn("git -c", text)
        self.assertIn("outcome is unknown", text)

    def test_commit_written_before_a_hanging_post_commit_hook_is_not_adopted(self):
        """Hängt erst der post-commit-Hook, existiert der Commit längst.

        Ohne synchronen Rückgabekanal lässt sich seine Herkunft aber nicht sicher
        beweisen. Der Commit bleibt deshalb bestehen, der echte Index wird nicht
        übernommen und `git status` zeigt die manuell zu prüfende Änderung weiter.
        """
        hook = self.repo / ".git" / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\nsleep 30\n")
        hook.chmod(0o755)
        head_before = gmf_module.current_head(self.repo, 10)
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            commit_selected(
                self.repo, ["file.txt"], "haengt danach", 10, commit_timeout=self.HOOK_TIMEOUT
            )
        # Die Ausnahme trägt den freigegebenen Baum — derselbe Weg, den auch
        # der Timeout-Zweig der Commit-Hilfe nimmt.
        done = gmf_module.finish_interrupted_commit(
            self.repo, head_before, 10, cm.exception.approved_tree
        )
        self.assertTrue(done)
        self.assertNotEqual(gmf_module.current_head(self.repo, 10), head_before)
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(status, "MM file.txt\n")

    def test_timeout_cannot_hide_a_hook_commit_on_another_branch(self):
        head_before = gmf_module.current_head(self.repo, 10)
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            "git branch other HEAD\n"
            "git symbolic-ref HEAD refs/heads/other\n"
            "git commit --no-verify -m nested-other\n"
            "git symbolic-ref HEAD refs/heads/main\n"
            "sleep 30\n"
        )
        hook.chmod(0o755)

        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            commit_selected(self.repo, ["file.txt"], "outer", 10, commit_timeout=self.HOOK_TIMEOUT)

        self.assertEqual(gmf_module.current_head(self.repo, 10), head_before)
        # Erst dieser Nachweis trennt "die Erkennung greift nicht" von "der Hook
        # kam vor dem Timeout gar nicht bis zu seinem Commit". Ohne ihn las sich
        # ein zu knapper HOOK_TIMEOUT wie ein Fehler in der Erkennung.
        foreign = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "-q", "--verify", "refs/heads/other"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertTrue(
            foreign and foreign != head_before,
            "der pre-commit-Hook schrieb seinen Fremd-Commit nicht vor dem Timeout",
        )
        with self.assertRaisesRegex(CommitSafetyError, "another branch"):
            gmf_module.finish_interrupted_commit(
                self.repo,
                raised.exception.approved_head,
                10,
                raised.exception.approved_tree,
                raised.exception.approved_ref,
                raised.exception.reflog_action,
            )

    def test_interrupted_commit_without_result_reports_false(self):
        head_before = gmf_module.current_head(self.repo, 10)
        self.assertFalse(gmf_module.finish_interrupted_commit(self.repo, head_before, 10))

    def test_timeout_never_rolls_back_a_foreign_commit(self):
        head_before = gmf_module.current_head(self.repo, 10)
        approved_tree = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (self.repo / "foreign.txt").write_text("parallel\n")
        git(self.repo, "add", "foreign.txt")
        git(self.repo, "commit", "-qm", "foreign")
        foreign_head = gmf_module.current_head(self.repo, 10)

        with self.assertRaisesRegex(CommitSafetyError, "approved tree"):
            gmf_module.finish_interrupted_commit(self.repo, head_before, 10, approved_tree)

        self.assertEqual(gmf_module.current_head(self.repo, 10), foreign_head)

    def test_hook_staged_extras_during_timeout_are_not_rolled_back(self):
        """Der Timeout-Zweig prüft den Commit gegen den freigegebenen Baum.

        Ein pre-commit-Hook stagt hier heimlich eine weitere Datei in den
        geerbten temporären Index; der post-commit-Hook hängt, sodass der
        Commit zwar entsteht, `git commit` aber in den Timeout läuft. Vorher
        verglich finish_interrupted_commit nur Pfadnamen — der Commit mit der
        geschmuggelten Datei blieb ungeprüft stehen. Jetzt wird sein Baum gegen
        die Freigabe geprüft und der Fehler gemeldet; ohne Eigentumsnachweis
        bleibt der Commit selbst bewusst unangetastet.
        """
        (self.repo / "geschmuggelt.txt").write_text("nicht freigegeben\n")
        pre = self.repo / ".git" / "hooks" / "pre-commit"
        pre.write_text("#!/bin/sh\ngit add geschmuggelt.txt\n")
        pre.chmod(0o755)
        post = self.repo / ".git" / "hooks" / "post-commit"
        post.write_text("#!/bin/sh\nsleep 30\n")
        post.chmod(0o755)
        head_before = gmf_module.current_head(self.repo, 10)
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            commit_selected(self.repo, ["file.txt"], "schmuggelt", 10, commit_timeout=3)
        with self.assertRaises(gmf_module.CommitSafetyError):
            gmf_module.finish_interrupted_commit(
                self.repo, head_before, 10, cm.exception.approved_tree
            )
        # Ohne Eigentumsnachweis bleibt der Commit erreichbar; gmf mutiert HEAD
        # nach einem Timeout niemals auf Verdacht.
        self.assertNotEqual(gmf_module.current_head(self.repo, 10), head_before)

    def test_timeout_keeps_the_commit_on_its_actual_base(self):
        """Der Aufrufer liest HEAD, BEVOR er commit_selected startet.

        Bewegt sich HEAD dazwischen — ein anderes Programm committet im selben
        Repo —, passt sein Wert nicht mehr zum Elternteil unseres Commits. Der
        atomare Rollback prüft aber genau diesen Elternwert: Er unterblieb, und
        ein vom Hook erweiterter Commit blieb trotz erkanntem Fehler stehen.
        Deshalb hängt commit_selected seine EIGENE HEAD-Basis an die Ausnahme.
        """
        veralteter_stand = gmf_module.current_head(self.repo, 10)
        # Der fremde Commit dazwischen.
        (self.repo / "fremd.txt").write_text("von woanders\n")
        git(self.repo, "add", "fremd.txt")
        git(self.repo, "commit", "-qm", "fremder Commit")
        fremder_stand = gmf_module.current_head(self.repo, 10)
        self.assertNotEqual(fremder_stand, veralteter_stand)

        (self.repo / "geschmuggelt.txt").write_text("nicht freigegeben\n")
        pre = self.repo / ".git" / "hooks" / "pre-commit"
        pre.write_text("#!/bin/sh\ngit add geschmuggelt.txt\n")
        pre.chmod(0o755)
        post = self.repo / ".git" / "hooks" / "post-commit"
        post.write_text("#!/bin/sh\nsleep 30\n")
        post.chmod(0o755)
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            commit_selected(self.repo, ["file.txt"], "schmuggelt", 10, commit_timeout=3)

        # Genau der Ausdruck, den der Timeout-Zweig der Commit-Hilfe benutzt.
        basis = getattr(cm.exception, "approved_head", veralteter_stand)
        self.assertEqual(basis, fremder_stand)
        with self.assertRaises(gmf_module.CommitSafetyError):
            gmf_module.finish_interrupted_commit(self.repo, basis, 10, cm.exception.approved_tree)
        # Der neue Commit bleibt stehen; insbesondere verschwindet der fremde
        # Eltern-Commit nicht durch einen spekulativen Rollback.
        head_after = gmf_module.current_head(self.repo, 10)
        self.assertNotEqual(head_after, fremder_stand)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "HEAD^"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            fremder_stand,
        )


class CommitGroupTests(unittest.TestCase):
    """Vorschlaege fuer die Dateiauswahl — reine Logik."""

    @staticmethod
    def _f(code, path, group=""):
        return ChangedFile(code, path, " M", group)

    def _labels(self, files):
        return [TUI.commit_group_label(g) for g in gmf_module.commit_groups(files)]

    def test_a_single_file_gets_no_suggestion(self):
        self.assertEqual(gmf_module.commit_groups([self._f("M", "a.py")]), [])

    def test_it_groups_by_change_kind_folder_and_extension(self):
        files = [
            self._f("M", "src/app.py"),
            self._f("M", "src/util.py"),
            self._f("U", "docs/neu.md"),
            self._f("M", "docs/guide.md"),
            self._f("M", "Makefile"),
        ]
        found = {(g.kind, g.key): set(g.paths) for g in gmf_module.commit_groups(files)}
        self.assertEqual(found[("dir", "src")], {"src/app.py", "src/util.py"})
        self.assertEqual(found[("dir", "docs")], {"docs/neu.md", "docs/guide.md"})
        # Nach Art der Aenderung: die vier geaenderten ohne die eine neue Datei.
        self.assertEqual(
            found[("code", "M")], {"src/app.py", "src/util.py", "docs/guide.md", "Makefile"}
        )
        # Die einzelne neue Datei ergibt keine Gruppe, und die Endung .py trifft
        # dieselben Dateien wie der Ordner src/ — beides faellt weg.
        self.assertNotIn(("code", "U"), found)
        self.assertNotIn(("ext", ".py"), found)

    def test_a_suggestion_covering_everything_is_dropped(self):
        # Alle Dateien sind geaendert und liegen in src/ — beides waere nur ein
        # umstaendliches "alle", das es schon gibt.
        files = [self._f("M", "src/a.py"), self._f("M", "src/b.py")]
        self.assertEqual(gmf_module.commit_groups(files), [])

    def test_identical_subsets_appear_only_once(self):
        # "Ordner docs/" und "Typ .md" treffen dieselben zwei Dateien.
        files = [self._f("M", "docs/a.md"), self._f("M", "docs/b.md"), self._f("M", "src/c.py")]
        groups = gmf_module.commit_groups(files)
        self.assertEqual([set(g.paths) for g in groups].count({"docs/a.md", "docs/b.md"}), 1)

    def test_a_group_of_one_file_is_no_group(self):
        files = [self._f("M", "src/a.py"), self._f("M", "src/b.py"), self._f("M", "einzeln/c.py")]
        self.assertNotIn("einzeln", [g.key for g in gmf_module.commit_groups(files)])

    def test_files_in_the_root_produce_no_folder_group(self):
        files = [self._f("M", "a.py"), self._f("M", "b.py"), self._f("U", "src/c.py")]
        self.assertEqual([g.key for g in gmf_module.commit_groups(files) if g.kind == "dir"], [])

    def test_a_dotfile_is_a_name_not_an_extension(self):
        files = [
            self._f("M", ".gitignore"),
            self._f("M", ".npmrc"),
            self._f("M", "src/a.py"),
            self._f("M", "src/b.py"),
        ]
        self.assertEqual([g.key for g in gmf_module.commit_groups(files) if g.kind == "ext"], [])

    def test_extensions_are_matched_regardless_of_case(self):
        files = [self._f("M", "a/One.PY"), self._f("M", "b/two.py"), self._f("M", "c/three.md")]
        by_ext = {g.key: set(g.paths) for g in gmf_module.commit_groups(files) if g.kind == "ext"}
        self.assertEqual(by_ext.get(".py"), {"a/One.PY", "b/two.py"})

    def test_a_rename_pair_is_never_split_across_a_suggestion(self):
        # Quelle und Ziel muessen zusammen committet werden; ein Vorschlag, der
        # nur eine Haelfte traefe, hinterliesse eine halbe Umbenennung.
        files = [
            self._f("D", "src/alt.py", "r1"),
            self._f("U", "docs/neu.md", "r1"),
            self._f("M", "src/x.py"),
            self._f("M", "src/y.py"),
        ]
        for group in gmf_module.commit_groups(files):
            with self.subTest(group=group.key):
                inside = {"src/alt.py", "docs/neu.md"} & set(group.paths)
                self.assertIn(len(inside), (0, 2))

    def test_a_completed_group_says_so_in_its_label(self):
        files = [
            self._f("D", "src/alt.py", "r1"),
            self._f("U", "docs/neu.md", "r1"),
            self._f("M", "src/x.py"),
            self._f("M", "src/y.py"),
        ]
        deleted = next(
            g for g in gmf_module.commit_groups(files) if g.kind == "code" and g.key == "D"
        )
        self.assertTrue(deleted.completed)
        # Ohne den Zusatz hiesse die Gruppe "deleted files (2)", obwohl eine der
        # beiden Dateien neu ist.
        self.assertIn(gmf_module.TR["group_with_rename"]["en"], TUI.commit_group_label(deleted))

    def test_bigger_suggestions_come_first_and_the_order_is_stable(self):
        files = [
            self._f("M", "src/a.py"),
            self._f("M", "src/b.py"),
            self._f("M", "src/c.py"),
            self._f("U", "docs/d.md"),
            self._f("U", "docs/e.md"),
        ]
        groups = gmf_module.commit_groups(files)
        sizes = [len(g.paths) for g in groups if g.kind == "dir"]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual([g.key for g in gmf_module.commit_groups(files)], [g.key for g in groups])

    def test_every_label_exists_in_both_languages(self):
        for code in ("M", "U", "D", "C"):
            with self.subTest(code=code):
                self.assertEqual(set(gmf_module.TR[f"group_code_{code}"]), {"en", "de"})


class CommitWizardSelectionTests(unittest.TestCase):
    """Die neuen Auswahltasten der Commit-Hilfe: A, N und G."""

    class Screen(FakeScreen):
        def __init__(self, keys):
            self.keys = iter(keys)
            self.drawn = []

        def getmaxyx(self):
            return (24, 100)

        def addstr(self, y, x, text, *a):
            self.drawn.append((y, text))

        def getch(self):
            return next(self.keys)

        def get_wch(self):
            return next(self.keys)

    FILES = [
        ChangedFile("M", "src/app.py", " M"),
        ChangedFile("M", "src/util.py", " M"),
        ChangedFile("U", "docs/neu.md", "??"),
    ]

    def _run(self, keys):
        st = RepoStatus(
            path=Path("/tmp/x"), rel="x", files=list(self.FILES), modified=2, untracked=1
        )
        ui = TUI(self.Screen(keys), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.all_statuses = ui.statuses = [st]
        taken = {}

        def step2(_self, _st, items):
            taken["paths"] = [it["path"] for it in items if it["include"]]
            return True

        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "_commit_step2", step2),
        ):
            ui.action_commit_wizard()
        return taken.get("paths"), ui

    def test_n_deselects_everything_and_a_brings_it_back(self):
        taken, _ = self._run([ord("n"), 10])
        self.assertEqual(taken, [])
        taken, _ = self._run([ord("n"), ord("a"), 10])
        self.assertEqual(len(taken), 3)

    def test_space_treats_one_path_as_one_decision(self):
        """Zwei Zeilen koennen denselben Pfad tragen.

        Nach `git rm --cached x` meldet der Status ihn als `D ` (aus dem Index
        entfernt) UND als `??` (im Arbeitsbaum noch da). Getrennt angehakt,
        widersprachen sich die beiden Zeilen auf dem Schirm, waehrend
        `_commit_step2()` sie ohnehin wieder zu einem Pfad zusammenfaltet — das
        Haekchen auf der einen Zeile blieb also wirkungslos.
        """
        st = RepoStatus(
            path=Path("/tmp/x"),
            rel="x",
            files=[
                ChangedFile("M", "a.txt", " M"),
                ChangedFile("D", "cfg.local", "D "),
                ChangedFile("U", "cfg.local", "??"),
            ],
        )
        gesehen = {}

        def step2(_self, _st, items):
            gesehen["items"] = [(it["path"], it["include"]) for it in items]
            return True

        # Zweimal ↓ auf die "??"-Zeile von cfg.local, dann ␣, dann ⏎.
        ui = TUI(
            self.Screen([curses.KEY_DOWN, curses.KEY_DOWN, ord(" "), 10]),
            Path("/tmp"),
            DEFAULT_CONFIG,
            None,
        )
        ui.all_statuses = ui.statuses = [st]
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "_commit_step2", step2),
        ):
            ui.action_commit_wizard()

        self.assertEqual(
            gesehen["items"], [("a.txt", True), ("cfg.local", False), ("cfg.local", False)]
        )

    def test_space_still_toggles_only_the_chosen_path(self):
        """Gegenprobe: Verschiedene Pfade bleiben unabhaengig."""
        taken, _ = self._run([ord(" "), 10])
        self.assertEqual(sorted(taken), ["docs/neu.md", "src/util.py"])

    def test_g_replaces_the_selection_with_the_chosen_suggestion(self):
        # G öffnet die Liste, ⏎ nimmt den ersten Vorschlag, ⏎ committet.
        taken, _ = self._run([ord("g"), 10, 10])
        groups = gmf_module.commit_groups(self.FILES)
        self.assertEqual(sorted(taken), sorted(groups[0].paths))
        # "Ersetzen" heisst ersetzen: nichts von vorher bleibt zusaetzlich drin.
        self.assertLess(len(taken), 3)

    def test_esc_in_the_suggestion_list_leaves_the_selection_untouched(self):
        taken, _ = self._run([ord("g"), 27, 10])
        self.assertEqual(len(taken), 3)

    def test_without_a_sensible_subset_g_says_so_instead_of_an_empty_list(self):
        st = RepoStatus(
            path=Path("/tmp/x"),
            rel="x",
            files=[ChangedFile("M", "src/a.py", " M"), ChangedFile("M", "src/b.py", " M")],
        )
        ui = TUI(self.Screen([]), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.all_statuses = ui.statuses = [st]
        note = ui._apply_commit_group(st, [])
        self.assertEqual(note, gmf_module.TR["group_none"]["en"])

    def test_g_without_suggestions_shows_the_reason_in_the_helper_itself(self):
        # Drei neue Dateien in der Wurzel: keine Teilmenge, die etwas taugt.
        # Die Meldung muss IM Bild der Hilfe stehen — die Zeile der Repo-Liste
        # ist hier nicht sichtbar.
        st = RepoStatus(
            path=Path("/tmp/x"),
            rel="x",
            files=[
                ChangedFile("U", "a.log", "??"),
                ChangedFile("U", "b.sh", "??"),
                ChangedFile("U", "c.md", "??"),
            ],
        )
        ui = TUI(self.Screen([ord("g"), ord("n"), 10]), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.all_statuses = ui.statuses = [st]
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "_commit_step2", lambda *a: True),
        ):
            ui.action_commit_wizard()
        self.assertTrue(any(gmf_module.TR["group_none"]["en"] in text for _, text in ui.scr.drawn))

    def test_the_applied_suggestion_is_named_in_the_helper(self):
        taken, ui = self._run([ord("g"), 10, 10])
        self.assertTrue(taken)
        self.assertTrue(
            any(gmf_module.TR["group_applied"]["en"][:12] in text for _, text in ui.scr.drawn)
        )

    def test_the_title_counts_how_many_are_selected(self):
        _, ui = self._run([ord("n"), 10])
        self.assertTrue(any("0/3" in text for _, text in ui.scr.drawn))
