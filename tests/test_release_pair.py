import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "tools" / "install-release-pair.py"
SPEC = importlib.util.spec_from_file_location("install_release_pair", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ReleasePairTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
