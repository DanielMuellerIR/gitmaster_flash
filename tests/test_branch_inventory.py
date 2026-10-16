"""Nebenbranches: tatsächliche Commit-Identität und Abdeckung durch bekannte Refs."""

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import gitmaster_flash as gmf


class BranchInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gmf-branches-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = copy.deepcopy(gmf.DEFAULT_CONFIG)
        self.env = patch.dict(os.environ, gmf._demo_env(), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        lang = patch.object(gmf, "UI_LANG", "en")
        lang.start()
        self.addCleanup(lang.stop)
        gmf.resume_git_calls()
        self.repo, self.bare = gmf._demo_repo(self.root, "project")

    def topic(self):
        gmf._dgit(self.repo, "switch", "-q", "-c", "topic")
        gmf._demo_commit(self.repo, "topic.txt", "topic\n", "topic work")
        gmf._dgit(self.repo, "switch", "-q", "main")

    def status(self):
        return gmf.collect_status(self.repo, self.root, self.cfg)

    def payload(self, status):
        return {"version": gmf.__version__, "repos": [gmf.status_dict(status)]}

    def test_uncovered_noncurrent_branch_needs_attention(self):
        before = self.payload(self.status())
        self.topic()
        st = self.status()
        branches = {b.name: b for b in st.branches}
        self.assertEqual(branches["topic"].coverage, "uncovered")
        self.assertEqual(branches["topic"].uncovered_commits, 1)
        self.assertEqual(branches["topic"].upstream, "")
        self.assertEqual(st.branch_review_count, 1)
        self.assertFalse(st.clean_and_synced)
        self.assertEqual(gmf.compact_mark(st)[0], "B1")
        data = self.payload(st)
        self.assertEqual(data["repos"][0]["branch_coverage_basis"], "local-remote-tracking-refs")
        topic = next(b for b in data["repos"][0]["branches"] if b["name"] == "topic")
        self.assertRegex(topic["oid"], r"^[0-9a-f]{40}$")
        self.assertTrue(any("topic" in line and "only" in line for line in
                            gmf.diff_status(before, data, "here", "there")))
        output = io.StringIO()
        with redirect_stdout(output):
            gmf.print_list([st], self.root)
        self.assertIn("Other branches to review: 1", output.getvalue())

    def test_no_upstream_is_not_uncovered_if_another_remote_contains_it(self):
        self.topic()
        gmf._dgit(self.repo, "switch", "-q", "topic")
        gmf._demo_extra_remote(self.root, self.repo, "backup", branch="topic")
        gmf._dgit(self.repo, "switch", "-q", "main")
        st = self.status()
        topic = next(b for b in st.branches if b.name == "topic")
        self.assertEqual(topic.upstream, "")
        self.assertEqual((topic.coverage, topic.uncovered_commits), ("covered", 0))
        self.assertTrue(st.clean_and_synced)

    def test_branch_commit_difference_is_detected_without_duplicate_head_line(self):
        self.topic()
        before = self.payload(self.status())
        gmf._dgit(self.repo, "switch", "-q", "topic")
        gmf._demo_commit(self.repo, "topic.txt", "new topic\n", "more topic work")
        gmf._dgit(self.repo, "switch", "-q", "main")
        lines = gmf.diff_status(before, self.payload(self.status()), "here", "there")
        self.assertEqual(sum("commit on 'topic' differs" in line for line in lines), 1, lines)
        gmf._demo_commit(self.repo, "main.txt", "main\n", "main work")
        lines = gmf.diff_status(before, self.payload(self.status()), "here", "there")
        self.assertEqual(sum("commit on 'main' differs" in line for line in lines), 1, lines)

    def test_missing_old_inventory_does_not_invent_missing_branches(self):
        self.topic()
        new = self.payload(self.status())
        old = copy.deepcopy(new)
        old["repos"][0].pop("branches")
        self.assertEqual(gmf.diff_status(old, new, "here", "there"), [])

    def test_replace_refs_cannot_hide_uncovered_commits(self):
        self.topic()
        oid = next(b.oid for b in gmf.read_branches(self.repo, self.cfg) if b.name == "topic")
        parent = gmf.current_head(self.repo, 10)
        gmf._dgit(self.repo, "replace", oid, parent)
        topic = next(b for b in self.status().branches if b.name == "topic")
        self.assertEqual(topic.uncovered_commits, 1)

    def test_remote_symref_to_a_local_branch_is_not_coverage(self):
        self.topic()
        gmf._dgit(self.repo, "symbolic-ref", "refs/remotes/other/topic", "refs/heads/topic")
        topic = next(b for b in self.status().branches if b.name == "topic")
        self.assertEqual(topic.coverage, "uncovered")

    def test_shallow_positive_remainder_is_unknown(self):
        self.topic()
        oid = next(b.oid for b in gmf.read_branches(self.repo, self.cfg) if b.name == "topic")
        gmf._git_path(self.repo, "shallow", 10).write_text(oid + "\n")
        st = self.status()
        topic = next(b for b in st.branches if b.name == "topic")
        self.assertEqual(topic.coverage, "unknown")
        self.assertIsNone(topic.uncovered_commits)
        self.assertEqual(st.branch_review_count, 1)

    def test_inventory_failure_is_visible_and_not_an_empty_success(self):
        with patch.object(gmf, "read_branch_inventory", side_effect=gmf.GitReadError("unreadable refs")):
            st = self.status()
        self.assertIn("unreadable refs", st.branches_error)
        self.assertFalse(st.clean_and_synced)
        self.assertEqual(gmf.compact_mark(st)[0], "✘")
        self.assertIn("unreadable refs", "\n".join(gmf.repo_info_lines(st, self.cfg)))

    def test_unborn_and_detached_repository_inventory(self):
        gmf._dgit(self.repo, "checkout", "--detach", "-q")
        st = self.status()
        self.assertEqual(st.branch, "(detached)")
        self.assertEqual({b.name for b in st.branches}, {"main"})
        unborn = self.root / "unborn"
        unborn.mkdir()
        gmf._dgit(unborn, "init", "-q", "-b", "main")
        st = gmf.collect_status(unborn, self.root, self.cfg)
        self.assertEqual(st.branches, [])
        self.assertEqual(st.branches_error, "")

    def test_branch_named_like_tag_keeps_full_identity(self):
        self.topic()
        gmf._dgit(self.repo, "tag", "topic")
        names = {b.name for b in self.status().branches}
        self.assertIn("topic", names)
        self.assertNotIn("heads/topic", names)

    def test_malformed_inventory_payload_is_rejected(self):
        payload = self.payload(self.status())
        payload["repos"][0]["branches"][0]["oid"] = "short"
        self.assertFalse(gmf._valid_remote_payload(payload))

    def test_ref_change_during_counting_is_not_reported_as_a_complete_inventory(self):
        self.topic()
        real = gmf.run_git
        changed = False
        def race(repo, *args, **kwargs):
            nonlocal changed
            result = real(repo, *args, **kwargs)
            if not changed and args[:2] == ("rev-list", "--count") and gmf.is_object_id(args[2]):
                changed = True
                gmf._dgit(self.repo, "branch", "raced")
            return result
        with patch.object(gmf, "run_git", side_effect=race):
            st = self.status()
        self.assertIn("refs changed", st.branches_error)
        self.assertEqual(st.branches, [])

    def test_checkout_to_same_oid_during_inventory_is_detected(self):
        gmf._dgit(self.repo, "branch", "other")
        real = gmf.read_ref_snapshot
        reads = 0
        def race(*args, **kwargs):
            nonlocal reads
            result = real(*args, **kwargs)
            reads += 1
            if reads == 2:
                gmf._dgit(self.repo, "switch", "-q", "other")
            return result
        with patch.object(gmf, "read_ref_snapshot", side_effect=race):
            st = self.status()
        self.assertIn("HEAD changed", st.branches_error)

    def test_non_utf8_and_unicode_separator_branch_names_remain_distinct(self):
        oid = gmf.current_head(self.repo, 10).encode("ascii")
        names = ["line\u2028break".encode(), b"we\xffird"]
        packed = gmf._git_path(self.repo, "packed-refs", 10)
        packed.write_bytes(b"# pack-refs with: sorted\n" + b"".join(
            oid + b" refs/heads/" + name + b"\n" for name in names))
        payload = self.payload(self.status())
        self.assertTrue(gmf._valid_remote_payload(payload))
        text = json.dumps(payload, ensure_ascii=False)
        text.encode("utf-8")
        self.assertEqual(len(payload["repos"][0]["branches"]), 3)

    def test_shared_uncovered_branch_is_reported_as_pending_sync(self):
        self.topic()
        payload = self.payload(self.status())
        lines = gmf.diff_status(payload, payload, "here", "there")
        self.assertTrue(any("SYNC" in line and "topic" in line for line in lines), lines)

    def test_sha256_branch_ids_remain_complete(self):
        repo = self.root / "sha256"
        repo.mkdir()
        result = gmf.run_git(repo, "init", "-q", "-b", "main", "--object-format=sha256")
        if result.returncode:
            self.skipTest("Git does not support SHA-256 repositories")
        gmf._demo_harden(repo)
        gmf._demo_commit(repo, "file.txt", "content\n", "first")
        status = gmf.collect_status(repo, self.root, self.cfg)
        payload = self.payload(status)
        self.assertEqual(len(payload["repos"][0]["branches"][0]["oid"]), 64)
        self.assertTrue(gmf._valid_remote_payload(payload))

    def test_inventory_error_does_not_invent_missing_branches(self):
        good = self.payload(self.status())
        with patch.object(gmf, "read_branch_inventory", side_effect=gmf.GitReadError("unreadable refs")):
            bad = self.payload(self.status())
        lines = gmf.diff_status(good, bad, "here", "there")
        self.assertTrue(any("inventory unavailable" in line for line in lines), lines)
        self.assertFalse(any("local branch" in line and "only" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
