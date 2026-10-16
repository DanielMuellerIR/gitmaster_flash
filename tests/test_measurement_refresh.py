"""Messalter, gezielter Fetch und die Prozessgrenze eines abbrechbaren Scans."""

import copy
import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gitmaster_flash as gmf


class MeasurementRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gmf-measure-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = copy.deepcopy(gmf.DEFAULT_CONFIG)
        env = patch.dict(os.environ, gmf._demo_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        lang = patch.object(gmf, "UI_LANG", "en")
        lang.start()
        self.addCleanup(lang.stop)
        gmf.resume_git_calls()
        self.addCleanup(gmf.resume_git_calls)
        self.repo, self.bare = gmf._demo_repo(self.root, "project")

    def status(self, fetch=False, remote=None):
        return gmf.collect_status(self.repo, self.root, self.cfg, fetch, remote)

    def ui(self, st):
        scr = Mock()
        scr.getmaxyx.return_value = (24, 110)
        ui = gmf.TUI(scr, self.root, self.cfg, None)
        ui.all_statuses = [st]
        ui.statuses = [st]
        return ui

    def test_local_measurement_does_not_invent_fetch_time(self):
        st = self.status()
        self.assertTrue(gmf.valid_timestamp(st.measured_at))
        self.assertNotEqual(st.measured_at, "")
        self.assertEqual(st.remotes[0].fetched_at, "")
        data = gmf.status_dict(st)
        self.assertEqual(data["measured_at"], st.measured_at)
        self.assertEqual(data["remotes"][0]["fetched_at"], "")
        self.assertIn("unknown", gmf.measurement_summary(st))

    def test_fetch_time_survives_local_refresh_only_with_same_branch_and_config(self):
        old = self.status(True)
        self.assertTrue(old.remotes[0].fetched_at)
        ui = self.ui(old)
        new = ui.refresh_one(old)
        self.assertEqual(new.remotes[0].fetched_at, old.remotes[0].fetched_at)
        gmf._dgit(self.repo, "switch", "-q", "-c", "topic")
        changed_branch = ui.refresh_one(new)
        self.assertEqual(changed_branch.remotes[0].fetched_at, "")
        gmf._dgit(self.repo, "switch", "-q", "main")
        old = self.status(True)
        other = self.root / "another.git"
        gmf._dgit(self.root, "clone", "--bare", "-q", str(self.bare), str(other))
        gmf._dgit(self.repo, "remote", "set-url", "origin", str(other))
        fresh = self.status()
        gmf.carry_fetch_times(old, fresh)
        self.assertEqual(fresh.remotes[0].fetched_at, "")

    def test_targeted_fetch_updates_only_one_remote(self):
        gmf._demo_extra_remote(self.root, self.repo, "backup")
        old = self.status(True)
        calls = []
        original = gmf.fetch_remote_safely
        def tracked(repo, remote, branch, timeout):
            calls.append(remote.name)
            return original(repo, remote, branch, timeout)
        with patch.object(gmf, "fetch_remote_safely", side_effect=tracked):
            new = self.status(True, "backup")
        self.assertEqual(calls, ["backup"])
        times = {r.name: r.fetched_at for r in new.remotes}
        self.assertEqual(times["origin"], "")
        self.assertTrue(times["backup"])
        gmf.carry_fetch_times(old, new)
        self.assertEqual(new.remotes[0].fetched_at, old.remotes[0].fetched_at)

    def test_failed_fetch_preserves_last_success_but_does_not_renew_it(self):
        old = self.status(True)
        old.remotes[0].fetched_at = "2020-01-01T00:00:00+00:00"
        ui = self.ui(old)
        failed = subprocess.CompletedProcess([], 128, "", "fatal: repository not found")
        with patch.object(gmf, "fetch_remote_safely", return_value=failed):
            new = ui.refresh_one(old, fetch_remote="origin")
        self.assertTrue(new.fetch_error)
        self.assertTrue(new.remotes[0].fetch_failed)
        self.assertEqual(new.remotes[0].fetched_at, "2020-01-01T00:00:00+00:00")
        self.assertIn("last fetch failed", "\n".join(gmf.repo_info_lines(new, self.cfg)))

    def test_configuration_race_does_not_claim_successful_fetch(self):
        original = gmf.fetch_remote_safely
        def changed(repo, remote, branch, timeout):
            result = original(repo, remote, branch, timeout)
            gmf._dgit(repo, "config", "--add", "remote.origin.fetch", "+refs/tags/*:refs/tags/*")
            return result
        with patch.object(gmf, "fetch_remote_safely", side_effect=changed):
            new = self.status(True)
        self.assertTrue(new.fetch_error)
        self.assertEqual(new.remotes[0].fetched_at, "")

    def test_missing_named_remote_is_an_explicit_failure(self):
        st = self.status(True, "missing")
        self.assertTrue(st.fetch_error)
        self.assertIn("missing", st.error)
        self.assertEqual(st.remotes[0].fetched_at, "")

    def test_legacy_payload_is_accepted_and_malformed_timestamps_are_rejected(self):
        payload = {"version": gmf.__version__, "repos": [gmf.status_dict(self.status())]}
        self.assertTrue(gmf._valid_remote_payload(payload))
        repo = payload["repos"][0]
        repo.pop("measured_at")
        repo["remotes"][0].pop("fetched_at")
        self.assertTrue(gmf._valid_remote_payload(payload))
        for bad in (None, 3, [], "tomorrow", "2026-01-01T00:00:00"):
            repo["measured_at"] = bad
            self.assertFalse(gmf._valid_remote_payload(payload), repr(bad))
        repo["measured_at"] = ""
        repo["remotes"][0]["fetched_at"] = False
        self.assertFalse(gmf._valid_remote_payload(payload))

    def test_age_handles_unknown_old_and_future_measurements(self):
        self.assertEqual(gmf.measurement_age(""), "unknown")
        self.assertEqual(gmf.measurement_age(gmf.utc_now()), "<1m")
        now = datetime.datetime.now(datetime.timezone.utc)
        self.assertEqual(gmf.measurement_age((now-datetime.timedelta(hours=3)).isoformat()), "3h")
        self.assertEqual(gmf.measurement_age((now+datetime.timedelta(hours=3)).isoformat()), "clock changed")

    def test_local_refresh_wins_over_scan_and_stop_keeps_completed_results(self):
        old = self.status()
        ui = self.ui(old)
        scan = Mock()
        import queue
        scan.queue = queue.Queue()
        scan.done = 0
        ui.scan = scan
        gmf._demo_commit(self.repo, "new.txt", "new\n", "new work")
        ui.dispatch_action("N")
        fresh = ui.current()
        scan.queue.put(("one", old))
        ui.drain_background_scan()
        self.assertIs(ui.current(), fresh)
        ui.dispatch_action("X")
        scan.stop.assert_called_once()
        self.assertIsNone(ui.scan)
        self.assertIs(ui.current(), fresh)
        self.assertIn("completed results", ui.message)

    def test_fetch_action_selects_remote_and_refuses_overlap_with_background(self):
        ui = self.ui(self.status())
        with patch.object(ui, "choose_pull_remote", return_value="origin"), patch.object(ui, "show_busy"):
            ui.dispatch_action("U")
        self.assertTrue(ui.current().remotes[0].fetched_at)
        ui.scan = Mock()
        with patch.object(ui, "choose_pull_remote") as choose:
            ui.dispatch_action("U")
        choose.assert_not_called()

    def test_cli_named_fetch_is_scoped_and_json_has_measurements(self):
        import io
        from contextlib import redirect_stdout
        output = io.StringIO()
        gmf._demo_extra_remote(self.root, self.repo, "backup")
        with patch.object(gmf, "load_config", return_value=self.cfg), redirect_stdout(output):
            code = gmf.main([str(self.repo), "--json", "--fetch-remote", "backup"])
        self.assertEqual(code, 0)
        data = json.loads(output.getvalue())["repos"][0]
        self.assertTrue(data["measured_at"])
        times = {r["name"]:r["fetched_at"] for r in data["remotes"]}
        self.assertTrue(times["backup"])
        self.assertEqual(times["origin"], "")


class ScopedCancellationTests(unittest.TestCase):
    def setUp(self):
        gmf.resume_git_calls()
        self.addCleanup(gmf.resume_git_calls)

    def test_scan_cancellation_leaves_independent_process_and_next_scan_alive(self):
        scope = gmf.GitCallScope()
        results = {}
        def run(key, owned):
            def call():
                return gmf._run_process_group([sys.executable, "-c", "import time; time.sleep(30)"],
                                               stdin=subprocess.DEVNULL, timeout=35)
            results[key] = scope.call(call) if owned else call()
        background = threading.Thread(target=run, args=("scan", True))
        foreground = threading.Thread(target=run, args=("foreground", False))
        background.start()
        foreground.start()
        self.addCleanup(background.join, 5)
        self.addCleanup(foreground.join, 5)
        self.addCleanup(gmf.cancel_git_calls)
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            with gmf._LIVE_LOCK:
                live = list(gmf._LIVE_PROCESSES)
                owned = list(scope.processes)
            if len(live)==2 and len(owned)==1:
                break
            time.sleep(.01)
        self.assertEqual(len(live), 2)
        outside = next(proc for proc in live if proc not in owned)
        scope.cancel()
        background.join(5)
        self.assertFalse(background.is_alive())
        self.assertIsNone(outside.poll(), "unabhängige Aktion wurde beendet")
        self.assertFalse(gmf._CANCEL.is_set())
        with self.assertRaises(gmf.GitCancelled), patch.object(gmf.subprocess, "Popen") as popen:
            scope.call(gmf.run_git, Path("."), "status")
        popen.assert_not_called()
        other = gmf.GitCallScope()
        done = other.call(
            lambda: gmf._run_process_group(["/bin/echo", "ok"], stdin=subprocess.DEVNULL, timeout=5))
        self.assertEqual(done.stdout.strip(), "ok")
        gmf._kill_process_group(outside)
        foreground.join(5)
        self.assertFalse(foreground.is_alive())
        self.assertFalse(scope.processes)
        self.assertFalse(gmf._LIVE_PROCESSES)

    def test_scope_is_restored_after_failure(self):
        scope = gmf.GitCallScope()
        with self.assertRaises(ValueError):
            scope.call(lambda: (_ for _ in ()).throw(ValueError()))
        self.assertIsNone(getattr(gmf._GIT_CONTEXT, "scope", None))
