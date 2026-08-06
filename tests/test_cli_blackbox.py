"""Black-Box-Tests fuer den kompletten lokalen/entfernten CLI-Vertrag.

Ein temporaeres ``ssh`` fuehrt den echten, ueber stdin uebertragenen GMF-Code lokal
aus. Damit pruefen diese Tests die Grenze, die Unit-Tests mit gemocktem subprocess
nicht sehen: Remote-JSON, Remote-Exit-Code und lokale Diff-Auswertung gemeinsam.
"""

import json
import os
import shlex
import shutil
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


class InstallScriptTests(unittest.TestCase):
    """install.sh gegen eine echte ~/.zshrc — Idempotenz ist hier das Thema."""

    #: Der Selbsttest in install.sh startet diese Suite erneut. Die Marke bricht
    #: die Rekursion nach genau einer Ebene ab.
    GUARD = "GMF_INSTALL_TEST_RUNNING"

    def setUp(self):
        if os.environ.get(self.GUARD):
            self.skipTest("läuft bereits im Selbsttest von install.sh")
        if not shutil.which("zsh"):
            self.skipTest("zsh nicht vorhanden")
        self.repo = Path(__file__).resolve().parent.parent
        self.tmp = tempfile.TemporaryDirectory()
        # macOS: /var ist ein Symlink auf /private/var — beide Seiten auflösen.
        self.home = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def _install(self):
        env = dict(os.environ, HOME=str(self.home), ZDOTDIR=str(self.home))
        env[self.GUARD] = "1"
        return subprocess.run([str(self.repo / "install.sh")],
                              capture_output=True, text=True, env=env, timeout=300)

    def test_tilde_form_counts_as_already_installed(self):
        # Genau der gemeldete Fall: die Zeile meint dasselbe, steht nur anders da.
        zshrc = self.home / ".zshrc"
        zshrc.write_text(f"source ~/{self.repo.name}/gmf.zsh\n")
        # Der Pfad muss unter dem Test-HOME auch wirklich dorthin zeigen.
        (self.home / self.repo.name).symlink_to(self.repo)

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already installed", result.stdout)
        self.assertEqual(zshrc.read_text(), f"source ~/{self.repo.name}/gmf.zsh\n")

    def test_lines_that_do_not_load_the_wrapper_are_no_installation(self):
        """Drei Zeilen, die gmf.zsh nur erwähnen: ein ganz auskommentierter
        `source`, ein Inline-Kommentar hinter einem echten Befehl und eine bloße
        Zuweisung. Keine davon lädt den Wrapper in einer neuen Shell; der
        Installer darf sie nicht als bestehende Installation werten und Erfolg
        melden, während `gmf` weiterhin fehlt."""
        zshrc = self.home / ".zshrc"
        zshrc.write_text("# source ~/irgendwo/gmf.zsh\n"
                         "echo ok # source ~/irgendwo/gmf.zsh\n"
                         "export GMF_WRAPPER=~/irgendwo/gmf.zsh\n")

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Already installed", result.stdout)
        content = zshrc.read_text()
        # Die drei Zeilen bleiben stehen, die echte source-Zeile kommt dazu.
        self.assertIn("# source ~/irgendwo/gmf.zsh", content)
        self.assertIn("echo ok # source ~/irgendwo/gmf.zsh", content)
        self.assertIn("export GMF_WRAPPER=~/irgendwo/gmf.zsh", content)
        self.assertIn(f'source -- "{self.repo}/gmf.zsh"', content)

    def test_the_dot_form_counts_as_already_installed(self):
        """`.` ist in der Shell dasselbe Kommando wie `source`, und die
        Erkennungsregel akzeptiert es ausdrücklich. Die Pfadauflösung schnitt
        aber nur die Zeichenfolge "source" weg — bei ". /pfad/gmf.zsh" blieb die
        ganze Zeile als vermeintlicher Pfad stehen, und der Installer brach mit
        "fremder Pfad" ab. Also nicht idempotent für eine Schreibweise, die er
        selbst anerkennt."""
        zshrc = self.home / ".zshrc"
        zeile = f". {self.repo}/gmf.zsh\n"
        zshrc.write_text(zeile)

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already installed", result.stdout)
        self.assertEqual(zshrc.read_text(), zeile)

    def test_a_separator_inside_a_comment_is_no_installation(self):
        """Die Erkennung sucht "source" hinter einem Trenner (";", "&&", …).
        Steht der Trenner in einem KOMMENTAR, führt die Zeile trotzdem nichts
        aus — sie galt aber als bestehende Installation, und der Installer
        meldete Erfolg, während `gmf` weiterhin fehlte. Ebenso umgekehrt: ein
        Kommentar, der nur `gmf.zsh` erwähnt, machte aus einer fremden
        source-Zeile einen vermeintlich falschen Pfad und brach ab."""
        zshrc = self.home / ".zshrc"
        zshrc.write_text("# erst aufräumen; source ~/irgendwo/gmf.zsh\n"
                         "source ~/irgendwo/anderes.zsh # ersetzt gmf.zsh\n")

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Already installed", result.stdout)
        content = zshrc.read_text()
        self.assertIn("# erst aufräumen; source ~/irgendwo/gmf.zsh", content)
        self.assertIn(f'source -- "{self.repo}/gmf.zsh"', content)

    def _is_active_line(self, line: str) -> bool:
        """Nur die Erkennungsregel aus install.sh anwenden — ohne Selbsttest."""
        script = (
            'source <(sed -n "/^active_gmf_line=/p" %s)\n'
            'print -r -- "$1" | grep -qE "$active_gmf_line"\n'
            % shlex.quote(str(self.repo / "install.sh"))
        )
        result = subprocess.run(["zsh", "-c", script, "zsh", line],
                                capture_output=True, text=True)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result.returncode == 0

    def test_only_a_real_source_command_counts_as_installed(self):
        """`source` muss als Kommando dastehen. Die alte Regel verlangte nur ein
        Nicht-`#` am Zeilenanfang und nahm deshalb auch einen Inline-Kommentar
        oder eine Zuweisung für eine Installation."""
        aktiv = ("source -- '/x/gmf.zsh'",
                 "  source ~/x/gmf.zsh",
                 ". ~/x/gmf.zsh",
                 "[[ -f ~/x/gmf.zsh ]] && source ~/x/gmf.zsh")
        passiv = ("# source ~/x/gmf.zsh",
                  "echo ok # source ~/x/gmf.zsh",
                  "export GMF_WRAPPER=~/x/gmf.zsh")
        for line in aktiv:
            with self.subTest(line=line):
                self.assertTrue(self._is_active_line(line))
        for line in passiv:
            with self.subTest(line=line):
                self.assertFalse(self._is_active_line(line))

    def _resolve(self, line: str) -> str:
        """Nur die Pfadauflösung aus install.sh laden — ohne den langen Selbsttest."""
        script = (
            'source <(sed -n "/^resolve_sourced_path()/,/^}/p" %s)\n'
            'resolve_sourced_path "$1"\n' % shlex.quote(str(self.repo / "install.sh"))
        )
        result = subprocess.run(["zsh", "-c", script, "zsh", line],
                                capture_output=True, text=True,
                                env=dict(os.environ, HOME=str(self.home)))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_the_same_path_is_recognized_in_every_spelling(self):
        target = f"{self.home}/git/gitmaster_flash/gmf.zsh"
        for line in (f"source {target}",
                     "source ~/git/gitmaster_flash/gmf.zsh",
                     "source $HOME/git/gitmaster_flash/gmf.zsh",
                     f'source -- "{target}"',
                     f"source '{target}'",
                     "[[ -f ~/git/gitmaster_flash/gmf.zsh ]] && "
                     "source ~/git/gitmaster_flash/gmf.zsh"):
            with self.subTest(line):
                self.assertEqual(self._resolve(line), target)
        # Ein wirklich anderes Repo bleibt unterscheidbar.
        self.assertNotEqual(self._resolve("source ~/git/woanders/gmf.zsh"), target)
        # Und der Punkt bedeutet dasselbe wie "source".
        for line in (f". {target}",
                     ". ~/git/gitmaster_flash/gmf.zsh",
                     "[[ -f ~/git/gitmaster_flash/gmf.zsh ]] && "
                     ". ~/git/gitmaster_flash/gmf.zsh"):
            with self.subTest(line):
                self.assertEqual(self._resolve(line), target)


if __name__ == "__main__":
    unittest.main()
