import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from gmf_test_support import FakeScreen, git, latest_stash, stash_preview

import gitmaster_flash as gmf_module
from gitmaster_flash import (
    DEFAULT_CONFIG,
    TUI,
    RepoStatus,
    collect_status,
    commit_selected,
    file_diff,
    repo_info_lines,
    status_dict,
)


class NonInteractiveGitTests(unittest.TestCase):
    """Git darf nie nach Zugangsdaten fragen — sonst zerlegt der Prompt die TUI."""

    @staticmethod
    def _deny_server():
        """Lokaler HTTP-Server, der jede Anfrage mit 401 + Basic-Auth abweist.

        Damit lässt sich der Login-Fall ohne Netz und ohne echten Host testen:
        Git fragt genau hier nach Username/Passwort.
        """

        class Deny(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="test"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Deny)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def _serving(self):
        """Server starten und am Testende wieder abbauen (Socket inklusive)."""
        srv = self._deny_server()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d" % srv.server_address[1]

    def test_fetch_fails_fast_instead_of_asking_for_a_username(self):
        base = self._serving()
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "faux", base + "/x.git")
            started = time.monotonic()
            r = gmf_module.run_git(repo, "fetch", "--prune", "--quiet", "--", "faux", timeout=30)
            elapsed = time.monotonic() - started
        self.assertNotEqual(r.returncode, 0)
        # Kein Warten auf eine Eingabe, die niemals kommt.
        self.assertLess(elapsed, 15)
        # `keychain_session` festnageln, damit der Test den Auth-Pfad prüft und
        # nicht an der Sitzung hängt: Über ssh (etwa der install.sh-Selbsttest
        # auf einem anderen Mac) würde aus "auth" sonst "nokeychain".
        with mock.patch.object(gmf_module, "keychain_session", return_value=True):
            self.assertTrue(gmf_module.credentials_missing(r), r.stderr)

    def _repo_with_denying_remotes(self, root: Path, *names: str) -> Path:
        base = self._serving()
        repo = root / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "t@example.invalid")
        git(repo, "config", "user.name", "T")
        (repo / "a").write_text("a")
        git(repo, "add", "a")
        git(repo, "commit", "-qm", "base")
        for name in names:
            git(repo, "remote", "add", name, "%s/%s.git" % (base, name))
        return repo

    def test_fetch_all_names_every_remote_that_wants_a_login(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repo_with_denying_remotes(root, "faux", "zwei")
            # Sitzung festnageln (siehe oben): Die Klassifikation läuft hier
            # INNERHALB von collect_status, deshalb um den Aufruf herum.
            with mock.patch.object(gmf_module, "keychain_session", return_value=True):
                st = collect_status(repo, root, DEFAULT_CONFIG, fetch=True)
        self.assertEqual(st.remote_state, "error")
        self.assertIn("faux", st.error)
        self.assertIn("zwei", st.error)
        self.assertIn("login", st.error.lower())

    def test_fetch_all_names_the_single_remote_too(self):
        # Bei genau einem Remote nennt Git den Namen nicht — die Meldung trotzdem.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repo_with_denying_remotes(root, "faux")
            with mock.patch.object(gmf_module, "keychain_session", return_value=True):
                st = collect_status(repo, root, DEFAULT_CONFIG, fetch=True)
        self.assertEqual(st.remote_state, "error")
        self.assertIn("faux", st.error)
        self.assertIn("login", st.error.lower())

    def test_failing_fetch_marks_the_remote_instead_of_only_the_repo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repo_with_denying_remotes(root, "faux", "zwei")
            # Ein drittes, lokales Remote muss unberührt bleiben.
            spare = root / "spare.git"
            git(root, "init", "-q", "--bare", str(spare))
            git(repo, "remote", "add", "lokal", str(spare))
            st = collect_status(repo, root, DEFAULT_CONFIG, fetch=True)
        failed = {r.name for r in st.remotes if r.fetch_failed}
        self.assertEqual(failed, {"faux", "zwei"})
        badges = {r.name: r.badge() for r in st.remotes}
        self.assertTrue(badges["faux"].startswith("✘"), badges)
        self.assertNotIn("✘", badges["lokal"])
        self.assertTrue(status_dict(st)["remotes"][0].get("fetch_failed") is not None)

    def test_mixed_fetch_failures_keep_their_own_causes(self):
        """Einzelne Fetches dürfen Ursachen nicht zwischen Remotes mischen."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repo_with_denying_remotes(root, "login")
            git(repo, "remote", "add", "gone", str(root / "missing.git"))
            valid = root / "valid.git"
            git(root, "init", "-q", "--bare", str(valid))
            git(repo, "remote", "add", "valid", str(valid))
            with mock.patch.object(gmf_module, "keychain_session", return_value=True):
                st = collect_status(repo, root, DEFAULT_CONFIG, fetch=True)

        remotes = {remote.name: remote for remote in st.remotes}
        self.assertEqual(remotes["login"].fetch_outcome, "auth")
        self.assertEqual(remotes["gone"].fetch_outcome, "gone")
        self.assertFalse(remotes["valid"].fetch_failed)
        self.assertIn("login", remotes["login"].fetch_error_long.lower())
        self.assertIn("no repository", remotes["gone"].fetch_error_long.lower())
        payloads = {remote["name"]: remote for remote in status_dict(st)["remotes"]}
        self.assertEqual(payloads["login"]["fetch_outcome"], "auth")
        self.assertEqual(payloads["gone"]["fetch_outcome"], "gone")

    def test_run_git_disables_prompts_and_keeps_caller_env(self):
        recorded = {}

        class FakePopen:
            """Nur so viel Popen, wie run_git benutzt: Kontextmanager + communicate."""

            def __init__(self, argv, **kwargs):
                recorded.update(kwargs)
                recorded["argv"] = argv
                self.returncode = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def communicate(self, timeout=None):
                return "", ""

        with mock.patch("gitmaster_flash.subprocess.Popen", FakePopen):
            gmf_module.run_git(Path("/tmp"), "status", env={"GIT_INDEX_FILE": "/tmp/i"})
        env = recorded["env"]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_ASKPASS"], "")
        self.assertEqual(env["LC_ALL"], "C")
        # Der Aufrufer-Env (temporärer Index beim Commit) darf nicht verloren gehen.
        self.assertEqual(env["GIT_INDEX_FILE"], "/tmp/i")
        self.assertEqual(recorded["stdin"], subprocess.DEVNULL)
        self.assertTrue(recorded["start_new_session"])

    def test_the_graft_blindfold_does_not_add_a_hint_to_every_stderr(self):
        """Die leere Graft-Datei ist gmf-intern und darf nicht mitreden.

        Git haelt `GIT_GRAFT_FILE=/dev/null` fuer eine BENUTZTE Graft-Datei und
        schreibt bei jedem Befehl, der den Graph liest, acht Zeilen
        Veralterungshinweis auf stderr — auch bei jedem Fetch und jedem Push.
        Die Zeilen erklaeren nichts, standen aber mit im Beleg, aus dem gmf die
        Ursache eines gescheiterten Zugriffs nennt.
        """
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a.txt").write_text("a\n")
            git(repo, "add", "a.txt")
            git(repo, "commit", "-qm", "base")

            # Gegenprobe: Ohne den Schalter redet Git wirklich.
            noisy = subprocess.run(
                ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
                env={**os.environ, "GIT_GRAFT_FILE": os.devnull, "LC_ALL": "C"},
                capture_output=True,
                text=True,
            )
            self.assertIn("grafts", noisy.stderr)

            for env in (gmf_module.RAW_OBJECT_ENV, gmf_module.TRANSFER_OBJECT_ENV):
                with self.subTest(env=sorted(env)):
                    quiet = gmf_module.run_git(
                        repo, "rev-list", "--count", "HEAD", timeout=10, env=env
                    )
                    self.assertEqual(quiet.returncode, 0, quiet.stderr)
                    self.assertEqual(quiet.stderr, "")

    def test_readers_disable_optional_index_writes_and_fsmonitor_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            tracked = repo / "tracked.txt"
            tracked.write_text("base\n")
            git(repo, "add", "tracked.txt")
            git(repo, "commit", "-qm", "base")
            tracked.write_text("stashed\n")
            git(repo, "stash", "push", "-qm", "preview")
            marker = root / "fsmonitor-ran"
            hook = root / "fsmonitor-hook"
            hook.write_text(
                f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\nprintf 'test-token\\n'\n"
            )
            hook.chmod(0o755)
            git(repo, "config", "core.fsmonitor", str(hook))
            git(repo, "config", "core.fsmonitorHookVersion", "2")

            # Die Gegenprobe belegt, dass die Repo-Konfiguration ohne unseren
            # Guard tatsächlich ausführbaren Code starten würde.
            probe_env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
            probe = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                env=probe_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertTrue(marker.exists())
            marker.unlink()

            index = repo / ".git" / "index"
            before_bytes = index.read_bytes()
            before_mtime = index.stat().st_mtime_ns
            # Der neue Zeitstempel muss in einer ANDEREN Sekunde liegen als der
            # im Index zwischengespeicherte: Git vergleicht ihn sekundengenau,
            # erst ein Unterschied macht den Stat-Cache überhaupt veraltet. Mit
            # `os.utime(tracked, None)` entschied allein der Zufall, ob die
            # Sekunde während des Aufbaus umsprang — der Test schlug dadurch in
            # rund 30 % der Läufe fehl.
            stale = index.stat().st_mtime + 5
            os.utime(tracked, (stale, stale))

            # Scan, Stash-Vorschau und Info-Seite lassen den Index trotz des
            # veralteten Stat-Caches unangetastet; genau das leistet
            # GIT_OPTIONAL_LOCKS=0 für `git status`, `git stash show` und
            # `git ls-files`.
            status = collect_status(repo, root, DEFAULT_CONFIG)
            oid, _ = latest_stash(repo, 10)
            ok_stash, _ = stash_preview(repo, 10, oid)
            repo_info_lines(status, DEFAULT_CONFIG)

            self.assertTrue(ok_stash)
            self.assertFalse(marker.exists())
            self.assertEqual(index.read_bytes(), before_bytes)
            self.assertEqual(index.stat().st_mtime_ns, before_mtime)

            # `git diff` ist die eine Ausnahme: Es schreibt den aufgefrischten
            # Stat-Cache zurück, auch mit GIT_OPTIONAL_LOCKS=0. Verändert wird
            # dabei ausschließlich diese Zwischenspeicherung — Einträge, Modi
            # und Objekt-IDs des Index bleiben gleich, und der fsmonitor-Hook
            # läuft auch dabei nicht.
            #
            # Auch die Kontrollablesung muss `core.fsmonitor` abschalten, sonst
            # startet SIE den Hook und der Nachweis unten prüfte sich selbst.
            staged = ("-c", "core.fsmonitor=false", "ls-files", "--stage")
            staged_before = git(repo, *staged)
            ok_file, _ = file_diff(repo, "M", "tracked.txt", 10)
            self.assertTrue(ok_file)
            self.assertFalse(marker.exists())
            self.assertEqual(git(repo, *staged), staged_before)

    def test_readers_never_run_signature_verifier_from_log_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "tracked").write_text("base\n")
            git(repo, "add", "tracked")
            git(repo, "commit", "-qm", "base")
            parent = git(repo, "rev-parse", "HEAD")
            tree = git(repo, "rev-parse", "HEAD^{tree}")
            identity = "T <t@example.invalid> 0 +0000"
            raw_commit = (
                f"tree {tree}\nparent {parent}\nauthor {identity}\n"
                f"committer {identity}\n"
                "gpgsig -----BEGIN PGP SIGNATURE-----\n"
                " fake\n -----END PGP SIGNATURE-----\n\n"
                "synthetically signed\n"
            )
            signed = subprocess.run(
                ["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"],
                input=raw_commit,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            git(repo, "update-ref", "refs/heads/main", signed, parent)
            marker = root / "gpg-verifier-ran"
            verifier = root / "fake-gpg"
            verifier.write_text(f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\nexit 1\n")
            verifier.chmod(0o755)
            git(repo, "config", "gpg.program", str(verifier))
            git(repo, "config", "log.showSignature", "true")

            probe = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%H"], capture_output=True, text=True
            )
            self.assertTrue(marker.exists(), probe.stderr)
            marker.unlink()

            status = collect_status(repo, root, DEFAULT_CONFIG)
            repo_info_lines(status, DEFAULT_CONFIG)
            shown = gmf_module.run_git(repo, "show", "--format=%H", "--no-patch", "HEAD")

            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertFalse(marker.exists())

    def test_communicate_oserror_kills_git_and_ssh_process_groups(self):
        real_popen = subprocess.Popen

        class LostResultProcess:
            pids = []

            def __init__(self, _argv, **kwargs):
                # Ein wirklich lebendes Kind belegt, dass der Fehlerpfad nicht
                # bloß ein bereits beendetes Fake-Objekt aufräumt.
                self.proc = real_popen(
                    ["/bin/sleep", "30"],
                    stdin=kwargs.get("stdin"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=kwargs["start_new_session"],
                )
                self.first = True
                self.pids.append(self.proc.pid)

            @property
            def pid(self):
                return self.proc.pid

            @property
            def returncode(self):
                return self.proc.returncode

            def communicate(self, timeout=None):
                if self.first:
                    self.first = False
                    raise OSError("lost process result")
                return self.proc.communicate(timeout=timeout)

            def kill(self):
                return self.proc.kill()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.proc.__exit__(*args)

        for operation in (
            lambda: gmf_module.run_git(Path.cwd(), "status", timeout=10),
            lambda: gmf_module._run_process_group(
                ["ssh", "example"], stdin=subprocess.DEVNULL, timeout=10
            ),
        ):
            with (
                self.subTest(operation=operation),
                mock.patch("gitmaster_flash.subprocess.Popen", LostResultProcess),
                self.assertRaises(OSError),
            ):
                operation()
            pid = LostResultProcess.pids[-1]
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_logged_fetch_and_ref_update_keep_unclear_oserrors_in_history(self):
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for args in (
            ("fetch", "--", "example"),
            (*gmf_module.safe_update_ref_args("--no-deref", "refs/remotes/origin/main", "a" * 40),),
        ):
            with (
                self.subTest(args=args),
                mock.patch.object(gmf_module, "run_git", side_effect=OSError("lost")),
                self.assertRaises(OSError),
            ):
                gmf_module.run_git_logged(Path.cwd(), *args)
            self.assertTrue(gmf_module.COMMAND_LOG[-1].startswith("… "))
            self.assertIn(args[-1], gmf_module.COMMAND_LOG[-1])

    def test_run_git_ignores_foreign_repository_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = []
            for name in ("a", "b"):
                repo = root / name
                repo.mkdir()
                git(repo, "init", "-q", "-b", "main")
                git(repo, "config", "user.email", "t@example.invalid")
                git(repo, "config", "user.name", "T")
                (repo / "tracked").write_text(name + "\n")
                git(repo, "add", "tracked")
                git(repo, "commit", "-qm", name)
                repos.append(repo)
            oid_b = subprocess.run(
                ["git", "-C", str(repos[1]), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            poisoned_index = root / "foreign-index"
            shutil.copy2(repos[0] / ".git" / "index", poisoned_index)
            sabotage = {
                "GIT_DIR": str(repos[0] / ".git"),
                "GIT_WORK_TREE": str(repos[0]),
                "GIT_INDEX_FILE": str(poisoned_index),
            }
            with mock.patch.dict(os.environ, sabotage):
                result = gmf_module.run_git(repos[1], "rev-parse", "HEAD")
                self.assertEqual(result.stdout.strip(), oid_b)
                (repos[1] / "tracked").write_text("changed\n")
                committed = commit_selected(repos[1], ["tracked"], "only repo b", 10)
            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repos[0]), "log", "-1", "--format=%s"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "a",
            )

    def test_credentials_missing_only_for_login_errors(self):
        def result(stderr):
            return subprocess.CompletedProcess(["git"], 128, "", stderr)

        # `keychain_session` festnageln: Ohne das haengt der Test an der Sitzung,
        # in der die Suite laeuft — in der GUI-Sitzung gruen, ueber ssh rot. Genau
        # solche Geisterfehler will man nicht.
        with mock.patch.object(gmf_module, "keychain_session", return_value=True):
            self.assertTrue(
                gmf_module.credentials_missing(
                    result(
                        "fatal: could not read Username for 'https://github.com': "
                        "terminal prompts disabled"
                    )
                )
            )
            self.assertTrue(
                gmf_module.credentials_missing(
                    result("git@github.com: Permission denied (publickey).")
                )
            )
            self.assertFalse(
                gmf_module.credentials_missing(result("fatal: couldn't find remote ref main"))
            )

    def test_auth_error_without_keychain_is_its_own_cause(self):
        """Dieselbe Git-Meldung, zwei sehr verschiedene Ursachen. In einer Sitzung
        ohne Schluesselbund (ssh, LaunchDaemon, cron) fehlen keine Zugangsdaten —
        sie sind nur nicht lesbar. "Login fehlt" schickte am 2026-08-05 in die
        falsche Richtung: Eine erneute Anmeldung würde empfohlen, obwohl alles in
        Ordnung war."""
        r = subprocess.CompletedProcess(
            ["git"],
            128,
            "",
            ("fatal: could not read Username for 'https://github.com': terminal prompts disabled"),
        )
        with mock.patch.object(gmf_module, "keychain_session", return_value=False):
            # Ohne positiven Helper-Nachweis ist das ein echter Auth-Fehler.
            self.assertEqual(gmf_module.classify_remote_check(r), "auth")
            self.assertEqual(
                gmf_module.classify_remote_check(r, keychain_helper=True), "nokeychain"
            )
            # Und es gilt ausdruecklich NICHT als fehlender Login.
            self.assertFalse(gmf_module.credentials_missing(r, keychain_helper=True))
            satz = gmf_module.remote_check_message("github", "nokeychain", 0, "", 30)
            self.assertIn("github", satz)
        with mock.patch.object(gmf_module, "keychain_session", return_value=True):
            self.assertEqual(gmf_module.classify_remote_check(r), "auth")

    def test_keychain_helper_must_be_effective_for_the_remote_url(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "origin", "https://example.invalid/org/repo.git")
            with mock.patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            ):
                self.assertFalse(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))
                git(repo, "config", "credential.helper", "osxkeychain")
                self.assertTrue(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))

    def test_keychain_remote_read_uses_the_callers_timeout(self):
        with mock.patch.object(gmf_module, "read_remote_configs", return_value={}) as read:
            self.assertFalse(gmf_module.remote_uses_keychain_helper(Path("/unused"), "origin", 3))
        self.assertEqual(read.call_args.args[1]["git_timeout"], 3)

    def test_keychain_diagnosis_fails_closed_on_secondary_read_errors(self):
        for error in (
            subprocess.TimeoutExpired(["git", "remote"], 1),
            ValueError("invalid URL port"),
            RuntimeError("unknown home directory"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(gmf_module, "read_remote_configs", side_effect=error),
            ):
                self.assertFalse(
                    gmf_module.remote_uses_keychain_helper(Path("/unused"), "origin", 1)
                )

    def test_keychain_match_timeout_does_not_replace_the_transfer_error(self):
        remote = gmf_module.RemoteConfig(
            "origin",
            ["https://example.invalid/repo.git"],
            ["https://example.invalid/repo.git"],
            [],
            [],
            settings=[],
        )
        with (
            mock.patch.object(gmf_module, "read_remote_configs", return_value={"origin": remote}),
            mock.patch.object(
                gmf_module, "run_git", side_effect=subprocess.TimeoutExpired(["git", "config"], 1)
            ),
        ):
            self.assertFalse(gmf_module.remote_uses_keychain_helper(Path("/unused"), "origin", 1))

    def test_push_checks_the_helper_for_the_push_url(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "origin", "git@github.com:example/repo.git")
            git(
                repo, "remote", "set-url", "--push", "origin", "https://github.com/example/repo.git"
            )
            with mock.patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            ):
                git(repo, "config", "credential.https://github.com.helper", "osxkeychain")
                remote = gmf_module.read_remote_configs(repo, DEFAULT_CONFIG)["origin"]
                self.assertTrue(remote.transfer_safe)
                self.assertFalse(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))
                self.assertTrue(
                    gmf_module.remote_uses_keychain_helper(repo, "origin", 10, for_push=True)
                )

    def test_keychain_probe_never_puts_a_secret_url_in_argv(self):
        secret_url = "https://" + "user:" + "token@" + "example.invalid/repo.git?token=query"
        remote = gmf_module.RemoteConfig("origin", [secret_url], [secret_url], [], [], settings=[])
        with (
            mock.patch.object(gmf_module, "read_remote_configs", return_value={"origin": remote}),
            mock.patch.object(gmf_module, "run_git") as run,
        ):
            self.assertFalse(gmf_module.remote_uses_keychain_helper(Path("/unused"), "origin", 10))
            self.assertFalse(
                gmf_module.remote_uses_keychain_helper(Path("/unused"), "origin", 10, for_push=True)
            )
        run.assert_not_called()

    def test_external_remote_helper_is_never_used_as_a_keychain_match_argv(self):
        helper_url = "ext::helper credential=" + "opaque-value"
        remote = gmf_module.RemoteConfig("origin", [helper_url], [helper_url], [], [], settings=[])
        with (
            mock.patch.object(gmf_module, "read_remote_configs", return_value={"origin": remote}),
            mock.patch.object(gmf_module, "run_git") as run,
        ):
            self.assertFalse(gmf_module.remote_uses_keychain_helper(Path("/unused"), "origin", 10))
        run.assert_not_called()

    def test_keychain_helper_name_must_not_be_a_substring(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "origin", "https://example.invalid/org/repo.git")
            with mock.patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            ):
                git(
                    repo, "config", "credential.helper", "store --file=/tmp/osxkeychain-credentials"
                )
                self.assertFalse(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))

    def test_keychain_helper_must_be_the_exact_osxkeychain_token(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "origin", "https://example.invalid/org/repo.git")
            with mock.patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            ):
                for helper in ("/tmp/osxkeychain", "!osxkeychain", "OSXKEYCHAIN"):
                    with self.subTest(helper=helper):
                        git(repo, "config", "credential.helper", helper)
                        self.assertFalse(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))

    def test_gh_keychain_helper_must_be_an_executable_command(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "origin", "https://example.invalid/org/repo.git")
            with mock.patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            ):
                git(repo, "config", "credential.helper", "gh auth git-credential")
                with mock.patch("gitmaster_flash.shutil.which", return_value="/usr/local/bin/gh"):
                    self.assertFalse(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))

                git(repo, "config", "credential.helper", "!gh auth git-credential")
                with mock.patch("gitmaster_flash.shutil.which", return_value="/usr/local/bin/gh"):
                    self.assertTrue(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))
                with mock.patch("gitmaster_flash.shutil.which", return_value=None):
                    self.assertFalse(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))

                absolute = repo / "gh"
                absolute.write_text("#!/bin/sh\nexit 0\n")
                absolute.chmod(0o755)
                git(repo, "config", "credential.helper", f"{absolute} auth git-credential")
                self.assertTrue(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))

                git(repo, "config", "credential.helper", "!gh AUTH GIT-CREDENTIAL")
                with mock.patch("gitmaster_flash.shutil.which", return_value="/usr/local/bin/gh"):
                    self.assertFalse(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))
                upper = repo / "GH"
                upper.write_text("#!/bin/sh\nexit 0\n")
                upper.chmod(0o755)
                git(repo, "config", "credential.helper", f"{upper} auth git-credential")
                self.assertFalse(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))

                git(repo, "config", "credential.helper", "!gh auth git-credential extra")
                with mock.patch("gitmaster_flash.shutil.which", return_value="/usr/local/bin/gh"):
                    self.assertFalse(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))

    def test_only_the_primary_fetch_url_can_explain_a_keychain_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "origin", "https://one.invalid/org/repo.git")
            git(repo, "remote", "set-url", "--add", "origin", "https://two.invalid/org/repo.git")
            with mock.patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            ):
                git(repo, "config", "credential.https://two.invalid.helper", "osxkeychain")
                self.assertFalse(gmf_module.remote_uses_keychain_helper(repo, "origin", 10))

    def test_a_rejected_ssh_key_is_never_excused_as_a_keychain_problem(self):
        """Gegenprobe zum Fall darueber: "Permission denied (publickey)" kommt von
        SSH und nicht von einem Credential-Helper. Ohne Helfer ist auch kein
        Schluesselbund im Spiel — die Meldung "Login ist in Ordnung, nur aus dieser
        Sitzung nicht messbar" schickte die Fehlersuche in die falsche Richtung,
        obwohl der Schluessel wirklich fehlt oder abgelehnt wird."""
        r = subprocess.CompletedProcess(
            ["git"],
            128,
            "",
            (
                "git@example.com: Permission denied (publickey).\n"
                "fatal: Could not read from remote repository."
            ),
        )
        for sitzung in (True, False):
            with self.subTest(keychain=sitzung):
                with mock.patch.object(gmf_module, "keychain_session", return_value=sitzung):
                    self.assertEqual(gmf_module.classify_remote_check(r), "auth")
                    self.assertTrue(gmf_module.credentials_missing(r))

    def test_keychain_session_only_true_in_gui_session(self):
        """`launchctl managername` nennt den Unterschied: "Aqua" ist die GUI-Sitzung,
        alles andere lebt daneben. Ausserhalb von macOS gibt es das Problem nicht."""

        def lauf(name):
            return subprocess.CompletedProcess(["launchctl"], 0, name + "\n", "")

        for ausgabe, platform, erwartet in (
            ("Aqua", "darwin", True),
            ("Background", "darwin", False),
            ("System", "darwin", False),
            ("Background", "linux", True),
        ):
            with self.subTest(ausgabe=ausgabe, platform=platform):
                gmf_module._KEYCHAIN_SESSION = None
                with (
                    mock.patch.object(gmf_module.sys, "platform", platform),
                    mock.patch.object(gmf_module.subprocess, "run", return_value=lauf(ausgabe)),
                ):
                    self.assertIs(gmf_module.keychain_session(), erwartet)
        # Faellt der Aufruf selbst aus, gilt der Schluesselbund als erreichbar —
        # lieber einen echten Auth-Fehler zeigen als ihn stillschweigend entschuldigen.
        gmf_module._KEYCHAIN_SESSION = None
        with (
            mock.patch.object(gmf_module.sys, "platform", "darwin"),
            mock.patch.object(gmf_module.subprocess, "run", side_effect=OSError("weg")),
        ):
            self.assertIs(gmf_module.keychain_session(), True)
        # Dasselbe fuer einen Aufruf, der zwar startet, aber mit Fehler endet:
        # Sein leeres stdout ist nicht "nicht Aqua", sondern gar keine Antwort.
        # Der Wert wird gemerkt — sonst gaelte fuer den REST des Prozesses jeder
        # Auth-Fehler als entschuldigt.
        gmf_module._KEYCHAIN_SESSION = None
        with (
            mock.patch.object(gmf_module.sys, "platform", "darwin"),
            mock.patch.object(
                gmf_module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["launchctl"], 1, "", "nope"),
            ),
        ):
            self.assertIs(gmf_module.keychain_session(), True)
        gmf_module._KEYCHAIN_SESSION = None

    def test_a_pinned_transfer_ignores_repo_and_inherited_ssh_wrappers(self):
        """Die gepinnte URL bindet die Adresse — der Transportweg gehört dazu.

        Ein `core.sshCommand` aus der Repo-Konfiguration und ein geerbtes
        GIT_SSH_COMMAND bekommen Host und Pfad zwar als Argumente, müssen sich
        aber nicht daran halten: Ein solcher Wrapper kann eine ganz andere
        Gegenstelle ansprechen, und der als zielgebunden bestätigte Transfer
        landete woanders. Für einen gebundenen Aufruf darf deshalb nur das
        gewöhnliche `ssh` laufen.
        """
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            marker = root / "wrapper-ran"
            wrapper = root / "fake-ssh"
            wrapper.write_text(f"#!/bin/sh\nprintf ran >> {shlex.quote(str(marker))}\nexit 1\n")
            wrapper.chmod(0o755)
            git(repo, "config", "core.sshCommand", str(wrapper))
            # Port 1 nimmt niemand an: Das echte ssh scheitert sofort, ohne Netz.
            url = "ssh://127.0.0.1:1/x.git"
            environment = {"GIT_SSH_COMMAND": str(wrapper), "GIT_SSH": str(wrapper)}

            pinned = gmf_module._PinnedRemoteURL(url)
            with mock.patch.dict(os.environ, environment):
                bound = gmf_module.run_git(repo, "ls-remote", "--heads", "--", pinned, timeout=30)
            self.assertNotEqual(bound.returncode, 0)
            self.assertFalse(
                marker.exists(), "ein Wrapper entschied über das Ziel eines gebundenen Transfers"
            )

            # Gegenprobe: Ohne Bindung greift der Wrapper weiterhin. Nur so ist
            # belegt, dass oben wirklich die Bindung gewirkt hat.
            with mock.patch.dict(os.environ, environment):
                gmf_module.run_git(repo, "ls-remote", "--heads", "--", url, timeout=30)
            self.assertTrue(marker.exists())

    def test_a_pinned_transfer_ignores_an_inherited_git_proxy_command(self):
        """Dasselbe fuer das git://-Protokoll.

        GIT_PROXY_COMMAND ist das Gegenstueck zu GIT_SSH_COMMAND: Git ruft das
        Programm mit Host und Port auf, und es entscheidet, wohin die
        Verbindung wirklich geht. Es fehlte in TRANSPORT_GIT_ENV, und der
        Wrapper lief auch bei einem gebundenen Fetch (Review-Fund 2026-09-03).
        """
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            marker = root / "proxy-ran"
            wrapper = root / "fake-proxy"
            wrapper.write_text(f"#!/bin/sh\nprintf ran >> {shlex.quote(str(marker))}\nexit 7\n")
            wrapper.chmod(0o755)
            url = "git://127.0.0.1/x.git"
            environment = {"GIT_PROXY_COMMAND": str(wrapper)}

            pinned = gmf_module._PinnedRemoteURL(url)
            with mock.patch.dict(os.environ, environment):
                bound = gmf_module.run_git(repo, "ls-remote", "--heads", "--", pinned, timeout=30)
            self.assertNotEqual(bound.returncode, 0)
            self.assertFalse(
                marker.exists(),
                "ein Proxy-Wrapper entschied über das Ziel eines gebundenen Transfers",
            )

            # Gegenprobe: Ohne Bindung behalten die Lesebefehle des Scans die
            # Benutzerumgebung — sonst belegt der Test oben nichts.
            with mock.patch.dict(os.environ, environment):
                gmf_module.run_git(repo, "ls-remote", "--heads", "--", url, timeout=30)
            self.assertTrue(marker.exists())


class ErrorRedactionTests(unittest.TestCase):
    """Git zitiert in Fehlermeldungen die komplette URL — inklusive Login/Query.

    Diese Zeilen erscheinen in TUI, Info-Seite und (als error_long) in --json;
    Tokens müssen deshalb schon an der Eingangsgrenze verschwinden.
    """

    @staticmethod
    def _result(stderr: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 128, "", stderr)

    def test_a_hint_line_never_becomes_the_reported_cause(self):
        """`hint:` ist Gits Ratgeberzeile, nie die Ursache — und steht oft zuletzt.

        Nach einem abgelehnten Push haengt Git seinen Ratschlag hinter die
        Fehlermeldung. Als Beleg genommen verdraengte er die eigentliche
        Ursache. Dank LC_ALL=C ist das Praefix stabil englisch.
        """
        result = self._result(
            "error: failed to push some refs to 'example.invalid/repo.git'\n"
            "hint: Updates were rejected because the remote contains work\n"
            "hint: that you do not have locally.\n"
        )
        self.assertEqual(
            gmf_module.last_error_line(result),
            "error: failed to push some refs to 'example.invalid/repo.git'",
        )

    def test_only_hints_still_yield_something_rather_than_nothing(self):
        """Gegenprobe: Bleibt nach dem Filtern nichts uebrig, gilt die letzte Zeile."""
        result = self._result("hint: see 'git help push'\n")
        self.assertEqual(gmf_module.last_error_line(result), "hint: see 'git help push'")
        self.assertEqual(gmf_module.last_error_line(self._result("")), "")

    def test_http_userinfo_query_and_fragment_are_removed(self):
        stderr = (
            "fatal: unable to access "
            "'https://user:s3cr3t@example.com/org/repo.git?private_token=abc123#frag': "
            "The requested URL returned error: 403"
        )
        line = gmf_module.last_error_line(self._result(stderr))
        self.assertNotIn("s3cr3t", line)
        self.assertNotIn("abc123", line)
        self.assertNotIn("user:", line)
        # Adresse und Ursache bleiben als Beleg erhalten.
        self.assertIn("example.com/org/repo.git", line)
        self.assertIn("403", line)

    def test_ssh_user_remains_part_of_the_address(self):
        stderr = "fatal: Could not read from remote repository ssh://git@example.com/x.git"
        line = gmf_module.last_error_line(self._result(stderr))
        self.assertIn("git@example.com", line)

    def test_password_disappears_under_every_scheme(self):
        """display_remote_url() wirft das Passwort bei JEDEM Schema weg; die
        Fehlerzeile muss dieselbe Politik fahren, sonst steht ein
        `ssh://user:passwort@host` weiter in error_long und in --json."""
        for url in (
            "ssh://user:s3cr3t@example.com/x.git",
            "git://user:s3cr3t@example.com/x.git",
            "ftps://user:s3cr3t@example.com/x.git",
        ):
            with self.subTest(url=url):
                line = gmf_module.last_error_line(self._result(f"fatal: unable to access '{url}'"))
                self.assertNotIn("s3cr3t", line)
                if url.startswith("ssh" + "://"):
                    self.assertIn("user@example.com", line)
                else:
                    self.assertNotIn("user@", line)
                # Und die Anzeige-Seite sagt dasselbe.
                self.assertNotIn("s3cr3t", gmf_module.display_remote_url(url))

    def test_a_scheme_with_punctuation_still_loses_its_password(self):
        """Git-Remote-Helper duerfen "+", "-" und "." im Schema tragen
        (`git+ssh://`, `foo+://`). Die Redaktion erkannte vorher nur \\w+ vor dem
        "://" — endete das Schema auf einem Satzzeichen, blieb das Passwort in
        error_long stehen und damit auch in der --json-Ausgabe."""
        for url in (
            "git+ssh://user:s3cr3t@example.com/x.git",
            "foo+://user:s3cr3t@example.com/x.git",
            "my-helper://user:s3cr3t@example.com/x.git",
        ):
            with self.subTest(url=url):
                line = gmf_module.last_error_line(self._result(f"fatal: unable to access '{url}'"))
                self.assertNotIn("s3cr3t", line)
                if url.startswith("git+ssh" + "://"):
                    self.assertIn("user@example.com", line)
                else:
                    self.assertNotIn("user@", line)
        # Query und Fragment fallen unter denselben Schemata ebenfalls weg.
        line = gmf_module.last_error_line(
            self._result("fatal: unable to access 'foo+://example.com/x.git?token=abc123'")
        )
        self.assertNotIn("abc123", line)

    def test_a_port_is_not_mistaken_for_a_password(self):
        stderr = "fatal: unable to access 'ssh://git@example.com:2222/x.git'"
        line = gmf_module.last_error_line(self._result(stderr))
        self.assertIn("git@example.com:2222/x.git", line)

    def test_plain_messages_pass_unchanged(self):
        stderr = "ssh: connect to host example.com port 22: Connection refused"
        self.assertEqual(gmf_module.last_error_line(self._result(stderr)), stderr)


class GitCancelTests(unittest.TestCase):
    """Abbruch laufender Git-Aufrufe — die Grundlage des Hintergrund-Fetch.

    Wird die Oberflaeche waehrend eines Fetch beendet, darf kein git und kein
    davon gestartetes ssh weiterlaufen.
    """

    def setUp(self):
        self.scope = gmf_module.GitCallScope()
        self.addCleanup(self.scope.cancel)

    def test_a_running_call_is_killed_and_its_whole_group_with_it(self):
        # Ein Kommando, das selbst ein langlebiges Enkelkind in derselben Gruppe
        # startet — genau die Konstellation git + ssh.
        script = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen(['/bin/sleep', '60'])\n"
            "sys.stdout.write(str(child.pid) + chr(10))\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)\n"
        )
        started = threading.Event()
        result = {}

        def call():
            started.set()
            try:
                result["run"] = self.scope.call(
                    gmf_module._run_process_group,
                    [sys.executable, "-c", script],
                    stdin=subprocess.DEVNULL,
                    timeout=60,
                )
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        started.wait(5)
        # Warten, bis der Prozess wirklich registriert ist.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.scope.processes:
            time.sleep(0.02)
        self.assertTrue(self.scope.processes, "Prozess wurde nie registriert")

        self.scope.cancel()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "Der Aufruf haengt trotz Abbruch")
        # Das Enkelkind darf den Abbruch nicht ueberleben.
        grandchild = int((result.get("run").stdout if result.get("run") else "0").strip() or 0)
        if grandchild:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild, 0)
                except OSError:
                    break
                time.sleep(0.05)
            else:
                os.kill(grandchild, signal.SIGKILL)
                self.fail("Enkelkind hat den Abbruch ueberlebt")

    def test_after_a_cancel_no_new_process_is_started_at_all(self):
        self.scope.cancel()
        with mock.patch("gitmaster_flash.subprocess.Popen") as popen:
            with self.assertRaises(gmf_module.GitCancelled):
                self.scope.call(
                    gmf_module._run_process_group,
                    ["/bin/echo", "hi"],
                    stdin=subprocess.DEVNULL,
                    timeout=5,
                )
        # Nicht "gestartet und dann getoetet", sondern gar nicht erst gestartet:
        # ein Fetch, der jetzt losliefe, haenge bis zu seinem eigenen Timeout.
        popen.assert_not_called()

    def test_the_registry_is_empty_again_after_a_normal_call(self):
        self.scope.call(
            gmf_module._run_process_group, ["/bin/echo", "hi"], stdin=subprocess.DEVNULL, timeout=10
        )
        self.assertFalse(self.scope.processes)


class BackgroundScanTests(unittest.TestCase):
    """Der Hintergrund-Scan als solcher: Meldungen, Reihenfolge, Abbruch."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="gmf-bg-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _drain(self, scan, timeout=20):
        messages = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            kind, payload = scan.queue.get(timeout=timeout)
            messages.append((kind, payload))
            if kind in ("done", "failed"):
                return messages
        self.fail("Der Scan hat sich nie gemeldet")

    def test_it_reports_a_total_then_each_repo_then_the_sorted_result(self):
        for name in ("beta", "alpha"):
            repo = self.root / name
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        scan = gmf_module.BackgroundScan(self.root, DEFAULT_CONFIG)
        scan.start()
        messages = self._drain(scan)
        self.assertEqual(messages[0], ("total", 2))
        self.assertEqual([kind for kind, _ in messages[1:-1]], ["one", "one"])
        kind, results = messages[-1]
        self.assertEqual(kind, "done")
        # Am Ende steht die fertige Sortierung — waehrend des Laufs bewusst nicht.
        self.assertEqual(
            [st.rel for st in results], [st.rel for st in gmf_module.sort_statuses(results)]
        )

    def test_an_empty_root_still_finishes_instead_of_hanging(self):
        scan = gmf_module.BackgroundScan(self.root, DEFAULT_CONFIG)
        scan.start()
        messages = self._drain(scan)
        self.assertEqual(messages[0], ("total", 0))
        self.assertEqual(messages[-1], ("done", []))

    def test_a_broken_scan_reports_failed_so_nobody_waits_forever(self):
        scan = gmf_module.BackgroundScan(self.root, DEFAULT_CONFIG)
        with mock.patch("gitmaster_flash.find_repos", side_effect=OSError("weg")):
            scan.start()
            messages = self._drain(scan)
        self.assertEqual(messages[-1][0], "failed")

    def test_stop_ends_the_worker_quickly_even_with_a_slow_git(self):
        repo = self.root / "langsam"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        # Ein Git, das haengt: ohne Abbruch liefe der Thread bis zum Timeout.
        slow = self.root / "bin"
        slow.mkdir()
        (slow / "git").write_text("#!/bin/sh\nsleep 120\n")
        (slow / "git").chmod(0o755)
        scan = gmf_module.BackgroundScan(
            self.root, {**DEFAULT_CONFIG, "fetch_timeout": 120, "git_timeout": 120}
        )
        with mock.patch.dict(os.environ, {"PATH": f"{slow}:{os.environ['PATH']}"}):
            # Beleg, dass wirklich DIESES git zum Zug kommt: Der Aufruf lautet
            # schlicht "git" und wird ueber den PATH aufgeloest. Eine Markerdatei
            # taugt hier nicht — SIGKILL trifft die Shell womoeglich, bevor sie
            # die erste Zeile ausgefuehrt hat.
            self.assertEqual(shutil.which("git"), str(slow / "git"))
            scan.start()
            # Warten, bis der haengende Aufruf wirklich laeuft.
            deadline = time.monotonic() + 10
            live = []
            while time.monotonic() < deadline and not live:
                live = [proc.args for proc in scan.scope.processes]
                time.sleep(0.02)
            self.assertTrue(live, "Es lief nie ein Git-Aufruf")
            self.assertEqual(live[0][:1], ["git"])
            started = time.monotonic()
            scan.stop()
            elapsed = time.monotonic() - started
        self.assertFalse(scan._thread.is_alive(), "Der Arbeiter laeuft weiter")
        # Deutlich unter dem Timeout von 120 s — sonst waere nichts abgebrochen.
        self.assertLess(elapsed, 15)
        self.assertFalse(scan.scope.processes)

    def test_quitting_during_a_hanging_fetch_leaves_no_git_process_behind(self):
        """Die ganze Kette: R druecken, sofort beenden, nichts bleibt uebrig.

        Das ist der Fall, der ohne Abbruchmechanik verwaiste git- und
        ssh-Prozesse hinterliesse — genau das, was dieses Programm sonst
        ueberall vermeidet.
        """
        repo = self.root / "haengt"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        slow = self.root / "bin"
        slow.mkdir()
        (slow / "git").write_text("#!/bin/sh\nsleep 300\n")
        (slow / "git").chmod(0o755)

        seen_live = []

        class Screen(FakeScreen):
            """R startet den Fetch, q beendet MITTEN darin.

            Der Zeitgeber-Tick wartet, bis wirklich ein Git-Prozess laeuft —
            sonst waere das q womoeglich schon durch, bevor ueberhaupt etwas zu
            beenden war, und der Test bewiese nichts.
            """

            def __init__(self):
                self.keys = iter([ord("R"), -1, ord("q")])

            def getmaxyx(self):
                return (24, 100)

            def getch(self):
                key = next(self.keys)
                if key == -1:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if ui.scan and ui.scan.scope.processes:
                            seen_live.append(ui.scan.scope)
                            break
                        time.sleep(0.02)
                return key

        cfg = {**DEFAULT_CONFIG, "git_timeout": 300, "fetch_timeout": 300}
        ui = TUI(Screen(), self.root, cfg, None)
        started = time.monotonic()
        with (
            mock.patch.dict(os.environ, {"PATH": f"{slow}:{os.environ['PATH']}"}),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "reload"),
        ):
            ui.run()
        elapsed = time.monotonic() - started
        self.assertTrue(seen_live, "Beim Beenden lief gar kein Git-Aufruf")
        # Ohne Abbruch haette das Beenden bis zum Timeout von 300 s gedauert.
        self.assertLess(elapsed, 20)
        self.assertIsNone(ui.scan)
        self.assertFalse(seen_live[0].processes)
        # Und kein sleep aus unserem Ersatz-git laeuft noch.
        leftovers = subprocess.run(
            ["pgrep", "-f", f"{slow}/git"], capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(leftovers, "", f"uebrige Prozesse: {leftovers}")


class BackgroundScanSettingsTests(unittest.TestCase):
    """Ein Scan ist EINE Messung und findet unter EINER Einstellung statt."""

    def test_a_setting_changed_mid_scan_does_not_reach_the_running_workers(self):
        """Vorher teilten Arbeiter und Oberflaeche dasselbe dict.

        Wer waehrend eines laufenden `R` ueber `,` etwa `fetch_timeout`
        aenderte, bekam eine Liste, deren vordere Repos unter der alten und
        deren hintere unter der neuen Geduldsgrenze gemessen wurden — ohne dass
        irgendwo stand, welches Repo unter welcher.
        """
        live = {**DEFAULT_CONFIG, "fetch_timeout": 30}
        scan = gmf_module.BackgroundScan(Path("/tmp"), live)

        live["fetch_timeout"] = 5
        live["skip_dirs"].append("nachtraeglich")

        self.assertEqual(scan.cfg["fetch_timeout"], 30)
        self.assertNotIn("nachtraeglich", scan.cfg["skip_dirs"])
        # Auch verschachtelte Werte sind wirklich kopiert, nicht geteilt.
        self.assertIsNot(scan.cfg["skip_dirs"], live["skip_dirs"])


class TuiBackgroundFetchTests(unittest.TestCase):
    """Wie die Oberflaeche mit den Ergebnissen des Hintergrund-Fetch umgeht."""

    class Screen(FakeScreen):
        def __init__(self, keys=()):
            self.keys = iter(keys)
            self.drawn = []

        def getmaxyx(self):
            return (24, 120)

        def addstr(self, y, x, text, *a):
            self.drawn.append((y, text))

        def getch(self):
            return next(self.keys)

        def get_wch(self):
            return next(self.keys)

    @staticmethod
    def _st(rel, **kw):
        return RepoStatus(path=Path("/tmp") / rel, rel=rel, **kw)

    def _ui(self, names, keys=()):
        ui = TUI(self.Screen(keys), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.all_statuses = [self._st(n) for n in names]
        ui.statuses = list(ui.all_statuses)
        return ui

    class FakeScan:
        """Ein Scan, dessen Meldungen der Test selbst vorgibt."""

        def __init__(self):
            self.queue = queue.Queue()
            self.total = 0
            self.done = 0
            self.stopped = False

        def stop(self):
            self.stopped = True

    def test_a_finished_repo_is_merged_without_reordering_the_list(self):
        ui = self._ui(["alpha", "beta", "gamma"])
        ui.scan = self.FakeScan()
        # "gamma" wird problematisch — beim Sortieren stuende es ganz oben.
        ui.scan.queue.put(("one", self._st("gamma", modified=3)))
        ui.drain_background_scan()
        self.assertEqual([s.rel for s in ui.all_statuses], ["alpha", "beta", "gamma"])
        self.assertEqual(ui.all_statuses[2].modified, 3)

    def test_the_final_result_sorts_and_takes_new_repos_along(self):
        ui = self._ui(["alpha", "beta"])
        ui.scan = self.FakeScan()
        results = gmf_module.sort_statuses(
            [self._st("alpha"), self._st("beta"), self._st("neu", modified=1)]
        )
        ui.scan.queue.put(("done", results))
        ui.drain_background_scan()
        self.assertIsNone(ui.scan)
        self.assertEqual([s.rel for s in ui.all_statuses], ["neu", "alpha", "beta"])

    def test_a_repo_that_vanished_is_gone_after_the_run(self):
        ui = self._ui(["alpha", "weg"])
        ui.scan = self.FakeScan()
        ui.scan.queue.put(("done", [self._st("alpha")]))
        ui.drain_background_scan()
        self.assertEqual([s.rel for s in ui.all_statuses], ["alpha"])

    def test_a_locally_refreshed_repo_keeps_its_newer_state(self):
        # Der Anwender committet waehrend des Fetch. Das Ergebnis des Scans ist
        # aelter; es darf den Commit nicht wieder als offene Aenderung zeigen.
        ui = self._ui(["alpha"])
        ui.scan = self.FakeScan()
        ui.all_statuses[0] = ui.statuses[0] = self._st("alpha", modified=0)
        # Geschluesselt ueber den echten Pfad, nicht ueber den Anzeigenamen
        # (Review-Fund 2026-08-25).
        ui.locally_refreshed.add(Path("/tmp") / "alpha")
        ui.scan.queue.put(("one", self._st("alpha", modified=9)))
        ui.drain_background_scan()
        self.assertEqual(ui.all_statuses[0].modified, 0)
        ui.scan.queue.put(("done", [self._st("alpha", modified=9)]))
        ui.drain_background_scan()
        self.assertEqual(ui.all_statuses[0].modified, 0)

    def test_refresh_one_only_marks_repos_while_a_scan_runs(self):
        ui = self._ui(["alpha"])
        fresh = self._st("alpha", modified=1)
        with mock.patch("gitmaster_flash.collect_status", return_value=fresh):
            ui.refresh_one(ui.all_statuses[0])
        self.assertEqual(ui.locally_refreshed, set())
        ui.scan = self.FakeScan()
        with mock.patch("gitmaster_flash.collect_status", return_value=fresh):
            ui.refresh_one(ui.all_statuses[0])
        self.assertEqual(ui.locally_refreshed, {Path("/tmp") / "alpha"})

    def test_two_repos_differing_only_in_unicode_form_stay_apart(self):
        """Review-Fund 2026-08-25: `rel` ist NFC-normalisiert, der Pfad nicht.

        Zwei unter Linux zulaessige Ordner, deren Namen sich nur in NFC/NFD
        unterscheiden, bekamen denselben Anzeigenamen. Ueber ihn zusammengefuehrt,
        ersetzte der Hintergrund-Fetch den einen Zustand durch den anderen.
        """
        nfc = "caf\u00e9"  # é als EIN Codepoint
        nfd = "cafe\u0301"  # e + kombinierender Akzent
        vorher = [
            RepoStatus(path=Path("/tmp") / nfc, rel=nfc, modified=1),
            RepoStatus(path=Path("/tmp") / nfd, rel=nfc, modified=2),
        ]
        ui = self._ui([])
        ui.all_statuses = list(vorher)
        ui.statuses = list(vorher)
        ui.scan = self.FakeScan()
        ui.locally_refreshed.add(Path("/tmp") / nfc)
        ui.scan.queue.put(
            (
                "done",
                [
                    RepoStatus(path=Path("/tmp") / nfc, rel=nfc, modified=99),
                    RepoStatus(path=Path("/tmp") / nfd, rel=nfc, modified=2),
                ],
            )
        )
        ui.drain_background_scan()

        # Nach Pfad nachschlagen, nicht nach Anzeigename: Der ist fuer beide gleich.
        nach_pfad = {str(s.path): s.modified for s in ui.all_statuses}
        self.assertEqual(len(ui.all_statuses), 2)
        self.assertEqual(
            nach_pfad[f"/tmp/{nfc}"],
            1,
            "Der lokal aufgefrischte Eintrag behaelt seinen juengeren Stand",
        )
        self.assertEqual(
            nach_pfad[f"/tmp/{nfd}"], 2, "Der andere Eintrag darf davon nicht ueberschrieben werden"
        )

    def test_the_visible_list_is_rebuilt_when_the_scan_finishes(self):
        """Wer eine Liste anfasst, muss die andere mitziehen.

        Die vorhandenen Tests des Scanendes sahen nur `all_statuses`. Ohne das
        `apply_filter()` in `_finish_scan()` bliebe `statuses` auf dem Stand von
        vor dem Scan stehen: veraltete Zahlen, und ein neu gefundenes Repo wäre
        gar nicht sichtbar.
        """
        ui = self._ui(["alpha", "beta"])
        ui.scan = self.FakeScan()
        ui.scan.queue.put(
            ("done", [self._st("alpha", modified=5), self._st("beta"), self._st("neu")])
        )
        ui.drain_background_scan()
        self.assertEqual(
            [(s.rel, s.modified) for s in ui.statuses], [("alpha", 5), ("beta", 0), ("neu", 0)]
        )
        self.assertEqual([s.rel for s in ui.all_statuses], [s.rel for s in ui.statuses])

    def test_a_filter_survives_the_end_of_the_scan(self):
        ui = self._ui(["api-gateway", "blog"])
        ui.filter_query = "api"
        ui.apply_filter()
        ui.scan = self.FakeScan()
        ui.scan.queue.put(
            ("done", [self._st("api-gateway", modified=3), self._st("blog"), self._st("api-docs")])
        )
        ui.drain_background_scan()
        # Sortiert wird nach Dringlichkeit: das geaenderte Repo zuerst.
        self.assertEqual(
            [(s.rel, s.modified) for s in ui.statuses], [("api-gateway", 3), ("api-docs", 0)]
        )
        self.assertEqual(len(ui.all_statuses), 3)

    def test_a_single_result_is_matched_by_path_not_by_display_name(self):
        """Einzelmeldung `("one", …)` mit zwei Ordnern, die nur NFC/NFD trennt.

        Der vorhandene Test schickte nur `("done", …)` und deckte damit
        `_finish_scan()` ab; `_merge_scanned()` blieb ungeschützt. Über `rel`
        gesucht, ersetzte das eine Repo den Zustand des anderen.
        """
        nfd = "cafe\u0301"
        nfc = unicodedata.normalize("NFC", nfd)
        ui = self._ui([])
        ui.all_statuses = [
            RepoStatus(path=Path("/tmp") / nfc, rel=nfc, modified=1),
            RepoStatus(path=Path("/tmp") / nfd, rel=nfc, modified=99),
        ]
        ui.statuses = list(ui.all_statuses)
        ui.scan = self.FakeScan()
        ui.scan.queue.put(("one", RepoStatus(path=Path("/tmp") / nfd, rel=nfc, modified=2)))
        ui.drain_background_scan()
        nach_pfad = {str(s.path): s.modified for s in ui.all_statuses}
        self.assertEqual(
            sorted(nach_pfad),
            sorted([f"/tmp/{nfc}", f"/tmp/{nfd}"]),
            "Ueber `rel` gesucht, ersetzt ein Repo das andere",
        )
        self.assertEqual(nach_pfad[f"/tmp/{nfc}"], 1)
        self.assertEqual(nach_pfad[f"/tmp/{nfd}"], 2)

    def test_every_scan_starts_with_an_empty_refresh_list(self):
        """Sonst verwirft JEDER weitere Scan das Ergebnis von damals.

        `locally_refreshed` merkt sich Repos, die während des laufenden Scans
        lokal neu eingelesen wurden. Bliebe der Eintrag stehen, käme das Repo
        auch in allen folgenden Scans nie wieder auf den neuesten Stand.
        """
        ui = self._ui(["alpha"])
        ui.locally_refreshed.add(Path("/tmp") / "alpha")
        with mock.patch.object(gmf_module, "BackgroundScan"):
            ui.dispatch_action("R")
        self.assertEqual(ui.locally_refreshed, set())
        ui.scan.queue = queue.Queue()
        ui.scan.queue.put(("done", [self._st("alpha", modified=42)]))
        ui.drain_background_scan()
        self.assertEqual([s.modified for s in ui.all_statuses], [42])

    def test_a_running_scan_shows_its_progress_in_the_header(self):
        ui = self._ui(["alpha"])
        ui.scan = self.FakeScan()
        ui.scan.total, ui.scan.done = 7, 3
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        self.assertIn("3/7", ui.scr.drawn[0][1])

    def test_a_failed_scan_is_reported_and_ends_the_run(self):
        ui = self._ui(["alpha"])
        ui.scan = self.FakeScan()
        ui.scan.queue.put(("failed", OSError("Netz weg")))
        ui.drain_background_scan()
        self.assertIsNone(ui.scan)
        self.assertIn("Netz weg", ui.message)

    def test_r_starts_one_scan_and_refuses_a_second(self):
        ui = self._ui(["alpha"])
        with mock.patch.object(gmf_module, "BackgroundScan") as factory:
            ui.dispatch_action("R")
            self.assertTrue(factory.return_value.start.called)
            ui.dispatch_action("R")
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(ui.message, gmf_module.TR["scan_already_running"]["en"])

    def test_leaving_the_interface_stops_a_running_scan(self):
        ui = self._ui(["alpha"], keys=[ord("q")])
        ui.scan = self.FakeScan()
        stopped = ui.scan
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "reload"),
        ):
            ui.run()
        self.assertTrue(stopped.stopped, "Der Scan lief nach dem Beenden weiter")
        self.assertIsNone(ui.scan)

    def test_an_exception_in_the_loop_still_stops_the_scan(self):
        ui = self._ui(["alpha"], keys=[])  # StopIteration beim ersten getch
        ui.scan = self.FakeScan()
        stopped = ui.scan
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "reload"),
        ):
            with self.assertRaises(StopIteration):
                ui.run()
        self.assertTrue(stopped.stopped)

    def test_the_timer_tick_redraws_without_wiping_the_message(self):
        # -1 heisst "keine Taste, nur der Zeitgeber". Eine Meldung, die dabei
        # verschwaende, waere nach 200 ms weg und nie lesbar.
        ui = self._ui(["alpha"], keys=[-1, ord("q")])
        ui.scan = self.FakeScan()
        ui.message = "wichtig"
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "reload"),
        ):
            ui.run()
        self.assertTrue(any("wichtig" in text for _, text in ui.scr.drawn))

    def test_without_a_scan_measurement_age_is_redrawn_and_timeout_is_reset(self):
        ui = self._ui(["alpha"])
        ui.scr.timeout = mock.Mock()
        ui.scr.keys = iter([ord("x")])
        ui._wait_for_key()
        self.assertEqual(
            [call.args[0] for call in ui.scr.timeout.call_args_list],
            [gmf_module.MEASUREMENT_POLL_MS, -1],
        )

    def test_during_a_scan_the_wait_is_bounded_and_reset_right_after(self):
        ui = self._ui(["alpha"])
        ui.scan = self.FakeScan()
        ui.scr.timeout = mock.Mock()
        ui.scr.keys = iter([-1])
        ui._wait_for_key()
        self.assertEqual(
            [call.args[0] for call in ui.scr.timeout.call_args_list],
            [gmf_module.BACKGROUND_POLL_MS, -1],
        )
