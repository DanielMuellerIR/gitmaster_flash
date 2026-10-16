"""Worktree-Zuordnung und Submodule bleiben lesend, pfadgebunden und unterscheidbar."""

import copy
import curses
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import gitmaster_flash as g


class TopologyFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gmf-topology-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        environment = patch.dict(os.environ, g._demo_env(), clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        language = patch.object(g, "UI_LANG", "en")
        language.start()
        self.addCleanup(language.stop)
        self.cfg = copy.deepcopy(g.DEFAULT_CONFIG)
        g.resume_git_calls()
        self.addCleanup(g.resume_git_calls)
        self.repo, self.bare = g._demo_repo(self.root, "project")

    def status(self, root=None):
        return g.collect_status(self.repo, root or self.root, self.cfg)

    def ui(self, status, keys=()):
        screen = Mock()
        screen.getmaxyx.return_value = (30, 110)
        screen.getch.side_effect = keys
        ui = g.TUI(screen, self.root, self.cfg, None)
        ui.all_statuses = [status]
        ui.statuses = [status]
        return ui

    def add_module(self):
        self.source, _ = g._demo_repo(self.root, "source")
        g._dgit(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                str(self.source), "deps/lib")
        self.module = self.repo / "deps/lib"
        g._demo_harden(self.module)
        g._dgit(self.repo, "commit", "-qam", "add module")
        g._dgit(self.repo, "push", "-q", "origin", "main")
        return self.module


class WorktreeTests(TopologyFixture):
    def test_shared_family_keeps_individual_working_states(self):
        linked = self.root / "topic"
        g._dgit(self.repo, "worktree", "add", "-q", "-b", "topic", str(linked))
        (linked / "draft.txt").write_text("draft\n")
        main = self.status()
        other = g.collect_status(linked, self.root, self.cfg)
        self.assertEqual(main.worktree_family_id, other.worktree_family_id)
        self.assertEqual(g.path_identity(main.common_dir), g.path_identity(other.common_dir))
        self.assertFalse(main.dirty)
        self.assertTrue(other.dirty)
        self.assertEqual(main.branch, "main")
        self.assertEqual(other.branch, "topic")
        self.assertEqual(len(main.worktrees), 2)
        self.assertTrue(g._valid_remote_payload(g.make_snapshot(self.root, [main, other])))
        text = "\n".join(g.worktree_lines(main, [main, other]))
        self.assertIn("draft", " ".join(entry.path for entry in other.files))
        self.assertIn("U:1", text)
        self.assertIn("topic", text)

    def test_external_worktree_is_registered_without_running_git_there(self):
        linked = self.root / "external"
        g._dgit(self.repo, "worktree", "add", "-q", "-b", "external", str(linked))
        (linked / "unmeasured.txt").write_text("unmeasured\n")
        calls = []
        original = g.run_git
        def record(repo, *args, **kwargs):
            calls.append(g.path_identity(repo))
            return original(repo, *args, **kwargs)
        with patch.object(g, "run_git", side_effect=record):
            status = self.status(root=self.repo)
        self.assertNotIn(g.path_identity(linked), calls)
        member = next(entry for entry in status.worktrees if entry.path == linked)
        self.assertFalse(member.in_scan_root)
        self.assertTrue(member.present)
        self.assertIn("outside scan root", "\n".join(g.worktree_lines(status, [status])))
        self.assertEqual(g.find_repos(self.repo, self.cfg["skip_dirs"]), [self.repo])

    def test_locked_missing_and_prunable_are_separate_states(self):
        locked, missing = self.root / "locked", self.root / "missing"
        g._dgit(self.repo, "worktree", "add", "-q", "-b", "locked", str(locked))
        g._dgit(self.repo, "worktree", "add", "-q", "-b", "missing", str(missing))
        reason = "drive offline\r\nkeep registration"
        g._dgit(self.repo, "worktree", "lock", "--reason", reason, str(locked))
        shutil.rmtree(locked)
        shutil.rmtree(missing)
        status = self.status()
        states = {entry.path.name: entry for entry in status.worktrees}
        self.assertTrue(states["locked"].locked)
        self.assertEqual(states["locked"].lock_reason, reason)
        self.assertFalse(states["locked"].present)
        self.assertTrue(states["missing"].prunable)
        self.assertFalse(states["missing"].present)
        text = "\n".join(g.worktree_lines(status, [status]))
        self.assertIn("working directory missing", text)
        self.assertIn("\\r\\n", text)
        self.assertNotIn("\r", text)

    def test_detached_and_bare_family_members_are_ordinary_metadata(self):
        linked = self.root / "detached"
        g._dgit(self.bare, "worktree", "add", "-q", "--detach", str(linked), "main")
        status = g.collect_status(linked, self.root, self.cfg)
        self.assertEqual(status.branch, "(detached)")
        self.assertEqual(status.worktrees_error, "")
        self.assertTrue(any(entry.bare for entry in status.worktrees))
        self.assertTrue(any(entry.is_current and entry.detached for entry in status.worktrees))
        self.assertTrue(g._valid_remote_payload(g.make_snapshot(self.root, [status])))

    def test_unborn_worktree_and_control_characters_are_preserved(self):
        unborn = self.root / "unborn \r\n"
        unborn.mkdir()
        g._dgit(unborn, "init", "-q", "-b", "main")
        g._demo_harden(unborn)
        status = g.collect_status(unborn, self.root, self.cfg)
        self.assertIsNone(status.head_oid)
        self.assertEqual(status.worktrees_error, "")
        self.assertEqual(status.worktrees[0].path, unborn)
        self.assertIsNone(status.worktrees[0].head_oid)
        self.assertTrue(status.worktrees[0].is_current)

    def test_worktree_parser_rejects_incomplete_or_ambiguous_records(self):
        oid = "a" * 40
        valid = f"worktree /tmp/path\r\nname\0HEAD {oid}\0branch refs/heads/main\0\0"
        self.assertEqual(str(g.parse_worktrees(valid)[0].path), "/tmp/path\r\nname")
        invalid = [valid[:-1], valid.replace("/tmp/path", "relative"),
                   valid.replace(f"HEAD {oid}", "HEAD invalid"),
                   valid.replace("branch refs/heads/main", "branch main"),
                   valid.replace("branch refs/heads/main", "detached\0branch refs/heads/main"),
                   valid + valid]
        for output in invalid:
            with self.subTest(output=repr(output)):
                with self.assertRaises(g.GitReadError):
                    g.parse_worktrees(output)

    def test_missing_optional_measurements_and_failures_are_not_empty_inventories(self):
        status = self.status()
        with patch.object(g, "collect_worktrees", side_effect=g.GitReadError("cannot list worktrees")):
            failed = self.status()
        self.assertFalse(failed.clean_and_synced)
        self.assertIn("W", g.inspection_summary(failed))
        findings = g.diff_findings(g.make_snapshot(self.root, [status]),
                                   g.make_snapshot(self.root, [failed]), "here", "there")
        self.assertTrue(any(row["type"] == "worktree_inventory_failed" for row in findings))
        self.assertFalse(any(row["category"] == "drift" for row in findings))
        with patch.object(g, "valid_timestamp", wraps=g.valid_timestamp):
            broken = g.status_dict(status)["worktree_family"]
            broken["id"] = []
            self.assertFalse(g.valid_worktree_family(broken, ""))
            self.assertFalse(g.valid_worktree_family(broken, "error"))

    def test_w_key_refreshes_locally_and_opens_the_read_only_family_view(self):
        status = self.status()
        ui = self.ui(status)
        with patch.object(ui, "show_busy"), patch.object(ui, "show_pager") as pager:
            ui.dispatch_action("W")
        self.assertIn("Worktree family", pager.call_args.args[0])
        self.assertIn(str(self.repo), "\n".join(pager.call_args.args[1]))
        self.assertFalse(any(remote.fetched_at for remote in ui.current().remotes))


class SubmoduleTests(TopologyFixture):
    def test_references_and_contents_are_distinct_and_parent_index_is_preserved(self):
        self.add_module()
        before = g.read_index_entries(self.repo, 10)
        status = self.status()
        module = status.submodules[0]
        self.assertTrue(module.initialized)
        self.assertEqual(module.worktree_kind, "repository")
        self.assertEqual(module.head_oid, module.index_oid)
        self.assertEqual(module.index_oid, module.checked_out_oid)
        self.assertFalse(module.content_dirty)
        self.assertTrue(status.clean_and_synced)
        self.assertEqual(before, g.read_index_entries(self.repo, 10))
        self.assertTrue(g._valid_remote_payload(g.make_snapshot(self.root, [status])))

    def test_ignore_settings_cannot_hide_module_contents_and_c_does_not_commit_them(self):
        self.add_module()
        (self.module / "README.md").write_text("changed module content\n")
        (self.module / "draft.txt").write_text("untracked module content\n")
        g._dgit(self.repo, "config", "diff.ignoreSubmodules", "all")
        g._dgit(self.repo, "config", "submodule.deps/lib.ignore", "all")
        status = self.status()
        self.assertTrue(status.dirty)
        self.assertEqual(status.submodule_review_count, 1)
        module = status.submodules[0]
        self.assertEqual(module.modified, 1)
        self.assertEqual(module.untracked, 1)
        entry = next(entry for entry in status.files if entry.path == module.path)
        self.assertFalse(g.committable_against_head(entry, module))
        before = g.read_index_entries(self.repo, 10)
        ui = self.ui(status)
        ui.dispatch_action("C")
        self.assertIn("Commit inside the module first", ui.message)
        self.assertEqual(before, g.read_index_entries(self.repo, 10))
        self.assertIn("Uncommitted files", "\n".join(g.submodule_lines(status)))

    def test_parent_commit_records_only_the_changed_reference_and_preserves_other_staging(self):
        self.add_module()
        g._demo_commit(self.module, "feature.txt", "feature\n", "module feature")
        (self.module / "draft.txt").write_text("leave inside module\n")
        (self.repo / "README.md").write_text("foreign staging\n")
        g._dgit(self.repo, "add", "README.md")
        staged_before = g.run_git(self.repo, "ls-files", "--stage", "--", "README.md").stdout
        status = self.status()
        module = status.submodules[0]
        entry = next(entry for entry in status.files if entry.path == module.path)
        self.assertTrue(g.committable_against_head(entry, module))
        result = g.commit_selected(self.repo, [module.path], "update module reference", 10)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = g.read_head_gitlinks(self.repo, g.current_head(self.repo, 10), {module.path}, 10)
        self.assertEqual(recorded[module.path], module.checked_out_oid)
        self.assertTrue((self.module / "draft.txt").exists())
        self.assertEqual(g.run_git(self.repo, "ls-files", "--stage", "--", "README.md").stdout, staged_before)
        self.assertTrue(self.status().dirty)

    def test_uninitialized_module_is_not_inspected_as_its_parent(self):
        self.add_module()
        g._dgit(self.repo, "submodule", "deinit", "-f", "--", "deps/lib")
        calls = []
        original = g.run_git
        def record(repo, *args, **kwargs):
            calls.append(Path(repo))
            return original(repo, *args, **kwargs)
        with patch.object(g, "run_git", side_effect=record):
            status = self.status()
        module = status.submodules[0]
        self.assertFalse(module.initialized)
        self.assertEqual(module.worktree_kind, "directory")
        self.assertIsNone(module.checked_out_oid)
        self.assertNotIn(self.module, calls)
        self.assertIn("not initialized", "\n".join(g.submodule_lines(status)))

    def test_staged_reference_without_checkout_is_not_mislabeled_as_only_contents(self):
        self.add_module()
        g._demo_commit(self.source, "next.txt", "next\n", "source next")
        target = g.current_head(self.source, 10)
        g._dgit(self.repo, "submodule", "deinit", "-f", "--", "deps/lib")
        g._dgit(self.repo, "update-index", "--cacheinfo", "160000", target, "deps/lib")
        status = self.status()
        ui = self.ui(status)
        ui.dispatch_action("C")
        self.assertIn("cannot be committed from this state", ui.message)
        self.assertNotIn("Only module contents", ui.message)
        self.assertEqual(status.submodules[0].index_oid, target)

    def test_escaping_symlink_is_reported_without_reading_the_foreign_repository(self):
        self.add_module()
        shutil.rmtree(self.module)
        self.module.symlink_to(self.source, target_is_directory=True)
        calls = []
        original = g.run_git
        def record(repo, *args, **kwargs):
            calls.append(g.path_identity(repo))
            return original(repo, *args, **kwargs)
        with patch.object(g, "run_git", side_effect=record):
            status = self.status()
        self.assertNotIn(g.path_identity(self.source), calls)
        self.assertIn("leaves parent", status.submodules[0].error)
        self.assertTrue(g.repo_matches_view(status, "access"))
        self.assertIn("unknown", "\n".join(g.submodule_lines(status)))

    def test_index_race_is_an_explicit_inventory_failure_not_missing_modules(self):
        self.add_module()
        good = self.status()
        original = g.read_index_entries
        calls = 0
        def changed(repo, timeout):
            nonlocal calls
            entries = original(repo, timeout)
            calls += 1
            if calls == 2:
                entries["deps/lib"] = [("160000", "b" * 40, 0)]
            return entries
        with patch.object(g, "read_index_entries", side_effect=changed):
            failed = self.status()
        self.assertIn("changed during inventory", failed.submodules_error)
        self.assertFalse(failed.submodules_scanned)
        findings = g.diff_findings(g.make_snapshot(self.root, [good]),
                                   g.make_snapshot(self.root, [failed]), "here", "there")
        self.assertTrue(any(row["type"] == "submodule_inventory_failed" for row in findings))
        self.assertFalse(any(row["type"] == "submodule_state" for row in findings))

    def test_child_head_race_is_a_local_inspection_failure(self):
        self.add_module()
        original = g.current_head
        calls = 0
        def changed(repo, timeout, **kwargs):
            nonlocal calls
            if Path(repo) == self.module:
                calls += 1
                if calls == 2:
                    return "b" * 40
            return original(repo, timeout, **kwargs)
        with patch.object(g, "current_head", side_effect=changed):
            status = self.status()
        self.assertIn("HEAD changed", status.submodules[0].error)
        self.assertEqual(status.submodules_error, "")
        self.assertTrue(g._valid_remote_payload(g.make_snapshot(self.root, [status])))

    def test_active_child_operation_without_file_changes_stays_actionable(self):
        self.add_module()
        marker = g._git_path(self.module, "MERGE_HEAD", 10)
        marker.write_text(g.current_head(self.module, 10) + "\n")
        status = self.status()
        self.assertEqual(status.submodules[0].operation, "merge")
        self.assertEqual(status.submodule_review_count, 1)
        self.assertFalse(status.clean_and_synced)
        self.assertTrue(g.repo_matches_view(status, "changes"))
        self.assertTrue(g.repo_matches_view(g.status_dict(status), "changes"))
        self.assertIn("merge", status.submodules[0].operation)

    def test_module_replaced_by_a_file_can_commit_the_file_change(self):
        self.add_module()
        shutil.rmtree(self.module)
        self.module.write_text("replacement\n")
        g._dgit(self.repo, "add", "--", "deps/lib")
        status = self.status()
        module = status.submodules[0]
        self.assertEqual(module.worktree_kind, "file")
        self.assertEqual(module.index_mode, "100644")
        entry = next(entry for entry in status.files if entry.path == "deps/lib")
        self.assertTrue(g.committable_against_head(entry, module))
        self.assertIn("replaced by a file", "\n".join(g.submodule_lines(status)))
        self.assertTrue(g._valid_remote_payload(g.make_snapshot(self.root, [status])))

    def test_saved_comparison_reports_module_state_locally_and_legacy_limits(self):
        self.add_module()
        before = g.make_snapshot(self.root, [self.status()])
        (self.module / "draft.txt").write_text("draft\n")
        after = g.make_snapshot(self.root, [self.status()])
        findings = g.diff_findings(before, after, "here", "there")
        module = next(row for row in findings if row["type"] == "submodule_state")
        self.assertEqual(module["category"], "local")
        self.assertEqual(module["values"]["path"], "deps/lib")
        self.assertEqual(module["values"]["there_state"]["untracked"], 1)
        legacy = copy.deepcopy(before)
        for key in ("submodules", "submodules_error", "submodule_review_count"):
            legacy["repos"][0].pop(key, None)
        self.assertTrue(any(limit["code"] == "submodule_inventory_unknown" for limit in
                            g.snapshot_limitations(legacy, "here", after)))

    def test_module_schema_rejects_malformed_counts_paths_and_modes(self):
        self.add_module()
        payload = g.make_snapshot(self.root, [self.status()])
        for key, value in (("path", "../outside"), ("modified", -1), ("index_mode", "blob"),
                           ("worktree_kind", "unknown"), ("initialized", False), ("index_oid", "b" * 40)):
            broken = copy.deepcopy(payload)
            broken["repos"][0]["submodules"][0][key] = value
            self.assertFalse(g._valid_remote_payload(broken), key)

    def test_o_key_is_read_only_and_file_rows_name_the_module(self):
        self.add_module()
        (self.module / "draft.txt").write_text("draft\n")
        status = self.status()
        before = g.read_index_entries(self.repo, 10)
        ui = self.ui(status)
        with patch.object(ui, "show_busy"), patch.object(ui, "show_pager") as pager:
            ui.dispatch_action("O")
        self.assertIn("Submodules", pager.call_args.args[0])
        self.assertIn("Parent index", "\n".join(pager.call_args.args[1]))
        self.assertEqual(before, g.read_index_entries(self.repo, 10))
        self.assertIn("[submodule]", g.file_row("M", "deps/lib", True)[0])

    def test_conflicted_gitlinks_keep_index_stages_and_block_parent_commit(self):
        self.add_module()
        base = g.current_head(self.module, 10)
        g._demo_commit(self.module, "one.txt", "one\n", "module one")
        ours = g.current_head(self.module, 10)
        g._demo_commit(self.module, "two.txt", "two\n", "module two")
        theirs = g.current_head(self.module, 10)
        g._dgit(self.repo, "update-index", "--force-remove", "deps/lib")
        stages = "".join(f"160000 {oid} {stage}\tdeps/lib\n"
                         for stage, oid in enumerate((base, ours, theirs), start=1))
        subprocess.run(["git", "-C", str(self.repo), "update-index", "--index-info"],
                       input=stages, text=True, capture_output=True, check=True,
                       env=g._demo_env())
        status = self.status()
        module = status.submodules[0]
        self.assertTrue(module.conflicted)
        self.assertEqual({stage for mode, oid, stage in module.index_entries}, {1, 2, 3})
        self.assertIsNone(module.index_oid)
        self.assertGreater(status.conflicts, 0)
        ui = self.ui(status)
        ui.dispatch_action("C")
        self.assertEqual(ui.message, g.t("commit_conflicts"))
        self.assertIn("Index stages", "\n".join(g.submodule_lines(status)))
        self.assertTrue(g._valid_remote_payload(g.make_snapshot(self.root, [status])))

    def test_removed_module_is_still_known_from_parent_head(self):
        self.add_module()
        g._dgit(self.repo, "rm", "-q", "-f", "--", "deps/lib")
        status = self.status()
        module = status.submodules[0]
        self.assertTrue(module.head_oid)
        self.assertIsNone(module.index_oid)
        self.assertEqual(module.worktree_kind, "missing")
        entry = next(entry for entry in status.files if entry.path == module.path)
        self.assertTrue(g.committable_against_head(entry, module))
        self.assertIn("working directory missing", "\n".join(g.submodule_lines(status)))

    def test_module_path_with_crlf_keeps_its_exact_name(self):
        source, _ = g._demo_repo(self.root, "raw-source")
        path = "deps/lib\r\n"
        g._dgit(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                "--name", "raw-module", str(source), path)
        g._dgit(self.repo, "commit", "-qam", "add raw module")
        status = self.status()
        self.assertEqual(status.submodules_error, "")
        self.assertEqual(status.submodules[0].path, path)
        self.assertTrue(status.submodules[0].initialized)
        self.assertNotIn("\r", "\n".join(g.submodule_lines(status)))
        self.assertIn("\\r\\n", "\n".join(g.submodule_lines(status)))
        self.assertTrue(g._valid_remote_payload(g.make_snapshot(self.root, [status])))

    def test_custom_module_update_command_is_never_run(self):
        self.add_module()
        marker = self.root / "unexpected-update"
        g._dgit(self.repo, "config", "submodule.deps/lib.update", "!touch " + str(marker))
        before = g.read_index_entries(self.repo, 10)
        status = self.status()
        g.submodule_lines(status)
        self.assertFalse(marker.exists())
        self.assertEqual(before, g.read_index_entries(self.repo, 10))

    def test_commit_wizard_disables_module_contents_beside_regular_files(self):
        self.add_module()
        (self.module / "draft.txt").write_text("draft\n")
        (self.repo / "README.md").write_text("parent work\n")
        status = self.status()
        ui = self.ui(status, keys=[27])
        drawn = []
        ui.scr.addstr.side_effect = lambda y, x, text, *args: drawn.append(text)
        before = g.read_index_entries(self.repo, 10)
        with patch.object(g.curses, "color_pair", return_value=0):
            ui.dispatch_action("C")
        self.assertTrue(any("module contents" in text for text in drawn))
        self.assertTrue(any("1/2" in text for text in drawn))
        self.assertEqual(before, g.read_index_entries(self.repo, 10))

    def test_local_measurement_precedes_fetch_completion(self):
        clock = ["2020-01-01T00:00:00+00:00"]
        original = g.fetch_remote_safely
        def fetched(*args):
            result = original(*args)
            clock[0] = "2020-01-01T00:05:00+00:00"
            return result
        with patch.object(g, "utc_now", side_effect=lambda: clock[0]), \
                patch.object(g, "fetch_remote_safely", side_effect=fetched):
            status = g.collect_status(self.repo, self.root, self.cfg, fetch=True)
        self.assertEqual(status.measured_at, "2020-01-01T00:00:00+00:00")
        self.assertEqual(status.remotes[0].fetched_at, "2020-01-01T00:05:00+00:00")


if __name__ == "__main__":
    unittest.main()
