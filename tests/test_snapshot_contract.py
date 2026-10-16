"""Stabile Befunde und begrenzte Offline-Vergleiche gespeicherter Messungen."""

import copy
import io
import json
import os
import pty
import select
import signal
import time
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import gitmaster_flash as g


class SnapshotContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gmf-snapshot-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        language = patch.object(g, "UI_LANG", "en")
        language.start()
        self.addCleanup(language.stop)

    def status(self, name="project", **kwargs):
        return g.RepoStatus(self.root / name, name, branch="main", head_oid="a" * 40,
                            measured_at="2020-01-01T00:00:00+00:00", **kwargs)

    def snapshot(self, statuses=None):
        return g.make_snapshot(self.root, [self.status()] if statuses is None else statuses)

    def file(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        return path

    def compare(self, here, there, *options):
        left, right = self.file("here.json", here), self.file("there.json", there)
        output, error = io.StringIO(), io.StringIO()
        with patch.object(g, "load_config", side_effect=AssertionError("config read")), \
                patch.object(g, "collect_all", side_effect=AssertionError("scan")), \
                patch.object(g, "fetch_remote_status", side_effect=AssertionError("SSH")), \
                redirect_stdout(output), redirect_stderr(error):
            code = g.main(["/nonexistent-root", "--compare", str(left), str(right),
                           "--lang", "en", *options])
        return code, output.getvalue(), error.getvalue()

    def test_findings_are_language_independent_and_keep_raw_unborn_identity(self):
        left = self.snapshot()
        right = copy.deepcopy(left)
        right["repos"][0]["head_oid"] = None
        right["repos"][0]["operation"] = "rebase"
        results = []
        messages = []
        for language in ("en", "de"):
            with patch.object(g, "UI_LANG", language):
                findings = g.diff_findings(left, right, "same", "same")
                messages.append([finding["message"] for finding in findings])
                results.append([{key: value for key, value in finding.items() if key != "message"}
                                for finding in findings])
                self.assertEqual(g.diff_status(left, right, "same", "same"), messages[-1])
        self.assertEqual(results[0], results[1])
        self.assertNotEqual(messages[0], messages[1])
        head = next(row for row in results[0] if row["type"] == "head")
        self.assertEqual(head["category"], "drift")
        self.assertEqual(head["branch"], "main")
        self.assertIsNone(head["values"]["there_oid"])
        operation = next(row for row in results[0] if row["type"] == "operation")
        self.assertEqual(operation["values"]["operation"], "rebase")
        self.assertEqual(operation["side"], "there")

    def test_sides_stay_distinct_even_when_human_labels_are_identical(self):
        here = self.snapshot([self.status(modified=1, fetch_error=True)])
        there = copy.deepcopy(here)
        findings = g.diff_findings(here, there, "same", "same")
        failed = [row for row in findings if row["type"] == "fetch_failed_side"]
        self.assertEqual({row["side"] for row in failed}, {"here", "there"})
        self.assertTrue(all(row["values"]["count"] == 1 for row in failed))

    def test_different_checked_out_branches_do_not_create_remote_delta_drift(self):
        here = self.snapshot([self.status(remotes=[g.RemoteStatus("origin", branch_exists=True,
                                                                ahead=1, is_sync=True)])])
        there = copy.deepcopy(here)
        there["repos"][0]["branch"] = "topic"
        there["repos"][0]["remotes"][0]["ahead"] = 7
        findings = g.diff_findings(here, there, "here", "there")
        self.assertEqual([row["type"] for row in findings], ["branch"])
        self.assertEqual(findings[0]["values"], {"here_branch": "main", "there_branch": "topic"})

    def test_security_findings_name_changed_fields_without_exporting_urls(self):
        here = self.snapshot([self.status(remotes=[g.RemoteStatus("origin", branch_exists=True,
                                                                fetch_fingerprints=["opaque-a"])])])
        there = copy.deepcopy(here)
        there["repos"][0]["remotes"][0]["fetch_fingerprints"] = ["opaque-b"]
        there["repos"][0]["remotes"][0]["url"] = "https://user:secret-token@example.invalid/repo"
        rows = g.diff_findings(here, there, "here", "there")
        finding = next(row for row in rows if row["type"] == "remote_security")
        self.assertEqual(finding["values"]["changed_fields"], ["fetch_fingerprints"])
        self.assertNotIn("secret-token", json.dumps(rows))
        self.assertNotIn("opaque-a", json.dumps(rows))
        self.assertNotIn("opaque-b", json.dumps(rows))

    def test_snapshot_schema_is_separate_from_product_version(self):
        payload = self.snapshot()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["version"], g.__version__)
        self.assertTrue(g.valid_timestamp(payload["generated_at"]))
        self.assertEqual(g.load_snapshot(self.file("snapshot.json", payload)), payload)
        for bad in (0, 2, "1", True, None):
            broken = copy.deepcopy(payload)
            broken["schema_version"] = bad
            self.assertFalse(g._valid_remote_payload(broken))

    def test_offline_comparison_reads_only_files_and_reports_measurement_metadata(self):
        payload = self.snapshot()
        other = copy.deepcopy(payload)
        other["generated_at"] = "2022-01-01T00:00:00Z"
        other["repos"][0]["measured_at"] = "2021-01-01T00:00:00Z"
        code, output, error = self.compare(payload, other, "--json", "--color", "always")
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["differences"], [])
        self.assertEqual(report["sources"]["there"]["generated_at"], "2022-01-01T00:00:00Z")
        self.assertEqual(report["sources"]["there"]["local_measurement"]["earliest"], "2021-01-01T00:00:00Z")
        self.assertNotIn("\x1b", output)

    def test_legacy_object_snapshots_are_accepted_with_explicit_limits(self):
        payload = {"version": "0.23.0", "repos": [{"rel": "project", "branch": "main"}]}
        code, output, error = self.compare(payload, payload, "--json")
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(report["findings"], [])
        self.assertEqual({row["code"] for row in report["limitations"]}, {
            "legacy_schema", "head_identity_unknown", "branch_inventory_unknown",
            "local_measurement_unknown"})
        code, output, error = self.compare(payload, payload)
        self.assertEqual(code, 0, error)
        self.assertIn("legacy JSON", output)
        self.assertIn("unknown", output)

    def test_saved_work_views_are_rejected_before_false_missing_findings(self):
        st = self.status(modified=1)
        payload = g.make_snapshot(self.root, [st], all_statuses=[st], view="changes")
        code, output, error = self.compare(payload, self.snapshot(), "--json")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("omit a counterpart", error)

    def test_saved_path_filters_must_have_the_same_effect(self):
        payload = self.snapshot()
        payload["filter"] = {"query": "api work", "matches": 1, "total": 5}
        other = copy.deepcopy(payload)
        other["filter"]["query"] = "WORK API api"
        code, output, error = self.compare(payload, other, "--json")
        self.assertEqual(code, 0, error)
        other["filter"]["query"] = "other"
        code, output, error = self.compare(payload, other, "--json")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("filters differ", error)

    def test_comparison_view_retains_an_unchanged_counterpart(self):
        here = self.snapshot([self.status(modified=1)])
        there = self.snapshot()
        code, output, error = self.compare(here, there, "--json", "--view", "changes")
        self.assertEqual(code, 1, error)
        data = json.loads(output)
        self.assertEqual(data["filter"]["selection"], "either-side")
        self.assertEqual(data["filter"]["host_matches"], 1)
        self.assertEqual(data["filter"]["host_view_matches"], 0)
        self.assertEqual([row["type"] for row in data["findings"]], ["dirty"])

    def test_future_schema_malformed_json_and_duplicate_keys_are_rejected(self):
        for text in ('{"version":"x","version":"y","repos":[]}',
                     '{"version":"x","repos":[],"extra":NaN}',
                     '{"version":"x","repos":[],"extra":Infinity}', '{broken'):
            path = self.root / "bad.json"
            path.write_text(text)
            with self.assertRaises(ValueError):
                g.load_snapshot(path)
        payload = self.snapshot()
        payload["schema_version"] = 9
        code, output, error = self.compare(payload, self.snapshot(), "--json")
        self.assertEqual(code, 2)
        self.assertIn("incompatible JSON", error)

    def test_negative_counts_duplicate_remotes_and_inconsistent_scopes_are_rejected(self):
        payload = self.snapshot([self.status(remotes=[g.RemoteStatus("origin")])])
        broken = copy.deepcopy(payload)
        broken["repos"][0]["ahead"] = -1
        self.assertFalse(g._valid_remote_payload(broken))
        broken = copy.deepcopy(payload)
        broken["repos"][0]["remotes"].append(copy.deepcopy(broken["repos"][0]["remotes"][0]))
        self.assertFalse(g._valid_remote_payload(broken))
        broken = copy.deepcopy(payload)
        broken["filter"] = {"query": "", "matches": 1, "total": 5}
        self.assertFalse(g._valid_remote_payload(broken))
        broken["filter"]["query"] = "api"
        broken["view"] = {"id": "all", "matches": 1, "total": 6, "hidden_attention": 0}
        self.assertFalse(g._valid_remote_payload(broken))

    def test_file_size_invalid_utf8_and_bom_are_handled(self):
        path = self.file("snapshot.json", self.snapshot())
        with patch.object(g, "MAX_SNAPSHOT_BYTES", 16):
            with self.assertRaises(ValueError):
                g.load_snapshot(path)
        path.write_bytes(b"\xff")
        with self.assertRaises(UnicodeError):
            g.load_snapshot(path)
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(self.snapshot()).encode())
        self.assertTrue(g._valid_remote_payload(g.load_snapshot(path)))

    def test_ssh_boundary_rejects_duplicate_keys_too(self):
        result = subprocess.CompletedProcess([], 0, '{"version":"a","version":"b","repos":[]}', "")
        with patch.object(g, "_run_process_group", return_value=result):
            with self.assertRaises(RuntimeError):
                g.fetch_remote_status("example", "~/projects", fetch=False)

    def test_source_times_sort_by_instant_and_metadata_drops_unknown_fields(self):
        st = self.status()
        payload = self.snapshot([st, self.status("other")])
        payload["repos"][0]["measured_at"] = "2020-01-01T01:00:00+02:00"
        payload["repos"][1]["measured_at"] = "2020-01-01T00:00:00Z"
        payload["filter"] = {"query": "api", "matches": 2, "total": 5, "password": "secret-token"}
        metadata = g.snapshot_metadata(payload)
        self.assertEqual(metadata["local_measurement"]["earliest"], "2020-01-01T01:00:00+02:00")
        self.assertNotIn("secret-token", json.dumps(metadata))

    def test_empty_snapshots_do_not_claim_checked_repositories(self):
        code, output, error = self.compare(self.snapshot([]), self.snapshot([]))
        self.assertEqual(code, 0, error)
        self.assertIn("nothing was compared", output)

    def test_compare_rejects_network_options_before_reading_or_creating_config(self):
        output = io.StringIO()
        with patch.object(g, "load_config", side_effect=AssertionError("config")), redirect_stderr(output):
            with self.assertRaises(SystemExit) as stopped:
                g.main(["--compare", "one.json", "two.json", "--fetch", "--lang", "de"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("nicht", output.getvalue())

    def test_color_policy_is_explicit_and_json_stays_plain(self):
        for policy, terminal, no_color, expected in (
                ("auto", False, False, False), ("auto", True, False, True),
                ("auto", True, True, False), ("always", False, True, True),
                ("never", True, False, False)):
            output = io.StringIO()
            output.isatty = lambda: terminal
            environment = {"NO_COLOR": ""} if no_color else {}
            with patch.dict(os.environ, environment, clear=True), redirect_stdout(output):
                g.print_list([self.status(modified=1)], color=policy)
            self.assertEqual("\x1b[" in output.getvalue(), expected, policy)

    def test_untrusted_unicode_and_deep_json_do_not_break_output(self):
        here, there = self.snapshot(), self.snapshot()
        here["version"] = "old\udcff\x1b[31m"
        code, output, error = self.compare(here, there, "--json")
        self.assertEqual(code, 1, error)
        output.encode("utf-8")
        self.assertNotIn("\x1b", output)
        self.assertEqual(json.loads(output)["findings"][0]["type"], "version")
        with self.assertRaises(ValueError):
            g.parse_snapshot_json("[" * 3000 + "0" + "]" * 3000)

    def test_real_saved_scans_compare_with_git_and_ssh_disabled(self):
        with patch.dict(os.environ, g._demo_env(), clear=True):
            repo, _ = g._demo_repo(self.root, "real")
            cfg = copy.deepcopy(g.DEFAULT_CONFIG)
            before = g.make_snapshot(self.root, [g.collect_status(repo, self.root, cfg)])
            g._demo_ahead(repo, 1)
            after = g.make_snapshot(self.root, [g.collect_status(repo, self.root, cfg)])
        left, right = self.file("before.json", before), self.file("after.json", after)
        binaries, home = self.root / "bin", self.root / "home"
        binaries.mkdir()
        home.mkdir()
        marker = self.root / "unexpected-command"
        for name in ("git", "ssh"):
            executable = binaries / name
            executable.write_text("#!/bin/sh\nprintf called > " + shlex.quote(str(marker)) + "\nexit 99\n")
            executable.chmod(0o755)
        environment = dict(os.environ, PATH=str(binaries), HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, g.__file__, "--compare", str(left), str(right),
                                 "--json", "--lang", "en"], env=environment, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(any(row["type"] == "head" for row in report["findings"]))
        self.assertFalse(marker.exists())
        self.assertFalse((home / ".config").exists())
    def test_list_auto_color_uses_a_real_pty_and_stays_plain_in_a_pipe(self):
        with patch.dict(os.environ, g._demo_env(), clear=True):
            repo, _ = g._demo_repo(self.root, "terminal")
        home = self.root / "terminal-home"
        home.mkdir()
        environment = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
        environment.pop("NO_COLOR", None)
        command = [sys.executable, g.__file__, str(repo), "--list", "--lang", "en"]
        piped = subprocess.run(command, env=environment, capture_output=True, timeout=15)
        self.assertEqual(piped.returncode, 0, piped.stderr)
        self.assertNotIn(b"\x1b[", piped.stdout)
        master, slave = pty.openpty()
        proc = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        output = bytearray()
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], .1)
                if not ready:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == 5:
                        break
                    raise
                if not chunk:
                    break
                output.extend(chunk)
            self.assertEqual(proc.wait(timeout=3), 0, bytes(output))
            self.assertIn(b"\x1b[", output)
        finally:
            os.close(master)
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)



if __name__ == "__main__":
    unittest.main()
