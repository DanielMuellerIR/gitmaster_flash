"""Echte lokale Remotes: Pull-Quelle, Freigabe und abweichender Sync-Upstream."""
import os
import importlib.util
import pty
import select
import sys
import time
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import gitmaster_flash as gmf


spec = importlib.util.spec_from_file_location(
    "gmf_pull_screens", Path(gmf.__file__).parent / "docs/make-screens.py")
screens = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screens)


def cleanup_pty(pid, fd):
    # Git hat eine eigene Sitzung; seine Nachfahren vor dem PTY-Schließen merken.
    descendants = screens._descendant_pids(pid)
    try:
        os.close(fd)
    finally:
        descendants += [later for later in screens._descendant_pids(pid)
                        if later not in descendants]
        screens._terminate_pty_child(pid)
        screens._terminate_descendants(descendants)


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull,
                                               "GIT_CONFIG_NOSYSTEM": "1"})
        self.env.start()
        for name in ("origin.git", "mirror.git"):
            self.git(self.root, "init", "--bare", "-b", "main", name)
        self.peer = self.root / "peer"
        self.git(self.root, "init", "-b", "main", "peer")
        self.git(self.peer, "config", "user.name", "Test")
        self.git(self.peer, "config", "user.email", "test@example.invalid")
        self.commit("base")
        for name in ("origin", "mirror"):
            self.git(self.peer, "remote", "add", name, str(self.root / (name + ".git")))
            self.git(self.peer, "push", name, "main")
        self.repo = self.root / "local"
        self.git(self.root, "clone", str(self.root / "mirror.git"), "local")
        self.git(self.repo, "remote", "rename", "origin", "mirror")
        self.git(self.repo, "remote", "add", "origin", str(self.root / "origin.git"))
        self.git(self.repo, "config", "user.name", "Test")
        self.git(self.repo, "config", "user.email", "test@example.invalid")
        self.commit("incoming")
        self.git(self.peer, "push", "origin", "main")
        self.ui = gmf.TUI(None, self.root, dict(gmf.DEFAULT_CONFIG), None)
        status = gmf.collect_status(self.repo, self.root, self.ui.cfg)
        self.ui.statuses = [status]
        self.ui.all_statuses = [status]
        self.ui.choose_pull_remote = mock.Mock(return_value="origin")
        self.ui.show_busy = mock.Mock()
        self.ui.confirm_in_pager = mock.Mock(return_value=True)
        self.flush = mock.patch.object(gmf.curses, "flushinp")
        self.flush.start()

    def tearDown(self):
        self.flush.stop()
        self.env.stop()
        self.tmp.cleanup()

    def git(self, root, *args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, name):
        (self.peer / (name + ".txt")).write_text(name + "\n")
        self.git(self.peer, "add", name + ".txt")
        self.git(self.peer, "commit", "-m", name)

    def pull(self):
        self.ui.dispatch_action("L")

    def test_selected_remote_ff_keeps_sync_upstream_and_disables_hooks(self):
        hook_marker = self.root / "hook-ran"
        hook = self.repo / ".git/hooks/post-merge"
        hook.write_text(f'#!/bin/sh\ntouch "{hook_marker}"\n')
        hook.chmod(0o755)
        self.pull()
        self.assertTrue((self.repo / "incoming.txt").exists())
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"),
                         self.git(self.peer, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.repo, "rev-parse", "--symbolic-full-name", "@{upstream}"),
                         "refs/remotes/mirror/main")
        self.assertFalse(hook_marker.exists())
        preview = "\n".join(self.ui.confirm_in_pager.call_args.args[1])
        self.assertIn("incoming", preview)
        self.assertIn("incoming.txt", preview)

    def test_ignored_incoming_file_is_preserved(self):
        (self.repo / ".git/info/exclude").write_text("incoming.txt\n")
        incoming = self.repo / "incoming.txt"
        incoming.write_text("private local data\n")
        before = self.git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "")
        self.pull()
        self.assertEqual(incoming.read_text(), "private local data\n")
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(self.ui.message, gmf.t("pull_local_collision"))

    def test_squash_config_cannot_change_fast_forward(self):
        self.git(self.repo, "config", "branch.main.mergeOptions", "--squash")
        self.pull()
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"),
                         self.git(self.peer, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(self.ui.message, gmf.t("pull_done", r="origin", b="main", n=1))

    def test_success_without_target_head_is_not_reported_as_pull(self):
        original = gmf.run_git_logged
        def no_merge(repo, *args, **kwargs):
            if "merge" in args:
                return subprocess.CompletedProcess(args, 0, "", "")
            return original(repo, *args, **kwargs)
        with mock.patch.object(gmf, "run_git_logged", side_effect=no_merge):
            self.pull()
        self.assertFalse((self.repo / "incoming.txt").exists())
        self.assertEqual(self.ui.message, gmf.t("pull_unknown"))

    def test_clean_cherry_pick_sequence_blocks_pull(self):
        # Nach eigenständigem Konflikt-Commit bleibt nur der Sequencer als Beleg.
        self.git(self.peer, "switch", "-c", "sequence", "HEAD~1")
        (self.peer / "base.txt").write_text("first\n")
        self.git(self.peer, "add", "base.txt")
        self.git(self.peer, "commit", "-m", "first")
        first = self.git(self.peer, "rev-parse", "HEAD")
        self.commit("second")
        second = self.git(self.peer, "rev-parse", "HEAD")
        (self.repo / "base.txt").write_text("local\n")
        self.git(self.repo, "add", "base.txt")
        self.git(self.repo, "commit", "-m", "local")
        self.git(self.peer, "push", "origin", "sequence")
        self.git(self.repo, "fetch", "origin")
        result = subprocess.run(["git", "-C", str(self.repo), "cherry-pick", first, second],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        (self.repo / "base.txt").write_text("resolved\n")
        self.git(self.repo, "add", "base.txt")
        self.git(self.repo, "commit", "-m", "resolved")
        self.assertFalse((self.repo / ".git/CHERRY_PICK_HEAD").exists())
        self.assertTrue((self.repo / ".git/sequencer").exists())
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "")
        before = self.git(self.repo, "rev-parse", "HEAD")
        self.pull()
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), before)
        self.assertFalse(self.ui.confirm_in_pager.called)
        self.assertEqual(gmf.inspect_transfer(self.repo, "origin", "main", "pull").reason,
                         "operation-in-progress")

    def test_aborted_pty_terminates_running_git_session(self):
        wrapper = self.root / "bin"
        wrapper.mkdir()
        pid_file = self.root / "git.pid"
        script = wrapper / "git"
        script.write_text(f"#!{sys.executable}\nimport os,time\n"
                          f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                          "time.sleep(120)\n")
        script.chmod(0o755)
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["PATH"] = str(wrapper) + os.pathsep + os.environ["PATH"]
            gmf.run_git(self.repo, "ls-remote", "origin", timeout=120)
            os._exit(0)
        git_pid = None
        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.exists())
            git_pid = int(pid_file.read_text())
            self.assertEqual(os.getpgid(git_pid), git_pid)
        finally:
            cleanup_pty(pid, fd)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(git_pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail("Git session survived PTY cleanup")

    def test_all_fetches_both_remotes_then_integrates_only_selected_source(self):
        self.ui.choose_pull_remote.side_effect = ["", "origin"]
        self.git(self.peer, "reset", "--hard", "HEAD~1")
        self.commit("mirror-only")
        self.git(self.peer, "push", "mirror", "main")
        mirror_head = self.git(self.peer, "rev-parse", "HEAD")
        self.pull()
        self.assertTrue((self.repo / "incoming.txt").exists())
        self.assertFalse((self.repo / "mirror-only.txt").exists())
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/remotes/mirror/main"), mirror_head)
        self.assertEqual(self.ui.choose_pull_remote.call_count, 2)

    def test_cancel_and_dirty_and_divergent_never_integrate(self):
        before = self.git(self.repo, "rev-parse", "HEAD")
        self.ui.confirm_in_pager.return_value = False
        self.pull()
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), before)
        self.ui.confirm_in_pager.return_value = True
        (self.repo / "base.txt").write_text("local work\n")
        self.pull()
        self.assertEqual((self.repo / "base.txt").read_text(), "local work\n")
        self.git(self.repo, "add", "base.txt")
        self.git(self.repo, "commit", "-m", "local")
        local = self.git(self.repo, "rev-parse", "HEAD")
        self.pull()
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), local)
        self.assertFalse((self.repo / "incoming.txt").exists())

    def test_branch_switch_to_same_oid_during_confirmation_stops_pull(self):
        self.git(self.repo, "branch", "other")
        def confirm(*args):
            self.git(self.repo, "switch", "other")
            return True
        self.ui.confirm_in_pager.side_effect = confirm
        self.pull()
        self.assertFalse((self.repo / "incoming.txt").exists())
        self.assertEqual(self.git(self.repo, "symbolic-ref", "HEAD"), "refs/heads/other")
        self.assertEqual(self.ui.message, gmf.t("pull_changed"))

    def test_new_remote_commit_during_confirmation_requires_new_preview(self):
        def confirm(*args):
            self.commit("newer")
            self.git(self.peer, "push", "origin", "main")
            return True
        self.ui.confirm_in_pager.side_effect = confirm
        self.pull()
        self.assertFalse((self.repo / "incoming.txt").exists())
        self.assertEqual(self.ui.message, gmf.t("pull_changed"))

    def test_operation_detached_and_no_upstream(self):
        self.git(self.repo, "fetch", "origin")
        head = self.git(self.repo, "rev-parse", "HEAD")
        marker = self.repo / ".git/MERGE_HEAD"
        marker.write_text(head + "\n")
        self.assertEqual(gmf.inspect_transfer(self.repo, "origin", "main", "pull").reason,
                         "operation-in-progress")
        marker.unlink()
        self.git(self.repo, "checkout", "--detach")
        self.pull()
        self.assertFalse((self.repo / "incoming.txt").exists())
        self.git(self.repo, "switch", "main")
        self.git(self.repo, "branch", "--unset-upstream")
        self.pull()
        self.assertTrue((self.repo / "incoming.txt").exists())

    def test_fetch_and_push_addresses_may_differ_for_reading_pull(self):
        self.git(self.repo, "remote", "set-url", "--push", "origin", str(self.root / "mirror.git"))
        self.pull()
        self.assertTrue((self.repo / "incoming.txt").exists())

    def test_missing_remote_branch_and_unsafe_refspec_stop_pull(self):
        self.git(self.root / "origin.git", "update-ref", "-d", "refs/heads/main")
        self.pull()
        self.assertFalse((self.repo / "incoming.txt").exists())
        self.assertFalse(self.ui.confirm_in_pager.called)
        self.git(self.repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
        self.pull()
        self.assertFalse((self.repo / "incoming.txt").exists())

    def test_real_terminal_remote_selection_and_confirmation(self):
        # Nur der eigene PTY-Prozess bekommt Tasten; keine globale Eingabe.
        pid, fd = pty.fork()
        if pid == 0:
            env = dict(os.environ, TERM="xterm-256color", HOME=str(self.root / "test-home"))
            os.execve(sys.executable, [sys.executable, str(Path(gmf.__file__)),
                                     str(self.root), "--lang", "en", "--filter", "local"], env)
        def until(marker):
            data = b""
            deadline = time.monotonic() + 15
            while marker not in data and time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    data += chunk
            self.assertIn(marker, data, data[-1500:])
        try:
            until(b"L pull")
            os.write(fd, b"l")
            until(b"Pull source")
            # Origin ist die erste Quelle; echte Pfeiltasten einmal hin und zurück.
            os.write(fd, b"\x1bOB\x1bOA\r")
            until(b"Apply this fast-forward?")
            os.write(fd, b"Y\r")
            until(b"Pulled origin/main")
            self.assertTrue((self.repo / "incoming.txt").exists())
            os.write(fd, b"q")
        finally:
            cleanup_pty(pid, fd)

    def test_oid_argument_rejects_options_and_mutable_refs(self):
        for value in ("HEAD", "--force", "a" * 40 + ":refs/heads/other"):
            with self.assertRaises(ValueError):
                gmf.safe_pull_args(value)


if __name__ == "__main__":
    unittest.main()
