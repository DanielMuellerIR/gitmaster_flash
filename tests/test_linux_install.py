"""Linux-Registrierung schützt Startdateien und bewahrt Shell-cd und Exit-Codes."""
import importlib.util
import io
from contextlib import redirect_stderr, redirect_stdout
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import gitmaster_flash as g

SPEC = importlib.util.spec_from_file_location('linux_install', Path(__file__).parents[1] / 'tools/install-linux.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class LinuxInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gmf-linux-install-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home ' $HOME"
        self.home.mkdir()
        self.config = self.home / '.config'
        self.repo = self.root / "repo ' $(touch INJECTION)"
        self.repo.mkdir()
        (self.repo / 'gitmaster_flash.py').write_text('''import os, sys
from pathlib import Path
if '--version' in sys.argv:
    print('fixture-version')
    raise SystemExit(0)
if '--cd-file' in sys.argv and os.environ.get('GMF_TEST_TARGET'):
    Path(sys.argv[sys.argv.index('--cd-file')+1]).write_text(os.environ['GMF_TEST_TARGET'])
raise SystemExit(int(os.environ.get('GMF_TEST_RC', '0')))
''')
        self.language = patch.object(g, 'UI_LANG', 'en')
        self.language.start(); self.addCleanup(self.language.stop)

    def install(self, shell='bash'):
        return installer.install(self.repo, self.home, shell, self.config, self.home)

    def test_bash_registration_is_idempotent_and_preserves_original_without_newline(self):
        rc = self.home / '.bashrc'; original = b'# original\nprintf existing'
        rc.write_bytes(original)
        wrapper = self.install()
        first = rc.read_bytes(); mtime = rc.stat().st_mtime_ns
        self.assertTrue(first.startswith(original + b'\n\n'))
        self.assertIn(str(wrapper).replace("'", "'\"'\"'").encode(), first)
        self.assertEqual(len(list(self.home.glob('.bashrc.gmf-backup-*'))), 1)
        self.assertEqual(next(self.home.glob('.bashrc.gmf-backup-*')).read_bytes(), original)
        self.install()
        self.assertEqual(rc.read_bytes(), first); self.assertEqual(rc.stat().st_mtime_ns, mtime)
        self.assertFalse((self.root/'INJECTION').exists())

    def test_damaged_or_foreign_registration_and_incomplete_shell_are_protected(self):
        rc = self.home / '.bashrc'
        for text in ('gmf() { echo custom; }\n', installer.START+'\n',
                     'if true; then\n', 'cat <<EOF\nunfinished\n'):
            with self.subTest(text=text):
                rc.write_text(text)
                with self.assertRaises(ValueError): self.install()
                self.assertEqual(rc.read_text(), text)
                self.assertFalse((self.config/'gitmaster_flash/shell/gmf.bash').exists())

    def test_linked_startup_file_or_foreign_wrapper_is_not_overwritten(self):
        target = self.root/'dotfile'; target.write_text('# original\n')
        rc = self.home/'.bashrc';rc.symlink_to(target)
        with self.assertRaises(ValueError):self.install()
        self.assertTrue(rc.is_symlink());self.assertEqual(target.read_text(),'# original\n')
        rc.unlink()
        wrapper=self.config/'gitmaster_flash/shell/gmf.bash'
        wrapper.parent.mkdir(parents=True);wrapper.write_text('# my file\n')
        with self.assertRaises(ValueError):self.install()
        self.assertEqual(wrapper.read_text(),'# my file\n')

    def test_rc_change_during_preparation_is_not_lost(self):
        rc=self.home/'.bashrc';rc.write_text('# before\n')
        original_write=installer.atomic_write
        def write(path,data,**kwargs):
            if path==rc:rc.write_text('# concurrent edit\n')
            return original_write(path,data,**kwargs)
        with patch.object(installer,'atomic_write',side_effect=write):
            with self.assertRaises(ValueError):self.install()
        self.assertEqual(rc.read_text(),'# concurrent edit\n')

    def test_shell_wrapper_preserves_newlines_exit_code_and_temp_cleanup(self):
        for shell in ('bash','zsh','fish'):
            if not shutil.which(shell):continue
            with self.subTest(shell=shell):
                wrapper=self.install(shell)
                target=self.root/'target with\ntrailing newline\n';target.mkdir(exist_ok=True)
                tmp=self.root/'temporary files';tmp.mkdir(exist_ok=True)
                env=dict(os.environ,HOME=str(self.home),XDG_CONFIG_HOME=str(self.config),
                         TMPDIR=str(tmp),GMF_TEST_TARGET=str(target),GMF_TEST_RC='7')
                if shell=='fish':
                    code='source '+installer.quote_fish(str(wrapper))+"; gmf; set rc $status; printf '%s\\0%s' \"$PWD\" $rc"
                else:
                    import shlex
                    code='source '+shlex.quote(str(wrapper))+"; gmf; result=$?; printf '%s\\0%s' \"$PWD\" \"$result\""
                result=subprocess.run([shell,'-c',code],env=env,cwd=self.root,capture_output=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stdout,os.fsencode(target)+b'\0' + b'7')
                self.assertEqual(list(tmp.iterdir()),[])
                self.assertFalse((self.root/'INJECTION').exists())

    def test_fish_autoload_does_not_touch_config_fish(self):
        if not shutil.which('fish'):self.skipTest('fish unavailable')
        conf=self.config/'fish/config.fish';conf.parent.mkdir(parents=True);conf.write_text('# user settings\n')
        self.install('fish')
        result=subprocess.run(['fish','-c','gmf --version'],env=dict(os.environ,HOME=str(self.home),
                              XDG_CONFIG_HOME=str(self.config)),capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout,b'fixture-version\n')
        self.assertEqual(conf.read_text(),'# user settings\n')

    def test_explicit_shell_and_relative_xdg_paths_are_rejected(self):
        with self.assertRaises(ValueError):self.install('sh')
        with self.assertRaises(ValueError):installer.install(self.repo,self.home,'bash',Path('relative'),self.home)

    def test_failed_selftest_never_registers_files(self):
        with patch.object(installer.sys,'platform','linux'), \
             patch.object(installer,'prerequisites'), \
             patch.object(installer.subprocess,'run',return_value=Mock(returncode=1)), \
             patch.object(installer,'install') as write, \
             redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(installer.main(['--shell','bash']),1)
        write.assert_not_called()

    def test_check_runs_selftests_without_installing(self):
        with patch.object(installer.sys,'platform','linux'), \
             patch.object(installer,'prerequisites'), \
             patch.object(installer.subprocess,'run',return_value=Mock(returncode=0)), \
             patch.object(installer,'install') as write,redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(['--shell','bash','--check']),0)
        write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
