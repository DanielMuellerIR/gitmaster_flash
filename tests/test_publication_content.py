"""Öffentliche Dateistände unabhängig von privaten Commit-Historien erkennen."""
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import gitmaster_flash as g


class PublicationContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gmf-publication-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, g._demo_env(), clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.language = patch.object(g, "UI_LANG", "en")
        self.language.start()
        self.addCleanup(self.language.stop)
        self.cfg = copy.deepcopy(g.DEFAULT_CONFIG)
        self.repo, _ = g._demo_repo(self.root, "project")
        g._demo_extra_remote(self.root, self.repo, "github",
                            url="https://github.com/example/project.git")
        tree = self.git("rev-parse", "HEAD^{tree}")
        public = self.git("commit-tree", tree, "-m", "Public snapshot")
        self.git("update-ref", "refs/remotes/github/main", public)

    def git(self, *args):
        result = g.run_git(self.repo, *args, env=g._demo_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def status(self):
        return g.collect_status(self.repo, self.root, self.cfg)

    def commit(self, path="source.txt", text="new source\n"):
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        g._demo_commit(self.repo, path, text, "Update source")
        self.git("push", "-q", "origin", "main")

    def public(self, st):
        return next(remote for remote in st.remotes if remote.public)

    def test_equal_squashed_snapshot_is_healthy_in_every_work_view_and_json(self):
        st = self.status()
        remote = self.public(st)
        self.assertGreater(remote.ahead, 0)
        self.assertGreater(remote.behind, 0)
        self.assertTrue(remote.content_equal)
        self.assertTrue(remote.history_differs)
        self.assertTrue(st.sync_clean)
        self.assertTrue(st.clean_and_synced)
        self.assertEqual(st.severity(), 4)
        self.assertEqual(g.compact_mark(st), ("✔", g.C_GREEN))
        payload = g.status_dict(st)
        for view in ("publish", "incoming", "outgoing", "access"):
            self.assertFalse(g.repo_matches_view(st, view), view)
            self.assertFalse(g.repo_matches_view(payload, view), view)
        exported = next(r for r in payload["remotes"] if r["public"])
        self.assertTrue(exported["content_equal"])
        self.assertTrue(exported["history_differs"])
        self.assertEqual(exported["ahead"], remote.ahead)
        self.assertEqual(exported["behind"], remote.behind)
        output = io.StringIO()
        with redirect_stdout(output):
            g.print_list([st], color="always")
        self.assertIn("\033[32mGitHub ✓", output.getvalue())
        self.assertIn("\033[90mHistory ≠", output.getvalue())
        self.assertNotIn("publication pending", output.getvalue())
        self.assertNotIn("\033[31m", output.getvalue())
        with patch.object(g, "load_config", return_value=self.cfg), patch.object(
                g, "collect_all", return_value=[st]), redirect_stdout(io.StringIO()):
            self.assertEqual(g.main([str(self.root), "--json"]), 0)

    def test_new_source_change_after_squash_needs_publication(self):
        self.commit()
        st = self.status()
        self.assertFalse(self.public(st).content_equal)
        self.assertTrue(st.publication_pending)
        self.assertFalse(st.clean_and_synced)
        self.assertEqual(g.compact_mark(st)[0], "G")
        self.assertIn("GitHub ≠", self.public(st).badge())
        for view in ("incoming", "outgoing"):
            self.assertFalse(g.repo_matches_view(st, view))
            self.assertFalse(g.repo_matches_view(g.status_dict(st), view))

    def test_exclusions_are_explicit_scoped_and_literal(self):
        self.commit("review[1].txt", "internal\n")
        self.assertFalse(self.public(self.status()).content_equal)
        self.cfg["publication_excludes"] = {"EXAMPLE/project": ["review[1].txt"]}
        st = self.status()
        self.assertTrue(self.public(st).content_equal)
        self.assertEqual(self.public(st).content_excludes, ["review[1].txt"])
        self.commit("review1.txt", "public\n")
        self.assertFalse(self.public(self.status()).content_equal)
        self.cfg["publication_excludes"] = {"example/another": ["review[1].txt", "review1.txt"]}
        self.assertFalse(self.public(self.status()).content_equal)

    def test_directory_exclusion_does_not_hide_source_changes(self):
        self.commit("internal/review.txt", "private\n")
        self.cfg["publication_excludes"] = {"example/project": ["internal"]}
        self.assertTrue(self.public(self.status()).content_equal)
        self.commit()
        self.assertFalse(self.public(self.status()).content_equal)

    def test_file_modes_and_replace_refs_cannot_fake_equal_content(self):
        self.git("update-index", "--chmod=+x", "README.md")
        self.git("commit", "-qm", "Executable readme")
        self.git("push", "-q", "origin", "main")
        self.assertFalse(self.public(self.status()).content_equal)
        before = self.git("rev-parse", "HEAD~1")
        self.git("replace", "HEAD", before)
        self.assertFalse(self.public(self.status()).content_equal)

    def test_external_diff_and_textconv_never_run(self):
        marker = self.root / "unexpected-driver"
        driver = self.root / "driver.sh"
        driver.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        driver.chmod(0o755)
        self.git("config", "diff.external", str(driver))
        self.git("config", "diff.untrusted.textconv", str(driver))
        self.commit(".gitattributes", "*.txt diff=untrusted\n")
        self.commit()
        self.assertFalse(self.public(self.status()).content_equal)
        self.assertFalse(marker.exists())

    def test_read_error_and_stale_fetch_never_show_green(self):
        run_git = g.run_git

        def fail_diff(repo, *args, **kwargs):
            if args[:2] == ("diff", "--quiet"):
                return g.subprocess.CompletedProcess(args, 128, "", "cannot read tree")
            return run_git(repo, *args, **kwargs)

        with patch.object(g, "run_git", side_effect=fail_diff):
            st = self.status()
        remote = self.public(st)
        self.assertIsNone(remote.content_equal)
        self.assertTrue(remote.content_error)
        self.assertTrue(g.repo_matches_view(st, "access"))
        self.assertNotIn("GitHub ✓", remote.badge())
        healthy = self.status()
        self.public(healthy).fetch_failed = True
        self.assertFalse(healthy.clean_and_synced)
        self.assertNotIn("GitHub ✓", self.public(healthy).badge())

    def test_private_sync_divergence_remains_actionable(self):
        public_oid = self.git("rev-parse", "refs/remotes/github/main")
        self.git("update-ref", "refs/remotes/origin/main", public_oid)
        st = self.status()
        self.assertTrue(self.public(st).content_equal)
        self.assertFalse(st.sync_clean)
        self.assertEqual(g.compact_mark(st)[0], "⇅")
        self.assertTrue(g.repo_matches_view(st, "incoming"))
        self.assertTrue(g.repo_matches_view(st, "outgoing"))

    def test_github_used_as_sync_remote_also_has_neutral_history(self):
        self.cfg["sync_remote_names"] = ["github"]
        st = self.status()
        self.assertTrue(st.sync_clean)
        self.assertTrue(st.clean_and_synced)
        self.assertEqual(st.severity(), 4)
        self.assertEqual(g.compact_mark(st)[0], "✔")
        for view in ("incoming", "outgoing", "publish"):
            self.assertFalse(g.repo_matches_view(st, view))
            self.assertFalse(g.repo_matches_view(g.status_dict(st), view))

    def test_selected_row_keeps_content_and_history_colors_separate(self):
        screen = Mock()
        screen.getmaxyx.return_value = (24, 120)
        ui = g.TUI(screen, self.root, self.cfg, None)
        st = self.status()
        self.assertIn(("GitHub ✓", g.C_GREEN), g.selected_repo_context_parts(st, 120))
        self.assertIn(("History ≠", g.C_HISTORY), g.selected_repo_context_parts(st, 120))
        context = g.selected_repo_context(st, 63)
        self.assertIn("GitHub ✓", context)
        self.assertIn("History ≠", context)
        self.assertLessEqual(g.cell_width(context), 63)
        for selected in (False, True):
            with patch.object(g, "safe_addstr") as draw, patch.object(
                    g.curses, "color_pair", side_effect=lambda pair: pair << 8):
                ui.draw_repo_line(2, st, selected)
            parts = {call.args[3]: call.args[4] for call in draw.call_args_list}
            with patch.object(g.curses, "color_pair", side_effect=lambda pair: pair << 8):
                self.assertEqual(parts["GitHub ✓"], g.color_attr(g.C_GREEN, selected))
                self.assertEqual(parts["History ≠"], g.color_attr(g.C_HISTORY, selected))

    def test_invalid_config_and_snapshot_types_are_rejected(self):
        for paths in (["."], ["../source"], ["/source"], ["source/"], [None]):
            self.assertFalse(g.valid_publication_excludes({"example/project": paths}))
        payload = {"version": g.__version__, "repos": [g.status_dict(self.status())]}
        self.assertTrue(g._valid_remote_payload(payload))
        for key, value in (("content_equal", "yes"), ("history_differs", 1),
                           ("content_excludes", "all"), ("content_error", [])):
            invalid = copy.deepcopy(payload)
            invalid["repos"][0]["remotes"][-1][key] = value
            self.assertFalse(g._valid_remote_payload(invalid), key)

    def test_invalid_config_keeps_the_file_and_compares_all_paths(self):
        config = self.root / "config.json"
        original = json.dumps({"publication_excludes": {"example/project": ["."]}})
        config.write_text(original)
        error = io.StringIO()
        with patch.object(g, "CONFIG_PATH", config), redirect_stderr(error):
            loaded = g.load_config()
        self.assertEqual(loaded["publication_excludes"], {})
        self.assertIn("publication_excludes", error.getvalue())
        self.assertEqual(config.read_text(), original)

    def test_ref_change_discards_the_green_comparison(self):
        compare = g.compare_publication_content

        def move_ref(*args):
            result = compare(*args)
            self.git("update-ref", "refs/remotes/github/main", "HEAD")
            return result

        with patch.object(g, "compare_publication_content", side_effect=move_ref):
            st = self.status()
        self.assertTrue(st.error)
        self.assertIsNone(self.public(st).content_equal)
        self.assertNotIn("GitHub ✓", self.public(st).badge())

    def test_missing_branch_and_unborn_head_never_claim_equal_content(self):
        self.git("update-ref", "-d", "refs/remotes/github/main")
        st = self.status()
        self.assertIsNone(self.public(st).content_equal)
        self.assertTrue(st.publication_pending)
        self.assertNotIn("GitHub ✓", self.public(st).badge())
        self.git("update-ref", "refs/remotes/github/main", "HEAD")
        self.git("update-ref", "-d", "refs/heads/main")
        st = self.status()
        self.assertFalse(st.error)
        self.assertIsNone(self.public(st).content_equal)
        self.assertFalse(self.public(st).content_error)
        self.assertNotIn("GitHub ✓", self.public(st).badge())

    def test_ambiguous_fetch_target_is_unknown_instead_of_comparing_one_target(self):
        configs = g.read_remote_configs(self.repo, self.cfg)
        configs["github"].fetch_targets = []
        states = g.collect_remote_statuses(self.repo, "main", "origin", self.cfg, configs)
        remote = next(r for r in states if r.public)
        self.assertIsNone(remote.content_equal)
        self.assertTrue(remote.content_error)

    def test_related_history_still_has_a_direction_and_legacy_snapshots_keep_their_counts(self):
        for ahead, behind, view in ((2, 0, "outgoing"), (0, 2, "incoming")):
            remote = g.RemoteStatus("github", public=True, branch_exists=True,
                                    ahead=ahead, behind=behind, content_equal=False)
            st = g.RepoStatus(self.repo, "project", branch="main", head_oid="a"*40,
                              remotes=[remote])
            self.assertTrue(g.repo_matches_view(st, view))
        remote.ahead = remote.behind = 2
        remote.content_equal = None
        self.assertTrue(g.repo_matches_view(st, "incoming"))
        self.assertTrue(g.repo_matches_view(st, "outgoing"))
