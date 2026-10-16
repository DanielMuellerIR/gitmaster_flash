"""Zustandsverträge mit echten lokalen Repositories und unveränderten Nutzerdateien."""

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import gitmaster_flash as gmf


class StateContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gmf-state-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = copy.deepcopy(gmf.DEFAULT_CONFIG)
        self.env = patch.dict(os.environ, gmf._demo_env(), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.lang = patch.object(gmf, "UI_LANG", "en")
        self.lang.start()
        self.addCleanup(self.lang.stop)
        gmf.resume_git_calls()

    def test_equal_counters_do_not_hide_different_commits(self):
        left = self.root / "left"
        right = self.root / "right"
        left.mkdir()
        right.mkdir()
        a, bare = gmf._demo_repo(left, "project")
        b = right / "project"
        gmf._dgit(right, "clone", "-q", "-b", "main", str(bare), str(b))
        gmf._demo_harden(b)
        gmf._demo_commit(a, "left.txt", "A\n", "left work")
        gmf._demo_commit(b, "right.txt", "B\n", "right work")
        statuses = [gmf.collect_status(repo, root, self.cfg)
                    for repo, root in ((a, left), (b, right))]
        self.assertEqual([(s.ahead, s.behind) for s in statuses], [(1, 0), (1, 0)])
        payloads = [{"version": gmf.__version__, "repos": [gmf.status_dict(s)]}
                    for s in statuses]
        self.assertEqual(payloads[0]["repos"][0]["head_oid"], statuses[0].head_oid)
        lines = gmf.diff_status(*payloads, "here", "there")
        self.assertTrue(any("DRIFT" in line and "commit" in line for line in lines), lines)
        self.assertFalse(any(line.startswith("SYNC") for line in lines), lines)

    def test_same_commit_and_old_payloads_do_not_create_head_drift(self):
        repo, _ = gmf._demo_repo(self.root, "project")
        payload = {"version": gmf.__version__, "repos": [
            gmf.status_dict(gmf.collect_status(repo, self.root, self.cfg))]}
        self.assertEqual(gmf.diff_status(payload, payload, "here", "there"), [])
        old = copy.deepcopy(payload)
        old["repos"][0].pop("head_oid", None)
        self.assertEqual(gmf.diff_status(payload, old, "here", "there"), [])

    def test_clean_pending_merge_needs_attention_in_every_output(self):
        repo, _ = gmf._demo_repo(self.root, "project")
        gmf._dgit(repo, "switch", "-q", "-c", "topic")
        gmf._dgit(repo, "commit", "--allow-empty", "-qm", "topic")
        gmf._dgit(repo, "switch", "-q", "main")
        gmf._dgit(repo, "merge", "--no-ff", "--no-commit", "topic")
        st = gmf.collect_status(repo, self.root, self.cfg)
        self.assertFalse(st.dirty)
        self.assertEqual(st.operation, "merge")
        self.assertFalse(st.clean_and_synced)
        self.assertNotEqual(gmf.compact_mark(st)[0], "✔")
        self.assertEqual(gmf.status_dict(st)["operation"], "merge")
        self.assertIn("Merge", "\n".join(gmf.repo_info_lines(st, self.cfg)))
        payload = {"version": gmf.__version__, "repos": [gmf.status_dict(st)]}
        self.assertTrue(any("Merge" in line for line in
                            gmf.diff_status(payload, payload, "here", "there")))

    def test_operation_reader_uses_the_worktree_git_directory(self):
        repo, _ = gmf._demo_repo(self.root, "project")
        worktree = self.root / "linked"
        gmf._dgit(repo, "worktree", "add", "-q", "-b", "linked", str(worktree))
        marker = gmf._git_path(worktree, "CHERRY_PICK_HEAD", 10)
        marker.write_text(gmf.current_head(worktree, 10) + "\n")
        self.assertEqual(gmf.repository_operation(worktree, 10), "cherry-pick")
        self.assertEqual(gmf.repository_operation(repo, 10), "")
        self.assertTrue(gmf.repository_operation_in_progress(worktree, 10))

    def test_payload_rejects_malformed_commit_and_operation(self):
        payload = {"version": "test", "repos": [{"rel": "x", "head_oid": "bad"}]}
        self.assertFalse(gmf._valid_remote_payload(payload))
        payload["repos"][0] = {"rel": "x", "operation": ["merge"]}
        self.assertFalse(gmf._valid_remote_payload(payload))
        payload["repos"][0] = {"rel": "x", "head_oid": None, "operation": ""}
        self.assertTrue(gmf._valid_remote_payload(payload))

    def test_operation_markers_share_one_rule_with_preflight(self):
        repo, _ = gmf._demo_repo(self.root, "project")
        git_dir = gmf._git_path(repo, "HEAD", 10).parent
        for marker, operation in gmf.OPERATION_MARKERS:
            with self.subTest(marker=marker):
                path = git_dir / marker
                if marker in ("rebase-merge", "rebase-apply", "sequencer"):
                    path.mkdir()
                else:
                    path.write_text("test\n")
                try:
                    self.assertEqual(gmf.repository_operation(repo, 10), operation)
                    self.assertTrue(gmf.repository_operation_in_progress(repo, 10))
                finally:
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.unlink()

    def test_unborn_and_different_checked_out_branches_are_distinguished(self):
        a = {"version": "test", "repos": [{"rel": "x", "branch": "main", "head_oid": None}]}
        b = copy.deepcopy(a)
        b["repos"][0]["head_oid"] = "1" * 40
        self.assertTrue(any("commit on 'main' differs" in line for line in
                            gmf.diff_status(a, b, "here", "there")))
        b["repos"][0]["branch"] = "other"
        lines = gmf.diff_status(a, b, "here", "there")
        self.assertFalse(any("commit on" in line for line in lines), lines)


class ConfigValueTests(unittest.TestCase):
    def test_invalid_values_fall_back_without_rewriting_the_file(self):
        cases = [("git_timeout", -1), ("git_timeout", True),
                 ("commit_timeout", 86401), ("compact_from", -1),
                 ("skip_dirs", [["nested"]]), ("skip_dirs", ["a/b"]),
                 ("sync_remote_names", [42]), ("sync_remote_hosts", [{}]),
                 ("lang", "unsupported")]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            with patch.object(gmf, "CONFIG_PATH", path):
                for key, value in cases:
                    with self.subTest(key=key, value=value):
                        content = json.dumps({key: value})
                        path.write_text(content)
                        err = io.StringIO()
                        with redirect_stderr(err):
                            cfg = gmf.load_config()
                        self.assertEqual(cfg[key], gmf.DEFAULT_CONFIG[key])
                        self.assertIn(key, err.getvalue())
                        self.assertEqual(path.read_text(), content)


if __name__ == "__main__":
    unittest.main()
