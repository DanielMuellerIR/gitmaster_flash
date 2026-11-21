"""Diff-Ebenen und ihre CLI ändern weder Vormerkung noch Dateiinhalt."""

import copy
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from textwrap import dedent
from unittest.mock import Mock, patch

from gmf_test_support import (
    add_local_remote,
    commit_file,
    create_repo,
    git,
    init_repo,
    isolated_git_environment,
    mock_screen,
)

import gitmaster_flash as g


class DiffModeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gmf-diff-modes-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        environment = patch.dict(os.environ, isolated_git_environment(self.root), clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        language = patch.object(g, "UI_LANG", "en")
        language.start()
        self.addCleanup(language.stop)
        self.repo = create_repo(self.root)
        self.cfg = copy.deepcopy(g.DEFAULT_CONFIG)
        g.COMMAND_LOG.clear()
        self.addCleanup(g.COMMAND_LOG.clear)

    def diff(self, mode, path="README.md", code="M"):
        before = g.read_index_entries(self.repo, 10)
        ok, text = g.file_diff(self.repo, code, path, 10, mode)
        self.assertTrue(ok, text)
        self.assertEqual(before, g.read_index_entries(self.repo, 10))
        self.assertEqual(g.COMMAND_LOG, [])
        return text

    def mixed(self):
        (self.repo / "README.md").write_text("staged version\n")
        git(self.repo, "add", "README.md")
        (self.repo / "README.md").write_text("working version\n")


class DiffModeTests(DiffModeFixture):
    def test_mixed_file_has_three_different_comparisons(self):
        self.mixed()
        staged = self.diff("staged")
        unstaged = self.diff("unstaged")
        combined = self.diff("combined")
        self.assertIn("-# project", staged)
        self.assertIn("+staged version", staged)
        self.assertNotIn("working version", staged)
        self.assertIn("-staged version", unstaged)
        self.assertIn("+working version", unstaged)
        self.assertIn("-# project", combined)
        self.assertIn("+working version", combined)
        self.assertNotIn("staged version", combined)
        self.assertEqual((self.repo / "README.md").read_text(), "working version\n")

    def test_unborn_staged_and_working_versions_stay_separate(self):
        self.repo = init_repo(self.root / "unborn")
        (self.repo / "new.txt").write_text("index\n")
        git(self.repo, "add", "new.txt")
        (self.repo / "new.txt").write_text("worktree\n")
        self.assertIn("+index", self.diff("staged", "new.txt"))
        self.assertIn("-index", self.diff("unstaged", "new.txt"))
        self.assertIn("+worktree", self.diff("combined", "new.txt"))
        self.assertIsNone(g.current_head(self.repo, 10))

    def test_untracked_is_absent_from_staged_but_present_in_other_modes(self):
        (self.repo / "new.txt").write_text("new content\n")
        self.assertEqual(self.diff("staged", "new.txt", "U"), "")
        for mode in ("combined", "unstaged"):
            self.assertIn("+new content", self.diff(mode, "new.txt", "U"))

    def test_staged_deletion_and_untracked_replacement_are_distinct(self):
        git(self.repo, "rm", "--cached", "README.md")
        status = g.collect_status(self.repo, self.root, self.cfg)
        staged = g.files_for_diff_mode(status.files, "staged")
        unstaged = g.files_for_diff_mode(status.files, "unstaged")
        self.assertEqual([(e.code, e.path) for e in staged], [("D", "README.md")])
        self.assertEqual([(e.code, e.path) for e in unstaged], [("U", "README.md")])
        self.assertIn("-# project", self.diff("staged"))
        self.assertIn("+# project", self.diff("unstaged", code="U"))

    def test_staged_rename_and_later_edit_have_separate_layers(self):
        git(self.repo, "mv", "README.md", "renamed.md")
        (self.repo / "renamed.md").write_text("# project\nextra worktree line\n")
        staged = self.diff("staged", "renamed.md")
        unstaged = self.diff("unstaged", "renamed.md")
        self.assertIn("+# project", staged)
        self.assertNotIn("extra worktree line", staged)
        self.assertIn("+extra worktree line", unstaged)
        self.assertTrue(
            g.files_for_diff_mode(g.collect_status(self.repo, self.root, self.cfg).files, "staged")
        )

    def test_binary_and_mode_changes_are_not_empty(self):
        (self.repo / "binary.dat").write_bytes(b"base\0data")
        git(self.repo, "add", "binary.dat")
        git(self.repo, "commit", "-qm", "binary")
        (self.repo / "binary.dat").write_bytes(b"index\0data")
        git(self.repo, "add", "binary.dat")
        (self.repo / "binary.dat").write_bytes(b"worktree\0data")
        for mode in g.DIFF_MODES:
            self.assertIn("Binary files", self.diff(mode, "binary.dat"))
        git(self.repo, "config", "core.filemode", "true")
        (self.repo / "README.md").chmod(0o755)
        self.assertIn("new mode 100755", self.diff("unstaged"))
        git(self.repo, "add", "README.md")
        self.assertIn("new mode 100755", self.diff("staged"))

    def test_literal_crlf_paths_do_not_select_a_neighbor(self):
        for path, body in (
            ("line\rname.txt", "CR"),
            ("line\nname.txt", "LF"),
            (":(glob)*.txt", "magic"),
            ("other.txt", "other"),
        ):
            (self.repo / path).write_text(body + " base\n")
            git(self.repo, "--literal-pathspecs", "add", "--", path)
        git(self.repo, "commit", "-qm", "paths")
        for path, body in (
            ("line\rname.txt", "CR"),
            ("line\nname.txt", "LF"),
            (":(glob)*.txt", "magic"),
            ("other.txt", "other"),
        ):
            (self.repo / path).write_text(body + " index\n")
            git(self.repo, "--literal-pathspecs", "add", "--", path)
            (self.repo / path).write_text(body + " work\n")
        for mode in g.DIFF_MODES:
            text = self.diff(mode, "line\rname.txt")
            self.assertIn("CR ", text)
            self.assertNotIn("LF ", text)
            text = self.diff(mode, ":(glob)*.txt")
            self.assertIn("magic ", text)
            self.assertNotIn("other ", text)

    def test_no_layer_runs_external_diff_or_textconv(self):
        marker = self.root / "driver-ran"
        driver = self.root / "driver"
        driver.write_text("#!/bin/sh\ntouch " + shlex.quote(str(marker)) + "\n")
        driver.chmod(0o755)
        (self.repo / ".gitattributes").write_text("README.md diff=custom\n")
        git(self.repo, "add", ".gitattributes")
        git(self.repo, "commit", "-qm", "attributes")
        git(self.repo, "config", "diff.custom.textconv", str(driver))
        git(self.repo, "config", "color.ui", "always")
        self.mixed()
        with patch.dict(os.environ, {"GIT_EXTERNAL_DIFF": str(driver)}):
            for mode in g.DIFF_MODES:
                self.assertNotIn("\x1b", self.diff(mode))
        self.assertFalse(marker.exists())

    def test_replaced_head_does_not_change_any_layer(self):
        self.mixed()
        git(self.repo, "commit", "-qm", "replacement")
        replacement = g.current_head(self.repo, 10)
        git(self.repo, "reset", "--mixed", "-q", "HEAD^")
        real = g.current_head(self.repo, 10)
        git(self.repo, "replace", real, replacement)
        self.mixed()
        self.assertIn("-# project", self.diff("combined"))
        self.assertIn("-# project", self.diff("staged"))
        self.assertIn("-staged version", self.diff("unstaged"))

    def test_submodule_reference_and_dirty_contents_are_read_only(self):
        source = create_repo(self.root, "source")
        git(self.repo, "submodule", "add", "-q", str(source), "module")
        module = self.repo / "module"
        git(self.repo, "commit", "-qam", "module")
        base = g.current_head(module, 10)
        commit_file(module, "one.txt", "one\n", "one")
        index = g.current_head(module, 10)
        git(self.repo, "add", "module")
        commit_file(module, "two.txt", "two\n", "two")
        work = g.current_head(module, 10)
        (module / "draft.txt").write_text("draft\n")
        git(self.repo, "config", "diff.submodule", "diff")
        staged = self.diff("staged", "module")
        unstaged = self.diff("unstaged", "module")
        combined = self.diff("combined", "module")
        self.assertIn(base, staged)
        self.assertIn(index, staged)
        self.assertNotIn(work, staged)
        self.assertIn(index, unstaged)
        self.assertIn(work, unstaged)
        self.assertIn("-dirty", unstaged)
        self.assertIn(base, combined)
        self.assertIn(work, combined)
        self.assertEqual((module / "draft.txt").read_text(), "draft\n")

    def test_unmerged_path_is_available_in_every_mode(self):
        git(self.repo, "checkout", "-qb", "side")
        commit_file(self.repo, "README.md", "side\n", "side")
        git(self.repo, "checkout", "-q", "main")
        commit_file(self.repo, "README.md", "main\n", "main")
        result = g.run_git(self.repo, "merge", "side")
        self.assertNotEqual(result.returncode, 0)
        status = g.collect_status(self.repo, self.root, self.cfg)
        for mode in g.DIFF_MODES:
            self.assertTrue(g.files_for_diff_mode(status.files, mode))
            self.assertTrue(self.diff(mode))

    def test_mode_filters_preserve_mixed_entries_and_new_files(self):
        files = [
            g.ChangedFile("M", "index", "M "),
            g.ChangedFile("M", "work", " M"),
            g.ChangedFile("M", "both", "MM"),
            g.ChangedFile("U", "new", "??"),
            g.ChangedFile("C", "conflict", "UU"),
        ]
        self.assertEqual(
            [e.path for e in g.files_for_diff_mode(files, "staged")],
            ["index", "both", "conflict"],
        )
        self.assertEqual(
            [e.path for e in g.files_for_diff_mode(files, "unstaged")],
            ["work", "both", "new", "conflict"],
        )
        self.assertEqual(g.files_for_diff_mode(files, "combined"), files)
        with self.assertRaises(ValueError):
            g.files_for_diff_mode(files, "invalid")

    def test_tui_switches_empty_mode_back_to_working_files(self):
        (self.repo / "new.txt").write_text("new\n")
        status = g.collect_status(self.repo, self.root, self.cfg)
        screen = mock_screen(size=(15, 65), keys=[ord("2"), 10, ord("3"), 10, ord("q")])
        ui = g.TUI(screen, self.root, self.cfg, None)
        ui.statuses = [status]
        with (
            patch("gitmaster_flash.curses.color_pair", return_value=0),
            patch.object(ui, "show_pager") as pager,
        ):
            ui.action_file_changes()
        self.assertEqual(pager.call_count, 1)
        self.assertIn("unstaged", pager.call_args.args[0])
        self.assertIn("+new", "\n".join(pager.call_args.args[1]))
        self.assertTrue(
            any(g.t("diff_mode_empty") in str(call) for call in screen.addstr.call_args_list)
        )

    def test_cli_json_is_typed_and_does_not_scan_or_create_config(self):
        self.mixed()
        output = io.StringIO()
        with (
            patch.object(g, "collect_all", side_effect=AssertionError("scan")),
            patch.object(g, "load_config", side_effect=AssertionError("config")),
            redirect_stdout(output),
        ):
            result = g.main(
                [
                    str(self.repo),
                    "--file-diff",
                    "README.md",
                    "--diff-mode",
                    "staged",
                    "--json",
                ]
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(payload["kind"], "file_diff")
        self.assertEqual(payload["mode"], "staged")
        self.assertTrue(payload["ok"])
        self.assertIn("+staged version", payload["diff"])
        self.assertNotIn("working version", payload["diff"])

    def test_cli_empty_and_failed_diffs_have_distinct_exits(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = g.main(
                [
                    str(self.repo),
                    "--file-diff",
                    "README.md",
                    "--diff-mode",
                    "staged",
                    "--json",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["diff"], "")
        for path in (
            "../outside",
            "/absolute",
            "missing.txt",
            "",
            ".git/config",
            ".GIT/config",
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                result = g.main([str(self.repo), "--file-diff", path, "--json"])
            self.assertEqual(result, 2, path)
            self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_cli_diff_preserves_a_staged_deletion_in_both_output_formats(self):
        git(self.repo, "rm", "README.md")
        for mode, expected in (("combined", 1), ("staged", 1), ("unstaged", 0)):
            with self.subTest(mode=mode):
                output = io.StringIO()
                plain = io.StringIO()
                args = [str(self.repo), "--file-diff", "README.md", "--diff-mode", mode]
                with redirect_stdout(output):
                    self.assertEqual(g.main([*args, "--json"]), expected)
                with redirect_stdout(plain):
                    self.assertEqual(g.main(args), expected)
                payload = json.loads(output.getvalue())
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["diff"], plain.getvalue())
                if expected:
                    self.assertIn("deleted file mode", payload["diff"])
                else:
                    self.assertEqual(payload["diff"], "")

    def test_cli_rejects_incompatible_actions_before_any_scan(self):
        for extra in (
            ["--fetch"],
            ["--diff", "example"],
            ["--compare", "a", "b"],
            ["--demo"],
            ["--filter", "x"],
            ["--view", "changes"],
            ["--list"],
        ):
            with (
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as stopped,
            ):
                g.main([str(self.repo), "--file-diff", "README.md", *extra])
            self.assertEqual(stopped.exception.code, 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            g.main(["--diff-mode", "staged"])

    def test_cli_requires_repository_root(self):
        sub = self.repo / "nested"
        sub.mkdir()
        output = io.StringIO()
        with redirect_stdout(output):
            result = g.main([str(sub), "--file-diff", "README.md", "--json"])
        self.assertEqual(result, 2)
        self.assertIn("root", json.loads(output.getvalue())["error"])

    def test_cli_preserves_lines_but_neutralizes_terminal_controls(self):
        (self.repo / "new.txt").write_text("one\n\x1b[31mtwo\rthree\n")
        output = io.StringIO()
        with redirect_stdout(output):
            result = g.main([str(self.repo), "--file-diff", "new.txt"])
        self.assertEqual(result, 1)
        self.assertIn("+one\n", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())
        self.assertIn("\\r", output.getvalue())

    def test_failure_without_stderr_is_not_an_empty_success(self):
        result = subprocess.CompletedProcess([], 128, stdout="", stderr="")
        with patch.object(g, "run_git", return_value=result):
            ok, text = g.file_diff(self.repo, "M", "README.md", 10, "unstaged")
        self.assertFalse(ok)
        self.assertIn("128", text)
        output = io.StringIO()
        with (
            patch.object(g, "file_diff", return_value=(False, "")),
            redirect_stdout(output),
        ):
            result = g.main([str(self.repo), "--file-diff", "README.md", "--json"])
        self.assertEqual(result, 2)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_cli_blackbox_reports_selected_mode(self):
        self.mixed()
        env = dict(os.environ, HOME=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [
                sys.executable,
                str(Path(g.__file__).resolve()),
                str(self.repo),
                "--file-diff",
                "README.md",
                "--diff-mode",
                "unstaged",
                "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("-staged version", json.loads(result.stdout)["diff"])
        self.assertFalse((self.root / ".config/gitmaster_flash").exists())

    def test_macos_app_shortcut_reports_unconfirmed_launch_after_timeout(self):
        app = self.root / "Editor.app"
        app.mkdir()
        cfg = dict(self.cfg, git_timeout=3, apps={"E": {"name": "Editor", "path": str(app)}})
        ui = g.TUI(Mock(), self.root, cfg, None)
        ui.statuses = [g.RepoStatus(self.repo, "project")]
        with (
            patch.object(g.sys, "platform", "darwin"),
            patch.object(
                g.subprocess, "run", side_effect=subprocess.TimeoutExpired("open", 3)
            ) as run,
        ):
            ui.action_open_app("E")

        run.assert_called_once_with(
            ["open", "-a", str(app), str(self.repo)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(ui.message, "Could not confirm Editor opened within 3s.")

    def test_linux_app_shortcut_requires_desktop(self):
        screen = Mock()
        cfg = dict(self.cfg, apps={"E": {"name": "Editor", "path": str(self.root)}})
        ui = g.TUI(screen, self.root, cfg, None)
        ui.statuses = [g.RepoStatus(self.repo, "project")]
        with (
            patch.object(g.sys, "platform", "linux"),
            patch.object(g.subprocess, "run") as run,
            patch.dict(os.environ, {}, clear=True),
        ):
            ui.action_open_app("E")
        run.assert_not_called()
        self.assertEqual(ui.message, g.t("app_no_desktop"))

    def test_linux_app_shortcut_passes_folder_without_shell(self):
        app = self.root / "editor with spaces"
        app.write_text("#!/bin/sh\nexit 0\n")
        app.chmod(0o755)
        cfg = dict(self.cfg, apps={"E": {"name": "Editor", "path": str(app)}})
        ui = g.TUI(Mock(), self.root, cfg, None)
        ui.statuses = [g.RepoStatus(self.repo, "project")]
        child = Mock()
        child.wait.side_effect = subprocess.TimeoutExpired("editor", 0.2)
        with (
            patch.object(g.sys, "platform", "linux"),
            patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-1"}, clear=True),
            patch.object(g.subprocess, "Popen", return_value=child) as spawn,
            patch.object(g.threading, "Thread"),
        ):
            ui.action_open_app("E")
        self.assertEqual(spawn.call_args.args[0], [str(app), str(self.repo)])
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        spawn.call_args.kwargs["stderr"].close()
        self.assertEqual(ui.message, g.t("app_starting", name="Editor", rel="project"))
        child.wait.side_effect = None
        child.wait.return_value = 2
        child.stderr.read.return_value = b"failure"
        with (
            patch.object(g.sys, "platform", "linux"),
            patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True),
            patch.object(g.subprocess, "Popen", return_value=child),
            patch.object(ui, "_app_launch_result", return_value=(2, "failure")),
        ):
            ui.action_open_app("E")
        self.assertIn("2", ui.message)

    def test_linux_app_shortcut_rejects_directories_and_spawn_errors(self):
        cfg = dict(self.cfg, apps={"E": {"name": "Editor", "path": str(self.root)}})
        ui = g.TUI(Mock(), self.root, cfg, None)
        ui.statuses = [g.RepoStatus(self.repo, "project")]
        with (
            patch.object(g.sys, "platform", "linux"),
            patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True),
            patch.object(g.subprocess, "Popen") as spawn,
        ):
            ui.action_open_app("E")
        spawn.assert_not_called()
        self.assertEqual(ui.message, g.t("app_not_found", p=str(self.root)))
        app = self.root / "editor"
        app.write_text("invalid")
        app.chmod(0o755)
        ui.cfg["apps"]["E"]["path"] = str(app)
        with (
            patch.object(g.sys, "platform", "linux"),
            patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True),
            patch.object(g.subprocess, "Popen", side_effect=OSError("launch failed")),
        ):
            ui.action_open_app("E")
        self.assertIn("launch failed", ui.message)

    def test_relative_launcher_uses_checked_file_and_reports_delayed_failure(self):
        app = self.root / "editor"
        wrong = self.root / "searched"
        wrong.mkdir()
        (wrong / "editor").write_text("#!/bin/sh\nexit 0\n")
        (wrong / "editor").chmod(0o755)
        app.write_text("#!/bin/sh\nsleep 0.4\necho fixture_failure >&2\nexit 7\n")
        app.chmod(0o755)
        cfg = dict(self.cfg, apps={"E": {"name": "Editor", "path": "editor"}})
        ui = g.TUI(Mock(), self.root, cfg, None)
        ui.statuses = [g.RepoStatus(self.repo, "project")]
        before = Path.cwd()
        try:
            os.chdir(self.root)
            with (
                patch.object(g.sys, "platform", "linux"),
                patch.dict(
                    os.environ,
                    {
                        "DISPLAY": ":1",
                        "PATH": str(wrong) + os.pathsep + os.environ["PATH"],
                    },
                    clear=True,
                ),
            ):
                ui.action_open_app("E")
            self.assertEqual(ui.message, g.t("app_starting", name="Editor", rel="project"))
            deadline = time.monotonic() + 5
            while ui.app_results.empty() and time.monotonic() < deadline:
                time.sleep(0.01)
            ui.drain_app_results()
            self.assertIn("Exit 7", ui.message)
            self.assertIn("fixture_failure", ui.message)
        finally:
            os.chdir(before)

    def test_symlink_launcher_keeps_its_configured_name(self):
        app, link = self.root / "launcher", self.root / "gui-editor"
        marker = self.root / "received"
        app.write_text(
            '#!/bin/sh\ncase "$0" in */gui-editor) printf gui > '
            + shlex.quote(str(marker))
            + ";; *) exit 9;; esac\n"
        )
        app.chmod(0o755)
        link.symlink_to(app)
        ui = g.TUI(
            Mock(),
            self.root,
            dict(self.cfg, apps={"E": {"name": "Editor", "path": str(link)}}),
            None,
        )
        ui.statuses = [g.RepoStatus(self.repo, "project")]
        with (
            patch.object(g.sys, "platform", "linux"),
            patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True),
        ):
            ui.action_open_app("E")
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(marker.read_text(), "gui")

    def test_directory_symlink_parent_keeps_checked_launcher_identity(self):
        local = self.root / "local"
        local.mkdir()
        version = self.root / "versions" / "v1"
        version.mkdir(parents=True)
        (local / "link").symlink_to(version, target_is_directory=True)
        marker = self.root / "received"
        correct = version.parent / "editor"
        correct.write_text("#!/bin/sh\nprintf correct > " + shlex.quote(str(marker)) + "\n")
        correct.chmod(0o755)
        (local / "editor").write_text("#!/bin/sh\nexit 9\n")
        (local / "editor").chmod(0o755)
        before = os.getcwd()
        try:
            os.chdir(self.root)
            ui = g.TUI(
                Mock(),
                self.root,
                dict(
                    self.cfg,
                    apps={"E": {"name": "Editor", "path": "local/link/../editor"}},
                ),
                None,
            )
            ui.statuses = [g.RepoStatus(self.repo, "project")]
            with (
                patch.object(g.sys, "platform", "linux"),
                patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True),
            ):
                ui.action_open_app("E")
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(marker.read_text(), "correct")
        finally:
            os.chdir(before)

    def test_loop_displays_delayed_app_failure_without_user_input(self):
        app = self.root / "editor"
        app.write_text("#!/bin/sh\nsleep .4\necho delayed_failure >&2\nexit 7\n")
        app.chmod(0o755)
        screen = Mock()
        ui = g.TUI(
            screen,
            self.root,
            dict(self.cfg, apps={"E": {"name": "Editor", "path": str(app)}}),
            None,
        )
        ui.statuses = [g.RepoStatus(self.repo, "project")]
        timeouts, messages = [], []
        screen.timeout.side_effect = lambda value: timeouts.append(value)

        def getch():
            if "Exit 7" in ui.message:
                return ord("q")
            self.assertLess(time.monotonic(), deadline)
            self.assertEqual(timeouts[-1], g.BACKGROUND_POLL_MS)
            time.sleep(timeouts[-1] / 1000)
            return -1

        screen.getch.side_effect = getch
        ui.draw = lambda: messages.append(ui.message)
        deadline = time.monotonic() + 3
        with (
            patch.object(g.sys, "platform", "linux"),
            patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True),
        ):
            ui.action_open_app("E")
            ui._loop()
        self.assertTrue(any("delayed_failure" in message for message in messages))
        self.assertEqual(ui.pending_app_launches, 0)

    def test_delayed_launcher_reports_exit_when_diagnostics_are_unavailable(self):
        app = self.root / "editor"
        app.write_text("#!/bin/sh\n")
        app.chmod(0o755)
        cfg = dict(self.cfg, apps={"E": {"name": "Editor", "path": str(app)}})
        ui = g.TUI(Mock(), self.root, cfg, None)
        ui.statuses = [g.RepoStatus(self.repo, "project")]
        child = Mock()
        child.wait.side_effect = [subprocess.TimeoutExpired("editor", 0.2), 7]
        diagnostics = Mock()
        diagnostics.read_tail.side_effect = OSError("fixture diagnostics failed")
        with (
            patch.object(g.sys, "platform", "linux"),
            patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True),
            patch.object(g.subprocess, "Popen", return_value=child),
            patch.object(g, "AppDiagnostics", return_value=diagnostics),
        ):
            ui.action_open_app("E")
            deadline = time.monotonic() + 3
            while ui.pending_app_launches and time.monotonic() < deadline:
                ui.drain_app_results()
                time.sleep(0.01)
        self.assertEqual(ui.pending_app_launches, 0)
        self.assertIn("Exit 7", ui.message)
        self.assertIn("fixture diagnostics failed", ui.message)
        diagnostics.close.assert_called_once()

    def test_launcher_exit_does_not_wait_for_descendants_stderr(self):
        self.check_launcher_descendant(fast=False)

    def test_fast_launcher_exit_does_not_block_tui_on_inherited_stderr(self):
        self.check_launcher_descendant(fast=True)

    def check_launcher_descendant(self, *, fast):
        # The real shell child writes only after the starter result is reported.
        app = self.root / "editor"
        release, completed = self.root / "release-writer", self.root / "completed"
        app.write_text(
            "#!/bin/sh\nsleep 0.4\n(\n"
            "while [ ! -f " + shlex.quote(str(release)) + " ]; do sleep 0.01; done\n"
            "printf late_write >&2 && printf complete > "
            + shlex.quote(str(completed))
            + "\n) &\necho fixture_failure >&2\nexit 7\n"
        )
        app.chmod(0o755)
        cfg = dict(self.cfg, apps={"E": {"name": "Editor", "path": str(app)}})
        ui = g.TUI(Mock(), self.root, cfg, None)
        ui.statuses = [g.RepoStatus(self.repo, "project")]
        children, threads = [], []
        real_spawn, real_thread = subprocess.Popen, threading.Thread

        def spawn(*args, **kwargs):
            child = real_spawn(*args, **kwargs)
            children.append(child)
            if fast:
                child.wait(timeout=5)  # Deterministically exercise the synchronous path.
            return child

        def make_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            threads.append(thread)
            return thread

        worker = real_thread(target=ui.action_open_app, args=("E",), daemon=True)
        try:
            with (
                patch.object(g.sys, "platform", "linux"),
                patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True),
                patch.object(g.subprocess, "Popen", side_effect=spawn),
                patch.object(g.threading, "Thread", side_effect=make_thread),
            ):
                worker.start()
                worker.join(timeout=2)
                self.assertFalse(
                    worker.is_alive(),
                    "TUI waits for inherited stderr after launcher exit",
                )
                deadline = time.monotonic() + 2
                while children[0].poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(children[0].poll(), 7)
                while "Exit 7" not in ui.message and time.monotonic() < deadline:
                    ui.drain_app_results()
                    time.sleep(0.01)
                self.assertIn("Exit 7", ui.message)
                self.assertIn("fixture_failure", ui.message)
                release.touch()
                deadline = time.monotonic() + 2
                while not completed.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(completed.read_text(), "complete")
        finally:
            for child in children:
                g._kill_process_group(child)
                child.wait(timeout=5)
            worker.join(timeout=5)
            for thread in threads:
                thread.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_linux_real_launcher_receives_exact_repository_argument(self):
        target = self.root / "project ; literal $name"
        target.mkdir()
        app = self.root / "editor launcher"
        received = self.root / "received"
        app.write_text('#!/bin/sh\nprintf "%s" "$1" > "' + str(received) + '"\n')
        app.chmod(0o755)
        cfg = dict(self.cfg, apps={"E": {"name": "Editor", "path": str(app)}})
        ui = g.TUI(Mock(), self.root, cfg, None)
        ui.statuses = [g.RepoStatus(target, "project")]
        with (
            patch.object(g.sys, "platform", "linux"),
            patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True),
        ):
            ui.action_open_app("E")
        deadline = time.monotonic() + 5
        while not received.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(received.read_text(), str(target))


