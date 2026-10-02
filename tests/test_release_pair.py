import importlib.util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "tools" / "install-release-pair.py"
SPEC = importlib.util.spec_from_file_location("install_release_pair", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ReleasePairTests(unittest.TestCase):
    def test_termination_during_install_leaves_a_complete_pair_and_no_lock(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            source = root / "new.tar.gz"
            checksum = root / "new.sha256"
            target = root / "release.tar.gz"
            target_checksum = root / "release.sha256"
            source.write_bytes(b"archive")
            checksum.write_bytes(b"checksum")
            code = '''import importlib.util, os, signal, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("pair", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real_link = os.link
def interrupted_link(source, target):
    real_link(source, target)
    if str(target).endswith("release.tar.gz"):
        os.kill(os.getpid(), signal.SIGTERM)
module.os.link = interrupted_link
module.install_pair(*(Path(value) for value in sys.argv[2:]))
'''
            result = subprocess.run(
                [sys.executable, "-c", code, str(SCRIPT), str(source), str(checksum),
                 str(target), str(target_checksum)], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, -signal.SIGTERM, result.stderr)
            self.assertEqual(target.read_bytes(), b"archive")
            self.assertTrue(target_checksum.exists(), "Prüfsumme fehlt nach SIGTERM")
            self.assertEqual(target_checksum.read_bytes(), b"checksum")
            self.assertFalse(target.with_name(f".{target.name}.release-lock").exists())

    def test_existing_archive_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            source = root / "new.tar.gz"
            checksum = root / "new.tar.gz.sha256"
            target = root / "release.tar.gz"
            target_checksum = root / "release.tar.gz.sha256"
            source.write_bytes(b"neu")
            checksum.write_text("neu\n", encoding="utf-8")
            target.write_bytes(b"alt")

            with self.assertRaises(FileExistsError):
                MODULE.install_pair(source, checksum, target, target_checksum)

            self.assertEqual(target.read_bytes(), b"alt")
            self.assertFalse(target_checksum.exists())

    def test_second_target_collision_rolls_back_own_archive(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            source = root / "new.tar.gz"
            checksum = root / "new.tar.gz.sha256"
            target = root / "release.tar.gz"
            target_checksum = root / "release.tar.gz.sha256"
            source.write_bytes(b"neu")
            checksum.write_text("neu\n", encoding="utf-8")
            target_checksum.write_text("alt\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                MODULE.install_pair(source, checksum, target, target_checksum)

            self.assertFalse(target.exists())
            self.assertEqual(target_checksum.read_text(encoding="utf-8"), "alt\n")

    def test_existing_lock_blocks_parallel_release(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            source = root / "new.tar.gz"
            checksum = root / "new.tar.gz.sha256"
            target = root / "release.tar.gz"
            target_checksum = root / "release.tar.gz.sha256"
            source.write_bytes(b"neu")
            checksum.write_text("neu\n", encoding="utf-8")
            target.with_name(f".{target.name}.release-lock").mkdir()

            with self.assertRaises(FileExistsError):
                MODULE.install_pair(source, checksum, target, target_checksum)

            self.assertFalse(target.exists())
            self.assertFalse(target_checksum.exists())


@unittest.skipUnless(shutil.which("zsh"), "zsh nicht vorhanden")
class ReleaseScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gmf-release-test-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", os.devnull)
        (self.repo / "tools").mkdir()
        shutil.copy2(SCRIPT, self.repo / "tools" / SCRIPT.name)
        shutil.copy2(SCRIPT.parents[1] / "release.sh", self.repo / "release.sh")
        (self.repo / "gitmaster_flash.py").write_text('print("1.0.0")\n')
        for name in ("gmf.zsh", "install.sh", "README.md", "README.de.md", "LICENSE"):
            (self.repo / name).write_text("fixture\n")
        (self.repo / ".gitignore").write_text("dist/\n")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                              capture_output=True, text=True, check=True, timeout=15)

    def release_with_build(self, body):
        build = self.repo / "build.sh"
        build.write_text("#!/bin/zsh\nset -eu\n" + body + "\n")
        build.chmod(0o755)
        self.git("add", "release.sh", "build.sh", "tools/install-release-pair.py",
                 "gitmaster_flash.py", "gmf.zsh", "install.sh", "README.md", "README.de.md",
                 "LICENSE", ".gitignore")
        self.git("commit", "-qm", "fixture")
        return subprocess.run([str(self.repo / "release.sh")], cwd=self.repo,
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_commit_during_build_aborts_without_publishing_an_archive(self):
        result = self.release_with_build(
            "print changed > introduced.txt\ngit add introduced.txt\ngit commit -qm concurrent")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(list((self.repo / "dist").glob("*.tar.gz")))

    def test_tracked_edit_during_build_aborts_without_publishing_an_archive(self):
        result = self.release_with_build("print changed >> README.md")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(list((self.repo / "dist").glob("*.tar.gz")))

    def test_unchanged_build_releases_an_archive_with_matching_checksum(self):
        result = self.release_with_build("true")
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = self.repo / "dist" / "gitmaster_flash-1.0.0.tar.gz"
        self.assertTrue(archive.exists())
        checked = subprocess.run(["shasum", "-c", archive.name + ".sha256"],
                                 cwd=archive.parent, capture_output=True, text=True, timeout=15)
        self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    unittest.main()
