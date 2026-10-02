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


class ShellWrapperTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("zsh"), "zsh nicht vorhanden")
    def test_cd_preserves_trailing_newlines_in_the_selected_path(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir).resolve()
            selected = root / "repo\n\n"
            selected.mkdir()
            (root / "repo").mkdir()
            fake_program = root / "selection.py"
            fake_program.write_text(
                "import os, pathlib, sys\n"
                "pathlib.Path(sys.argv[2]).write_text(os.environ['GMF_TEST_TARGET'])\n")
            wrapper = Path(SCRIPT).with_name("gmf.zsh")
            result = subprocess.run(
                ["zsh", "-fc", 'source "$1"; GMF_SCRIPT="$2"; gmf; print -rn -- "$PWD"',
                 "test", str(wrapper), str(fake_program)],
                env=dict(os.environ, GMF_TEST_TARGET=str(selected)),
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, str(selected))


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

    def test_negation_is_only_a_modifier_where_zsh_accepts_it(self):
        """`!` ist kein beliebig kombinierbares Präfix.

        zsh erkennt die Negation nur ganz am Anfang einer Pipeline und hinter
        `time`, das eine ganze Pipeline nimmt. Hinter `noglob`, `nocorrect`,
        `builtin`, `command` oder einem zweiten `!` ist `!` dagegen der
        Kommandoname: `noglob ! source -- …` scheitert mit "command not
        found: !", und `gmf` bleibt undefiniert. Der Installer hielt `!` für
        frei kombinierbar, meldete für solche Zeilen "Already installed" und
        liess den Nutzer ohne Wrapper und ohne Fehlermeldung zurück (Fund
        2026-09-02). Jede Reihenfolge hier wird gegen ein echtes `zsh`
        gegengeprüft — der Erwartungswert ist nicht abgeschrieben, sondern
        gemessen.
        """
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        wirksam = ("!", "! time", "! noglob", "! nocorrect", "! builtin",
                   "time !", "time ! noglob")
        unwirksam = ("! !", "! command", "noglob !", "nocorrect !",
                     "builtin !", "command !", "time ! time", "! noglob time")
        for prefix in wirksam + unwirksam:
            with self.subTest(prefix=prefix):
                zeile = f"{prefix} source -- {wrapper}\n"
                zshrc.write_text(zeile)
                geladen = self._loads_the_wrapper()
                self.assertEqual(geladen, prefix in wirksam,
                                 f"zsh selbst widerspricht der Erwartung fuer {prefix!r}")
                self.assertEqual(self._is_active_line(zeile), geladen)
                if not geladen:
                    # Der Installer darf sich hier nicht auf die kaputte Zeile
                    # verlassen, sondern muss eine wirksame Registrierung
                    # ergaenzen.
                    installiert = self._install()
                    self.assertEqual(installiert.returncode, 0, installiert.stderr)
                    self.assertIn("Registered wrapper", installiert.stdout)
                    # Ob die ergaenzte Zeile dann auch greift, haengt an der
                    # kaputten Zeile davor: `! !` und `nocorrect !` sind ein
                    # zsh-Parse-Fehler und brechen die ganze .zshrc ab. Das
                    # liegt ausserhalb dessen, was der Installer heilen kann.

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
        """Den quote-aware Lexer aus install.sh anwenden — ohne Selbsttest.

        Geladen wird der GANZE Funktionsblock, nicht nur `resolve_sourced_path`.
        Die Funktion ruft `is_command_separator`, `is_assignment_word` und
        `is_command_modifier` auf; fehlen sie, meldet zsh "command not found",
        die Aufrufe gelten als falsch, und der Scanner arbeitet stiller nach
        einer anderen Grammatik als der Installer. Zwei Zeilen des Korpus
        entschied er dadurch nachweislich anders, und eine kaputte
        Helferfunktion waere hier gar nicht aufgefallen (Fund 2026-08-29).
        Der Block enthaelt ausschliesslich Definitionen — der Selbsttest und
        das Anhaengen an die .zshrc stehen dahinter und laufen deshalb nicht
        mit. `wrapper_path`/`quoted_wrapper` setzt der Aufrufer, weil
        `resolve_sourced_path` die vom Installer selbst geschriebene
        serialisierte Form daran erkennt.
        """
        script = (
            'source <(sed -n "/^is_assignment_word()/,/^# Nur echte, lexikalisch/p" %s'
            ' | sed "\\$d")\n'
            'wrapper_path=%s\n'
            'quoted_wrapper="${(qqq)wrapper_path}"\n'
            'path="$(resolve_sourced_path "$1")" || exit 1\n'
            '[[ "${path:t}" == gmf.zsh ]]\n'
            % (shlex.quote(str(self.repo / "install.sh")),
               shlex.quote(str(self.repo / "gmf.zsh")))
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

    def test_the_lexer_check_really_loads_the_shared_grammar(self):
        """Zwei Zeilen, die nur MIT den Helferfunktionen richtig entschieden werden.

        Frueher lud die Pruefung nur `resolve_sourced_path`. `is_assignment_word`
        und `is_command_separator` fehlten dann, ihre Aufrufe galten als falsch,
        und beide Zeilen hier wurden abgelehnt — obwohl der Installer selbst sie
        als Registrierung erkennt. Eine kaputte Helferfunktion waere so nie
        aufgefallen.
        """
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        # Praefix-Zuweisung: braucht is_assignment_word im ersten Durchgang.
        self.assertTrue(self._is_active_line(f"VAR=1 source -- {wrapper}"))
        # Geschlossener Block plus Trenner: braucht is_command_separator, damit
        # das `fi` ueberhaupt als Kommandoanfang gesehen wird.
        self.assertTrue(
            self._is_active_line(f"if true; then :; fi; source -- {wrapper}"))

    def test_a_foreach_loop_does_not_block_the_installation(self):
        """`foreach x (…) … end` ist gueltiges zsh — der Blockstapel kannte `end` nicht.

        Der Stapel blieb dadurch bis zum Dateiende offen, und der Installer
        brach mit "unclosed or unsupported shell block" ab, statt sich
        einzutragen: eine einwandfreie .zshrc, in die man nicht installieren
        konnte (Fund 2026-08-29).
        """
        zshrc = self.home / ".zshrc"
        zshrc.write_text("foreach x (a b)\n  print -r -- $x\nend\n")
        # Erst belegen, dass zsh die Datei wirklich annimmt.
        check = subprocess.run(["zsh", "-n", str(zshrc)],
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Registered wrapper", result.stdout)
        self.assertTrue(self._loads_the_wrapper())
        # Und der zweite Lauf erkennt die eigene Zeile wieder.
        again = self._install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Already installed", again.stdout)

    def test_a_sublist_terminator_is_always_a_command_separator(self):
        """`ends_sublist` muss eine Teilmenge von `is_command_separator` sein.

        Beide beantworten verschiedene Fragen — "beginnt hier ein neues
        Kommando?" gegen "endet hier die Teilliste?" —, aber jeder Trenner, der
        eine Teilliste beendet, beginnt zwangslaeufig auch ein neues Kommando.
        Laufen die Listen auseinander, endet ein Kurzform-Rumpf an einem Wort,
        das der Rest des Scanners noch fuer ein Argument haelt.
        """
        script = (
            'source <(sed -n "/^is_assignment_word()/,/^# Nur echte, lexikalisch/p" %s'
            ' | sed "\\$d")\n'
            'for w in ";" ";;" ";&" ";|" "&" "&!" "&|" "&&" "||" "|" "|&" "x" "do"; do\n'
            '  ends_sublist "$w" && e=1 || e=0\n'
            '  is_command_separator "$w" && s=1 || s=0\n'
            '  print -r -- "$w $e $s"\n'
            'done\n'
            % shlex.quote(str(self.repo / "install.sh"))
        )
        result = subprocess.run(["zsh", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        beendend = []
        for line in result.stdout.splitlines():
            word, ends, sep = line.rsplit(" ", 2)
            if ends == "1":
                beendend.append(word)
                self.assertEqual(sep, "1", f"{word!r} beendet, trennt aber nicht")
        # Die verbindenden Trenner duerfen NICHT beenden, sonst risse der Rumpf
        # von `for x (a b) print $x && print y` mitten auseinander.
        for word in ("&&", "||", "|", "|&"):
            self.assertNotIn(word, beendend)
        self.assertIn(";", beendend)

    def test_the_short_loop_forms_do_not_block_the_installation(self):
        """zsh-Kurzformen: Der Rumpf endet nach EINER Teilliste, ohne `done`.

        Der Blockstapel blieb bei allen diesen Formen bis zum Dateiende offen,
        und der Installer brach mit "unclosed or unsupported shell block" ab —
        bei Dateien, die zsh anstandslos ausfuehrt (Fund 2026-08-29).
        """
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        formen = (
            "for x (a b) print -r -- $x",              # Klammerliste
            "for x (a b) { print -r -- $x }",          # Klammerrumpf
            "for x (a b)\n  print -r -- $x",           # Rumpf auf der Folgezeile
            "for ((i = 0; i < 2; i++)) print -r -- $i",  # arithmetischer Kopf
            "for x in a b; print -r -- $x",            # in-Fassung mit ;
            "repeat 2 print -r -- hi",                 # repeat mit Anzahl
            "repeat 2\n  print -r -- hi",              # dito, Rumpf danach
            "for x (a b) print -r -- $x && print -r -- y",   # && gehoert dazu
            "for x (a b) {\n  print -r -- $x\n}",       # Klammerrumpf mehrzeilig
            "for x (a b)\n{\n  print -r -- $x\n}",     # Klammer erst danach
        )
        for form in formen:
            with self.subTest(form=form.replace("\n", " ⏎ ")):
                zshrc.write_text(f"{form}\nsource -- {wrapper}\n")
                # Erst belegen, dass zsh die Datei wirklich ausfuehrt.
                self.assertTrue(self._loads_the_wrapper())

                result = self._install()

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Already installed", result.stdout)

    def test_short_for_and_select_without_in_have_a_complete_head(self):
        """`for x;` und `select x;` sind gueltige zsh-Kurzformen ohne `in`.

        Der Trenner direkt hinter dem Variablennamen beendet ihren Kopf. Der
        Installer hielt ihn bisher fuer offen und lehnte die wirksame
        Registrierung auf der Folgezeile als unbelegbar ab.
        """
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        for keyword in ("for", "select"):
            with self.subTest(keyword=keyword):
                content = f"{keyword} x; print -r -- $x\nsource -- {wrapper}\n"
                zshrc.write_text(content)
                syntax = subprocess.run(
                    ["zsh", "-n", str(zshrc)], capture_output=True, text=True)
                self.assertEqual(syntax.returncode, 0, syntax.stderr)

                result = self._install()

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Already installed", result.stdout)
                self.assertEqual(zshrc.read_text(), content)

    def test_closed_short_loop_braces_do_not_hide_a_later_source(self):
        """Ein belegter Klammerrumpf darf den Top-Level-Suffix nicht sperren."""
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        forms = (
            f"for x (a b) {{ print -r -- $x }}; source -- {wrapper}\n",
            f"for x (a b)\n{{ print -r -- $x }}\nsource -- {wrapper}\n",
            f"for x (a b)\nprint -r -- $x; source -- {wrapper}\n",
        )
        for content in forms:
            with self.subTest(content=content.replace("\n", " ⏎ ")):
                zshrc.write_text(content)
                self.assertTrue(self._loads_the_wrapper())

                result = self._install()

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Already installed", result.stdout)
                self.assertEqual(zshrc.read_text(), content)

    def test_the_long_forms_of_the_same_keywords_stay_blocks(self):
        """Die lange Fassung darf durch die Kurzform-Erkennung nicht aufgehen.

        Sonst gaelte ein `source` INNERHALB einer Schleife als Registrierung —
        obwohl es bei leerer Wortliste nie ausgefuehrt wird.
        """
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        formen = (
            f"for x in a b\ndo\n  source -- {wrapper}\ndone",
            f"for x (a b)\ndo\n  source -- {wrapper}\ndone",
            f"repeat 2\ndo\n  source -- {wrapper}\ndone",
            f"for x (a b) {{\n  source -- {wrapper}\n}}",
        )
        for form in formen:
            with self.subTest(form=form.replace("\n", " ⏎ ")):
                zshrc.write_text(form + "\n")
                result = self._install()

                self.assertEqual(result.returncode, 0, result.stderr)
                # Der Wrapper wird zwar geladen, aber nur aus dem Schleifenrumpf
                # heraus — als Beleg fuer eine Registrierung zaehlt das nicht.
                self.assertIn("Registered wrapper", result.stdout)

    def test_a_return_in_a_short_loop_body_still_aborts_the_file(self):
        """`for x (a b) return` bricht die .zshrc ab — alles danach ist tot.

        Der Rumpf einer Kurzform beginnt an einem Kommandoanfang. Ohne diese
        Marke stand der Scanner dort noch auf "mitten im Kommando", übersah das
        `return` und erklärte eine spätere source-Zeile für wirksam — der
        Installer meldete "Already installed", während `gmf` nie entsteht
        (Fund 2026-08-29 beim Nachbuchen).
        """
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        rumpf = (
            "for x (a b) {stopper}",              # gewoehnlicher Rumpf
            "for x (a b) ! {stopper}",            # Negation ist nur ein Praefix
            "repeat 2; {stopper}",                # Trenner vor dem Rumpf
            "for x (a b) {{ {stopper} }}",        # Klammerrumpf
            "for x (a b)\n{stopper}",             # Rumpf auf der Folgezeile
        )
        for stopper in ("return", "exit 0"):
          for form in rumpf:
            with self.subTest(stopper=stopper, form=form):
                zshrc.write_text(
                    form.format(stopper=stopper) + f"\nsource -- {wrapper}\n")
                # Belegen, dass die Datei den Wrapper wirklich nicht lädt.
                self.assertFalse(self._loads_the_wrapper())

                result = self._install()

                self.assertEqual(result.returncode, 1)
                self.assertIn("cannot prove a top-level registration",
                              result.stderr)

    def test_a_while_condition_is_not_treated_as_a_short_loop_head(self):
        """`while false; print x` sieht wie eine Kurzform aus und ist keine.

        Die Bedingung ist eine LISTE; das `;` beendet sie nicht. Die Zeile
        laeuft endlos, eine folgende source-Zeile wird nie erreicht. Wer das
        `;` fuer die Kopfgrenze hielte, erklaerte die Zeile fuer abgeschlossen
        und meldete "Already installed", ohne dass `gmf` je entsteht.
        """
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        zshrc.write_text(f"while false; print -r -- x\nsource -- {wrapper}\n")

        result = self._install()

        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot prove a top-level registration", result.stderr)

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
        """`source` hinter einer Fortsetzung ist ein Argument, kein Kommando.

        Die beiden Zeilen sind für zsh EINE: `print ignored source -- …`. Das
        `source` gehört dort zu `print` und lädt nichts. Der Installer darf es
        deshalb nicht als vorhandene Registrierung zählen — und trägt seine
        eigene an.
        """
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        content = f"print ignored \\\nsource -- {wrapper}\n"
        zshrc.write_text(content)

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Already installed", result.stdout)
        self.assertTrue(zshrc.read_text().startswith(content))
        appended = zshrc.read_text()[len(content):]
        self.assertIn("source -- ", appended)
        self.assertIn(str(self.repo / "gmf.zsh"), appended)
        # Der zweite Lauf erkennt genau diese Zeile wieder.
        self.assertIn("Already installed", self._install().stdout)

    def test_a_plain_continuation_line_never_blocks_the_installation(self):
        """Eine Zeilenfortsetzung ist keine unklare Syntax.

        Bis 2026-09-03 galt der Backslash am Zeilenende wie ein offener Quote:
        Der Kontextscanner meldete dauerhaft `opaque`, und der Installer lehnte
        eine völlig gewöhnliche .zshrc mit „cannot prove a top-level
        registration" ab — `zsh -n` akzeptiert sie klaglos.
        """
        zshrc = self.home / ".zshrc"
        content = "export GMF_TEST_PATH=/a:\\\n/b\n"
        zshrc.write_text(content)

        result = self._install()
        loaded = subprocess.run(
            ["zsh", "-c", 'source "$ZDOTDIR/.zshrc"; whence -w gmf;'
             ' print -r -- "$GMF_TEST_PATH"'],
            capture_output=True, text=True,
            env=dict(os.environ, HOME=str(self.home), ZDOTDIR=str(self.home)))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gmf: function", loaded.stdout)
        # Die Fortsetzung wird wie von zsh ohne Trennzeichen zusammengesetzt.
        self.assertIn("/a:/b", loaded.stdout)
        self.assertTrue(zshrc.read_text().startswith(content))

    def test_a_file_ending_in_a_continuation_is_still_scanned(self):
        """Die letzte Zeile verfällt nicht, nur weil ihr Backslash ins Leere zeigt."""
        wrapper = shlex.quote(str(self.repo / "gmf.zsh"))
        zshrc = self.home / ".zshrc"
        zshrc.write_text(f"source -- {wrapper} \\")

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already installed", result.stdout)

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