class AppDiagnosticLifetimeTests(unittest.TestCase):
    def test_child_writes_after_actual_launcher_parent_exit(self):
        with tempfile.TemporaryDirectory(prefix="gmf-app-lifetime-") as directory:
            root = Path(directory)
            release, marker = root / "release", root / "completed"
            writer = root / "editor"
            writer_script = dedent(f"""\
                import sys
                import time
                from pathlib import Path

                deadline = time.monotonic() + 10
                while not Path({str(release)!r}).exists():
                    if time.monotonic() >= deadline:
                        sys.exit(2)
                    time.sleep(0.01)
                sys.stderr.buffer.write(b"x" * (256 * 1024))
                sys.stderr.buffer.flush()
                Path({str(marker)!r}).write_text("complete")
                """)
            writer.write_text(f"#!{sys.executable}\n{writer_script}")
            writer.chmod(0o755)
            script = dedent("""\
                import sys
                from pathlib import Path
                from unittest.mock import Mock

                import gitmaster_flash as g

                g.sys.platform = "linux"
                root = Path(sys.argv[1])
                cfg = dict(g.DEFAULT_CONFIG, apps={
                    "E": {"name": "Editor", "path": sys.argv[2]},
                })
                ui = g.TUI(Mock(), root, cfg, None)
                ui.statuses = [g.RepoStatus(root, "project")]
                ui.action_open_app("E")
                """)
            env = dict(os.environ, DISPLAY=":1")
            env.pop("SSH_CONNECTION", None)
            env.pop("SSH_TTY", None)
            parent = subprocess.run(
                [sys.executable, "-c", script, str(root), str(writer)],
                env=env,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(parent.returncode, 0, parent.stderr)
            release.touch()
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(marker.read_text(), "complete")

    def test_reader_bounds_diagnostic(self):
        diagnostics = g.AppDiagnostics()
        script = dedent("""\
            import sys

            for _ in range(512):
                sys.stderr.buffer.write(b"x" * 65536)
            sys.stderr.buffer.write(b"failure")
            sys.exit(7)
            """)
        child = subprocess.Popen(
            [sys.executable, "-c", script],
            stderr=diagnostics,
            stdout=subprocess.DEVNULL,
        )
        self.assertEqual(
            g.TUI._app_launch_result(child, diagnostics),
            (7, "x" * (4096 - 7) + "failure"),
        )
        self.assertTrue(diagnostics.closed)


class CoverageCountTests(DiffModeFixture):
    def setUp(self):
        super().setUp()
        add_local_remote(self.repo, self.root / "origin.git", upstream=True)

    def test_same_oid_is_counted_once_per_ref_measurement(self):
        for i in range(24):
            git(self.repo, "branch", f"alias-{i}")
        calls = []
        original = g.run_git

        def record(*args, **kwargs):
            if args[1:3] == ("rev-list", "--count"):
                calls.append(args[3])
            return original(*args, **kwargs)

        with patch.object(g, "run_git", side_effect=record):
            branches = g.read_branch_inventory(self.repo, self.cfg)
        self.assertEqual(len(branches), 25)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(b.coverage == "covered" for b in branches))

    def test_distinct_tips_and_later_scan_get_fresh_counts(self):
        git(self.repo, "checkout", "-qb", "topic")
        commit_file(self.repo, "new.txt", "new\n", "topic")
        git(self.repo, "branch", "alias")
        git(self.repo, "checkout", "-q", "main")
        before = {b.name: b for b in g.read_branch_inventory(self.repo, self.cfg)}
        self.assertEqual(before["topic"].uncovered_commits, 1)
        git(self.repo, "push", "-q", "origin", "topic")
        after = {b.name: b for b in g.read_branch_inventory(self.repo, self.cfg)}
        self.assertEqual(after["topic"].uncovered_commits, 0)
        self.assertEqual(after["alias"].uncovered_commits, 0)

    def test_ref_race_is_still_rejected_with_shared_oid_counts(self):
        git(self.repo, "branch", "alias")
        original = g.run_git
        changed = False

        def move(*args, **kwargs):
            nonlocal changed
            result = original(*args, **kwargs)
            if args[1:3] == ("rev-list", "--count") and not changed:
                changed = True
                git(self.repo, "branch", "new-alias")
            return result

        with (
            patch.object(g, "run_git", side_effect=move),
            self.assertRaises(g.GitReadError),
        ):
            g.read_branch_inventory(self.repo, self.cfg)
