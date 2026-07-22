"""Black-Box-Tests fuer den kompletten lokalen/entfernten CLI-Vertrag.

Ein temporaeres ``ssh`` fuehrt den echten, ueber stdin uebertragenen GMF-Code lokal
aus. Damit pruefen diese Tests die Grenze, die Unit-Tests mit gemocktem subprocess
nicht sehen: Remote-JSON, Remote-Exit-Code und lokale Diff-Auswertung gemeinsam.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = str(Path(__file__).resolve().parents[1] / "gitmaster_flash.py")


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


class DiffCliBlackBoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.local_home = self.base / "local-home"
        self.remote_home = self.base / "remote-home"
        self.local_root = self.local_home / "repos"
        self.remote_root = self.remote_home / "repos"
        self.fakebin = self.base / "fakebin"
        for path in (self.local_root, self.remote_root, self.fakebin):
            path.mkdir(parents=True)

        self.remote_stdout = self.base / "remote-stdout.json"
        self.remote_rc = self.base / "remote-rc.txt"
        fake_ssh = self.fakebin / "ssh"
        fake_ssh.write_text(
            """#!/bin/sh
while [ "$#" -ge 2 ] && [ "$1" = "-o" ]; do
    shift 2
done
if [ "$#" -ne 2 ] || [ "$1" != "fakehost" ]; then
    echo "unexpected fake ssh arguments" >&2
    exit 64
fi
command=$2
case "${GMF_FAKE_SSH_MODE:-exec}" in
    exit255)
        echo "synthetic ssh failure" >&2
        exit 255
        ;;
    badjson0)
        printf '{broken'
        exit 0
        ;;
    badjson1)
        printf '{broken'
        exit 1
        ;;
    deny_git)
        export GIT_ALLOW_PROTOCOL=none
        ;;
esac
export HOME="$GMF_REMOTE_HOME"
/bin/sh -c "$command" >"$GMF_FAKE_SSH_STDOUT"
rc=$?
cat "$GMF_FAKE_SSH_STDOUT"
printf '%s\n' "$rc" >"$GMF_FAKE_SSH_RC"
exit "$rc"
"""
        )
        fake_ssh.chmod(0o755)

        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("GIT_")}
        self.env.update({
            "HOME": str(self.local_home),
            "PATH": str(self.fakebin) + os.pathsep + os.environ["PATH"],
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GMF_REMOTE_HOME": str(self.remote_home),
            "GMF_FAKE_SSH_STDOUT": str(self.remote_stdout),
            "GMF_FAKE_SSH_RC": str(self.remote_rc),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _init_repo(self, path: Path) -> None:
        path.mkdir(parents=True)
        git(path, "init", "-q", "-b", "main")
        git(path, "config", "user.email", "test@example.invalid")
        git(path, "config", "user.name", "Test")
        (path / "tracked.txt").write_text("base\n")
        git(path, "add", "tracked.txt")
        git(path, "commit", "-qm", "base")

    def _clone_pair(self) -> tuple[Path, Path]:
        source = self.base / "source"
        self._init_repo(source)
        origin = self.base / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", "--initial-branch=main", str(origin)],
            check=True, capture_output=True, text=True,
        )
        git(source, "remote", "add", "origin", str(origin))
        git(source, "push", "-qu", "origin", "main")
        local = self.local_root / "demo"
        remote = self.remote_root / "demo"
        for target in (local, remote):
            subprocess.run(
                ["git", "clone", "-q", "--branch", "main", str(origin), str(target)],
                check=True, capture_output=True, text=True,
            )
        return local, remote

    def _run_gmf(self, mode: str = "exec") -> subprocess.CompletedProcess:
        self.remote_stdout.unlink(missing_ok=True)
        self.remote_rc.unlink(missing_ok=True)
        env = dict(self.env, GMF_FAKE_SSH_MODE=mode)
        return subprocess.run(
            [sys.executable, SCRIPT, "--lang", "en", "--diff", "fakehost",
             "--fetch", str(self.local_root)],
            env=env, capture_output=True, text=True,
        )

    def _remote_payload(self) -> dict:
        return json.loads(self.remote_stdout.read_text())

    def test_clean_remote_exit_zero_compares_successfully(self):
        self._clone_pair()

        result = self._run_gmf()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_rc.read_text().strip(), "0")
        self.assertIn("No differences to fakehost.", result.stdout)

    def test_remote_finding_exit_one_is_not_an_ssh_failure(self):
        _, remote = self._clone_pair()
        (remote / "tracked.txt").write_text("changed\n")

        result = self._run_gmf()

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.remote_rc.read_text().strip(), "1")
        self.assertEqual(self._remote_payload()["repos"][0]["modified"], 1)
        self.assertIn("changed/new file", result.stdout)
        self.assertNotIn("Cannot reach", result.stderr)

    def test_remote_fetch_failure_keeps_known_remotes(self):
        self._clone_pair()

        result = self._run_gmf("deny_git")
        repo = self._remote_payload()["repos"][0]

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.remote_rc.read_text().strip(), "1")
        self.assertTrue(repo["error"])
        self.assertEqual(repo["remote_state"], "error")
        self.assertEqual([remote["name"] for remote in repo["remotes"]], ["origin"])
        self.assertNotIn("remote 'origin' only", result.stdout)
        self.assertNotIn("Cannot reach", result.stderr)

    def test_real_ssh_failure_stays_a_transport_error(self):
        result = self._run_gmf("exit255")

        self.assertEqual(result.returncode, 2)
        self.assertIn("synthetic ssh failure", result.stderr)

    def test_invalid_remote_json_is_rejected_for_both_valid_exit_codes(self):
        for mode in ("badjson0", "badjson1"):
            with self.subTest(mode=mode):
                result = self._run_gmf(mode)
                self.assertEqual(result.returncode, 2)
                self.assertIn("unreadable JSON", result.stderr)


if __name__ == "__main__":
    unittest.main()
