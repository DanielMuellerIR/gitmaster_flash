import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from gmf_test_support import FakeScreen, git, latest_stash, stash_preview
from test_cli_blackbox import make_installer_fixture

import gitmaster_flash as gmf_module
from gitmaster_flash import (
    DEFAULT_CONFIG,
    TUI,
    ChangedFile,
    RepoStatus,
    __version__,
    cell_width,
    collect_status,
    fetch_remote_status,
    pad_cells,
    safe_push_args,
    status_dict,
    terminal_text,
    truncate_cells,
)


class StashAndReadFailureTests(unittest.TestCase):
    def test_stash_preview_includes_untracked_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "base").write_text("base\n")
            git(repo, "add", "base")
            git(repo, "commit", "-qm", "base")
            (repo / "untracked.bin").write_bytes(bytes(range(256)) * 2)
            git(repo, "stash", "push", "-qu")
            ok, preview = stash_preview(repo, 10)
            self.assertTrue(ok)
            self.assertIn("untracked.bin", preview)
            self.assertIn("Binary files /dev/null and b/untracked.bin differ", preview)

    def test_stash_preview_never_runs_an_external_diff_driver(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "base").write_text("base\n")
            git(repo, "add", "base")
            git(repo, "commit", "-qm", "base")
            (repo / "base").write_text("changed\n")
            git(repo, "stash", "push", "-q")
            marker = repo / "external-driver-ran"
            driver = repo / "external-driver"
            driver.write_text("#!/bin/sh\nprintf ran > " + shlex.quote(str(marker)) + "\n")
            driver.chmod(0o755)

            with mock.patch.dict(os.environ, {"GIT_EXTERNAL_DIFF": str(driver)}):
                ok, preview = stash_preview(repo, 10)

            self.assertTrue(ok, preview)
            self.assertIn("+changed", preview)
            self.assertFalse(marker.exists())

    def test_stash_view_uses_one_oid_bound_snapshot_for_title_and_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            path = repo / "a"
            path.write_text("base\n")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "base")
            path.write_text("old\n")
            git(repo, "stash", "push", "-qm", "old")

            class Screen:
                def getmaxyx(self):
                    return (30, 100)

            ui = TUI(Screen(), root, DEFAULT_CONFIG, None)
            ui.statuses = [collect_status(repo, root, DEFAULT_CONFIG)]
            path.write_text("new\n")
            git(repo, "stash", "push", "-qm", "new")
            shown = {}

            def capture(_self, status, entries):
                shown["entries"] = entries
                newest = entries[0]
                item = gmf_module.stash_files(repo, newest.oid, 10)[0]
                shown["text"] = "\n".join(gmf_module.stash_file_diff(repo, newest.oid, item, 10))

            with mock.patch.object(TUI, "browse_stashes", new=capture):
                ui.action_stash_show()

            self.assertIn("new", shown["entries"][0].label)
            self.assertEqual(len(shown["entries"]), 2)
            self.assertIn("+new", shown["text"])
            self.assertNotIn("+old", shown["text"])

    def test_replace_ref_cannot_swap_the_stash_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            path = repo / "a"
            path.write_text("base\n")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "base")
            path.write_text("original\n")
            git(repo, "stash", "push", "-qm", "original")
            original = latest_stash(repo, 10)[0]
            path.write_text("replacement\n")
            git(repo, "stash", "push", "-qm", "replacement")
            replacement = latest_stash(repo, 10)[0]
            git(repo, "stash", "drop", "-q", "stash@{0}")
            git(repo, "replace", original, replacement)

            ok, preview = stash_preview(repo, 10)
            self.assertTrue(ok, preview)
            self.assertIn("+original", preview)
            self.assertNotIn("+replacement", preview)

    def test_repository_graft_cannot_change_stash_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            path = repo / "a"
            path.write_text("base\n")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "base")
            path.write_text("original\n")
            git(repo, "stash", "push", "-qm", "original")
            original = latest_stash(repo, 10)[0]
            path.write_text("other\n")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "other base")
            grafts = repo / ".git" / "info" / "grafts"
            grafts.write_text(f"{original} {git(repo, 'rev-parse', 'HEAD')}\n")

            ok, preview = stash_preview(repo, 10)
            self.assertTrue(ok, preview)
            self.assertIn("+original", preview)

    def test_broken_index_sets_error_instead_of_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "base")
            (repo / ".git" / "index").write_bytes(b"broken")
            st = collect_status(repo, root, DEFAULT_CONFIG)
            self.assertTrue(st.error)
            self.assertEqual(st.remote_state, "error")
            self.assertFalse(st.clean_and_synced)

    def test_stash_mutation_keys_cannot_change_a_new_checkout(self):
        """U/D bleiben ohne atomare Bindung des Zielzustands unerreichbar."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            path = repo / "a"
            path.write_text("base\n")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "base")
            path.write_text("approved\n")
            git(repo, "stash", "push", "-qm", "approved")
            ui_status = collect_status(repo, root, DEFAULT_CONFIG)
            git(repo, "switch", "-qc", "other")
            path.write_text("other branch edit\n")
            before_head = git(repo, "rev-parse", "HEAD")
            before_status = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
            before_stack = git(repo, "stash", "list", "--format=%H %gd %gs")

            class Screen:
                def getmaxyx(self):
                    return (30, 100)

            ui = TUI(Screen(), root, DEFAULT_CONFIG, None)
            ui.statuses = [ui_status]
            with mock.patch.object(gmf_module, "run_git_logged") as logged:
                ui.dispatch_action("U")
                ui.dispatch_action("D")

            logged.assert_not_called()
            self.assertEqual(git(repo, "rev-parse", "HEAD"), before_head)
            self.assertEqual(
                git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"), before_status
            )
            self.assertEqual(git(repo, "stash", "list", "--format=%H %gd %gs"), before_stack)
            self.assertEqual(path.read_text(), "other branch edit\n")


class DisplayAndIntegrationSafetyTests(unittest.TestCase):
    def test_i_key_opens_repo_info_case_insensitively(self):
        class Screen(FakeScreen):
            def __init__(self):
                self.keys = iter((ord("i"), ord("Q")))

            def getch(self):
                return next(self.keys)

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        with (
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(ui, "reload"),
            mock.patch.object(ui, "draw"),
            mock.patch.object(ui, "action_repo_info") as action,
        ):
            ui.run()
        action.assert_called_once_with()

    def test_terminal_controls_are_visible_and_width_is_cell_aware(self):
        escaped = terminal_text("name\n\x1b[31m\x85")
        self.assertNotIn("\n", escaped)
        self.assertNotIn("\x1b", escaped)
        self.assertIn("\\n", escaped)
        self.assertIn("\\x1b", escaped)
        self.assertEqual(cell_width("a\u0308界✔"), 4)
        self.assertEqual(truncate_cells("界x", 2), "界")
        self.assertEqual(cell_width(pad_cells("界", 4)), 4)

    def test_unicode_format_and_line_separators_are_visible(self):
        escaped = terminal_text("a\u202eb\u2066c\u2028d\u2029")
        self.assertEqual(escaped, "a\\u202eb\\u2066c\\u2028d\\u2029")
        self.assertEqual(terminal_text("byte\udcff"), "byte\\udcff")

    def test_surrogateescaped_repo_name_is_safe_in_json_and_terminal_text(self):
        raw = b"repo-\xff"
        rel = os.fsdecode(raw)
        st = RepoStatus(path=Path("/tmp") / rel, rel=rel)

        payload = status_dict(st)

        self.assertEqual(payload["rel"], "repo-\\udcff")
        self.assertNotIn("\udcff", json.dumps(payload, ensure_ascii=True))

    def test_the_branch_not_on_line_is_escaped_like_every_other_git_text(self):
        """`--list` schrieb Branch- und Remotenamen als einzige Stelle roh.

        Git erlaubt in Ref- und Remotenamen Bytes ohne UTF-8-Bedeutung; die
        Leser dekodieren sie mit surrogateescape. Genau diese eine Zeile ging
        ohne `terminal_text()` nach stdout: In einem Terminal mit UTF-8-Locale
        brach `--list` mit UnicodeEncodeError ab, und ein `\x1b[` im Namen fuhr
        als Steuersequenz durch (Review-Fund 2026-09-10).
        """
        st = RepoStatus(
            path=Path("/tmp/x"),
            rel="x",
            branch=os.fsdecode(b"feat/\xff") + "\x1b[31m",
            remote=os.fsdecode(b"orig\xfe"),
            remote_state="no-branch",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gmf_module.print_list([st])
        text = out.getvalue()

        # Beides muss gelten: streng nach UTF-8 kodierbar UND kein Steuerzeichen
        # aus den NAMEN. Die Farbcodes setzt print_list selbst; nur sie duerfen
        # als echtes Escape ueberbleiben.
        text.encode("utf-8")
        for colour in ("\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m"):
            text = text.replace(colour, "")
        self.assertNotIn("\x1b", text)
        self.assertIn("\\x1b[31m", text)
        self.assertIn("\\udcff", text)
        self.assertIn("\\udcfe", text)

    def test_the_json_root_is_escaped_like_every_other_field(self):
        """`root` war das einzige unentschaerfte Feld der `--json`-Nutzlast.

        `print_list()` schreibt denselben Wert seit jeher ueber
        `terminal_text()`; nur die JSON-Form nahm ihn roh. Ein Scan-Start mit
        einem Steuerzeichen im Ordnernamen schickte es damit ungefiltert ins
        Terminal — und ein Byte ohne UTF-8-Bedeutung haette `print()` zerlegt.
        """
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "scan\x1b[31m"
            root.mkdir()
            config = Path(temp) / "config.json"
            with mock.patch.object(gmf_module, "CONFIG_PATH", config):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    gmf_module.main([str(root), "--json"])
            payload = json.loads(out.getvalue())

        self.assertNotIn("\x1b", payload["root"])
        self.assertIn("\\x1b[31m", payload["root"])

    @unittest.skipUnless(shutil.which("zsh"), "zsh unavailable")
    def test_direct_cd_output_is_safe_and_shell_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "line\n;echo nope\x1b\u0085\u202e"
            path.mkdir()

            class Screen:
                def getmaxyx(self):
                    return (30, 100)

            ui = TUI(Screen(), Path(temp), DEFAULT_CONFIG, None)
            ui.statuses = [RepoStatus(path=path, rel="unsafe")]
            with mock.patch("builtins.print") as printed:
                self.assertTrue(ui.action_cd_and_quit())

            command = printed.call_args_list[0].args[0].strip()
            self.assertNotIn("\x1b", command)
            self.assertNotIn("\n", command)
            self.assertTrue(command.startswith("cd -- $'"), command)
            executed = subprocess.run(
                ["zsh", "-fc", command + "; pwd -P"], capture_output=True, text=True
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertEqual(Path(executed.stdout.strip()), path.resolve())

    def test_logged_command_shows_exactly_the_configuration_run_git_sets(self):
        """Protokoll (H) und tatsächlicher Aufruf müssen dieselbe Config nennen.

        Das Protokoll verspricht, dass jede Zeile im Terminal genauso läuft.
        Solange beide Seiten ihre GIT_CONFIG_*-Liste getrennt aufbauten, konnte
        eine weitere Ersatzeinstellung in `run_git()` still danebenlaufen — der
        kopierte Befehl liefe dann mit einer anderen Konfiguration.
        """
        args = safe_push_args("/tmp/approved.git", "main", "a" * 40, "b" * 40)
        seen = {}

        class FakePopen:
            def __init__(self, cmd, **kw):
                seen.update(kw["env"])
                self.returncode = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def communicate(self, timeout=None):
                return ("", "")

        with (
            mock.patch("gitmaster_flash.subprocess.Popen", FakePopen),
            mock.patch.dict(
                os.environ, {key: "/tmp/wrapper" for key in gmf_module.TRANSPORT_GIT_ENV}
            ),
        ):
            gmf_module.run_git(Path("."), *args, timeout=1)

        logged = gmf_module.format_git_command(args)
        prefix = logged.split(" git ", 1)[0]
        # `shlex.split` nimmt die Quotierung wieder heraus; verglichen werden
        # die Werte selbst, nicht ihre Schreibweise im Protokoll.
        words = shlex.split(prefix)
        # Ab `env -u …` stehen die Variablen, die der gebundene Transfer aus der
        # Umgebung entfernt; davor die Ersatzkonfiguration als Zuweisungen.
        env_at = words.index("env")
        assignments = dict(item.split("=", 1) for item in words[:env_at])
        unset = {
            words[index + 1] for index, word in enumerate(words[env_at:], env_at) if word == "-u"
        }
        self.assertEqual(unset, gmf_module.TRANSPORT_GIT_ENV)
        # Der tatsächliche Aufruf muss sie ebenfalls losgeworden sein, sonst
        # entschiede ein geerbter SSH-Wrapper über das wahre Transferziel.
        for key in gmf_module.TRANSPORT_GIT_ENV:
            self.assertNotIn(key, seen)
        actual = {
            key: value
            for key, value in seen.items()
            if key == "GIT_CONFIG_COUNT" or re.fullmatch(r"GIT_CONFIG_(KEY|VALUE)_\d+", key)
        }
        self.assertEqual(len(assignments), len(actual))
        for key, value in actual.items():
            self.assertEqual(assignments[key], value, key)
        # Die Anzahl muss zu den tatsächlich gesetzten Paaren passen; sonst
        # ignoriert Git den Rest der Liste stillschweigend.
        self.assertEqual(int(actual["GIT_CONFIG_COUNT"]), (len(actual) - 1) // 2)

    def test_long_pinned_command_wraps_without_losing_text(self):
        command = (
            gmf_module.format_git_command(
                safe_push_args(
                    "/tmp/a-very-long-approved-remote-name.git", "main", "a" * 40, "b" * 40
                )
            )
            + "\u202e"
        )
        visible = terminal_text(command)
        wrapped = gmf_module.wrap_cells(visible, 80)
        self.assertGreater(len(wrapped), 1)
        self.assertEqual("".join(wrapped), visible)
        self.assertNotIn("\u202e", "".join(wrapped))
        self.assertIn("\\u202e", "".join(wrapped))
        self.assertTrue(all(cell_width(line) <= 80 for line in wrapped))

    def test_commit_picker_survives_39_columns(self):
        class Screen(FakeScreen):
            def getmaxyx(self):
                return (10, 39)

            def getch(self):
                return 27

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.statuses = [
            RepoStatus(
                path=Path("/tmp/repo"),
                rel="r",
                files=[ChangedFile("M", "very-long-name.txt", " M")],
                modified=1,
            )
        ]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_commit_wizard()
        self.assertEqual(ui.message, "Commit helper cancelled.")

    def test_remote_root_is_one_shell_argument(self):
        root = "~/My Projects/x;touch PWNED"
        completed = subprocess.CompletedProcess(
            [], 0, stdout='{"version":"x","repos":[]}', stderr=""
        )
        with mock.patch("gitmaster_flash._run_process_group", return_value=completed) as run:
            fetch_remote_status("example", root, fetch=False)
        command = run.call_args.args[0][-1]
        self.assertEqual(shlex.split(command)[-1], root)
        self.assertEqual(shlex.split(command).count(root), 1)
        self.assertEqual(shlex.split(command)[-2], "--")

    def test_remote_side_gets_the_local_ui_language(self):
        """Ohne --lang wählte die Gegenseite ihre Sprache selbst — derselbe
        lokalisierte Fehlertext sähe im --diff dann wie DRIFT aus."""
        completed = subprocess.CompletedProcess(
            [], 0, stdout='{"version":"x","repos":[]}', stderr=""
        )
        with mock.patch("gitmaster_flash._run_process_group", return_value=completed) as run:
            fetch_remote_status("example", "~/git", fetch=False)
        command = shlex.split(run.call_args.args[0][-1])
        self.assertIn("--lang", command)
        self.assertEqual(command[command.index("--lang") + 1], gmf_module.UI_LANG)

    def test_remote_json_with_attention_exit_is_accepted(self):
        """Exit 1 ist bei --json ein Befund, kein fehlgeschlagener SSH-Aufruf."""
        payload = {"version": "x", "repos": [{"rel": "needs-attention"}]}
        completed = subprocess.CompletedProcess([], 1, stdout=json.dumps(payload), stderr="")
        with mock.patch("gitmaster_flash._run_process_group", return_value=completed):
            self.assertEqual(fetch_remote_status("example", "~/git", fetch=True), payload)

    def test_remote_process_uses_the_derived_timeout(self):
        payload = {"version": "x", "repos": []}
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with mock.patch("gitmaster_flash._run_process_group", return_value=completed) as run:
            fetch_remote_status("example", "~/git", fetch=True, process_timeout=777)
        self.assertEqual(run.call_args.kwargs["timeout"], 777)

    def test_run_diff_uses_the_configured_hard_wall_clock_timeout(self):
        payload = {"version": __version__, "repos": []}
        cfg = {**DEFAULT_CONFIG, "git_timeout": 10, "fetch_timeout": 30}
        with (
            mock.patch.object(gmf_module, "fetch_remote_status", return_value=payload) as remote,
            mock.patch.object(gmf_module, "collect_all", return_value=[]),
            mock.patch("builtins.print"),
        ):
            result = gmf_module.run_diff("example", Path("/work"), cfg, fetch=True, as_json=False)
        self.assertEqual(result, 0)
        remote.assert_called_once()
        self.assertTrue(remote.call_args.kwargs["fetch"])
        self.assertEqual(remote.call_args.kwargs["process_timeout"], DEFAULT_CONFIG["diff_timeout"])

    def test_a_connected_but_stuck_remote_process_is_reported(self):
        timeout = subprocess.TimeoutExpired(["ssh", "example"], 1)
        with mock.patch("gitmaster_flash._run_process_group", side_effect=timeout):
            with self.assertRaisesRegex(RuntimeError, "Cannot reach"):
                fetch_remote_status("example", "~/git", fetch=False, process_timeout=1)

    def test_remote_deadline_kills_ssh_children(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fakebin = root / "bin"
            fakebin.mkdir()
            child_file = root / "child.pid"
            ssh = fakebin / "ssh"
            ssh.write_text(
                "#!/bin/sh\n"
                "/bin/sleep 30 &\n"
                "child=$!\n"
                'printf \'%s\\n\' "$child" > "$GMF_SSH_CHILD_PID"\n'
                'wait "$child"\n'
            )
            ssh.chmod(0o755)
            env = {
                "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
                "GMF_SSH_CHILD_PID": str(child_file),
            }
            with mock.patch.dict(os.environ, env):
                with self.assertRaisesRegex(RuntimeError, "Cannot reach"):
                    fetch_remote_status("example", "~/git", fetch=False, process_timeout=1)

            self.assertTrue(child_file.exists(), "fake ssh did not start")
            child_pid = int(child_file.read_text())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("ssh child process survived the deadline")

    def test_valid_json_with_wrong_structure_is_rejected(self):
        for payload in (
            [],
            {"version": "x", "repos": {}},
            {"version": "x", "repos": [{"rel": "x", "remotes": [{}]}]},
        ):
            with self.subTest(payload=payload):
                completed = subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps(payload), stderr=""
                )
                with mock.patch("gitmaster_flash._run_process_group", return_value=completed):
                    with self.assertRaisesRegex(RuntimeError, "JSON structure"):
                        fetch_remote_status("example", "~/git", fetch=False)

    def test_remote_process_failure_without_stderr_reports_exit_code(self):
        completed = subprocess.CompletedProcess([], 255, stdout="", stderr="")
        with mock.patch("gitmaster_flash._run_process_group", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "255"):
                fetch_remote_status("example", "~/git", fetch=False)

    @staticmethod
    def _make_screens_module():
        source = Path(__file__).resolve().parents[1] / "docs" / "make-screens.py"
        spec = importlib.util.spec_from_file_location("gmf_make_screens_test", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, source

    def test_screen_replay_understands_the_sequences_curses_emits(self):
        """Der Bildnachbau muss dieselben Zeilen treffen wie das echte Terminal.

        Die absolute Zeilenwahl (ESC[<n>d) fehlte lange: ncurses setzt damit den
        Cursor auf die Fußzeile, der Nachbau ließ ihn stehen und zeichnete sie
        direkt unter die Liste. Die Bilder sahen plausibel aus, zeigten aber eine
        Aufteilung, die es so nie gab — deshalb hier festgenagelt.
        """
        module, _ = self._make_screens_module()
        grid = module.replay(
            "\x1b[H\x1b[2Jlist\x1b[4dfooter"  # ESC[4d: 4. Zeile
            "\x1b[?25l"  # privater Modus
            "\x1b[2;1Hab\x08X",  # Backspace
            cols=12,
            rows=5,
        )
        lines = ["".join(cell.ch for cell in row).rstrip() for row in grid]
        self.assertEqual(lines[0], "list")
        # ESC[<n>d wechselt nur die Zeile, die Spalte bleibt hinter "list" stehen.
        self.assertEqual(lines[3], "    footer")
        self.assertEqual(lines[1], "aX")
        self.assertNotIn("25l", "".join(lines))
        # Diese Symbole sind für macOS-curses einzellig. Würde der Nachbau sie
        # breiter zählen, wanderten spätere ↑n-/↓n-Zeilen im SVG seitlich.
        self.assertEqual(module.cell_width("✔"), 1)
        self.assertEqual(module.cell_width("⚑"), 1)
        # Der Nachbau misst mit derselben Funktion wie das Programm — eine
        # zweite Fassung hier könnte still auseinanderlaufen und das Bild
        # zeigte plausibel etwas anderes, als gmf zeichnet.
        self.assertIs(module.cell_width, gmf_module.cell_width)

    def test_screen_replay_scrolls_the_region_curses_asks_it_to(self):
        """ncurses schiebt Zeilen, statt sie neu zu malen — das muss nachgebildet sein.

        Beobachtet am 2026-08-23 beim Filter ohne Treffer: ncurses setzte den
        Bereich (ESC[2;30r), sprang ans Ende und scrollte ihn (ESC[6S). Der
        Nachbau kannte weder das eine noch das andere und behielt die alte
        Repo-Liste im Bild — ringsum stimmte alles, das Ergebnis sah deshalb
        plausibel aus und war falsch.
        """
        module, _ = self._make_screens_module()
        painted = "\x1b[H\x1b[2J" + "".join(f"\x1b[{n};1Hzeile{n}" for n in range(1, 6))
        # Bereich Zeile 2-4, ans Ende springen, um zwei Zeilen hochschieben.
        grid = module.replay(painted + "\x1b[2;4r\x1b[4;1H\x1b[2S", cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in grid]
        # Ausserhalb des Bereichs bleibt alles stehen …
        self.assertEqual(lines[0], "zeile1")
        self.assertEqual(lines[4], "zeile5")
        # … innerhalb rueckt zeile4 nach oben, der Rest wird leer.
        self.assertEqual(lines[1], "zeile4")
        self.assertEqual(lines[2], "")
        self.assertEqual(lines[3], "")

    def test_screen_replay_scrolls_the_region_down_as_well(self):
        module, _ = self._make_screens_module()
        painted = "\x1b[H\x1b[2J" + "".join(f"\x1b[{n};1Hzeile{n}" for n in range(1, 6))
        grid = module.replay(painted + "\x1b[2;4r\x1b[2;1H\x1b[1T", cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in grid]
        self.assertEqual(lines[0], "zeile1")
        self.assertEqual(lines[1], "")
        self.assertEqual(lines[2], "zeile2")
        self.assertEqual(lines[3], "zeile3")
        self.assertEqual(lines[4], "zeile5")

    def test_screen_replay_scrolling_further_than_the_region_clears_it(self):
        module, _ = self._make_screens_module()
        painted = "\x1b[H\x1b[2J" + "".join(f"\x1b[{n};1Hzeile{n}" for n in range(1, 6))
        grid = module.replay(painted + "\x1b[2;4r\x1b[4;1H\x1b[9S", cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in grid]
        self.assertEqual(lines[1:4], ["", "", ""])
        self.assertEqual([lines[0], lines[4]], ["zeile1", "zeile5"])

    def test_screen_replay_inserts_and_deletes_lines_inside_the_region(self):
        module, _ = self._make_screens_module()
        painted = "\x1b[H\x1b[2J" + "".join(f"\x1b[{n};1Hzeile{n}" for n in range(1, 6))
        # ESC[M loescht die Cursorzeile, alles darunter rueckt hoch.
        deleted = module.replay(painted + "\x1b[2;1H\x1b[1M", cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in deleted]
        self.assertEqual(lines, ["zeile1", "zeile3", "zeile4", "zeile5", ""])
        # ESC[L schiebt sie nach unten und laesst eine leere Zeile zurueck.
        inserted = module.replay(painted + "\x1b[2;1H\x1b[1L", cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in inserted]
        self.assertEqual(lines, ["zeile1", "", "zeile2", "zeile3", "zeile4"])

    def test_a_line_feed_at_the_bottom_edge_scrolls_the_region(self):
        """Der Zeilenvorschub ist die haeufigste Scroll-Ursache — und fehlte.

        Die Ergaenzung vom 2026-08-23 bildete ESC[S/T/L/M nach, nicht aber den
        einfachen Zeilenvorschub. terminfo nennt ihn `ind`, und ncurses schickt
        dafuer genau ein Byte. Der Nachbau setzte den Cursor am unteren Rand
        einfach wieder auf dieselbe Zeile: Die oberste Zeile blieb stehen, alles
        Folgende landete eine Zeile zu hoch — wieder ein Bild, das ringsum
        stimmt und trotzdem falsch ist. In den sechs aufgezeichneten Stroemen
        kommt dieser Fall achtmal vor (gemessen 2026-08-29).
        """
        module, _ = self._make_screens_module()
        painted = "\x1b[H\x1b[2J" + "".join(f"\x1b[{n};1Hzeile{n}" for n in range(1, 6))
        # Bereich Zeile 1-3, Cursor auf dessen letzte Zeile, dann ein CR/LF.
        grid = module.replay(painted + "\x1b[1;3r\x1b[3;1H\r\nneu", cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in grid]
        # Der Bereich rueckt hoch, die neue Zeile bleibt in seiner letzten Zeile.
        self.assertEqual(lines[:3], ["zeile2", "zeile3", "neu"])
        # Ausserhalb des Bereichs bleibt alles unberuehrt.
        self.assertEqual(lines[3:], ["zeile4", "zeile5"])

    def test_a_reverse_index_at_the_top_edge_scrolls_the_region_down(self):
        """ESC M ist das Gegenstueck (`ri`); der Zweig fuer unbekannte Escapes
        verschluckte es lautlos."""
        module, _ = self._make_screens_module()
        painted = "\x1b[H\x1b[2J" + "".join(f"\x1b[{n};1Hzeile{n}" for n in range(1, 6))
        grid = module.replay(painted + "\x1b[2;4r\x1b[2;1H\x1bM", cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in grid]
        self.assertEqual(lines, ["zeile1", "", "zeile2", "zeile3", "zeile5"])
        # Mitten im Bereich bewegt es nur den Cursor eine Zeile nach oben.
        grid = module.replay(painted + "\x1b[1;5r\x1b[3;1H\x1bMX", cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in grid]
        self.assertEqual(lines[1], "Xeile2")

    def test_setting_the_region_puts_the_cursor_home_like_a_terminal(self):
        module, _ = self._make_screens_module()
        grid = module.replay("\x1b[H\x1b[2J\x1b[5;1Hunten\x1b[1;5rX", cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in grid]
        self.assertEqual(lines[0], "X")

    def test_screen_replay_normalizes_the_random_transfer_alias(self):
        module, _ = self._make_screens_module()
        grid = module.replay("gmf-pin-" + "a" * 32 + "://approved", cols=64, rows=1)
        line = "".join(cell.ch for cell in grid[0]).rstrip()
        self.assertEqual(line, "gmf-pin-" + "0" * 32 + "://approved")

    def test_screen_replay_keeps_real_background_colours(self):
        """Ein Farbpaar mit eigenem Hintergrund muss im Bild eine Fläche werden.

        Die markierte Zeile stellt Rot so dar: helle Schrift AUF Rot statt Rot als
        Fläche mit schwarzer Schrift. Ohne Hintergrundfarben im Nachbau ging die
        Fläche verloren — die helle Schrift stand dann auf dem dunklen Fenster und
        das Bild zeigte etwas, das das Programm nie gezeichnet hat.
        """
        module, _ = self._make_screens_module()
        grid = module.replay("\x1b[H\x1b[2J\x1b[37;41mM:1\x1b[0m ok", cols=10, rows=2)
        self.assertEqual(grid[0][0].bg, module.ANSI_BG[41])
        self.assertFalse(grid[0][0].rev)
        self.assertIsNone(grid[0][4].bg)  # nach ESC[0m wieder normal
        self.assertIn(f'fill="{module.ANSI_BG[41]}"', module.to_svg(grid, "t"))

    def test_screen_replay_paints_the_area_under_a_wide_character(self):
        """Auch die Fortsetzungszelle eines breiten Zeichens trägt die Fläche.

        `to_svg()` fasst nur Zellen mit gleichem `bg` zu einem Lauf zusammen.
        Blieb dort der Wert des vorigen Bildaufbaus stehen, brach der rote
        Balken der markierten Zeile mitten in einem ostasiatischen Repo-Namen
        auf (Review-Fund 2026-09-03).
        """
        module, _ = self._make_screens_module()
        # Erst eine Fläche malen, dann darüber ein breites Zeichen in einer
        # anderen: Nur so wird ein zurückgelassener alter Wert sichtbar.
        grid = module.replay(
            "\x1b[H\x1b[2J\x1b[37;42mxxxx\x1b[H\x1b[37;41m日本\x1b[0m", cols=8, rows=2
        )
        self.assertEqual(grid[0][0].ch, "日")
        self.assertEqual(grid[0][1].ch, " ")  # Fortsetzungszelle
        for x in range(4):
            self.assertEqual(grid[0][x].bg, module.ANSI_BG[41])
        svg = module.to_svg(grid, "t")
        self.assertEqual(svg.count(f'fill="{module.ANSI_BG[41]}"'), 1)
        self.assertNotIn(f'fill="{module.ANSI_BG[42]}"', svg)

    def test_screenshot_settle_and_owned_tmpdir_are_wired(self):
        module, _ = self._make_screens_module()
        owned = []

        def fake_render(args, keys, settle, tmpdir, cols, rows, ready_marker, key_markers):
            self.assertEqual(settle, 0.123)
            self.assertTrue(Path(tmpdir).is_dir())
            self.assertIsNone(ready_marker)
            self.assertIsNone(key_markers)
            owned.append(Path(tmpdir))
            return []

        with mock.patch.object(module, "_render_in_pty", side_effect=fake_render):
            self.assertEqual(module.render_in_pty(["--version"], settle=0.123), [])
        self.assertEqual(len(owned), 1)
        self.assertFalse(owned[0].exists())

    def test_pty_reader_waits_for_marker_across_a_quiet_period(self):
        """Ausgaberuhe darf eine noch laufende, stille Aktion nicht beenden."""
        module, _ = self._make_screens_module()
        read_fd, write_fd = os.pipe()

        def delayed_finish():
            try:
                os.write(write_fd, b"first draw")
                time.sleep(0.08)  # deutlich laenger als das Ruhefenster unten
                os.write(write_fd, b"action finished")
            finally:
                os.close(write_fd)

        writer = threading.Thread(target=delayed_finish)
        writer.start()
        try:
            output = module._read_pty_until(
                read_fd,
                quiet=0.01,
                cap=1.0,
                first_wait=0.2,
                marker=b"action finished",
            )
        finally:
            os.close(read_fd)
            writer.join()

        self.assertIn(b"action finished", output)

    def test_pty_reader_does_not_hide_unexpected_read_errors(self):
        """Nur das für ein beendetes PTY übliche EIO darf wie EOF gelten."""
        module, _ = self._make_screens_module()
        read_error = OSError(9, "bad file descriptor")
        with (
            mock.patch.object(module.select, "select", return_value=([123], [], [])),
            mock.patch.object(module.os, "read", side_effect=read_error),
        ):
            with self.assertRaisesRegex(OSError, "bad file descriptor"):
                module._read_pty_until(123, cap=0.1, first_wait=0.1)

    def test_pty_ioctl_failure_still_closes_and_reaps_the_child(self):
        module, _ = self._make_screens_module()
        with (
            mock.patch.object(module.pty, "fork", return_value=(123, 456)),
            mock.patch.object(module.fcntl, "ioctl", side_effect=OSError("ioctl")),
            mock.patch.object(module.os, "write", side_effect=OSError),
            mock.patch.object(module.os, "close") as close,
            mock.patch.object(module.os, "kill") as kill,
            mock.patch.object(module.os, "waitpid", return_value=(123, 0)) as waitpid,
            mock.patch.object(module, "_descendant_pids", return_value=[]),
            mock.patch.object(module.time, "sleep"),
        ):
            with self.assertRaisesRegex(OSError, "ioctl"):
                module._render_in_pty([], b"", 0.01, "/tmp")
        close.assert_called_once_with(456)
        kill.assert_called_once_with(123, module.signal.SIGTERM)
        waitpid.assert_called_once_with(123, module.os.WNOHANG)

    def test_pty_cleanup_kills_a_child_that_ignores_sigterm(self):
        module, _ = self._make_screens_module()
        # Die Anzahl aus dem Modul ableiten: Ein anderes Wartefenster dort darf
        # den Test nicht still an der falschen Stelle prüfen lassen.
        attempts = int(module.TERMINATE_GRACE / module.TERMINATE_POLL)
        waits = [(0, 0)] * attempts + [(123, 0)]
        with (
            mock.patch.object(module.os, "kill") as kill,
            mock.patch.object(module.os, "waitpid", side_effect=waits) as waitpid,
            mock.patch.object(module.time, "sleep"),
        ):
            module._terminate_pty_child(123)

        self.assertEqual(
            kill.call_args_list,
            [
                mock.call(123, module.signal.SIGTERM),
                mock.call(123, module.signal.SIGKILL),
            ],
        )
        self.assertEqual(waitpid.call_args_list[-1], mock.call(123, 0))

    def test_pty_cleanup_also_ends_the_git_descendants(self):
        """Nachfahren der TUI laufen in einer EIGENEN Prozessgruppe.

        Jeder Git-Aufruf von gmf startet eine eigene Session. Ein Signal an das
        PTY-Kind erreicht diese Enkel deshalb nicht — bricht die Aufnahme
        mitten in Scan, Commit oder Hook ab, blieben sie laufen. Der Generator
        muss sie über die Eltern-Kind-Kette selbst einsammeln.
        """
        module, _ = self._make_screens_module()
        parent = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys, time;"
                "child = subprocess.Popen(['/bin/sleep', '30'],"
                " start_new_session=True);"
                "print(child.pid, flush=True);"
                "time.sleep(30)",
            ],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            grandchild = int(parent.stdout.readline().strip())
            descendants = module._descendant_pids(parent.pid)
            self.assertIn(grandchild, descendants)

            module._terminate_pty_child(parent.pid)
            module._terminate_descendants(descendants)

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild, 0)
                except OSError:
                    break
                time.sleep(0.05)
            else:
                self.fail("git descendant survived the capture cleanup")
        finally:
            parent.stdout.close()
            parent.kill()
            parent.wait()

    def test_pty_cleanup_notes_the_descendants_before_the_pty_closes(self):
        """Der Schnappschuss muss VOR `os.close(fd)` liegen, nicht danach.

        Das PTY-Kind ist Sitzungsführer seines Terminals: Sobald der Generator
        den Master schließt, bekommt es SIGHUP und stirbt. Ein erst danach
        gezogener Schnappschuss über die Eltern-Kind-Kette findet die in einer
        eigenen Session gestarteten Git-/Hook-Enkel nicht mehr — sie hängen dann
        an init und liefen weiter. Dieser Test fährt genau diese Reihenfolge im
        echten `_render_in_pty()` und nicht in einem Nachbau ab.
        """
        module, _ = self._make_screens_module()
        owned_tmp = tempfile.mkdtemp(prefix="gmf-pty-cleanup-")
        pid_file = os.path.join(owned_tmp, "grandchild.pid")
        stand_in = os.path.join(owned_tmp, "stand_in.py")
        # Steht anstelle von gitmaster_flash.py im PTY: startet wie ein echter
        # Git-Aufruf einen Enkel in EIGENER Session und blockiert danach, damit
        # erst das Schließen des Masters die Aufnahme beendet.
        Path(stand_in).write_text(
            "import subprocess, sys, time\n"
            "child = subprocess.Popen(['/bin/sleep', '30'], start_new_session=True)\n"
            "with open(sys.argv[1], 'w') as fh:\n"
            "    fh.write(str(child.pid))\n"
            "sys.stdout.write('READY\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )

        grandchild = None
        try:
            with mock.patch.object(module, "GMF", stand_in):
                module._render_in_pty([pid_file], b"", 0.2, owned_tmp, ready_marker=b"READY")
            grandchild = int(Path(pid_file).read_text().strip())

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild, 0)
                except OSError:
                    break
                time.sleep(0.05)
            else:
                self.fail("Enkel in eigener Session hat die Aufnahme überlebt")
        finally:
            if grandchild is not None:
                try:
                    os.kill(grandchild, signal.SIGKILL)
                except OSError:
                    pass
            shutil.rmtree(owned_tmp, ignore_errors=True)

    def test_readme_screens_exist_in_both_languages(self):
        module, _ = self._make_screens_module()
        captures = {(name, lang) for name, lang, *_ in module.SCREENS}
        self.assertEqual(
            captures,
            {
                ("compact.svg", "en"),
                ("command-log.svg", "en"),
                ("overview.svg", "en"),
                ("publication.svg", "en"),
                ("compact.de.svg", "de"),
                ("command-log.de.svg", "de"),
                ("overview.de.svg", "de"),
                ("publication.de.svg", "de"),
            },
        )

    def test_demo_conflict_call_ignores_a_foreign_git_dir(self):
        """Der Stash-Konflikt der Demo lief als rohes subprocess.run: ohne den
        Env-Filter und ohne Blick auf den Exit-Code. Ein gesetztes GIT_DIR
        schlägt `-C <repo>` durch — der Aufruf landete dann in einem fremden
        Repo, und die Demo baute still ein falsches Bild."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, _ = gmf_module._demo_repo(root, "konflikt")
            (repo / "README.md").write_text("# konflikt\n\ngestasht\n")
            gmf_module._dgit(repo, "stash")
            (repo / "README.md").write_text("# konflikt\n\nim Commit\n")
            gmf_module._dgit(repo, "commit", "-qam", "conflicting change")

            fremd, _ = gmf_module._demo_repo(root, "fremd")
            with mock.patch.dict(os.environ, {"GIT_DIR": str(fremd / ".git")}):
                gmf_module._dgit_conflict(repo, "stash", "pop")

            status = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
            ).stdout
            self.assertIn("UU README.md", status)  # Konflikt im richtigen Repo
            stashes = subprocess.run(
                ["git", "-C", str(repo), "stash", "list"], capture_output=True, text=True
            ).stdout
            self.assertEqual(len(stashes.splitlines()), 1)  # Stash bleibt erhalten

    def test_demo_conflict_call_reports_every_other_exit_code(self):
        """Erwartet ist genau Exit 1 (Konflikt). Läuft der Aufruf glatt durch
        oder scheitert er anders, ist die Demo-Szene kaputt und muss auffallen."""
        with tempfile.TemporaryDirectory() as temp:
            repo, _ = gmf_module._demo_repo(Path(temp), "sauber")
            with self.assertRaises(subprocess.CalledProcessError):
                gmf_module._dgit_conflict(repo, "stash", "list")  # Exit 0
            with self.assertRaises(subprocess.CalledProcessError):
                gmf_module._dgit_conflict(repo, "rev-parse", "--verify", "gibtesnicht")  # Exit 128

    def test_demo_repos_ignore_user_attributes_excludes_and_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            sandbox = gmf_module.build_demo_sandbox(Path(temp))
            for repo in gmf_module.find_repos(sandbox, DEFAULT_CONFIG["skip_dirs"]):
                for key in ("core.excludesFile", "core.hooksPath", "core.attributesFile"):
                    with self.subTest(repo=repo.name, key=key):
                        self.assertEqual(git(repo, "config", "--local", "--get", key), "/dev/null")

    def test_demo_commits_ignore_the_callers_git_environment(self):
        """Demo-Commit-IDs sind ein Vertrag (Bild-Check): GIT_*-Variablen der
        aufrufenden Shell dürfen Identität und damit die IDs nicht verschieben."""
        sabotage = {
            "GIT_AUTHOR_NAME": "Evil",
            "GIT_AUTHOR_EMAIL": "evil@example.invalid",
            "GIT_COMMITTER_NAME": "Evil",
            "GIT_COMMITTER_EMAIL": "evil@example.invalid",
        }
        heads = []
        for env in (sabotage, {}):
            with tempfile.TemporaryDirectory() as temp:
                with mock.patch.dict(os.environ, env):
                    repo, _ = gmf_module._demo_repo(Path(temp), "probe")
                head = subprocess.run(
                    ["git", "-C", str(repo), "log", "-1", "--format=%H %an %ae"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.split()
                heads.append(head)
        self.assertEqual(heads[0], heads[1])
        self.assertEqual(heads[0][1:], ["Demo", "demo@example.invalid"])

    @unittest.skipUnless(shutil.which("zsh"), "zsh unavailable")
    def test_installer_quotes_weird_clone_path(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo = base / 'My Projects; "quoted"'
            make_installer_fixture(source, repo)
            zdot = base / "zdot"
            zdot.mkdir()
            env = dict(
                os.environ,
                HOME=str(base / "home"),
                ZDOTDIR=str(zdot),
                XDG_CONFIG_HOME=str(base / "config"),
            )
            result = subprocess.run(
                [str(repo / "install.sh"), "--shell", "zsh"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            sourced = subprocess.run(
                [
                    "zsh",
                    "-fc",
                    'source "$1"; print -r -- "$GMF_SCRIPT"',
                    "zsh",
                    str(zdot / ".zshrc"),
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(sourced.returncode, 0, sourced.stderr)
            self.assertEqual(
                Path(sourced.stdout.strip()).resolve(), (repo / "gitmaster_flash.py").resolve()
            )
