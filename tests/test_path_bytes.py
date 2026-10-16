"""Git-Pfadbytes bleiben von Prozessausgabe bis Commit-Freigabe unverändert."""

import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import gitmaster_flash as g


class PathByteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gmf-path-bytes-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = patch.dict(os.environ, g._demo_env(), clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        g.resume_git_calls()
        self.addCleanup(g.resume_git_calls)
        self.repo, _ = g._demo_repo(self.root, "project")

    def test_cr_lf_and_crlf_are_not_translated(self):
        names = ["line\rname.txt", "line\nname.txt", "line\r\nname.txt"]
        for name in names:
            (self.repo / name).write_text("new\n")
        result = g.run_git(self.repo, "status", "--porcelain=v1", "-z")
        entries = g.parse_porcelain(result.stdout)[4]
        self.assertEqual({entry.path for entry in entries}, set(names))
        status = g.collect_status(self.repo, self.root, copy.deepcopy(g.DEFAULT_CONFIG))
        self.assertEqual({entry.path for entry in status.files}, set(names))

    def test_process_output_preserves_non_utf8_bytes(self):
        result = g._run_process_group(
            [sys.executable, "-c", "import os; os.write(1, b'path\\xff\\0')"],
            stdin=subprocess.DEVNULL, timeout=5)
        self.assertEqual(result.stdout, os.fsdecode(b"path\xff\0"))

    def test_commit_of_cr_name_never_commits_the_distinct_lf_name(self):
        cr, lf = "line\rname.txt", "line\nname.txt"
        (self.repo / cr).write_text("approved\n")
        (self.repo / lf).write_text("other work\n")
        result = g.commit_selected(self.repo, [cr], "approved CR path", 10)
        self.assertEqual(result.returncode, 0, result.stderr)
        committed = g.run_git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-z", "-r", "HEAD")
        self.assertEqual(committed.stdout.split("\0"), [cr, ""])
        entries = g.parse_porcelain(g.run_git(self.repo, "status", "--porcelain=v1", "-z").stdout)[4]
        self.assertEqual([entry.path for entry in entries], [lf])

    def test_reported_repository_path_keeps_trailing_whitespace(self):
        moved = self.root / "project \r\n"
        self.repo.rename(moved)
        reported = g._git_reported_path(moved, "--show-toplevel", timeout=10)
        self.assertEqual(reported, moved.resolve())
        self.assertEqual(g._git_path(moved, "HEAD", 10), moved / ".git" / "HEAD")

    def test_timeout_output_uses_the_same_byte_preserving_decoder(self):
        code = "import os,time; os.write(1,b'path\\r\\n\\xff\\0'); os.write(2,b'error\\r\\n'); time.sleep(30)"
        with self.assertRaises(subprocess.TimeoutExpired) as stopped:
            g._run_process_group([sys.executable, "-c", code], stdin=subprocess.DEVNULL, timeout=.2)
        self.assertEqual(stopped.exception.output, os.fsdecode(b"path\r\n\xff\0"))
        self.assertEqual(stopped.exception.stderr, "error\r\n")
        self.assertFalse(g._LIVE_PROCESSES)


if __name__ == "__main__":
    unittest.main()
