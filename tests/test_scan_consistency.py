"""Ein Scan darf keine erfolgreichen Zustände verschiedener Ref-Messungen mischen."""

import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gitmaster_flash as g


class ScanConsistencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gmf-consistency-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = copy.deepcopy(g.DEFAULT_CONFIG)
        environment = patch.dict(os.environ, g._demo_env(), clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        language = patch.object(g, "UI_LANG", "en")
        language.start()
        self.addCleanup(language.stop)
        self.repo, self.bare = g._demo_repo(self.root, "project")
        g._demo_commit(self.repo, "second.txt", "second\n", "second commit")
        g._dgit(self.repo, "push", "-q", "origin", "main")

    def status(self):
        return g.collect_status(self.repo, self.root, self.cfg)

    def assert_rejected_head_change(self, change):
        before = g.status_dict(self.status())
        inventory = g.read_branch_inventory

        def changed(repo, cfg):
            change()
            return inventory(repo, cfg)

        with patch.object(g, "read_branch_inventory", side_effect=changed):
            status = self.status()
        self.assertEqual(status.head_oid, "")
        self.assertTrue(status.error)
        self.assertTrue(status.branches_error)
        self.assertFalse(status.branches_scanned)
        self.assertEqual(status.branches, [])
        self.assertFalse(status.sync_clean)
        self.assertEqual(g.compact_mark(status)[0], "✘")
        payload = {"version": g.__version__, "repos": [g.status_dict(status)]}
        self.assertTrue(g._valid_remote_payload(payload))
        findings = g.diff_findings(
            {"version": g.__version__, "repos": [before]}, payload, "here", "there"
        )
        self.assertFalse(any(row["type"] == "head" for row in findings), findings)
        self.assertEqual(self.status().error, "")

    def unborn(self):
        self.repo = self.root / "unborn"
        self.repo.mkdir()
        g._dgit(self.repo, "init", "-q", "-b", "main")
        g._demo_harden(self.repo)

    def test_first_commit_during_scan_does_not_leave_a_proven_unborn_head(self):
        self.unborn()
        self.assert_rejected_head_change(
            lambda: g._demo_commit(self.repo, "first.txt", "first\n", "first commit")
        )

    def test_unborn_branch_switch_is_detected_even_without_any_commit(self):
        self.unborn()
        self.assert_rejected_head_change(
            lambda: g._dgit(self.repo, "symbolic-ref", "HEAD", "refs/heads/other")
        )

    def test_detached_to_branch_at_same_oid_is_detected(self):
        g._dgit(self.repo, "checkout", "-q", "--detach")
        self.assert_rejected_head_change(lambda: g._dgit(self.repo, "switch", "-q", "main"))

    def test_detached_oid_change_is_detected(self):
        g._dgit(self.repo, "checkout", "-q", "--detach")
        self.assert_rejected_head_change(
            lambda: g._dgit(self.repo, "checkout", "-q", "--detach", "HEAD^")
        )

    def test_branch_switch_at_same_oid_is_detected(self):
        g._dgit(self.repo, "branch", "other")
        self.assert_rejected_head_change(lambda: g._dgit(self.repo, "switch", "-q", "other"))

    def test_commit_removed_during_scan_does_not_keep_old_identity(self):
        self.assert_rejected_head_change(
            lambda: g._dgit(self.repo, "update-ref", "-d", "refs/heads/main")
        )

    def test_remote_ref_change_between_counters_and_inventory_invalidates_both(self):
        inventory = g.read_branch_inventory
        g._dgit(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD^")

        def changed(repo, cfg):
            g._dgit(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            return inventory(repo, cfg)

        with patch.object(g, "read_branch_inventory", side_effect=changed):
            status = self.status()
        self.assertTrue(status.error)
        self.assertTrue(status.branches_error)
        self.assertEqual(status.branches, [])
        self.assertFalse(status.branches_scanned)
        self.assertFalse(status.clean_and_synced)
        self.assertEqual(g.compact_mark(status)[0], "✘")
        self.assertFalse(g.status_dict(status)["sync_clean"])
        fresh = self.status()
        self.assertEqual(fresh.error, "")
        self.assertEqual((fresh.ahead, fresh.behind), (0, 0))
        self.assertEqual(status.ahead, 1)
        for scanned in (status, g.status_dict(status)):
            self.assertFalse(g.repo_matches_view(scanned, "outgoing"))
            self.assertTrue(g.repo_matches_view(scanned, "access"))
        raced_payload = {"version": g.__version__, "repos": [g.status_dict(status)]}
        fresh_payload = {"version": g.__version__, "repos": [g.status_dict(fresh)]}
        for here, there in ((raced_payload, fresh_payload), (fresh_payload, raced_payload)):
            findings = g.diff_findings(here, there, "here", "there")
            kinds = {finding["type"] for finding in findings}
            self.assertIn("branch_inventory_failed", kinds)
            self.assertTrue(kinds.isdisjoint({"remote_state", "remote_branch", "sync_even"}))

    def test_remote_ref_change_after_inventory_is_detected(self):
        upstream = g.upstream_delta

        def changed(repo, *args):
            result = upstream(repo, *args)
            g._dgit(repo, "update-ref", "refs/remotes/origin/main", "HEAD^")
            return result

        with patch.object(g, "upstream_delta", side_effect=changed):
            status = self.status()
        self.assertTrue(status.error)
        self.assertFalse(status.clean_and_synced)
        self.assertEqual(status.branches, [])

    def test_final_ref_error_preserves_the_primary_measurement_error(self):
        def changed(repo, *args, **kwargs):
            g._dgit(repo, "update-ref", "refs/remotes/origin/main", "HEAD^")
            raise g.GitReadError("primary measurement error")

        with patch.object(g, "collect_remote_statuses", side_effect=changed):
            status = self.status()
        self.assertEqual(status.error, "primary measurement error")
        self.assertIn(g.t("scan_refs_changed"), status.error_long)
        self.assertEqual(status.branches_error, g.t("scan_refs_changed"))
        self.assertEqual(status.head_oid, "")
        self.assertFalse(status.branches_scanned)

    def test_programming_error_is_not_reported_as_a_broken_repository(self):
        with patch.object(g, "collect_remote_statuses", side_effect=TypeError("unexpected value")):
            with self.assertRaisesRegex(TypeError, "unexpected value"):
                self.status()

    def test_successful_own_fetch_can_update_refs_without_invalidating_scan(self):
        peer = self.root / "peer"
        g._dgit(self.root, "clone", "-q", "-b", "main", str(self.bare), str(peer))
        g._demo_harden(peer)
        g._demo_commit(peer, "incoming.txt", "incoming\n", "incoming commit")
        g._dgit(peer, "push", "-q", "origin", "main")
        status = g.collect_status(self.repo, self.root, self.cfg, fetch=True)
        self.assertEqual(status.error, "")
        self.assertEqual(status.branches_error, "")
        self.assertTrue(status.branches_scanned)
        self.assertEqual((status.ahead, status.behind), (0, 1))
        self.assertTrue(status.remotes[0].fetched_at)

    def test_graph_failure_is_local_even_when_a_remote_fetch_also_failed(self):
        inventory = g.read_branch_inventory

        def changed(repo, cfg):
            g._dgit(repo, "update-ref", "refs/remotes/origin/main", "HEAD^")
            return inventory(repo, cfg)

        failed = subprocess.CompletedProcess([], 128, "", "fatal: authentication failed")
        with (
            patch.object(g, "fetch_remote_safely", return_value=failed),
            patch.object(g, "read_branch_inventory", side_effect=changed),
        ):
            status = g.collect_status(self.repo, self.root, self.cfg, fetch=True)
        self.assertTrue(status.remotes[0].fetch_failed)
        self.assertFalse(status.fetch_error)
        self.assertTrue(status.error)
        payload = {"version": g.__version__, "repos": [g.status_dict(status)]}
        findings = g.diff_findings(payload, payload, "here", "there")
        self.assertTrue(any(row["type"] == "branch_inventory_failed" for row in findings))

    def test_timeout_in_final_head_check_is_a_visible_scan_error(self):
        head = g.current_head
        inventory = g.read_branch_inventory
        finished = False

        def collected(*args):
            nonlocal finished
            result = inventory(*args)
            finished = True
            return result

        def checked(*args):
            if finished:
                raise subprocess.TimeoutExpired("git rev-parse HEAD", 0.01)
            return head(*args)

        with (
            patch.object(g, "read_branch_inventory", side_effect=collected),
            patch.object(g, "current_head", side_effect=checked),
        ):
            status = self.status()
        self.assertEqual(status.head_oid, "")
        self.assertTrue(status.error)
        self.assertFalse(status.clean_and_synced)


if __name__ == "__main__":
    unittest.main()
