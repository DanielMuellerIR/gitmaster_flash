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
    wrongshape0)
        printf '[]'
        exit 0
        ;;
    wrongshape1)
        printf '{"version":"x","repos":[{"rel":"x","remotes":[{}]}]}'
        exit 1
        ;;
    wrongshape2)
        printf '{"version":"x","repos":[{"rel":"x","modified":"1","remotes":[]}]}'
        exit 0
        ;;
    wrongshape3)
        printf '{"version":"x","repos":[{"rel":"x","remotes":[{"name":"origin","ahead":[]}]}]}'
        exit 1
        ;;
    deny_git)
        export GIT_ALLOW_PROTOCOL=none
        ;;
esac
export HOME="$GMF_REMOTE_HOME"
cd "$GMF_REMOTE_HOME" || exit 70
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
        remote = self._remote_payload()["repos"][0]["remotes"][0]
        self.assertEqual(remote["fetch_fingerprints"],
                         [remote["fetch_fingerprint"]])
        self.assertIs(remote["fetch_refspecs_safe"], True)
        self.assertIs(remote["branch_mapping_safe"], True)
        self.assertIs(remote["fetch_url_safe"], True)
        self.assertIs(remote["push_url_safe"], True)
        self.assertIsInstance(remote["fetch_refspec_fingerprint"], str)
        self.assertTrue(remote["fetch_refspec_fingerprint"])

    def test_a_non_utf8_branch_name_still_yields_valid_json(self):
        """Git erlaubt Bytes ohne UTF-8-Bedeutung im Refnamen.

        Die Git-Leser dekodieren mit ``surrogateescape``; roh in ``json.dumps``
        gegeben, stand im Ergebnis ein ungueltiges Byte oder der Lauf brach mit
        einem UnicodeEncodeError ab. ``--json`` war damit fuer ein voellig
        gueltiges Repo unbrauchbar — und der Rechnervergleich las es nicht.
        """
        repo = self.local_root / "odd"
        self._init_repo(repo)
        oid = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        # Als Datei kann APFS diesen Namen nicht anlegen; in packed-refs steht
        # er trotzdem, und Git nimmt ihn an.
        (repo / ".git" / "packed-refs").write_bytes(
            b"# pack-refs with: peeled fully-peeled sorted \n"
            + oid.encode("ascii") + b" refs/heads/we\xffird\n")
        (repo / ".git" / "HEAD").write_bytes(b"ref: refs/heads/we\xffird\n")

        result = subprocess.run(
            [sys.executable, SCRIPT, "--lang", "en", "--json",
             str(self.local_root)],
            env=self.env, capture_output=True)

        self.assertIn(result.returncode, (0, 1), result.stderr)
        # Genau hier scheiterte es vorher: rohes 0xff ist kein gueltiges UTF-8.
        payload = json.loads(result.stdout.decode("utf-8"))
        odd = next(item for item in payload["repos"] if item["rel"] == "odd")
        self.assertEqual(odd["branch"], "we\\udcffird")

    def test_two_different_unparsable_remote_urls_count_as_drift(self):
        """Eine nicht zerlegbare Remote-URL hat kein kanonisches Ziel.

        Frueher stand dafuer auf beiden Rechnern dieselbe leere Fingerprintliste
        — zwei Macs mit VERSCHIEDENEN kaputten URLs sahen deshalb identisch aus,
        und ``--diff`` verschwieg den Ziel-Drift ausgerechnet dort, wo die
        Konfiguration ohnehin nicht belegbar ist.
        """
        local, remote = self._clone_pair()
        # Beide URLs scheitern beim Zerlegen (ungueltige IPv6-Klammer bzw. Port
        # ausserhalb 0-65535) und meinen doch verschiedene Ziele.
        git(local, "remote", "set-url", "origin", "https://[broken-here/x.git")
        git(remote, "remote", "set-url", "origin", "ssh://host:99999/other.git")

        result = self._run_gmf()

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("security/endpoint identity for origin differs",
                      result.stdout)

    def test_diff_rejects_an_ssh_option_instead_of_running_it(self):
        marker = self.base / "proxy-command-ran"
        option = "-oProxyCommand=touch " + str(marker)

        result = subprocess.run(
            [sys.executable, SCRIPT, "--lang", "en", "--diff=" + option,
             str(self.local_root)],
            env=self.env, capture_output=True, text=True)

        self.assertEqual(result.returncode, 2)
        self.assertIn("safe SSH destination", result.stderr)
        self.assertFalse(marker.exists())

    def test_remote_path_named_fetch_is_not_parsed_as_an_option(self):
        remote_path = self.remote_home / "--fetch"
        remote_path.mkdir()

        result = subprocess.run(
            [sys.executable, SCRIPT, "--lang", "en", "--diff",
             "fakehost:--fetch", str(self.local_root)],
            env=self.env, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self._remote_payload()
        self.assertEqual(Path(payload["root"]), remote_path)
        self.assertEqual(payload["repos"], [])

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
        remote = repo["remotes"][0]
        self.assertTrue(remote["fetch_failed"])
        self.assertEqual(remote["fetch_outcome"], "unknown")
        self.assertTrue(remote["fetch_error_long"])
        self.assertTrue(remote["fetch_error_detail"])
        self.assertNotIn("remote 'origin' only", result.stdout)
        self.assertNotIn("Cannot reach", result.stderr)

    def test_remote_unsafe_fetch_refspec_is_reported_without_running_it(self):
        _, remote_repo = self._clone_pair()
        git(remote_repo, "branch", "protected")
        before = subprocess.run(
            ["git", "-C", str(remote_repo), "rev-parse", "refs/heads/protected"],
            check=True, capture_output=True, text=True).stdout.strip()
        git(remote_repo, "config", "--unset-all", "remote.origin.fetch")
        git(remote_repo, "config", "--add", "remote.origin.fetch",
            "+refs/heads/*:refs/heads/*")

        result = self._run_gmf()
        remote = self._remote_payload()["repos"][0]["remotes"][0]

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(remote["fetch_outcome"], "unsafe_refspec")
        self.assertIs(remote["fetch_refspecs_safe"], False)
        self.assertIs(remote["branch_mapping_safe"], False)
        after = subprocess.run(
            ["git", "-C", str(remote_repo), "rev-parse", "refs/heads/protected"],
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(after, before)

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

    def test_wrong_remote_json_structure_is_rejected_for_both_valid_exit_codes(self):
        for mode in ("wrongshape0", "wrongshape1", "wrongshape2", "wrongshape3"):
            with self.subTest(mode=mode):
                result = self._run_gmf(mode)
                self.assertEqual(result.returncode, 2)
                self.assertIn("JSON structure", result.stderr)


class InstallScriptTests(unittest.TestCase):
    """install.sh gegen eine echte ~/.zshrc — Idempotenz ist hier das Thema."""

    def setUp(self):
        if not shutil.which("zsh"):
            self.skipTest("zsh nicht vorhanden")
        self.repo = Path(__file__).resolve().parent.parent
        self.tmp = tempfile.TemporaryDirectory()
        # macOS: /var ist ein Symlink auf /private/var — beide Seiten auflösen.
        self.home = Path(self.tmp.name).resolve()
        self.fakebin = self.home / "bin"; self.fakebin.mkdir()
        self.selftest_log = self.home / "selftest.log"
        fake_python = self.fakebin / "python3"
        fake_python.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$GMF_SELFTEST_LOG\"\nexit 0\n")
        fake_python.chmod(0o755)
        self.addCleanup(self.tmp.cleanup)

    def _install(self, script: Path | None = None):
        env = dict(os.environ, HOME=str(self.home), ZDOTDIR=str(self.home),
                   PATH=str(self.fakebin) + os.pathsep + os.environ["PATH"],
                   GMF_SELFTEST_LOG=str(self.selftest_log))
        return subprocess.run([str(script or self.repo / "install.sh")],
                              capture_output=True, text=True, env=env, timeout=300)

    def test_installer_runs_one_controlled_selftest(self):
        result = self._install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.selftest_log.read_text().splitlines(),
            ["-m unittest discover -s tests -q"],
        )

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

    def test_missing_final_newline_is_repaired_before_source_line(self):
        """Eine vorhandene letzte Zeile darf nicht mit ``source`` verschmelzen.

        Konfigurationsdateien ohne abschliessenden Zeilenumbruch sind zwar
        ungewoehnlich, aber gueltig. Der Installer muss seine Registrierung in
        diesem Fall trotzdem als eigene Shell-Anweisung anhaengen.
        """
        zshrc = self.home / ".zshrc"
        zshrc.write_text("export DEMO_SETTING=1")

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            zshrc.read_text(),
            f'export DEMO_SETTING=1\nsource -- "{self.repo}/gmf.zsh"\n',
        )

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

    def test_command_does_not_turn_source_into_a_working_builtin(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        for source_word in ("source", "."):
            with self.subTest(source=source_word):
                original = f"command {source_word} -- {wrapper}\n"
                zshrc.write_text(original)
                loaded = subprocess.run(
                    ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
                    capture_output=True, text=True,
                    env=dict(os.environ, HOME=str(self.home),
                             ZDOTDIR=str(self.home)))
                self.assertNotIn("gmf: function", loaded.stdout)
                self.assertFalse(self._is_active_line(original))

                installed = self._install()
                self.assertEqual(installed.returncode, 0, installed.stderr)
                self.assertIn("Registered wrapper", installed.stdout)

        line = f"builtin source -- {wrapper}\n"
        zshrc.write_text(line)
        loaded = subprocess.run(
            ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
            capture_output=True, text=True,
            env=dict(os.environ, HOME=str(self.home), ZDOTDIR=str(self.home)))
        self.assertTrue(self._is_active_line(line))
        self.assertIn("gmf: function", loaded.stdout)

        ineffective_prefixes = (
            "builtin time", "builtin nocorrect", "time time",
            "noglob time", "noglob nocorrect", "nocorrect time",
        )
        for prefix in ineffective_prefixes:
            with self.subTest(prefix=prefix):
                original = f"{prefix} source -- {wrapper}\n"
                zshrc.write_text(original)
                loaded = subprocess.run(
                    ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
                    capture_output=True, text=True,
                    env=dict(os.environ, HOME=str(self.home),
                             ZDOTDIR=str(self.home)))
                self.assertNotIn("gmf: function", loaded.stdout)
                self.assertFalse(self._is_active_line(original))
                installed = self._install()
                self.assertEqual(installed.returncode, 0, installed.stderr)
                self.assertIn("Registered wrapper", installed.stdout)

        effective_prefixes = (
            "builtin noglob", "nocorrect noglob", "time noglob",
            "time nocorrect", "noglob noglob", "nocorrect nocorrect",
        )
        for prefix in effective_prefixes:
            with self.subTest(prefix=prefix):
                original = f"{prefix} source -- {wrapper}\n"
                zshrc.write_text(original)
                loaded = subprocess.run(
                    ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
                    capture_output=True, text=True,
                    env=dict(os.environ, HOME=str(self.home),
                             ZDOTDIR=str(self.home)))
                self.assertIn("gmf: function", loaded.stdout)
                self.assertTrue(self._is_active_line(original))

        combined = (
            "command source /tmp/not-a-wrapper; "
            f"source -- {wrapper}\n"
        )
        zshrc.write_text(combined)
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn("Already installed", installed.stdout)
        self.assertEqual(zshrc.read_text(), combined)

    def test_last_effective_source_candidate_on_a_line_wins(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        first_candidates = (
            "source -- /does/not/exist",
            "source -- relative",
            "time time source -- /does/not/exist",
            "noglob time source -- /does/not/exist",
            "noglob nocorrect source -- /does/not/exist",
            "builtin time source -- /does/not/exist",
            "builtin nocorrect source -- /does/not/exist",
            "print if",
            "print function",
        )
        for first in first_candidates:
            with self.subTest(first=first):
                content = f"{first}; source -- {wrapper}\n"
                zshrc.write_text(content)
                result = self._install()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Already installed", result.stdout)
                self.assertEqual(zshrc.read_text(), content)

        other = self.home / "other" / "gmf.zsh"
        other.parent.mkdir()
        other.write_text("gmf() { print other; }\n")
        content = f"source -- {wrapper}; source -- {shlex.quote(str(other))}\n"
        zshrc.write_text(content)
        result = self._install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("different path", result.stderr)
        self.assertEqual(zshrc.read_text(), content)

        content = f"source -- {wrapper}; source -- /does/not/exist\n"
        zshrc.write_text(content)
        result = self._install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already installed", result.stdout)
        self.assertEqual(zshrc.read_text(), content)

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

    def test_control_word_in_comment_does_not_duplicate_registration(self):
        zshrc = self.home / ".zshrc"
        line = f'source -- "{self.repo}/gmf.zsh" # if never parsed\n'
        zshrc.write_text(line)

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already installed", result.stdout)
        self.assertEqual(zshrc.read_text(), line)

    def _loads_the_wrapper(self) -> bool:
        """Definiert ein `zsh`, das diese .zshrc sourct, wirklich `gmf`?"""
        loaded = subprocess.run(
            ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
            capture_output=True, text=True,
            env=dict(os.environ, HOME=str(self.home), ZDOTDIR=str(self.home)))
        return "gmf: function" in loaded.stdout

    def test_a_source_inside_a_command_substitution_is_no_installation(self):
        """Ein gequotetes `)` schliesst keine Kommandoersetzung.

        Der Mehrzeilen-Scanner merkte sich eine offene Ersetzung nur, wenn im
        Wort ueberhaupt kein `)` vorkam. In `value=$(print ')'` stammt das
        vorhandene `)` aber aus einem Argument in Quotes; die naechste Zeile
        laeuft in zsh im Unterprozess der Ersetzung. Der Installer meldete
        trotzdem "Already installed", waehrend `gmf` in der aufrufenden Shell
        gar nicht entsteht.
        """
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        content = "value=$(print ')'\n" + f"source -- {wrapper}\n" + ")\n"
        zshrc.write_text(content)
        self.assertFalse(self._loads_the_wrapper())

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Registered wrapper", result.stdout)
        self.assertIn(content, zshrc.read_text())
        self.assertTrue(self._loads_the_wrapper())

    def test_a_closed_control_block_on_the_same_line_stays_idempotent(self):
        """Ein Kontrollblock verwarf frueher die GANZE Zeile.

        Damit galt weder ein `source` vor dem Block noch eines hinter einem
        laengst geschlossenen Block als Registrierung — der Installer trug ein
        zweites Mal ein, und der Wrapper wurde beim Shellstart doppelt geladen.
        """
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        for content in (f"source -- {wrapper}; if true; then :; fi\n",
                        f"if true; then :; fi; source -- {wrapper}\n"):
            with self.subTest(content=content):
                zshrc.write_text(content)
                self.assertTrue(self._loads_the_wrapper())

                result = self._install()

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Already installed", result.stdout)
                self.assertEqual(zshrc.read_text(), content)

        # Im Block selbst bleibt es dagegen kein Beleg: `false` laesst das
        # `source` nie laufen, und `gmf` fehlt.
        content = f"if false; then source -- {wrapper}; fi\n"
        zshrc.write_text(content)
        self.assertFalse(self._loads_the_wrapper())

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Registered wrapper", result.stdout)

    def _is_active_line(self, line: str) -> bool:
        """Den quote-aware Lexer aus install.sh anwenden — ohne Selbsttest."""
        script = (
            'source <(sed -n "/^resolve_sourced_path()/,/^}/p" %s)\n'
            'path="$(resolve_sourced_path "$1")" || exit 1\n'
            '[[ "${path:t}" == gmf.zsh ]]\n'
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
                 "source -- '/x/project$cash/gmf.zsh'",
                 "  source ~/x/gmf.zsh",
                 ". ~/x/gmf.zsh",
                 "source /x/gmf.zsh && echo loaded",
                 "source /x/gmf.zsh || echo failed")
        passiv = ("# source ~/x/gmf.zsh",
                  "echo ok # source ~/x/gmf.zsh",
                  "export GMF_WRAPPER=~/x/gmf.zsh",
                  "echo 'a; source /x/gmf.zsh'",
                  "source '$HOME/x/gmf.zsh'",
                  "source '~/x/gmf.zsh'",
                  'source "~/x/gmf.zsh"',
                  "source $HOME_SUFFIX/x/gmf.zsh",
                  "source ~someone/x/gmf.zsh",
                  "source relative/x/gmf.zsh",
                  "false && source ~/x/gmf.zsh",
                  "true || source ~/x/gmf.zsh")
        for line in aktiv:
            with self.subTest(line=line):
                self.assertTrue(self._is_active_line(line))
        for line in passiv:
            with self.subTest(line=line):
                self.assertFalse(self._is_active_line(line))

    def test_hash_inside_quoted_clone_path_stays_idempotent(self):
        clone = self.home / "project # copy"
        clone.mkdir()
        shutil.copy2(self.repo / "install.sh", clone / "install.sh")
        shutil.copy2(self.repo / "gmf.zsh", clone / "gmf.zsh")

        first = self._install(clone / "install.sh")
        second = self._install(clone / "install.sh")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Already installed", second.stdout)
        lines = [line for line in (self.home / ".zshrc").read_text().splitlines()
                 if "gmf.zsh" in line]
        self.assertEqual(len(lines), 1)

    def test_dollar_inside_quoted_clone_path_stays_idempotent(self):
        clone = self.home / "project $cash"
        clone.mkdir()
        shutil.copy2(self.repo / "install.sh", clone / "install.sh")
        shutil.copy2(self.repo / "gmf.zsh", clone / "gmf.zsh")

        first = self._install(clone / "install.sh")
        second = self._install(clone / "install.sh")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Already installed", second.stdout)
        lines = [line for line in (self.home / ".zshrc").read_text().splitlines()
                 if "gmf.zsh" in line]
        self.assertEqual(len(lines), 1)

    def test_subshell_or_pipeline_source_is_not_an_installation(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        for line in (f"( source -- {wrapper} )\n",
                     f"source -- {wrapper} | cat\n",
                     f"source -- {wrapper} & wait\n",
                     f"source -- {wrapper} &!\n",
                     f"source -- {wrapper} &|\n",
                     f"false && source -- {wrapper}\n",
                     f"true || source -- {wrapper}\n"):
            with self.subTest(line=line.strip()):
                zshrc.write_text(line)
                result = self._install()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("Already installed", result.stdout)
                second = self._install()
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertIn("Already installed", second.stdout)
                loaded = subprocess.run(
                    ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
                    capture_output=True, text=True,
                    env=dict(os.environ, HOME=str(self.home),
                             ZDOTDIR=str(self.home)))
                self.assertEqual(loaded.returncode, 0, loaded.stderr)
                self.assertIn("gmf: function", loaded.stdout)

    def test_source_inside_inactive_multiline_block_is_not_an_installation(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        for content in (
                f"if false; then\n  source -- {wrapper}\nfi\n",
                f"if false; then\n  echo fi\n  source -- {wrapper}\nfi\n",
                f"lazy_gmf() {{\n  source -- {wrapper}\n}}\n",
                f"ignored=$(\n  source -- {wrapper}\n)\n"):
            with self.subTest(content=content.splitlines()[0]):
                zshrc.write_text(content)
                result = self._install()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("Already installed", result.stdout)
                second = self._install()
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertIn("Already installed", second.stdout)
                loaded = subprocess.run(
                    ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
                    capture_output=True, text=True,
                    env=dict(os.environ, HOME=str(self.home),
                             ZDOTDIR=str(self.home)))
                self.assertEqual(loaded.returncode, 0, loaded.stderr)
                self.assertIn("gmf: function", loaded.stdout)

    def test_source_inside_a_single_line_control_block_is_not_an_installation(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        for content in (
                f"lazy() {{ true; source -- {wrapper}; }}\n",
                f"if false; then true; source -- {wrapper}; fi\n"):
            with self.subTest(content=content.split(";", 1)[0]):
                zshrc.write_text(content)
                first = self._install()
                second = self._install()
                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertNotIn("Already installed", first.stdout)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertIn("Already installed", second.stdout)
                loaded = subprocess.run(
                    ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
                    capture_output=True, text=True,
                    env=dict(os.environ, HOME=str(self.home), ZDOTDIR=str(self.home)))
                self.assertEqual(loaded.returncode, 0, loaded.stderr)
                self.assertIn("gmf: function", loaded.stdout)

    def test_control_word_used_as_an_argument_does_not_open_a_shell_block(self):
        zshrc = self.home / ".zshrc"
        zshrc.write_text("echo if\n")

        first = self._install()
        second = self._install()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Already installed", second.stdout)
        registrations = [line for line in zshrc.read_text().splitlines()
                         if line.startswith("source -- ") and "gmf.zsh" in line]
        self.assertEqual(len(registrations), 1)
        loaded = subprocess.run(
            ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
            capture_output=True, text=True,
            env=dict(os.environ, HOME=str(self.home), ZDOTDIR=str(self.home)))
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertIn("gmf: function", loaded.stdout)

    def test_unclosed_shell_context_fails_without_appending_a_registration(self):
        zshrc = self.home / ".zshrc"
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        for content in (
                "if false; then\n",
                "cat <<GMF_TEXT\n",
                f"cat <<GMF_TEXT >/dev/null\nsource -- {wrapper}\nGMF_TEXT\n",
                f"value=`echo\nsource -- {wrapper}\n`\n"):
            with self.subTest(content=content.strip()):
                zshrc.write_text(content)

                first = self._install()
                second = self._install()

                self.assertEqual(first.returncode, 1)
                self.assertEqual(second.returncode, 1)
                self.assertIn("cannot prove a top-level registration",
                              first.stderr)
                self.assertEqual(zshrc.read_text(), content)

    def test_source_inside_a_subshell_function_body_is_not_an_installation(self):
        zshrc = self.home / ".zshrc"
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc.write_text(
            f"gmf_setup() (\n  source -- {wrapper}\n)\n")

        first = self._install()
        second = self._install()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("Registered wrapper", first.stdout)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Already installed", second.stdout)
        loaded = subprocess.run(
            ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
            capture_output=True, text=True,
            env=dict(os.environ, HOME=str(self.home), ZDOTDIR=str(self.home)))
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertIn("gmf: function", loaded.stdout)

    def test_source_inside_process_substitution_is_not_an_installation(self):
        zshrc = self.home / ".zshrc"
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        for opener in ("<(", ">("):
            with self.subTest(opener=opener):
                zshrc.write_text(
                    f"print {opener}\n  source -- {wrapper}\n)\n")

                first = self._install()
                second = self._install()

                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertIn("Already installed", second.stdout)
                loaded = subprocess.run(
                    ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
                    capture_output=True, text=True,
                    env=dict(os.environ, HOME=str(self.home),
                             ZDOTDIR=str(self.home)))
                self.assertEqual(loaded.returncode, 0, loaded.stderr)
                self.assertIn("gmf: function", loaded.stdout)

    def test_source_inside_coproc_is_not_an_installation(self):
        zshrc = self.home / ".zshrc"
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        for opening, closing in (("coproc {", "}"), ("coproc (", ")")):
            with self.subTest(opening=opening):
                zshrc.write_text(
                    f"{opening}\n  source -- {wrapper}\n{closing}\n")

                first = self._install()
                second = self._install()

                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertIn("Already installed", second.stdout)
                loaded = subprocess.run(
                    ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
                    capture_output=True, text=True,
                    env=dict(os.environ, HOME=str(self.home),
                             ZDOTDIR=str(self.home)))
                self.assertEqual(loaded.returncode, 0, loaded.stderr)
                self.assertIn("gmf: function", loaded.stdout)

    def test_left_side_of_boolean_list_is_an_unconditional_installation(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        for line in (f"source -- {wrapper} && echo loaded\n",
                     f"source -- {wrapper} || echo failed\n"):
            with self.subTest(line=line.strip()):
                zshrc.write_text(line)
                result = self._install()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Already installed", result.stdout)
                self.assertEqual(zshrc.read_text(), line)

    def test_installer_never_echoes_the_rest_of_a_source_line(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        marker = "PRIVATE_VALUE" + "\x1b]0;changed\x07"
        zshrc.write_text(f"source -- {wrapper}; print -r -- {shlex.quote(marker)}\n")

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already installed", result.stdout)
        self.assertNotIn("PRIVATE_VALUE", result.stdout + result.stderr)
        self.assertNotIn("\x1b", result.stdout + result.stderr)

    def test_source_after_return_or_exit_is_never_claimed_as_installed(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        for builtin in ("return", "exit", "exec false",
                        "X=1 return", "X=1 exit", "X=1 exec false",
                        "X+=1 return", "X+=1 exit", "X+=1 exec false",
                        "X=(1) return", "X=(1) exit", "X=(1) exec false",
                        "X[1]=1 return", "X[1]=1 exit", "X[1]=1 exec false",
                        r"X[1\]]=1 exec false",
                        "noglob exit", "nocorrect return", "time exit"):
            content = f"{builtin}; source -- {wrapper}\n"
            with self.subTest(builtin=builtin):
                zshrc.write_text(content)
                result = self._install()
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("Already installed", result.stdout)
                self.assertIn("cannot prove", result.stderr)
                self.assertEqual(zshrc.read_text(), content)
                loaded = subprocess.run(
                    ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
                    capture_output=True, text=True,
                    env=dict(os.environ, HOME=str(self.home),
                             ZDOTDIR=str(self.home)))
                self.assertNotIn("gmf: function", loaded.stdout)

        content = (
            "if true; then\n"
            "  return\n"
            "fi\n"
            f"source -- {wrapper}\n"
        )
        zshrc.write_text(content)
        result = self._install()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Already installed", result.stdout)
        self.assertIn("cannot prove", result.stderr)
        self.assertEqual(zshrc.read_text(), content)

    def test_closed_case_block_does_not_block_installation(self):
        zshrc = self.home / ".zshrc"
        zshrc.write_text(
            "case $TERM in\n"
            "  xterm*) export DEMO=1 ;;&\n"
            "  *) true ;;\n"
            "esac\n")

        first = self._install()
        second = self._install()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Already installed", second.stdout)

    def test_multiline_quote_is_rejected_instead_of_parsed_as_commands(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        content = f"print '\nsource -- {wrapper}\n'\n"
        zshrc.write_text(content)

        result = self._install()

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Already installed", result.stdout)
        self.assertIn("cannot prove", result.stderr)
        self.assertEqual(zshrc.read_text(), content)

    def test_multiline_ansi_quote_is_not_parsed_as_commands(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        content = "print $'foo\\'\n" + f"source -- {wrapper}\n" + "'\n"
        zshrc.write_text(content)

        result = self._install()
        loaded = subprocess.run(
            ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
            capture_output=True, text=True,
            env=dict(os.environ, HOME=str(self.home), ZDOTDIR=str(self.home)))

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Already installed", result.stdout)
        self.assertIn("cannot prove", result.stderr)
        self.assertEqual(zshrc.read_text(), content)
        self.assertNotIn("gmf: function", loaded.stdout)

    def test_multiline_parameter_expansion_is_not_parsed_as_commands(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        content = (
            "unset gmf_missing\n"
            "print ${gmf_missing:-\n"
            f"source -- {wrapper}\n"
            "}\n"
        )
        zshrc.write_text(content)

        result = self._install()
        loaded = subprocess.run(
            ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
            capture_output=True, text=True,
            env=dict(os.environ, HOME=str(self.home), ZDOTDIR=str(self.home)))

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Already installed", result.stdout)
        self.assertIn("cannot prove", result.stderr)
        self.assertEqual(zshrc.read_text(), content)
        self.assertNotIn("gmf: function", loaded.stdout)

    def test_backslash_continuation_is_not_parsed_as_a_new_command(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        content = f"print ignored \\\nsource -- {wrapper}\n"
        zshrc.write_text(content)

        result = self._install()

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Already installed", result.stdout)
        self.assertIn("cannot prove", result.stderr)
        self.assertEqual(zshrc.read_text(), content)

    def test_source_inside_a_multiline_array_is_only_data(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        zshrc.write_text(f"plugins=(\nsource -- {wrapper}\n)\n")

        first = self._install()
        second = self._install()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn("Already installed", first.stdout)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Already installed", second.stdout)
        loaded = subprocess.run(
            ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf'],
            capture_output=True, text=True,
            env=dict(os.environ, HOME=str(self.home), ZDOTDIR=str(self.home)))
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertIn("gmf: function", loaded.stdout)

    def test_source_after_a_multiline_operator_is_not_unconditional(self):
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        for operator in ("false &&", "print ignored |"):
            content = f"{operator}\nsource -- {wrapper}\n"
            with self.subTest(operator=operator):
                zshrc.write_text(content)
                result = self._install()
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("Already installed", result.stdout)
                self.assertIn("cannot prove", result.stderr)
                self.assertEqual(zshrc.read_text(), content)

    def test_quoted_home_literals_are_not_mistaken_for_the_wrapper(self):
        wrapper = str(self.repo / "gmf.zsh")
        zshrc = self.home / ".zshrc"
        for line in ("source '$HOME/gmf.zsh'\n",
                     "source '~/gmf.zsh'\n",
                     'source "~/gmf.zsh"\n',
                     "source $HOME_SUFFIX/gmf.zsh\n"):
            with self.subTest(line=line.strip()):
                zshrc.write_text(line)
                result = self._install()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("Already installed", result.stdout)
                self.assertIn(f'source -- "{wrapper}"', zshrc.read_text())

    def test_relative_source_is_not_tied_to_the_installers_working_directory(self):
        zshrc = self.home / ".zshrc"
        zshrc.write_text("source gmf.zsh\n")
        result = self._install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Already installed", result.stdout)
        self.assertIn(f'source -- "{self.repo}/gmf.zsh"', zshrc.read_text())

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
                     f"source '{target}'"):
            with self.subTest(line):
                self.assertEqual(self._resolve(line), target)
        # Ein wirklich anderes Repo bleibt unterscheidbar.
        self.assertNotEqual(self._resolve("source ~/git/woanders/gmf.zsh"), target)
        # Und der Punkt bedeutet dasselbe wie "source".
        for line in (f". {target}",
                     ". ~/git/gitmaster_flash/gmf.zsh"):
            with self.subTest(line):
                self.assertEqual(self._resolve(line), target)


if __name__ == "__main__":
    unittest.main()
