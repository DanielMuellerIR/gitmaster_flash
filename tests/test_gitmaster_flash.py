"""Headless-Tests für die reine Logik (Parsing, Sicherheitsregeln, Repo-Scan).

Die TUI selbst wird nicht getestet — die Datensammlung dafür schon:
gegen ein echtes, temporär angelegtes Git-Repo.
"""

import io
import json
import importlib.util
import curses
import math
import os
import queue
import re
import shlex
import shutil
import signal
import stat
import string
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gitmaster_flash as gmf_module  # noqa: E402

from gitmaster_flash import (  # noqa: E402
    DEFAULT_CONFIG, __version__, ChangedFile, CommitSafetyError, RemoteStatus,
    RepoStatus, TUI,
    build_info_view, check_remote, classify_remote_check, collect_status, find_repos,
    canonical_remote_target, cell_width, commit_selected, diff_status, _remote_root,
    detect_sync_remote, display_remote_url, fetch_remote_status, file_diff,
    inspect_transfer, is_github_url, pad_cells, parse_porcelain, read_branches,
    remote_check_message, repo_info_lines, stash_preview, status_dict,
    safe_push_args, terminal_text, truncate_cells, upstream_delta,
)

# Die Defaults erkennen nur "origin" als Sync-Remote. Tests, die einen anders
# benannten Sync-Remote brauchen, nehmen diese Kopie.
SYNC_CONFIG = {**DEFAULT_CONFIG, "sync_remote_names": ["backup"]}


class TestParsePorcelain(unittest.TestCase):
    def test_counts_and_letters(self):
        output = "\0".join([
            " M geändert.py",
            "M  gestaged.py",
            " D geloescht.txt",
            "?? neu.md",
            "?? unterordner/noch-neu.md",
            "A  hinzugefuegt.py",
            "",
        ])
        m, d, u, c, files = parse_porcelain(output)
        self.assertEqual((m, d, u, c), (3, 1, 2, 0))
        self.assertIn(ChangedFile("U", "neu.md", "??"), files)
        self.assertIn(ChangedFile("D", "geloescht.txt", " D"), files)
        self.assertIn(ChangedFile("M", "gestaged.py", "M "), files)

    def test_raw_status_survives_the_simplification(self):
        # Die Anzeige kürzt alles auf M — fürs Zurücksetzen muss aber
        # unterscheidbar bleiben, WO die Änderung liegt.
        output = "\0".join([" M nur-arbeitsbaum.py", "M  nur-index.py",
                            "MM beides.py", "A  neu-hinzugefuegt.py", ""])
        *_, files = parse_porcelain(output)
        self.assertEqual([f.code for f in files], ["M", "M", "M", "M"])
        self.assertEqual([f.xy for f in files], [" M", "M ", "MM", "A "])

    def test_conflicts_detected_first(self):
        # UU/UD/AA sind Merge-Konflikte und dürfen NICHT als M oder D zählen.
        output = "UU beide.txt\0UD ich-geloescht.txt\0AA beide-neu.txt\0"
        m, d, u, c, files = parse_porcelain(output)
        self.assertEqual((m, d, u, c), (0, 0, 0, 3))
        self.assertIn(ChangedFile("C", "beide.txt", "UU"), files)
        self.assertIn(ChangedFile("C", "ich-geloescht.txt", "UD"), files)

    def test_unicode_and_control_characters_remain_literal(self):
        output = "?? übungen/Abendsession 2026-07-21.pdf\0?? zeile\numbruch.txt\0"
        _, _, untracked, _, files = parse_porcelain(output)
        self.assertEqual(untracked, 2)
        self.assertIn(ChangedFile("U", "übungen/Abendsession 2026-07-21.pdf", "??"),
                      files)
        self.assertIn(ChangedFile("U", "zeile\numbruch.txt", "??"), files)

    def test_rename_keeps_destination_and_source(self):
        # Ein Rename ist Ziel UND Quelle: nur mit beiden Pfaden kann die
        # Commit-Hilfe den Rename komplett stagen — sonst committet sie eine
        # Kopie und die Löschung des alten Namens bleibt zurück.
        # Beide Hälften tragen dasselbe rohe "R ", nur daran sind sie später als
        # zusammengehörig erkennbar.
        output = "R  neu ü.txt\0alt ü.txt\0 M danach.txt\0"
        modified, deleted, _, _, files = parse_porcelain(output)
        self.assertEqual((modified, deleted), (2, 1))
        self.assertTrue(files[0].rename_group)
        self.assertEqual(files[0].rename_group, files[1].rename_group)
        self.assertEqual(files[0]._replace(rename_group=""),
                         ChangedFile("M", "neu ü.txt", "R "))
        self.assertEqual(files[1]._replace(rename_group=""),
                         ChangedFile("D", "alt ü.txt", "R "))
        self.assertEqual(files[2], ChangedFile("M", "danach.txt", " M"))

    def test_copy_does_not_invent_a_deletion(self):
        # Bei einer Kopie bleibt die Quelle unverändert liegen.
        output = "C  kopie.txt\0quelle.txt\0"
        modified, deleted, _, _, files = parse_porcelain(output)
        self.assertEqual((modified, deleted), (1, 0))
        self.assertEqual(files, [ChangedFile("M", "kopie.txt", "C ")])

    def test_empty(self):
        self.assertEqual(parse_porcelain(""), (0, 0, 0, 0, []))


class TranslationContractTests(unittest.TestCase):
    def test_every_user_text_has_matching_english_and_german_placeholders(self):
        formatter = string.Formatter()

        def placeholders(text):
            return {field for _, field, _, _ in formatter.parse(text)
                    if field is not None}

        for key, translations in gmf_module.TR.items():
            with self.subTest(key=key):
                self.assertEqual(set(translations), {"en", "de"})
                self.assertEqual(placeholders(translations["en"]),
                                 placeholders(translations["de"]))


class ConfirmDialogTests(unittest.TestCase):
    """Die Rückfrage kann eine dritte Antwort anbieten, ohne dass die
    bestehenden Ja/Nein-Aufrufer etwas davon merken."""

    def ask(self, keys, extra_key=""):
        drawn = []

        class Screen:
            def __init__(self):
                self.keys = iter(keys)

            def getmaxyx(self): return (24, 80)
            def addstr(self, y, x, text, *a): drawn.append(text)
            def refresh(self): pass
            def getch(self): return next(self.keys)

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            return ui.confirm("Wirklich?", extra_key), drawn

    def test_yes_and_no_are_unchanged(self):
        self.assertIs(self.ask([ord("j")])[0], True)
        self.assertIs(self.ask([ord("y")])[0], True)
        self.assertIs(self.ask([ord("n")])[0], False)
        self.assertIs(self.ask([27])[0], False)

    def test_extra_key_answers_with_itself_in_both_cases(self):
        self.assertEqual(self.ask([ord("a")], extra_key="A")[0], "A")
        self.assertEqual(self.ask([ord("A")], extra_key="A")[0], "A")

    def test_extra_key_is_ignored_when_not_offered(self):
        # Ohne dritte Antwort darf ein A nicht versehentlich etwas auslösen:
        # der Dialog wartet weiter, hier bis zum folgenden N.
        self.assertIs(self.ask([ord("a"), ord("n")])[0], False)

    def test_the_offered_key_appears_in_the_question(self):
        _, drawn = self.ask([ord("n")], extra_key="A")
        self.assertIn("A)", drawn[0])


class PagerConfirmTests(unittest.TestCase):
    """Die GitHub-Vorschau fragt in ihrer eigenen Ansicht nach: J/Y + ⏎
    bestätigt, alles andere bricht ab, und die Liste bleibt dabei scrollbar.
    Seit 2026-08-22 ersetzt das den getippten Satz „PUSH <remote>" nach dem
    Schließen des Pagers, der unten auf der Liste leicht übersehen wurde."""

    LINES = [f"line {i}" for i in range(1, 31)]

    def run_dialog(self, keys, size=(12, 60)):
        drawn = []   # (Zeile, Text) in Zeichenreihenfolge

        class Screen:
            def __init__(self):
                self.keys = iter(keys)

            def getmaxyx(self): return size
            def erase(self): drawn.append((None, "<erase>"))
            def addstr(self, y, x, text, *a): drawn.append((y, text))
            def move(self, *_args): pass
            def refresh(self): pass
            def get_wch(self): return next(self.keys)

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            result = ui.confirm_in_pager("Preview", self.LINES, "Publish? ")
        return result, drawn

    def test_yes_needs_return_in_both_languages(self):
        self.assertIs(self.run_dialog(["j", "\n"])[0], True)
        self.assertIs(self.run_dialog(["Y", "\r"])[0], True)
        self.assertIs(self.run_dialog(["j", "a", "\n"])[0], True)
        self.assertIs(self.run_dialog(["y", "e", "s", "\n"])[0], True)
        # Ohne ⏎ entscheidet ein J nichts: Der Dialog wartet auf die nächste
        # Taste (hier gibt es keine mehr). Ein bloßes J darf nicht reichen, weil
        # das gewohnte ⏎ danach sonst die Liste trifft — dort heißt ⏎ „beenden
        # und ins Repo wechseln".
        with self.assertRaises(StopIteration):
            self.run_dialog(["j"])

    def test_anything_but_yes_cancels(self):
        self.assertIs(self.run_dialog(["n", "\n"])[0], False)
        self.assertIs(self.run_dialog(["\n"])[0], False)
        self.assertIs(self.run_dialog(["q", "\n"])[0], False)
        self.assertIs(self.run_dialog(["j", "x", "\n"])[0], False)
        self.assertIs(self.run_dialog(["\x1b"])[0], False)

    def test_backspace_edits_the_answer(self):
        self.assertIs(self.run_dialog(["n", "\x7f", "j", "\n"])[0], True)
        self.assertIs(self.run_dialog(["j", curses.KEY_BACKSPACE, "\n"])[0], False)

    def test_scrolling_keeps_the_answer_and_the_list_visible(self):
        # 12 Zeilen hoch: Titel, 9 Textzeilen, Rückfrage, Fußzeile.
        result, drawn = self.run_dialog(
            [curses.KEY_DOWN, curses.KEY_NPAGE, "j", curses.KEY_UP, "\n"])
        self.assertIs(result, True)
        footers = [text for y, text in drawn if y == 11]
        self.assertIn(gmf_module.t("pager_footer_confirm", a=11, b=19, n=30).ljust(59),
                      footers)
        self.assertIn(gmf_module.t("pager_footer_confirm", a=10, b=18, n=30).ljust(59),
                      footers)
        # Die Antwort bleibt beim Scrollen stehen und steht in der vorletzten Zeile.
        self.assertIn("Publish? j".ljust(58), [text for y, text in drawn if y == 10])
        body_rows = {y for y, text in drawn if y is not None and 1 <= y <= 9}
        self.assertEqual(body_rows, set(range(1, 10)))


class TestRemoteBadges(unittest.TestCase):
    def test_synced_remote_is_still_named(self):
        remote = RemoteStatus("backup", is_sync=True, branch_exists=True)
        self.assertEqual(remote.badge(), "backup")

    def test_delta_and_missing_branch(self):
        self.assertEqual(RemoteStatus("github", public=True, branch_exists=True,
                                      ahead=2, behind=3).badge(), "↑2↓3 github")
        self.assertEqual(RemoteStatus("archive").badge(), "? archive")

    def test_github_url_detection_is_name_independent(self):
        self.assertTrue(is_github_url("git@github.com:example/demo.git"))
        self.assertTrue(is_github_url("https://github.com/example/demo.git"))
        self.assertFalse(is_github_url("ssh://internal.example/demo.git"))


class TestRemoteUrlDisplay(unittest.TestCase):
    def test_query_and_fragment_are_not_shown(self):
        separator = chr(58) + chr(47) * 2
        base = "https" + separator + "github.com/example/demo.git"
        shown = display_remote_url(base + "?redacted=true#private")
        self.assertEqual(shown, base)
        self.assertNotIn("redacted", shown)

    def test_ssh_user_is_preserved(self):
        separator = chr(58) + chr(47) * 2
        address = "ssh" + separator + "git@github.com/example/demo.git"
        self.assertEqual(display_remote_url(address), address)

    def test_non_ssh_userinfo_is_fully_redacted(self):
        separator = chr(58) + chr(47) * 2
        address = "ftp" + separator + "access-token@example.invalid/repo.git"
        shown = display_remote_url(address)
        self.assertEqual(shown, "ftp" + separator + "example.invalid/repo.git")
        self.assertNotIn("access-token", shown)
        error = gmf_module.redact_remote_error("fatal: " + address)
        self.assertNotIn("access-token", error)

    def test_local_file_url_remains_visible(self):
        separator = chr(58) + chr(47) * 2
        address = "file" + separator + "/tmp/example.git"
        self.assertEqual(display_remote_url(address), address)


class SelectedLineColourTests(unittest.TestCase):
    """Die markierte Zeile darf ihre wichtigste Angabe nicht unlesbar machen."""

    def test_red_uses_light_text_instead_of_being_inverted(self):
        # Umkehren hieße bei Rot: schwarze Schrift auf sattem Rot — praktisch nicht
        # zu lesen, und ausgerechnet Rot trägt die dringenden Angaben (M:, D:, ↓).
        self.assertEqual(gmf_module.selected_pair(gmf_module.C_RED),
                         (gmf_module.C_SEL, False))

    def test_light_colours_keep_the_plain_inversion(self):
        for pair in (gmf_module.C_GREEN, gmf_module.C_YELLOW,
                     gmf_module.C_CYAN, gmf_module.C_DIM):
            self.assertEqual(gmf_module.selected_pair(pair), (pair, True))


class TestSeveritySort(unittest.TestCase):
    def test_dirty_before_clean(self):
        dirty = RepoStatus(path=Path("/x"), rel="x", modified=1)
        clean = RepoStatus(path=Path("/y"), rel="y")
        self.assertLess(dirty.severity(), clean.severity())

    def test_clean_and_synced_property(self):
        st = RepoStatus(path=Path("/x"), rel="x")
        self.assertTrue(st.clean_and_synced)
        st.stashes = ["stash@{0} WIP"]
        self.assertFalse(st.clean_and_synced)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def git_output(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True).stdout.strip()


class TestAgainstRealRepo(unittest.TestCase):
    """Integration: temporäres Repo anlegen und den Status-Sammler prüfen."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "demo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "a.md").write_text("hallo\n")
        git(self.repo, "add", "a.md")
        git(self.repo, "commit", "-qm", "erster Commit")

    def tearDown(self):
        self.tmp.cleanup()

    def _changed_gitlink(self) -> Path:
        nested = self.repo / "vendor" / "sub"
        nested.mkdir(parents=True)
        git(nested, "init", "-q", "-b", "main")
        git(nested, "config", "user.email", "t@example.invalid")
        git(nested, "config", "user.name", "T")
        (nested / "module.txt").write_text("one\n")
        git(nested, "add", "module.txt")
        git(nested, "commit", "-qm", "module base")
        git(self.repo, "add", "vendor/sub")
        git(self.repo, "commit", "-qm", "add module pointer")
        (nested / "module.txt").write_text("two\n")
        git(nested, "commit", "-qam", "module update")
        return nested

    def test_find_repos(self):
        (self.root / "kein-repo").mkdir()
        repos = find_repos(self.root, DEFAULT_CONFIG["skip_dirs"])
        self.assertEqual(repos, [self.repo])

    def test_fetch_scan_stays_below_sshd_max_startups(self):
        """Ein Kaltstart darf nicht zwölf SSH-Logins gleichzeitig eröffnen.

        OpenSSH verwirft beim verbreiteten ``MaxStartups 10:30:100`` sonst
        zufällig einzelne Verbindungen, bevor ControlMaster seinen gemeinsamen
        Socket aufgebaut hat. Der normale lokale Scan darf parallel bleiben.
        """
        with mock.patch("gitmaster_flash.find_repos", return_value=[]), \
                mock.patch("gitmaster_flash.concurrent.futures.ThreadPoolExecutor") as pool:
            gmf_module.collect_all(self.root, DEFAULT_CONFIG, fetch=True)
        pool.assert_called_once_with(max_workers=8)

    def test_each_fetch_opens_only_one_connection_at_a_time(self):
        """Die acht Repo-Worker helfen nichts, wenn Git INNERHALB eines Aufrufs
        weitere Verbindungen aufmacht. Mit `fetch.parallel` oder
        `submodule.fetchJobs` in der Benutzerkonfiguration taete es genau das,
        und der sshd-Default MaxStartups verwuerfe wieder einzelne davon.
        `--jobs=1` deckt beide Faelle ab."""
        remote = self.root / "origin.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(remote)],
            check=True, capture_output=True, text=True)
        git(self.repo, "remote", "add", "origin", str(remote))
        aufrufe = []
        echt = gmf_module.run_git_logged

        def merken(repo, *args, **kwargs):
            aufrufe.append(args)
            return echt(repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git_logged", side_effect=merken):
            collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        fetches = [a for a in aufrufe if "fetch" in a]
        self.assertGreaterEqual(len(fetches), 1, aufrufe)
        for fetch in fetches:
            self.assertIn("--jobs=1", fetch, aufrufe)
            self.assertIn("--no-tags", fetch, aufrufe)
            self.assertIn("--no-prune-tags", fetch, aufrufe)
            self.assertIn("--no-recurse-submodules", fetch, aufrufe)
            self.assertIn("--refmap=", fetch, aufrufe)

    def test_transfer_fetch_shows_a_busy_line_while_fetching(self):
        # Ein Fetch zu GitHub kann bei großen Repos lange dauern; ohne diese
        # Zeile sah die TUI in der Zeit eingefroren aus (Befund 2026-08-22).
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(remote)],
            check=True, capture_output=True, text=True)
        git(self.repo, "remote", "add", "origin", str(remote))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        drawn = []

        class Screen:
            def getmaxyx(self): return (30, 100)
            def addstr(self, y, x, text, *a): drawn.append((y, text))
            def refresh(self): pass

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            refreshed = ui._fetch_remote(st, "origin")

        self.assertIsNotNone(refreshed)
        busy = gmf_module.t("fetch_busy", r="origin")
        self.assertTrue(any(y == 29 and text.startswith(busy)
                            for y, text in drawn), drawn)

    def test_transfer_fetch_uses_every_fetch_guard(self):
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(remote)],
            check=True, capture_output=True, text=True)
        git(self.repo, "remote", "add", "origin", str(remote))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        class Screen:
            def getmaxyx(self): return (30, 100)
            def addstr(self, *a): pass
            def refresh(self): pass

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        calls = []
        original = gmf_module.run_git

        def remember(repo, *args, **kwargs):
            calls.append(args)
            return original(repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git_logged", side_effect=remember), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            refreshed = ui._fetch_remote(st, "origin")

        self.assertIsNotNone(refreshed)
        fetches = [args for args in calls if "fetch" in args]
        self.assertEqual(len(fetches), 1, calls)
        for option in ("--jobs=1", "--no-tags", "--no-prune-tags",
                       "--no-recurse-submodules", "--refmap="):
            self.assertIn(option, fetches[0])

    def test_fetch_never_dereferences_a_symbolic_tracking_ref(self):
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(remote)],
            check=True, capture_output=True, text=True)
        git(self.repo, "remote", "add", "origin", str(remote))
        branch = git_output(self.repo, "symbolic-ref", "--short", "HEAD")
        before = git_output(self.repo, "rev-parse", "HEAD")
        git(self.repo, "branch", "victim", before)
        git(self.repo, "symbolic-ref", f"refs/remotes/origin/{branch}",
            "refs/heads/victim")
        tree = git_output(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", before],
            input="remote tip\n", check=True,
            capture_output=True, text=True).stdout.strip()
        git(self.repo, "push", "-q", str(remote),
            f"{remote_tip}:refs/heads/{branch}")
        config = gmf_module.read_remote_configs(
            self.repo, DEFAULT_CONFIG)["origin"]

        gmf_module.fetch_remote_safely(
            self.repo, config, branch, DEFAULT_CONFIG["fetch_timeout"])

        self.assertEqual(git_output(self.repo, "rev-parse", "refs/heads/victim"),
                         before)
        self.assertEqual(git_output(self.repo, "rev-parse", "HEAD"), before)

    def test_transfer_fetch_blocks_newly_invalid_remote_urls(self):
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(remote)],
            check=True, capture_output=True, text=True)
        git(self.repo, "remote", "add", "origin", str(remote))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        class Screen:
            def getmaxyx(self): return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        for invalid in (
                "https://example.invalid:not-a-port/repo.git",
                "~gmf-user-that-does-not-exist/repo.git"):
            with self.subTest(url=invalid):
                git(self.repo, "remote", "set-url", "origin", invalid)
                with mock.patch.object(gmf_module, "run_git_logged") as run:
                    self.assertIsNone(ui._fetch_remote(st, "origin"))
                run.assert_not_called()

    def test_transfer_fetch_rejects_config_changed_during_the_network_call(self):
        first = self.root / "first.git"
        second = self.root / "second.git"
        for remote in (first, second):
            subprocess.run(
                ["git", "clone", "-q", "--bare", str(self.repo), str(remote)],
                check=True, capture_output=True, text=True)
        git(self.repo, "remote", "add", "origin", str(first))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        class Screen:
            def getmaxyx(self): return (30, 100)
            def addstr(self, *a): pass
            def refresh(self): pass

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]

        def change_config(*_args, **_kwargs):
            git(self.repo, "remote", "set-url", "origin", str(second))
            return subprocess.CompletedProcess(["git", "fetch"], 0, "", "")

        with mock.patch.object(gmf_module, "fetch_remote_safely",
                               side_effect=change_config), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            refreshed = ui._fetch_remote(st, "origin")

        self.assertIsNone(refreshed)
        self.assertEqual(ui.message, gmf_module.t("transfer_changed"))

    def test_fetch_rolls_back_its_tracking_ref_after_config_change(self):
        first = self.root / "first.git"
        second = self.root / "second.git"
        for remote_path in (first, second):
            subprocess.run(
                ["git", "clone", "-q", "--bare", str(self.repo),
                 str(remote_path)], check=True, capture_output=True, text=True)
        git(self.repo, "remote", "add", "origin", str(first))
        branch = git_output(self.repo, "symbolic-ref", "--short", "HEAD")
        old_oid = git_output(self.repo, "rev-parse", "HEAD")
        tree = git_output(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", old_oid],
            input="remote tip\n", check=True, capture_output=True,
            text=True).stdout.strip()
        git(self.repo, "push", "-q", str(first),
            f"{remote_tip}:refs/heads/{branch}")
        # Ausgangs-Tracking-Ref gehört noch zum alten, freigegebenen Ziel.
        git(self.repo, "update-ref", f"refs/remotes/origin/{branch}", old_oid)
        config = gmf_module.read_remote_configs(
            self.repo, DEFAULT_CONFIG)["origin"]
        original = gmf_module.run_git_logged
        changed = False
        restored_name = False

        def change_after_update(repo, *args, **kwargs):
            nonlocal changed, restored_name
            result = original(repo, *args, **kwargs)
            if ("update-ref" in args and remote_tip in args and not changed):
                changed = True
                git(self.repo, "remote", "set-url", "origin", str(second))
            elif (changed and "update-ref" in args and old_oid in args
                  and not restored_name):
                # ABA: Vor der äußeren Nachprüfung sieht die Config wieder wie
                # am Anfang aus. Das interne Changed-Signal muss trotzdem leben.
                restored_name = True
                git(self.repo, "remote", "set-url", "origin", str(first))
            return result

        with mock.patch.object(
                gmf_module, "run_git_logged", side_effect=change_after_update), \
                self.assertRaises(gmf_module.RemoteConfigChangedError):
            gmf_module.fetch_remote_safely(self.repo, config, branch, 10)

        self.assertTrue(changed)
        self.assertTrue(restored_name)
        self.assertEqual(
            git_output(self.repo, "rev-parse",
                       f"refs/remotes/origin/{branch}"), old_oid)

    def test_batch_fetch_marks_a_timed_out_tracking_cas_as_unknown(self):
        remote_path = self.root / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo),
             str(remote_path)], check=True, capture_output=True, text=True)
        git(self.repo, "remote", "add", "origin", str(remote_path))
        branch = git_output(self.repo, "symbolic-ref", "--short", "HEAD")
        before = git_output(self.repo, "rev-parse", "HEAD")
        tree = git_output(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", before],
            input="remote tip\n", check=True, capture_output=True,
            text=True).stdout.strip()
        git(self.repo, "push", "-q", str(remote_path),
            f"{remote_tip}:refs/heads/{branch}")
        original = gmf_module.run_git_logged
        timed_out = False

        def update_then_timeout(repo, *args, **kwargs):
            nonlocal timed_out
            result = original(repo, *args, **kwargs)
            if ("update-ref" in args and remote_tip in args and not timed_out):
                timed_out = True
                raise subprocess.TimeoutExpired(
                    ["git", "-C", str(repo), "update-ref"], 1)
            return result

        with mock.patch.object(
                gmf_module, "run_git_logged", side_effect=update_then_timeout):
            status = collect_status(
                self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        self.assertTrue(timed_out)
        self.assertEqual(
            git_output(self.repo, "rev-parse",
                       f"refs/remotes/origin/{branch}"), remote_tip)
        remote = next(item for item in status.remotes
                      if item.name == "origin")
        self.assertEqual(remote.fetch_outcome, "outcome_unknown")
        self.assertIn("may already have changed", remote.fetch_error_long)

    def test_parent_fetch_never_recurses_into_unchecked_submodule_config(self):
        parent_remote = self.root / "parent.git"
        git(self.root, "init", "-q", "--bare", "--initial-branch=main",
            str(parent_remote))
        git(self.repo, "branch", "-M", "main")
        git(self.repo, "remote", "add", "origin", str(parent_remote))
        git(self.repo, "push", "-qu", "origin", "main")

        sub_source = self.root / "sub-source"
        sub_source.mkdir()
        git(sub_source, "init", "-q", "-b", "main")
        git(sub_source, "config", "user.email", "test@example.invalid")
        git(sub_source, "config", "user.name", "Test")
        (sub_source / "sub.txt").write_text("base\n")
        git(sub_source, "add", "sub.txt")
        git(sub_source, "commit", "-qm", "base")
        sub_remote = self.root / "sub.git"
        git(self.root, "init", "-q", "--bare", "--initial-branch=main",
            str(sub_remote))
        git(sub_source, "remote", "add", "origin", str(sub_remote))
        git(sub_source, "push", "-qu", "origin", "main")
        subprocess.run(
            ["git", "-c", "protocol.file.allow=always", "-C", str(self.repo),
             "submodule", "add", "-q", str(sub_remote), "sub"],
            check=True, capture_output=True, text=True)
        git(self.repo, "commit", "-qam", "add submodule")
        git(self.repo, "push", "-q", "origin", "main")

        sub = self.repo / "sub"
        git(sub, "config", "user.email", "test@example.invalid")
        git(sub, "config", "user.name", "Test")
        before = gmf_module.current_head(sub, 10)
        git(sub, "branch", "victim", before)
        (sub / "sub.txt").write_text("remote\n")
        git(sub, "commit", "-qam", "remote victim")
        git(sub, "push", "-q", "origin", "HEAD:refs/heads/victim")
        git(sub, "switch", "--detach", "-q", before)
        git(sub, "config", "--unset-all", "remote.origin.fetch")
        git(sub, "config", "--add", "remote.origin.fetch",
            "+refs/heads/*:refs/heads/*")
        git(self.repo, "config", "fetch.recurseSubmodules", "true")

        collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        self.assertEqual(subprocess.run(
            ["git", "-C", str(sub), "rev-parse", "refs/heads/victim"],
            check=True, capture_output=True, text=True).stdout.strip(), before)

    def test_fetch_never_prunes_local_tags_from_configuration(self):
        """`remote.*.pruneTags` darf die Zusage "nur Tracking-Refs" nicht umgehen."""
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(remote)],
            check=True, capture_output=True, text=True)
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "tag", "local-only")
        git(self.repo, "config", "remote.origin.pruneTags", "true")

        collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        result = gmf_module.run_git(
            self.repo, "show-ref", "--verify", "--quiet",
            "refs/tags/local-only", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_scan_keeps_full_parallelism(self):
        with mock.patch("gitmaster_flash.find_repos", return_value=[]), \
                mock.patch("gitmaster_flash.concurrent.futures.ThreadPoolExecutor") as pool:
            gmf_module.collect_all(self.root, DEFAULT_CONFIG, fetch=False)
        pool.assert_called_once_with(max_workers=12)

    def test_status_dirty_untracked_stash(self):
        (self.repo / "a.md").write_text("geändert\n")
        (self.repo / "neu.txt").write_text("neu\n")
        git(self.repo, "stash", "--include-untracked")
        (self.repo / "wieder.txt").write_text("x\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.rel, "demo")
        self.assertEqual(st.untracked, 1)          # wieder.txt
        self.assertEqual(len(st.stashes), 1)       # der Stash von oben
        self.assertEqual(st.remote_state, "no-remote")
        self.assertFalse(st.clean_and_synced)

    def test_unicode_path_from_status_can_be_staged(self):
        # Der reale Fehlerfall: Git maskierte den Umlaut ohne -z als
        # "\\303\\274bungen/..."; GMF reichte diese Anzeige an git add weiter.
        dirname = "u\u0308bungen"
        filename = "Abendsession2026-07-21-aufgaben.pdf"
        (self.repo / dirname).mkdir()
        (self.repo / dirname / "bestehend.txt").write_text("schon getrackt\n")
        git(self.repo, "add", "--", f"{dirname}/bestehend.txt")
        git(self.repo, "commit", "-qm", "Übungsordner anlegen")
        (self.repo / dirname / filename).write_bytes(b"test")

        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.untracked, 1)
        path = st.files[0][1]
        self.assertEqual(unicodedata.normalize("NFC", path), f"übungen/{filename}")

        # Derselbe unveränderte Wert, den der Commit-Assistent nutzt, muss ein
        # gültiger Pathspec sein. Das testet Gits echte macOS-Normalisierung mit.
        git(self.repo, "add", "--", path)
        tracked = subprocess.run(
            ["git", "-C", str(self.repo), "ls-files", "--error-unmatch", "--", path],
            capture_output=True, text=True,
        )
        self.assertEqual(tracked.returncode, 0, tracked.stderr)

    def test_stash_pop_conflict_keeps_stash(self):
        # Ein Stash, dessen Änderungen mit dem inzwischen committeten Stand
        # kollidieren -> pop erzeugt einen Konflikt,
        # der Stash bleibt erhalten. Das Tool muss das als Konflikt (C) erkennen,
        # nicht als simple Modifikation (M).
        (self.repo / "a.md").write_text("stash-variante\n")
        git(self.repo, "stash")                              # Stash mit Änderung an a.md
        (self.repo / "a.md").write_text("andere-variante\n")  # kollidierende Änderung
        git(self.repo, "commit", "-qam", "kollidierender Commit")
        # pop schlägt fehl (Konflikt); Rückgabecode != 0, Stash bleibt
        res = subprocess.run(["git", "-C", str(self.repo), "stash", "pop"],
                             capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.conflicts, 1)
        self.assertEqual(st.modified, 0)
        self.assertEqual(len(st.stashes), 1)   # Stash NICHT verloren
        self.assertTrue(st.dirty)

    def test_status_clean_without_remote(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertFalse(st.dirty)
        # sauber, aber ohne Sync-Remote -> nicht "synchron"
        self.assertFalse(st.clean_and_synced)
        self.assertEqual(st.branch, "main" if st.branch == "main" else st.branch)

    def test_repo_info_contains_clickable_github_url_and_useful_details(self):
        git(self.repo, "tag", "v1.0")
        git(self.repo, "remote", "add", "origin",
            "https://github.com/example/demo.git")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        lines = repo_info_lines(st, DEFAULT_CONFIG)
        text = "\n".join(lines)

        self.assertRegex(text, rf"Path: +{re.escape(str(self.repo))}")
        self.assertRegex(text, rf"Branch: +{st.branch}")
        self.assertIn("HEAD:", text)
        self.assertIn("Last commit:", text)
        self.assertRegex(text, r"Working tree: +clean")
        self.assertRegex(text, r"Tags at HEAD: +v1\.0")
        self.assertIn("origin [sync, GitHub]", text)
        # Identische Fetch- und Push-Adresse steht in einer Zeile, nicht zweimal.
        self.assertRegex(text, r"fetch\+push: +https://github\.com/example/demo\.git")
        self.assertNotIn("\n    push:", text)
        self.assertRegex(text, r"web: +https://github\.com/example/demo")
        # Die Commit-Betreffzeile steht direkt unter "Last commit".
        subject_index = next(i for i, line in enumerate(lines) if "erster Commit" in line)
        self.assertIn("Last commit:", lines[subject_index - 1])

    def test_info_values_start_in_the_same_column(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/example/demo.git")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        lines = repo_info_lines(st, DEFAULT_CONFIG)

        def value_columns(candidates):
            """Spalte, in der der Wert beginnt — für jede Label/Wert-Zeile."""
            columns = set()
            for line in candidates:
                label, _, value = line.partition(":")
                if value.strip():
                    columns.add(len(line) - len(value.lstrip()))
            return columns

        head = [line for line in lines if not line.startswith(" ")]
        details = [line for line in lines if line.startswith("    ")]
        self.assertEqual(len(value_columns(head)), 1, head)
        self.assertEqual(len(value_columns(details)), 1, details)

    def test_file_preview_never_hides_a_changed_gitlink(self):
        self._changed_gitlink()
        git(self.repo, "config", "diff.ignoreSubmodules", "all")
        git(self.repo, "config", "submodule.vendor/sub.ignore", "all")

        ok, preview = file_diff(self.repo, "M", "vendor/sub", 10)

        self.assertTrue(ok, preview)
        self.assertIn("Subproject commit", preview)

    def test_stash_preview_never_hides_a_staged_gitlink(self):
        self._changed_gitlink()
        git(self.repo, "add", "vendor/sub")
        tree = git_output(self.repo, "write-tree")
        head = git_output(self.repo, "rev-parse", "HEAD")
        index_commit = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", head],
            input="stash index\n", check=True, capture_output=True,
            text=True).stdout.strip()
        stash_commit = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree,
             "-p", head, "-p", index_commit],
            input="stash worktree\n", check=True, capture_output=True,
            text=True).stdout.strip()
        git(self.repo, "stash", "store", "-m", "gitlink update", stash_commit)
        git(self.repo, "config", "diff.ignoreSubmodules", "all")
        git(self.repo, "config", "submodule.vendor/sub.ignore", "all")
        oid, _ = gmf_module.latest_stash(self.repo, 10)

        ok, preview = stash_preview(self.repo, 10, oid)

        self.assertTrue(ok, preview)
        self.assertIn("Subproject commit", preview)


class TestUpstreamDeltaTwoRemotes(unittest.TestCase):
    """Zwei Remotes: Der Branch trackt einen NICHT-Sync-Remote (github) und ist
    ihm voraus, ist aber mit dem Sync-Remote (backup) synchron. Genau dieser Fall
    soll sauber gelten und trotzdem den Zusatz-Badge zeigen."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Zwei bare-Repos als Remotes.
        self.gh = self.root / "github.git"
        self.bk = self.root / "backup.git"
        for bare in (self.gh, self.bk):
            bare.mkdir()
            git(bare, "init", "-q", "--bare")
        self.repo = self.root / "demo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        git(self.repo, "remote", "add", "github", str(self.gh))
        git(self.repo, "remote", "add", "backup", str(self.bk))
        (self.repo / "a.md").write_text("1\n")
        git(self.repo, "add", "a.md")
        git(self.repo, "commit", "-qm", "c1")
        git(self.repo, "push", "-q", "github", "main")
        git(self.repo, "push", "-q", "backup", "main")
        # main trackt github/main, nicht den Sync-Remote.
        git(self.repo, "branch", "--set-upstream-to=github/main", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ahead_of_upstream_but_synced_with_sync_remote(self):
        # Zwei weitere Commits, nur zum Sync-Remote gepusht -> 2 voraus ggü. github.
        for n in (2, 3):
            (self.repo / "a.md").write_text(f"{n}\n")
            git(self.repo, "commit", "-qam", f"c{n}")
        git(self.repo, "push", "-q", "backup", "main")

        up, ahead, behind = upstream_delta(self.repo, "backup", SYNC_CONFIG)
        self.assertEqual(up, "github/main")
        self.assertEqual((ahead, behind), (2, 0))

        st = collect_status(self.repo, self.root, SYNC_CONFIG)
        self.assertEqual(st.remote, "backup")
        self.assertEqual((st.ahead, st.behind), (0, 0))     # mit backup synchron
        self.assertEqual(st.upstream, "github/main")
        self.assertEqual(st.upstream_ahead, 2)
        self.assertTrue(st.clean_and_synced)                # gilt trotzdem als sauber

        # Alle Remotes erscheinen, auch der synchrone Backup-Remote. Nach URL-
        # Klassifikation steht GitHub unabhängig vom Namen ganz rechts.
        git(self.repo, "remote", "set-url", "github",
            "https://github.com/example/demo.git")
        st = collect_status(self.repo, self.root, SYNC_CONFIG)
        self.assertEqual([r.name for r in st.remotes], ["backup", "github"])
        self.assertEqual([r.badge() for r in st.remotes], ["backup", "↑2 github"])
        self.assertFalse(st.remotes[0].public)
        self.assertTrue(st.remotes[-1].public)

    def test_mixed_fetch_and_push_url_is_visible_and_blockable(self):
        git(self.repo, "remote", "set-url", "github",
            "https://github.com/example/demo.git")
        git(self.repo, "remote", "set-url", "--push", "github", str(self.gh))
        st = collect_status(self.repo, self.root, SYNC_CONFIG)
        public = next(r for r in st.remotes if r.name == "github")
        self.assertTrue(public.public)
        self.assertTrue(public.mixed_public)

    def test_no_badge_when_upstream_is_sync_remote(self):
        # Trackt der Branch den Sync-Remote selbst, gibt es keinen Zusatz-Badge.
        git(self.repo, "branch", "--set-upstream-to=backup/main", "main")
        up, ahead, behind = upstream_delta(self.repo, "backup", SYNC_CONFIG)
        self.assertIsNone(up)

    def test_push_preflight_and_explicit_safe_refspec(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        self.assertEqual(check.ahead, 1)
        self.assertEqual(check.commits[0].split(" ", 1)[1], "c2")
        self.assertEqual(check.files, ["M\ta.md"])
        self.assertEqual(check.branch, "main")
        self.assertEqual(check.head_oid,
                         subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                        check=True, capture_output=True, text=True).stdout.strip())
        self.assertTrue(check.index_oid)
        self.assertTrue(check.worktree_fingerprint)
        self.assertEqual(check.fetch_fingerprint, check.push_fingerprint)
        self.assertTrue(check.target_oid)

        args = safe_push_args(
            check.transfer_url, "main", check.head_oid, check.target_oid)
        self.assertRegex(args[-2], r"^gmf-pin-[0-9a-f]{32}://approved$")
        self.assertEqual(args[-1], f"{check.head_oid}:refs/heads/main")
        self.assertIn(str(self.gh), gmf_module.format_git_command(args))
        self.assertIn("--no-follow-tags", args)
        self.assertIn("--recurse-submodules=no", args)
        self.assertIn(f"--force-with-lease=refs/heads/main:{check.target_oid}", args)
        self.assertNotIn("--force", args)
        self.assertNotIn("--tags", args)
        pushed = gmf_module.run_git(self.repo, *args, timeout=10)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(
            subprocess.run(["git", f"--git-dir={self.gh}", "rev-parse", "main"],
                           check=True, capture_output=True, text=True).stdout.strip(),
            subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                           check=True, capture_output=True, text=True).stdout.strip(),
        )

    def test_inherited_git_namespace_cannot_hide_a_safe_fetch(self):
        before = git_output(self.repo, "rev-parse", "HEAD")
        tree = git_output(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", before],
            input="remote tip\n", check=True, capture_output=True,
            text=True).stdout.strip()
        git(self.repo, "push", "-q", str(self.gh),
            f"{remote_tip}:refs/heads/main")
        remote = gmf_module.read_remote_configs(
            self.repo, DEFAULT_CONFIG)["github"]

        with mock.patch.dict(os.environ, {"GIT_NAMESPACE": "review-ns"}):
            fetched = gmf_module.fetch_remote_safely(
                self.repo, remote, "main", 10)

        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertEqual(
            git_output(self.repo, "rev-parse", "refs/remotes/github/main"),
            remote_tip)

    def test_inherited_git_namespace_cannot_redirect_a_safe_push(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")

        with mock.patch.dict(os.environ, {"GIT_NAMESPACE": "review-ns"}):
            check = inspect_transfer(self.repo, "github", "main", "push")
            self.assertTrue(check.ready, check.reason)
            pushed = gmf_module.run_git(
                self.repo, *safe_push_args(
                    check.transfer_url, check.branch, check.head_oid,
                    check.target_oid), timeout=10)

        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(
            subprocess.run(
                ["git", f"--git-dir={self.gh}", "rev-parse", "main"],
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
            check.head_oid,
        )

    def test_successful_url_push_advances_the_tracking_ref_with_oid_cas(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        pushed = gmf_module.run_git(
            self.repo, *safe_push_args(
                check.transfer_url, check.branch, check.head_oid,
                check.target_oid), timeout=10)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(
            git_output(self.repo, "rev-parse", check.remote_ref),
            check.target_oid)

        self.assertTrue(gmf_module.update_tracking_after_push(
            self.repo, check, 10))

        self.assertEqual(
            git_output(self.repo, "rev-parse", check.remote_ref),
            check.head_oid)
        self.assertEqual(
            inspect_transfer(self.repo, "github", "main", "push").reason,
            "nothing-push")

    def test_tracking_update_never_dereferences_a_symbolic_ref(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        git(self.repo, "branch", "victim", check.target_oid)
        git(self.repo, "symbolic-ref", check.remote_ref, "refs/heads/victim")

        updated = gmf_module.update_tracking_after_push(self.repo, check, 10)

        self.assertTrue(updated)
        self.assertEqual(
            git_output(self.repo, "rev-parse", "refs/heads/victim"),
            check.target_oid)
        self.assertEqual(
            git_output(self.repo, "rev-parse", check.remote_ref),
            check.head_oid)
        symbolic = subprocess.run(
            ["git", "-C", str(self.repo), "symbolic-ref", "-q",
             check.remote_ref], capture_output=True, text=True)
        self.assertNotEqual(symbolic.returncode, 0)

    def test_fetch_and_tracking_cas_do_not_run_reference_hooks(self):
        marker = self.root / "reference-hook-ran"
        hook = self.repo / ".git" / "hooks" / "reference-transaction"
        hook.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$1\" >> {shlex.quote(str(marker))}\n"
            f"cat >> {shlex.quote(str(marker))}\n")
        hook.chmod(0o755)
        before = git_output(self.repo, "rev-parse", "HEAD")
        tree = git_output(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", before],
            input="remote update\n", check=True, capture_output=True,
            text=True).stdout.strip()
        git(self.repo, "push", "-q", str(self.gh),
            f"{remote_tip}:refs/heads/main")
        marker.unlink(missing_ok=True)  # nur den folgenden gmf-Fetch messen
        config = gmf_module.read_remote_configs(
            self.repo, DEFAULT_CONFIG)["github"]

        fetched = gmf_module.fetch_remote_safely(
            self.repo, config, "main", 10)
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertFalse(marker.exists(),
                         marker.read_text() if marker.exists() else "")

        # Der Nachzug nach einem URL-gebundenen Push benutzt denselben
        # hookfreien, nicht dereferenzierenden Ref-CAS.
        git(self.repo, "merge", "--ff-only", "-q", "github/main")
        (self.repo / "a.md").write_text("local push\n")
        git(self.repo, "commit", "-qam", "local push")
        marker.unlink(missing_ok=True)  # der normale Benutzer-Commit darf Hooks nutzen
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready, check.reason)
        pushed = gmf_module.run_git(
            self.repo, *safe_push_args(
                check.transfer_url, check.branch, check.head_oid,
                check.target_oid), timeout=10)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertTrue(gmf_module.update_tracking_after_push(
            self.repo, check, 10))
        self.assertFalse(marker.exists())

    def test_tracking_ref_is_rolled_back_if_remote_config_changes_after_push(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        original = gmf_module.run_git_logged
        changed = False

        def change_config_before_update(repo, *args, **kwargs):
            nonlocal changed
            if "update-ref" in args and not changed:
                changed = True
                git(self.repo, "remote", "set-url", "github", str(self.bk))
                git(self.repo, "remote", "set-url", "--push", "github",
                    str(self.bk))
            return original(repo, *args, **kwargs)

        with mock.patch.object(
                gmf_module, "run_git_logged",
                side_effect=change_config_before_update):
            updated = gmf_module.update_tracking_after_push(self.repo, check, 10)

        self.assertTrue(changed)
        self.assertFalse(updated)
        self.assertEqual(git_output(self.repo, "rev-parse", check.remote_ref),
                         check.target_oid)

    def test_tracking_cas_failure_never_accepts_a_changed_remote_config(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)

        def concurrent_fetch_and_config_change(_repo, *_args, **_kwargs):
            git(self.repo, "update-ref", check.remote_ref, check.head_oid)
            git(self.repo, "remote", "set-url", "github", str(self.bk))
            git(self.repo, "remote", "set-url", "--push", "github",
                str(self.bk))
            return subprocess.CompletedProcess([], 1, "", "changed")

        with mock.patch.object(
                gmf_module, "run_git_logged",
                side_effect=concurrent_fetch_and_config_change):
            updated = gmf_module.update_tracking_after_push(self.repo, check, 10)

        self.assertFalse(updated)

    def test_public_preview_keeps_files_added_then_deleted_in_outgoing_history(self):
        secret = self.repo / "secret.env"
        secret.write_text("TOKEN=not-for-publication\n")
        git(self.repo, "add", "secret.env")
        git(self.repo, "commit", "-qm", "add private draft")
        secret.unlink()
        git(self.repo, "add", "-u")
        git(self.repo, "commit", "-qm", "remove private draft")

        check = inspect_transfer(self.repo, "github", "main", "push")

        self.assertTrue(check.ready)
        self.assertEqual(check.ahead, 2)
        self.assertTrue(any(line.endswith("\tsecret.env") for line in check.files),
                        check.files)

    def test_public_preview_never_hides_an_outgoing_gitlink_update(self):
        nested = self.repo / "vendor" / "sub"
        nested.mkdir(parents=True)
        git(nested, "init", "-q", "-b", "main")
        git(nested, "config", "user.email", "t@example.invalid")
        git(nested, "config", "user.name", "T")
        (nested / "module.txt").write_text("one\n")
        git(nested, "add", "module.txt")
        git(nested, "commit", "-qm", "module base")
        git(self.repo, "add", "vendor/sub")
        git(self.repo, "commit", "-qm", "add module pointer")
        git(self.repo, "config", "diff.ignoreSubmodules", "all")
        git(self.repo, "config", "submodule.vendor/sub.ignore", "all")

        check = inspect_transfer(self.repo, "github", "main", "push")

        self.assertTrue(check.ready)
        self.assertTrue(any(line.endswith("\tvendor/sub") for line in check.files),
                        check.files)

    def test_push_preflight_blocks_dirty_and_new_remote_branch(self):
        (self.repo / "dirty.txt").write_text("x\n")
        self.assertEqual(
            inspect_transfer(self.repo, "github", "main", "push").reason,
            "dirty",
        )
        (self.repo / "dirty.txt").unlink()
        git(self.repo, "checkout", "-qb", "new-branch")
        self.assertEqual(
            inspect_transfer(self.repo, "github", "new-branch", "push").reason,
            "missing-branch",
        )

    def test_transfer_preflight_cannot_hide_untracked_files_via_config(self):
        git(self.repo, "config", "status.showUntrackedFiles", "no")
        (self.repo / "untracked-secret.txt").write_text("not approved\n")

        self.assertEqual(
            inspect_transfer(self.repo, "github", "main", "push").reason,
            "dirty")

    def test_transfer_preflight_binds_the_expected_public_class(self):
        git(self.repo, "remote", "set-url", "github",
            "https://github.com/example/demo.git")
        self.assertEqual(
            inspect_transfer(
                self.repo, "github", "main", "push", expected_public=False).reason,
            "remote-unsafe")
        self.assertEqual(
            inspect_transfer(
                self.repo, "backup", "main", "push", expected_public=True).reason,
            "remote-unsafe")

    def test_transfer_preflight_fails_closed_on_an_unresolvable_local_url(self):
        git(self.repo, "remote", "set-url", "github",
            "~gmf-user-that-does-not-exist/repo.git")

        check = inspect_transfer(self.repo, "github", "main", "push")

        self.assertEqual(check.reason, "remote-unsafe")

    def test_transfer_preflight_handles_a_non_utf8_dirty_filename(self):
        original = gmf_module.run_git

        def non_utf8_status(repo, *args, **kwargs):
            if args[:2] == ("status", "--porcelain=v1"):
                return subprocess.CompletedProcess(
                    ["git", *args], 0, "?? dirty-\udcff\0", "")
            return original(repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git", side_effect=non_utf8_status):
            check = inspect_transfer(self.repo, "github", "main", "push")

        self.assertEqual(check.reason, "dirty")

    def test_transfer_preflight_rejects_a_checkout_between_ref_and_oid_reads(self):
        (self.repo / "a.md").write_text("main ahead\n")
        git(self.repo, "commit", "-qam", "main ahead")
        git(self.repo, "branch", "other", "HEAD^")
        original = gmf_module.run_git
        switched = False

        def race(repo, *args, **kwargs):
            nonlocal switched
            result = original(repo, *args, **kwargs)
            if args[:3] == ("symbolic-ref", "-q", "HEAD") and not switched:
                switched = True
                git(self.repo, "switch", "-q", "other")
            return result

        with mock.patch.object(gmf_module, "run_git", side_effect=race):
            check = inspect_transfer(self.repo, "github", "main", "push")

        self.assertTrue(switched)
        self.assertEqual(check.reason, "inspect-failed")

    def test_inherited_graft_cannot_turn_divergence_into_a_safe_push(self):
        base = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True).stdout.strip()
        (self.repo / "a.md").write_text("local\n")
        git(self.repo, "commit", "-qam", "local")
        local = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", base + "^{tree}"],
            check=True, capture_output=True, text=True).stdout.strip()
        remote = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree,
             "-p", base, "-m", "remote"], check=True, capture_output=True,
            text=True).stdout.strip()
        git(self.repo, "update-ref", "refs/remotes/github/main", remote)
        graft = self.root / "grafts"
        graft.write_text(f"{local} {remote}\n")
        inherited = {**os.environ, "GIT_GRAFT_FILE": str(graft)}
        fooled = subprocess.run(
            ["git", "-C", str(self.repo), "rev-list", "--left-right",
             "--count", f"{local}...{remote}"], check=True,
            capture_output=True, text=True, env=inherited).stdout.split()
        self.assertEqual(fooled, ["1", "0"])

        with mock.patch.dict(os.environ, {"GIT_GRAFT_FILE": str(graft)}):
            self.assertEqual(
                inspect_transfer(self.repo, "github", "main", "push").reason,
                "divergent")

    def test_repository_graft_cannot_turn_divergence_into_a_safe_push(self):
        base = git_output(self.repo, "rev-parse", "HEAD")
        (self.repo / "a.md").write_text("local\n")
        git(self.repo, "commit", "-qam", "local")
        local = git_output(self.repo, "rev-parse", "HEAD")
        tree = git_output(self.repo, "rev-parse", base + "^{tree}")
        remote = git_output(
            self.repo, "commit-tree", tree, "-p", base, "-m", "remote")
        git(self.repo, "update-ref", "refs/remotes/github/main", remote)
        grafts = self.repo / ".git" / "info" / "grafts"
        grafts.write_text(f"{local} {remote}\n")
        fooled = git_output(
            self.repo, "rev-list", "--left-right", "--count",
            f"{local}...{remote}").split()
        self.assertEqual(fooled, ["1", "0"])

        self.assertEqual(
            inspect_transfer(self.repo, "github", "main", "push").reason,
            "divergent")

    def test_transfer_rejects_a_refspec_that_maps_dev_to_main(self):
        git(self.repo, "config", "--unset-all", "remote.github.fetch")
        git(self.repo, "config", "--add", "remote.github.fetch",
            "+refs/heads/dev:refs/remotes/github/main")
        before = git_output(self.repo, "rev-parse", "HEAD")

        check = inspect_transfer(self.repo, "github", "main", "push")

        self.assertEqual(check.reason, "remote-unsafe")
        self.assertEqual(git_output(self.repo, "rev-parse", "HEAD"), before)

    def test_push_stays_bound_to_the_approved_url_after_config_changes(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        backup_before = git_output(self.bk, "rev-parse", "refs/heads/main")
        git(self.repo, "remote", "set-url", "--push", "github", str(self.bk))
        git(self.repo, "config", f"url.{self.bk}.pushInsteadOf",
            check.transfer_url)

        pushed = gmf_module.run_git(
            self.repo, *safe_push_args(
                check.transfer_url, check.branch, check.head_oid,
                check.target_oid), timeout=10)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)

        self.assertEqual(
            git_output(self.gh, "rev-parse", "refs/heads/main"), check.head_oid)
        self.assertEqual(
            git_output(self.bk, "rev-parse", "refs/heads/main"), backup_before)

    def test_pinned_push_handles_equals_and_beats_an_exact_rewrite_rule(self):
        destination = self.root / "archive=copy.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.gh), str(destination)],
            check=True, capture_output=True, text=True)
        before = git_output(destination, "rev-parse", "refs/heads/main")
        backup_before = git_output(self.bk, "rev-parse", "refs/heads/main")
        (self.repo / "a.md").write_text("equals-safe\n")
        git(self.repo, "commit", "-qam", "equals-safe")
        head = git_output(self.repo, "rev-parse", "HEAD")
        # Eine gleich lange, bereits konfigurierte Regel darf nicht gewinnen.
        git(self.repo, "config", f"url.{self.bk}.insteadOf", str(destination))

        args = safe_push_args(str(destination), "main", head, before)
        pushed = gmf_module.run_git(self.repo, *args, timeout=10)

        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(git_output(destination, "rev-parse", "main"), head)
        self.assertEqual(git_output(self.bk, "rev-parse", "main"), backup_before)

    def test_safe_push_does_not_run_a_pre_push_hook(self):
        marker = self.root / "pre-push-ran"
        hook = self.repo / ".git" / "hooks" / "pre-push"
        git(self.repo, "tag", "not-approved")
        hook.write_text(
            "#!/bin/sh\n"
            f"printf ran > {shlex.quote(str(marker))}\n"
            "git push \"$2\" refs/tags/not-approved\n")
        hook.chmod(0o755)
        (self.repo / "a.md").write_text("hook-safe\n")
        git(self.repo, "commit", "-qam", "hook-safe")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)

        pushed = gmf_module.run_git(
            self.repo, *safe_push_args(
                check.transfer_url, check.branch, check.head_oid,
                check.target_oid), timeout=10)

        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertFalse(marker.exists())
        tag = subprocess.run(
            ["git", f"--git-dir={self.gh}", "show-ref", "--verify", "--quiet",
             "refs/tags/not-approved"])
        self.assertNotEqual(tag.returncode, 0)

    def test_safe_push_clears_push_options_and_disables_signing(self):
        option_marker = self.root / "received-push-options"
        gpg_marker = self.root / "gpg-ran"
        receive_hook = self.gh / "hooks" / "pre-receive"
        receive_hook.write_text(
            "#!/bin/sh\n"
            f"printf '%s:%s\\n' \"${{GIT_PUSH_OPTION_COUNT-unset}}\" "
            f"\"${{GIT_PUSH_OPTION_0-unset}}\" > "
            f"{shlex.quote(str(option_marker))}\n")
        receive_hook.chmod(0o755)
        git(self.gh, "config", "receive.advertisePushOptions", "true")
        git(self.gh, "config", "receive.certNonceSeed", "test-seed")
        fake_gpg = self.root / "fake-gpg"
        fake_gpg.write_text(
            "#!/bin/sh\n"
            f"printf ran > {shlex.quote(str(gpg_marker))}\n"
            "exit 1\n")
        fake_gpg.chmod(0o755)
        git(self.repo, "config", "push.pushOption", "deploy=all")
        git(self.repo, "config", "push.gpgSign", "true")
        git(self.repo, "config", "gpg.program", str(fake_gpg))
        (self.repo / "a.md").write_text("push config safe\n")
        git(self.repo, "commit", "-qam", "push config safe")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready, check.reason)

        args = safe_push_args(
            check.transfer_url, check.branch, check.head_oid, check.target_oid)
        pushed = gmf_module.run_git(self.repo, *args, timeout=10)

        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(option_marker.read_text(), "0:unset\n")
        self.assertFalse(gpg_marker.exists())
        self.assertIn("push.pushOption=", args)
        self.assertIn("--no-signed", args)

    def test_local_symlink_switch_cannot_change_the_approved_push_target(self):
        link = self.root / "selected.git"
        link.symlink_to(self.gh)
        git(self.repo, "remote", "set-url", "github", str(link))
        git(self.repo, "remote", "set-url", "--push", "github", str(link))
        (self.repo / "a.md").write_text("symlink-safe\n")
        git(self.repo, "commit", "-qam", "symlink-safe")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        self.assertEqual(check.transfer_url, str(self.gh.resolve()))
        backup_before = git_output(self.bk, "rev-parse", "main")
        link.unlink()
        link.symlink_to(self.bk)

        pushed = gmf_module.run_git(
            self.repo, *safe_push_args(
                check.transfer_url, check.branch, check.head_oid,
                check.target_oid), timeout=10)

        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(git_output(self.gh, "rev-parse", "main"), check.head_oid)
        self.assertEqual(git_output(self.bk, "rev-parse", "main"), backup_before)

    def test_push_preflight_distinguishes_failed_ref_read_from_missing_branch(self):
        original = gmf_module.run_git

        def fail_show_ref(repo, *args, **kwargs):
            if args and args[0] == "show-ref":
                return subprocess.CompletedProcess([], 128, "", "broken refs")
            return original(repo, *args, **kwargs)

        with mock.patch("gitmaster_flash.run_git", side_effect=fail_show_ref):
            check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertEqual(check.reason, "inspect-failed")

    def test_target_lease_prevents_recreating_branch_deleted_after_approval(self):
        (self.repo / "a.md").write_text("2\n")
        git(self.repo, "commit", "-qam", "c2")
        check = inspect_transfer(self.repo, "github", "main", "push")
        self.assertTrue(check.ready)
        git(self.gh, "update-ref", "-d", "refs/heads/main")
        result = gmf_module.run_git(
            self.repo, *safe_push_args(
                check.transfer_url, "main", check.head_oid,
                check.target_oid), timeout=10)
        self.assertNotEqual(result.returncode, 0)
        missing = subprocess.run(["git", f"--git-dir={self.gh}", "show-ref", "--verify",
                                  "--quiet", "refs/heads/main"])
        self.assertNotEqual(missing.returncode, 0)

def _repo(rel, branch="main", remotes=(), modified=0, untracked=0,
          error="", remote_state="ok", fetch_error=False):
    # remotes: (name, ahead, behind) oder (name, ahead, behind, is_sync)
    return {"rel": rel, "branch": branch, "modified": modified,
            "untracked": untracked, "deleted": 0, "error": error,
            "remote_state": remote_state, "fetch_error": fetch_error,
            "remotes": [{"name": r[0], "ahead": r[1], "behind": r[2],
                         "sync": bool(r[3]) if len(r) > 3 else False}
                        for r in remotes]}


def _side(version="9.9.9", repos=()):
    return {"version": version, "root": "/x", "repos": list(repos)}


class DiffTests(unittest.TestCase):
    """--diff compares two machines. The split is the point: DRIFT (should be
    identical, isn't -> actionable) vs local (different branch, dirty -> explainable).
    A report that lists everything gets ignored."""

    def test_identical_means_no_output(self):
        s = [_repo("a", remotes=[("origin", 0, 0)])]
        self.assertEqual(diff_status(_side(repos=s), _side(repos=s), "here", "there"), [])

    def test_missing_remote_is_drift(self):
        """The core case: git never transfers remotes, so they drift silently."""
        a = _side(repos=[_repo("x", remotes=[("origin", 0, 0), ("github", 0, 0)])])
        b = _side(repos=[_repo("x", remotes=[("origin", 0, 0)])])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("DRIFT", out[0])
        self.assertIn("github", out[0])
        self.assertIn("here", out[0])

    def test_missing_remote_other_direction(self):
        a = _side(repos=[_repo("x", remotes=[("origin", 0, 0)])])
        b = _side(repos=[_repo("x", remotes=[("origin", 0, 0), ("github", 0, 0)])])
        out = diff_status(a, b, "here", "there")
        self.assertIn("there", out[0])

    def test_fetch_refspec_safety_is_part_of_remote_drift(self):
        for field in ("fetch_refspecs_safe", "branch_mapping_safe",
                      "fetch_url_safe", "push_url_safe"):
            with self.subTest(field=field):
                left_repo = _repo("x", remotes=[("origin", 0, 0)])
                right_repo = _repo("x", remotes=[("origin", 0, 0)])
                left_repo["remotes"][0][field] = True
                right_repo["remotes"][0][field] = False

                out = diff_status(
                    _side(repos=[left_repo]), _side(repos=[right_repo]),
                    "here", "there")

                self.assertEqual(len(out), 1, out)
                self.assertIn("DRIFT", out[0])
                self.assertIn("origin", out[0])

    def test_differing_remote_state_is_drift(self):
        a = _side(repos=[_repo("x", remotes=[("github", 4, 2)])])
        b = _side(repos=[_repo("x", remotes=[("github", 0, 0)])])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("DRIFT", out[0])
        # Die eigene Maschine steht ohne Praeposition da ("here", nicht "on here").
        self.assertNotIn("on here", out[0])
        self.assertIn("on there", out[0])

    def test_sync_remote_equally_behind_is_shown(self):
        """Beide Rechner gleichauf, aber gemeinsam hinter dem Sync-Remote: das ist
        im reinen Zwei-Rechner-Vergleich unsichtbar, aber genau die Zahl, die
        interessiert (wie weit hinter dem Hub?). -> eigene SYNC-Zeile."""
        s = [_repo("x", remotes=[("origin", 0, 2, True)])]
        out = diff_status(_side(repos=s), _side(repos=s), "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("SYNC", out[0])
        self.assertNotIn("DRIFT", out[0])

    def test_sync_remote_in_sync_stays_silent(self):
        s = [_repo("x", remotes=[("origin", 0, 0, True)])]
        self.assertEqual(diff_status(_side(repos=s), _side(repos=s), "here", "there"), [])

    def test_offline_side_does_not_fake_drift(self):
        # fetch_failed haengt am Netz des jeweiligen Rechners und darf den Vergleich
        # nicht beeinflussen — sonst meldete jede Offline-Seite lauter Unterschiede.
        a = _side(repos=[_repo("x", remotes=[("github", 0, 0)])])
        b = _side(repos=[_repo("x", remotes=[("github", 0, 0)])])
        a["repos"][0]["remotes"][0]["fetch_failed"] = True
        self.assertEqual(diff_status(a, b, "here", "there"), [])

    def test_nonsync_remote_equally_behind_stays_silent(self):
        # Fuer Nicht-Sync-Remotes (z.B. github) bleibt gleicher Stand = kein Report.
        s = [_repo("x", remotes=[("github", 0, 2)])]
        self.assertEqual(diff_status(_side(repos=s), _side(repos=s), "here", "there"), [])

    def test_sync_remote_reported_before_other_remotes(self):
        a = _side(repos=[_repo("x", remotes=[("github", 4, 0), ("origin", 1, 0, True)])])
        b = _side(repos=[_repo("x", remotes=[("github", 0, 0), ("origin", 0, 0, True)])])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 2)
        self.assertIn("origin", out[0])   # Sync-Remote zuerst, github danach
        self.assertIn("github", out[1])

    def test_failed_fetch_is_not_drift(self):
        """Der Vorfall vom 2026-08-05: `gmf --diff` laeuft auf der Gegenseite per
        ssh, dort kommt der Credential-Helper nicht an den Schluesselbund, und jedes
        GitHub-Remote scheitert. Das erschien als zwei DRIFT-Zeilen (`error`,
        `remote_state`) je Repo und las sich wie ein kaputter Login auf dem anderen
        Mac. Es ist aber ein Merkmal der messenden Sitzung, kein Unterschied."""
        a = _side(repos=[_repo("x"), _repo("y")])
        b = _side(repos=[
            _repo("x", error="github: Schlüsselbund unerreichbar",
                  remote_state="error", fetch_error=True),
            _repo("y", error="github: Schlüsselbund unerreichbar",
                  remote_state="error", fetch_error=True)])
        out = diff_status(a, b, "here", "there")
        self.assertEqual([l for l in out if "DRIFT" in l], [])
        # Verschwiegen wird es aber nicht: EINE Zeile nennt Seite und Anzahl.
        erklaerung = [l for l in out if "there" in l]
        self.assertEqual(len(erklaerung), 1, out)
        self.assertIn("2", erklaerung[0])

    def test_failed_fetch_still_compares_conflicts_and_stashes(self):
        """Nur `error` und `remote_state` haengen am Fetch. Ein Merge-Konflikt oder
        ein Stash auf genau einer Seite bleibt ein echter Unterschied — sonst wuerde
        ein gescheiterter Fetch echte Befunde mitverschlucken."""
        a = _side(repos=[_repo("x")])
        b = _side(repos=[_repo("x", remote_state="error", fetch_error=True)])
        b["repos"][0]["conflicts"] = 2
        a["repos"][0]["conflicts"] = 0
        out = diff_status(a, b, "here", "there")
        drift = [l for l in out if "DRIFT" in l]
        self.assertEqual(len(drift), 1, out)
        self.assertIn("conflicts", drift[0])

    def test_error_without_fetch_error_stays_drift(self):
        """Gegenprobe: Ein Fehler, der NICHT vom Fetch kommt (kaputtes Repo, lokaler
        Lesefehler), muss weiter als DRIFT erscheinen."""
        a = _side(repos=[_repo("x")])
        b = _side(repos=[_repo("x", error="kaputt", remote_state="error")])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len([l for l in out if "DRIFT" in l]), 2, out)

    def test_a_stale_tracking_ref_after_a_failed_fetch_is_no_drift(self):
        """Der Fetch dieses Remotes scheiterte auf einer Seite.

        Dort steht der Tracking-Ref vom letzten gelungenen Lauf, drueben der
        frische — und schon meldete gmf eine DRIFT-Zeile mit zwei
        Ahead/Behind-Paaren, direkt neben dem Satz, der Stand sei gar nicht
        messbar. Genau dieser Widerspruch war der Fehlalarm."""
        a = _side(repos=[_repo("x", remotes=[("origin", 0, 7, True)],
                               error="origin: Schlüsselbund unerreichbar",
                               remote_state="error", fetch_error=True)])
        b = _side(repos=[_repo("x", remotes=[("origin", 0, 0, True)])])
        a["repos"][0]["remotes"][0]["fetch_failed"] = True
        out = diff_status(a, b, "here", "there")
        self.assertEqual([l for l in out if "DRIFT" in l or "SYNC" in l], [], out)
        # Nur die eine Zeile, die die betroffene Seite benennt, bleibt uebrig.
        self.assertEqual(len(out), 1, out)

    def test_a_local_error_survives_a_failed_fetch_on_the_other_side(self):
        """Kreuzfall: hier ein gescheiterter Fetch, drueben ein echter lokaler
        Schaden (unlesbarer Index). Vorher blendete der Fetch-Fehler das Feld
        `error` auf BEIDEN Seiten aus — das Fetch-Problem hier versteckte den
        Repo-Schaden dort."""
        a = _side(repos=[_repo("x", error="origin: kein Netz",
                               remote_state="error", fetch_error=True)])
        b = _side(repos=[_repo("x", error="cannot read Git index")])
        out = diff_status(a, b, "here", "there")
        drift = [l for l in out if "DRIFT" in l]
        self.assertEqual(len(drift), 1, out)
        self.assertIn("error", drift[0])
        self.assertIn("cannot read Git index", drift[0])
        # Der fetchbedingte Fehler DIESER Seite bleibt trotzdem draussen.
        self.assertNotIn("kein Netz", drift[0])

    def test_different_branch_is_local_not_drift(self):
        a = _side(repos=[_repo("x", branch="main")])
        b = _side(repos=[_repo("x", branch="feature")])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertNotIn("DRIFT", out[0])

    def test_dirty_is_local_not_drift(self):
        a = _side(repos=[_repo("x", modified=2, untracked=1)])
        b = _side(repos=[_repo("x")])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertNotIn("DRIFT", out[0])
        self.assertIn("3", out[0])

    def test_repo_only_on_one_side(self):
        a = _side(repos=[_repo("here-only"), _repo("both")])
        b = _side(repos=[_repo("both")])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("here-only", out[0])

    def test_version_mismatch_is_flagged_first(self):
        out = diff_status(_side("1.0.0"), _side("2.0.0"), "here", "there")
        self.assertTrue(out[0].startswith("!"))

    def test_errors_conflicts_stashes_and_remote_state_are_compared(self):
        broken = _repo("x")
        broken.update(error="git failed", conflicts=2, stashes=1, remote_state="error")
        clean = _repo("x")
        clean.update(error="", conflicts=0, stashes=0, remote_state="ok")
        out = diff_status(_side(repos=[broken]), _side(repos=[clean]), "here", "there")
        joined = "\n".join(out)
        for field in ("error", "conflicts", "stashes", "remote_state"):
            self.assertIn(field, joined)

    def test_remote_endpoint_fingerprint_drift_is_compared(self):
        a = _repo("x", remotes=[("origin", 0, 0, True)])
        b = _repo("x", remotes=[("origin", 0, 0, True)])
        a["remotes"][0]["fetch_fingerprint"] = "aaa"
        b["remotes"][0]["fetch_fingerprint"] = "bbb"
        out = diff_status(_side(repos=[a]), _side(repos=[b]), "here", "there")
        self.assertTrue(any("security/endpoint" in line for line in out))

    def test_all_fetch_fingerprints_are_compared(self):
        a = _repo("x", remotes=[("origin", 0, 0, True)])
        b = _repo("x", remotes=[("origin", 0, 0, True)])
        a["remotes"][0]["fetch_fingerprints"] = ["aaa", "common"]
        b["remotes"][0]["fetch_fingerprints"] = ["bbb", "common"]
        out = diff_status(_side(repos=[a]), _side(repos=[b]), "here", "there")
        self.assertTrue(any("security/endpoint" in line for line in out))

    def test_branch_presence_on_same_branch_is_state_drift_not_security(self):
        """Gleicher Branch, aber nur eine Seite kennt ihn auf dem Remote: das ist
        ein Zustandsunterschied — keine Sicherheits-/Endpunkt-Meldung."""
        a = _repo("x", remotes=[("origin", 0, 0)])
        b = _repo("x", remotes=[("origin", 0, 0)])
        a["remotes"][0]["branch_exists"] = True
        b["remotes"][0]["branch_exists"] = False
        out = diff_status(_side(repos=[a]), _side(repos=[b]), "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("DRIFT", out[0])
        self.assertIn("main", out[0])
        self.assertIn("here", out[0])
        self.assertNotIn("security/endpoint", out[0])

    def test_branch_presence_with_different_branches_is_no_drift(self):
        """Zwei Rechner mit identischen Remotes, aber verschiedenen Branches:
        `branch_exists` hängt am ausgecheckten Branch und darf keine
        Sicherheits-Drift und keinen Exit 1 wegen Endpunkt-Identität erzeugen."""
        a = _repo("x", branch="main", remotes=[("origin", 0, 0)])
        b = _repo("x", branch="feature", remotes=[("origin", 0, 0)])
        a["remotes"][0]["branch_exists"] = True
        b["remotes"][0]["branch_exists"] = False
        out = diff_status(_side(repos=[a]), _side(repos=[b]), "here", "there")
        self.assertEqual(len(out), 1)          # nur die erklärbare local-Zeile
        self.assertNotIn("DRIFT", out[0])

    def test_remote_root_uses_remote_home(self):
        """Home dirs differ between machines (/Users/anna vs /home/bob) — the path
        must be resolved against the REMOTE $HOME, not pasted absolutely."""
        r = _remote_root(Path.home() / "git", None)
        self.assertEqual(r, "~/git")
        self.assertIn("git", r)
        self.assertNotIn(str(Path.home()), r)

    def test_remote_root_explicit_path_wins(self):
        self.assertEqual(_remote_root(Path.home() / "git", "/srv/code"), "/srv/code")

    def test_remote_root_outside_home_stays_absolute(self):
        self.assertEqual(_remote_root(Path("/srv/code"), None), "/srv/code")


class RemoteSecurityTests(unittest.TestCase):
    def test_endpoint_fingerprint_omits_credentials_query_and_git_suffix(self):
        a = canonical_remote_target("https://user:secret@example.com/org/repo.git?token=x")
        b = canonical_remote_target("https://example.com/org/repo")
        self.assertEqual(a, b)
        self.assertEqual(a.host, "example.com")
        self.assertNotIn("secret", a.fingerprint)

    def test_reserved_percent_escapes_do_not_alias_another_network_path(self):
        encoded_slash = canonical_remote_target(
            "https://example.com/org%2Frepo.git")
        literal_slash = canonical_remote_target(
            "https://example.com/org/repo.git")
        encoded_parent = canonical_remote_target(
            "https://example.com/org/%2e%2e/private.git")
        literal_parent = canonical_remote_target(
            "https://example.com/org/../private.git")
        self.assertNotEqual(encoded_slash, literal_slash)
        self.assertNotEqual(encoded_parent, literal_parent)

    def test_ipv6_host_and_port_have_an_unambiguous_identity(self):
        with_port = canonical_remote_target("ssh://[::1]:2222/repo.git")
        host_text_ending_in_port = canonical_remote_target(
            "https://[::1:2222]/repo.git")
        self.assertNotEqual(with_port, host_text_ending_in_port)

    def test_network_path_segments_and_tilde_semantics_stay_distinct(self):
        self.assertNotEqual(
            canonical_remote_target("https://example.com/org//repo.git"),
            canonical_remote_target("ssh://example.com/org/repo.git"))
        self.assertNotEqual(
            canonical_remote_target("https://example.com/~/repo.git"),
            canonical_remote_target("ssh://example.com/~/repo.git"))
        separator = chr(58) + chr(47) * 2
        alice = "ssh" + separator + "alice@example.com/~/repo.git"
        bob = "ssh" + separator + "bob@example.com/~/repo.git"
        self.assertNotEqual(canonical_remote_target(alice),
                            canonical_remote_target(bob))

    def test_github_host_must_match_exactly(self):
        self.assertTrue(is_github_url("ssh://git@github.com/org/repo.git"))
        self.assertFalse(is_github_url("ssh://github.com.attacker.invalid/org/repo.git"))
        self.assertFalse(is_github_url("https://example.invalid/github.com/org/repo"))

    def test_scp_user_belongs_to_the_identity_of_home_relative_paths(self):
        """Relative SCP-Pfade liegen im Home des SSH-Benutzers.

        alice@host:repo und bob@host:repo sind also verschiedene Repositories —
        gälten sie als identisch, könnte ein "sicherer" Push im Repo des falschen
        Benutzers landen.
        """
        alice = canonical_remote_target("alice@host:repo.git")
        bob = canonical_remote_target("bob@host:repo.git")
        self.assertNotEqual(alice, bob)
        # Derselbe Benutzer, dieselbe Schreibweise: natürlich identisch.
        self.assertEqual(alice, canonical_remote_target("alice@host:repo.git"))
        # Der Benutzername bleibt trotzdem aus der Anzeige-Identität heraus.
        self.assertEqual(alice.repo_id, "/repo")

    def test_scp_relative_and_absolute_paths_stay_distinct(self):
        # host:repo wird im Remote-Home aufgelöst, host:/repo absolut — zwei Ziele.
        relative = canonical_remote_target("host:repo.git")
        absolute = canonical_remote_target("host:/repo.git")
        self.assertNotEqual(relative, absolute)
        # Tilde-Pfade hängen ebenfalls am Benutzer.
        self.assertNotEqual(canonical_remote_target("alice@host:~/repo.git"),
                            canonical_remote_target("bob@host:~/repo.git"))

    def test_scp_github_form_still_maps_to_the_web_url_path(self):
        # git@github.com:org/repo ist formal home-relativ; die Web-URL-Ableitung
        # (repo_id) muss davon unberührt bleiben.
        target = canonical_remote_target("git@github.com:example/demo.git")
        self.assertTrue(target.is_github)
        self.assertEqual(target.repo_id, "/example/demo")

    def test_scp_git_user_matches_the_https_form_of_the_same_repo(self):
        """Fetch per HTTPS, Push per SSH — der Standardfall bei GitHub & Co.

        Der virtuelle SSH-Benutzer "git" hat kein privates Home-Verzeichnis:
        sein SCP-Pfad ist derselbe Namensraum wie der HTTPS-Pfad. Vorher galten
        beide URLs als verschiedene Ziele (transfer_safe False), und P/G
        verweigerten die Übertragung, obwohl sie dasselbe Repo meinen.
        """
        ssh = canonical_remote_target("git@github.com:example/demo.git")
        https = canonical_remote_target("https://github.com/example/demo.git")
        self.assertEqual(ssh, https)
        # Ein ausdrückliches "~" bleibt benutzerabhängig — auch beim Benutzer git.
        self.assertNotEqual(canonical_remote_target("git@host:~/repo.git"),
                            canonical_remote_target("host:/repo.git"))
        # Andere Benutzernamen bleiben home-relativ und damit eigene Ziele.
        self.assertNotEqual(canonical_remote_target("alice@host:repo.git"),
                            canonical_remote_target("https://host/repo.git"))

    def test_https_fetch_with_ssh_push_is_transfer_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a")
            git(repo, "add", "a"); git(repo, "commit", "-qm", "base")
            git(repo, "remote", "add", "origin", "https://github.com/example/one.git")
            git(repo, "remote", "set-url", "--push", "origin",
                "git@github.com:example/one.git")
            st = collect_status(repo, root, DEFAULT_CONFIG)
            remote = st.remotes[0]
            self.assertFalse(remote.target_mismatch)
            self.assertTrue(remote.transfer_safe)

    def test_sync_host_must_match_exactly(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "mirror",
                "ssh://trusted.example.attacker.invalid/org/repo.git")
            cfg = {**DEFAULT_CONFIG, "sync_remote_names": [],
                   "sync_remote_hosts": ["trusted.example"]}
            self.assertIsNone(detect_sync_remote(repo, cfg))

    def test_same_class_different_push_target_is_blocked_and_fingerprinted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a")
            git(repo, "add", "a"); git(repo, "commit", "-qm", "base")
            git(repo, "remote", "add", "origin", "https://github.com/example/one.git")
            git(repo, "remote", "set-url", "--push", "origin",
                "https://github.com/example/two.git")
            st = collect_status(repo, root, DEFAULT_CONFIG)
            remote = st.remotes[0]
            self.assertTrue(remote.target_mismatch)
            self.assertFalse(remote.transfer_safe)
            payload = status_dict(st)["remotes"][0]
            self.assertTrue(payload["fetch_fingerprint"])
            self.assertEqual(payload["fetch_fingerprints"],
                             [payload["fetch_fingerprint"]])
            self.assertTrue(payload["push_fingerprints"])
            self.assertNotIn("github.com", json.dumps(payload))

    def test_multiple_pushurls_are_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bare = root / "bare.git"; bare.mkdir(); git(bare, "init", "-q", "--bare")
            repo = root / "repo"; repo.mkdir(); git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a"); git(repo, "add", "a"); git(repo, "commit", "-qm", "base")
            git(repo, "remote", "add", "origin", str(bare))
            git(repo, "push", "-qu", "origin", "main")
            git(repo, "remote", "set-url", "--add", "--push", "origin", str(bare))
            git(repo, "remote", "set-url", "--add", "--push", "origin", str(root / "other.git"))
            self.assertEqual(inspect_transfer(repo, "origin", "main", "push").reason,
                             "remote-unsafe")


class CommitSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"; self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "include.txt").write_text("base\n")
        (self.repo / "skip.txt").write_text("base\n")
        git(self.repo, "add", "include.txt", "skip.txt")
        git(self.repo, "commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def _status(self) -> str:
        return subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"],
                              check=True, capture_output=True, text=True).stdout

    def test_temporary_index_commits_only_approved_path_and_keeps_other_staging(self):
        (self.repo / "include.txt").write_text("approved\n")
        (self.repo / "skip.txt").write_text("staged but excluded\n")
        git(self.repo, "add", "skip.txt")
        r = commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        changed = subprocess.run(
            ["git", "-C", str(self.repo), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(changed, ["include.txt"])
        # Das bewusste Staging einer NICHT committeten Datei bleibt erhalten …
        self.assertIn("skip.txt", subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--cached", "--name-only"],
            check=True, capture_output=True, text=True).stdout)
        # … die committete Datei dagegen ist erledigt und verschwindet aus dem Status.
        self.assertNotIn("include.txt", self._status())

    def test_magic_filename_cannot_expand_into_foreign_staging(self):
        magic = ":(glob)*.txt"
        matching = "matching.txt"
        (self.repo / magic).write_text("base magic\n")
        (self.repo / matching).write_text("base matching\n")
        git(self.repo, "add", "--", magic, matching)
        git(self.repo, "commit", "-qm", "magic names")
        (self.repo / magic).write_text("approved magic\n")
        (self.repo / matching).write_text("foreign staging\n")
        git(self.repo, "add", "--", matching)

        result = commit_selected(self.repo, [magic], "selected", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        changed = subprocess.run(
            ["git", "-C", str(self.repo), "diff-tree", "--no-commit-id",
             "--name-only", "-r", "HEAD"], check=True, capture_output=True,
            text=True).stdout.splitlines()
        self.assertEqual(changed, [magic])
        cached = subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--cached", "--name-only"],
            check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(cached, [matching])

    def test_existing_head_replace_ref_cannot_change_the_approved_base_tree(self):
        head = gmf_module.current_head(self.repo, 10)
        temp_index = Path(self.tmp.name) / "replacement-index"
        env = {**os.environ, "GIT_INDEX_FILE": str(temp_index)}
        subprocess.run(
            ["git", "-C", str(self.repo), "read-tree", head], check=True,
            env=env, capture_output=True, text=True)
        (self.repo / "foreign.txt").write_text("foreign\n")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "--", "foreign.txt"],
            check=True, env=env, capture_output=True, text=True)
        replacement_tree = subprocess.run(
            ["git", "-C", str(self.repo), "write-tree"], check=True,
            env=env, capture_output=True, text=True).stdout.strip()
        replacement = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", replacement_tree,
             "-m", "replacement"], check=True, capture_output=True,
            text=True).stdout.strip()
        (self.repo / "foreign.txt").unlink()
        git(self.repo, "replace", head, replacement)
        (self.repo / "include.txt").write_text("approved\n")

        result = commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        tree_paths = subprocess.run(
            ["git", "-C", str(self.repo), "-c", "core.useReplaceRefs=false",
             "ls-tree", "-r", "--name-only", result.committed_head],
            check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertNotIn("foreign.txt", tree_paths)

    def test_commit_helper_refuses_a_conflict_free_merge(self):
        """Ein selektiver Baum darf keinen Merge mit unvollständigem Inhalt abschließen."""
        git(self.repo, "switch", "-qc", "feature")
        (self.repo / "feature-a.txt").write_text("a\n")
        (self.repo / "feature-b.txt").write_text("b\n")
        git(self.repo, "add", "feature-a.txt", "feature-b.txt")
        git(self.repo, "commit", "-qm", "feature")
        git(self.repo, "switch", "-q", "main")
        git(self.repo, "merge", "--no-ff", "--no-commit", "feature")
        head_before = gmf_module.current_head(self.repo, 10)
        index_before = (self.repo / ".git" / "index").read_bytes()

        with self.assertRaisesRegex(CommitSafetyError, "merge|sequencer"):
            commit_selected(self.repo, ["feature-a.txt"], "partial merge", 10)

        self.assertEqual(gmf_module.current_head(self.repo, 10), head_before)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index_before)

    def test_committed_file_is_clean_afterwards(self):
        """Der echte Index muss den Commit übernehmen, sonst lügt die Anzeige.

        Vorher blieb er auf dem Stand von vor dem Commit stehen: `git status` meldete
        die Datei als `MM`, obwohl Arbeitsbaum und HEAD längst identisch waren — in
        gmf sah es aus, als wäre der Commit gar nicht passiert.
        """
        head_before = gmf_module.current_head(self.repo, 10)
        (self.repo / "include.txt").write_text("approved\n")
        r = commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.committed_head, gmf_module.current_head(self.repo, 10))
        self.assertEqual(
            gmf_module.commit_undo_command(
                head_before, r.committed_head, r.approved_ref),
            f"git -c core.hooksPath=/dev/null update-ref --no-deref "
            f"refs/heads/main "
            f"{head_before} {r.committed_head}",
        )
        self.assertEqual(r.approved_head, head_before)
        self.assertEqual(r.approved_ref, "refs/heads/main")
        self.assertEqual(self._status(), "")
        st = collect_status(self.repo, self.repo.parent, DEFAULT_CONFIG)
        self.assertEqual((st.modified, st.deleted, st.untracked), (0, 0, 0))

    def test_real_index_adoption_refuses_a_moved_head(self):
        committed = gmf_module.current_head(self.repo, 10)
        (self.repo / "later.txt").write_text("later\n")
        git(self.repo, "add", "later.txt")
        git(self.repo, "commit", "-qm", "later")
        index_before = (self.repo / ".git" / "index").read_bytes()
        signature = gmf_module._real_index_signature(self.repo, 10)

        with self.assertRaisesRegex(CommitSafetyError, "HEAD changed"):
            gmf_module.adopt_commit_in_real_index(
                self.repo, ["include.txt"], committed, "refs/heads/main",
                signature, 10)

        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index_before)

    def test_commit_undo_refuses_a_branch_that_moved_again(self):
        before = gmf_module.current_head(self.repo, 10)
        (self.repo / "include.txt").write_text("approved\n")
        result = commit_selected(self.repo, ["include.txt"], "selected", 10)
        (self.repo / "later.txt").write_text("later\n")
        git(self.repo, "add", "later.txt")
        git(self.repo, "commit", "-qm", "later")
        moved = gmf_module.current_head(self.repo, 10)

        undo = shlex.split(gmf_module.commit_undo_command(
            result.approved_head, result.committed_head,
            result.approved_ref))[1:]
        attempted = subprocess.run(
            ["git", "-C", str(self.repo), *undo], capture_output=True, text=True)

        self.assertNotEqual(attempted.returncode, 0)
        self.assertEqual(gmf_module.current_head(self.repo, 10), moved)
        self.assertNotEqual(moved, before)

    def test_commit_undo_stays_bound_to_the_original_branch(self):
        before = gmf_module.current_head(self.repo, 10)
        (self.repo / "include.txt").write_text("approved\n")
        result = commit_selected(self.repo, ["include.txt"], "selected", 10)
        git(self.repo, "branch", "other", result.committed_head)
        git(self.repo, "switch", "-q", "other")

        undo = shlex.split(gmf_module.commit_undo_command(
            result.approved_head, result.committed_head,
            result.approved_ref))[1:]
        attempted = subprocess.run(
            ["git", "-C", str(self.repo), *undo], capture_output=True, text=True)

        self.assertEqual(attempted.returncode, 0, attempted.stderr)
        self.assertEqual(gmf_module.current_head(self.repo, 10), result.committed_head)
        self.assertEqual(subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "refs/heads/main"],
            check=True, capture_output=True, text=True).stdout.strip(), before)
        self.assertEqual(subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "refs/heads/other"],
            check=True, capture_output=True, text=True).stdout.strip(),
            result.committed_head)

    def test_commit_undo_never_dereferences_a_symbolic_branch(self):
        before = gmf_module.current_head(self.repo, 10)
        (self.repo / "include.txt").write_text("approved\n")
        result = commit_selected(self.repo, ["include.txt"], "selected", 10)
        git(self.repo, "branch", "other", result.committed_head)
        git(self.repo, "symbolic-ref", "refs/heads/main", "refs/heads/other")

        undo = shlex.split(gmf_module.commit_undo_command(
            result.approved_head, result.committed_head,
            result.approved_ref))[1:]
        attempted = subprocess.run(
            ["git", "-C", str(self.repo), *undo], capture_output=True, text=True)

        self.assertEqual(attempted.returncode, 0, attempted.stderr)
        self.assertEqual(
            git_output(self.repo, "rev-parse", "refs/heads/main"), before)
        self.assertEqual(
            git_output(self.repo, "rev-parse", "refs/heads/other"),
            result.committed_head)
        symbolic = subprocess.run(
            ["git", "-C", str(self.repo), "symbolic-ref", "-q",
             "refs/heads/main"], capture_output=True, text=True)
        self.assertNotEqual(symbolic.returncode, 0)
        self.assertNotEqual(result.committed_head, before)

    def test_commit_on_detached_head_is_blocked(self):
        git(self.repo, "switch", "--detach", "-q")
        head_before = gmf_module.current_head(self.repo, 10)
        (self.repo / "include.txt").write_text("approved\n")

        with self.assertRaisesRegex(CommitSafetyError, "detached HEAD"):
            commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertEqual(gmf_module.current_head(self.repo, 10), head_before)

    def test_head_read_timeout_never_starts_a_commit(self):
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        calls = []

        def record(target_repo, *args, **kwargs):
            calls.append(args)
            return original(target_repo, *args, **kwargs)

        timeout = subprocess.TimeoutExpired(["git", "rev-parse", "HEAD"], 10)
        with mock.patch.object(gmf_module, "current_head", side_effect=timeout), \
                mock.patch.object(gmf_module, "run_git", side_effect=record), \
                self.assertRaises(subprocess.TimeoutExpired):
            commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertFalse(any("commit" in args for args in calls))

    def test_commit_rolled_back_if_direct_head_change_targets_another_branch(self):
        before = gmf_module.current_head(self.repo, 10)
        git(self.repo, "branch", "other")
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        switched = False

        def race(target_repo, *args, **kwargs):
            nonlocal switched
            if "commit" in args and not switched:
                switched = True
                # `git switch` wird inzwischen schon vom Index-Lock gesperrt.
                # Ein direkter symbolischer Ref-Wechsel umgeht den Index bewusst
                # und prüft weiterhin den nachgelagerten Branch-CAS-Rollback.
                git(self.repo, "symbolic-ref", "HEAD", "refs/heads/other")
            return original(target_repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git", side_effect=race), \
                self.assertRaisesRegex(CommitSafetyError, "another branch.*rolled back"):
            commit_selected(self.repo, ["include.txt"], "selected", 10)

        for ref in ("refs/heads/main", "refs/heads/other"):
            self.assertEqual(subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", ref], check=True,
                capture_output=True, text=True).stdout.strip(), before)

    def test_merge_cannot_start_between_the_guard_and_commit(self):
        before = gmf_module.current_head(self.repo, 10)
        git(self.repo, "switch", "-qc", "feature")
        (self.repo / "feature.txt").write_text("feature\n")
        git(self.repo, "add", "feature.txt")
        git(self.repo, "commit", "-qm", "feature")
        git(self.repo, "switch", "-q", "main")
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        merge_attempt = None

        def race(target_repo, *args, **kwargs):
            nonlocal merge_attempt
            if "commit" in args and merge_attempt is None:
                merge_attempt = subprocess.run(
                    ["git", "-C", str(self.repo), "merge", "--no-ff",
                     "--no-commit", "feature"], capture_output=True, text=True)
            return original(target_repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git", side_effect=race):
            result = commit_selected(
                self.repo, ["include.txt"], "selected", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(merge_attempt)
        self.assertNotEqual(merge_attempt.returncode, 0)
        self.assertEqual(gmf_module.commit_parents(
            self.repo, result.committed_head, 10), [before])

    def test_temporary_index_is_built_from_the_approved_oid_not_moving_head(self):
        before = gmf_module.current_head(self.repo, 10)
        git(self.repo, "switch", "-qc", "other")
        (self.repo / "foreign.txt").write_text("foreign\n")
        git(self.repo, "add", "foreign.txt")
        git(self.repo, "commit", "-qm", "foreign")
        git(self.repo, "switch", "-q", "main")
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        raced = False

        def race(target_repo, *args, **kwargs):
            nonlocal raced
            if "read-tree" in args and not raced:
                raced = True
                git(self.repo, "symbolic-ref", "HEAD", "refs/heads/other")
                result = original(target_repo, *args, **kwargs)
                git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
                return result
            return original(target_repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git", side_effect=race):
            result = commit_selected(
                self.repo, ["include.txt"], "selected", 10)

        self.assertTrue(raced)
        self.assertEqual(result.returncode, 0, result.stderr)
        tree_paths = subprocess.run(
            ["git", "-C", str(self.repo), "ls-tree", "-r", "--name-only",
             result.committed_head], check=True, capture_output=True,
            text=True).stdout.splitlines()
        self.assertNotIn("foreign.txt", tree_paths)

    def test_index_adoption_blocks_a_checkout_between_check_and_update(self):
        git(self.repo, "branch", "other")
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        switch_attempt = None

        def race(target_repo, *args, **kwargs):
            nonlocal switch_attempt
            if ("reset" in args and "-q" in args
                    and kwargs.get("env", {}).get("GIT_INDEX_FILE")
                    and "gmf-adopt-index-" in kwargs["env"]["GIT_INDEX_FILE"]
                    and switch_attempt is None):
                switch_attempt = subprocess.run(
                    ["git", "-C", str(self.repo), "switch", "-q", "other"],
                    capture_output=True, text=True)
            return original(target_repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git", side_effect=race):
            result = commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(switch_attempt)
        self.assertNotEqual(switch_attempt.returncode, 0)
        self.assertEqual(
            gmf_module.current_symbolic_head_ref(self.repo, 10),
            "refs/heads/main")
        self.assertEqual(self._status(), "")

    def test_checkout_after_commit_does_not_roll_back_the_approved_branch(self):
        before = gmf_module.current_head(self.repo, 10)
        git(self.repo, "branch", "other")
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        committed = None

        def race(target_repo, *args, **kwargs):
            nonlocal committed
            result = original(target_repo, *args, **kwargs)
            if (committed is None and args[:2] == ("rev-parse", "HEAD")
                    and result.returncode == 0
                    and subprocess.run(
                        ["git", "-C", str(self.repo), "log", "-1", "--format=%s"],
                        check=True, capture_output=True, text=True).stdout.strip()
                    == "selected"):
                committed = result.stdout.strip()
                git(self.repo, "symbolic-ref", "HEAD", "refs/heads/other")
            return result

        with mock.patch.object(gmf_module, "run_git", side_effect=race), \
                self.assertRaisesRegex(
                    gmf_module.CommitOutcomeUnknownError,
                    "commit remains on the approved branch"):
            commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertIsNotNone(committed)
        self.assertEqual(subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "refs/heads/main"],
            check=True, capture_output=True, text=True).stdout.strip(), committed)
        self.assertEqual(subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "refs/heads/other"],
            check=True, capture_output=True, text=True).stdout.strip(), before)

    def test_foreign_followup_commit_is_never_rolled_back(self):
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git
        foreign = None

        def race(target_repo, *args, **kwargs):
            nonlocal foreign
            result = original(target_repo, *args, **kwargs)
            if "commit" in args and result.returncode == 0 and foreign is None:
                own = subprocess.run(
                    ["git", "-C", str(self.repo), "rev-parse", "refs/heads/main"],
                    check=True, capture_output=True, text=True).stdout.strip()
                tree = subprocess.run(
                    ["git", "-C", str(self.repo), "rev-parse", own + "^{tree}"],
                    check=True, capture_output=True, text=True).stdout.strip()
                foreign = subprocess.run(
                    ["git", "-C", str(self.repo), "commit-tree", tree,
                     "-p", own, "-m", "foreign followup"], check=True,
                    capture_output=True, text=True).stdout.strip()
                git(self.repo, "update-ref", "refs/heads/main", foreign, own)
            return result

        with mock.patch.object(gmf_module, "run_git", side_effect=race), \
                self.assertRaisesRegex(
                    gmf_module.CommitOutcomeUnknownError, "branch changed"):
            commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertIsNotNone(foreign)
        self.assertEqual(gmf_module.current_head(self.repo, 10), foreign)

    def test_post_commit_index_adoption_failure_is_distinguishable(self):
        (self.repo / "include.txt").write_text("approved\n")
        before = gmf_module.current_head(self.repo, 10)
        original = gmf_module._acquire_git_lock

        def block_head_lock(path):
            if path.name == "HEAD.lock":
                raise CommitSafetyError("synthetic HEAD lock")
            return original(path)

        with mock.patch.object(gmf_module, "_acquire_git_lock",
                               side_effect=block_head_lock), \
                self.assertRaises(gmf_module.CommitAdoptionError) as raised:
            commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertNotEqual(gmf_module.current_head(self.repo, 10), before)
        self.assertEqual(raised.exception.committed_head,
                         gmf_module.current_head(self.repo, 10))

    def test_post_commit_index_read_failure_is_an_adoption_failure(self):
        (self.repo / "include.txt").write_text("approved\n")
        before = gmf_module.current_head(self.repo, 10)
        original = gmf_module._real_index_signature
        calls = 0

        def fail_after_commit(repo, timeout):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise CommitSafetyError("synthetic index read failure")
            return original(repo, timeout)

        with mock.patch.object(gmf_module, "_real_index_signature",
                               side_effect=fail_after_commit), \
                self.assertRaises(gmf_module.CommitAdoptionError) as raised:
            commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertNotEqual(gmf_module.current_head(self.repo, 10), before)
        self.assertEqual(raised.exception.committed_head,
                         gmf_module.current_head(self.repo, 10))

    def test_commit_forces_its_single_reflog_proof_when_repo_disables_logs(self):
        git(self.repo, "config", "core.logAllRefUpdates", "false")
        (self.repo / "include.txt").write_text("approved\n")
        # Ein geerbtes GIT_CONFIG_PARAMETERS wird spaeter als die
        # GIT_CONFIG_COUNT-Umgebung ausgewertet. Nur das echte `git -c` des
        # Commit-Aufrufs gewinnt auch gegen diesen fremden Override.
        with mock.patch.dict(
                os.environ,
                {"GIT_CONFIG_PARAMETERS": "'core.logAllRefUpdates'='false'"}):
            result = commit_selected(
                self.repo, ["include.txt"], "selected", 10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.committed_head,
                         gmf_module.current_head(self.repo, 10))

    def test_head_reflog_marker_counts_as_an_unknown_commit_outcome(self):
        action = "gmf-detached-hook"
        oid = "a" * 40
        reflog = subprocess.CompletedProcess(
            [], 0, f"{oid}\0HEAD@{{0}}\0{action}: commit (initial): hook\n", "")
        with mock.patch.object(gmf_module, "_required_git",
                               return_value=reflog):
            matches = gmf_module._commit_reflog_matches(
                self.repo, action, 10)
            with self.assertRaises(CommitSafetyError):
                gmf_module._commit_reflog_proof(self.repo, action, 10)

        self.assertEqual(matches, [(oid, "HEAD")])

    def test_timeout_after_git_reported_success_never_claims_no_commit(self):
        (self.repo / "include.txt").write_text("approved\n")
        before = gmf_module.current_head(self.repo, 10)
        timeout = subprocess.TimeoutExpired(["git", "reflog"], 10)
        with mock.patch.object(
                gmf_module, "_commit_reflog_proof", side_effect=timeout), \
                self.assertRaises(gmf_module.CommitOutcomeUnknownError):
            commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertNotEqual(gmf_module.current_head(self.repo, 10), before)

    def test_read_error_after_git_reported_success_is_not_commit_failed(self):
        (self.repo / "include.txt").write_text("approved\n")
        before = gmf_module.current_head(self.repo, 10)
        with mock.patch.object(
                gmf_module, "_commit_reflog_proof",
                side_effect=gmf_module.GitReadError("broken reflog")), \
                self.assertRaises(gmf_module.CommitOutcomeUnknownError):
            commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertNotEqual(gmf_module.current_head(self.repo, 10), before)

    def test_older_staged_version_of_the_same_file_does_not_survive(self):
        """Committet wird der Arbeitsbaum-Stand — genau wie bei `git commit -- <pfad>`.

        Eine ältere, gestagete Fassung derselben Datei ist damit überholt; der Index
        zeigt danach den committeten Inhalt und nicht mehr die Zwischenversion.
        """
        (self.repo / "include.txt").write_text("staged version\n")
        git(self.repo, "add", "include.txt")
        (self.repo / "include.txt").write_text("worktree version\n")
        r = commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        for ref in ("HEAD:include.txt", ":include.txt"):
            self.assertEqual(subprocess.run(
                ["git", "-C", str(self.repo), "show", ref],
                check=True, capture_output=True, text=True).stdout, "worktree version\n")
        self.assertEqual(self._status(), "")

    def test_rename_commits_move_and_deletion_together(self):
        """Ein approvter Rename (Ziel + Quelle) ergibt einen echten Move-Commit.

        Vorher stagte die Hilfe nur den Zielpfad: der Commit enthielt eine Kopie,
        und die Löschung des alten Namens blieb als schmutziger Rest im Repo.
        """
        git(self.repo, "mv", "include.txt", "renamed.txt")
        st = collect_status(self.repo, self.repo.parent, DEFAULT_CONFIG)
        rename = [entry for entry in st.files if entry.rename_group]
        self.assertEqual({entry.path for entry in rename},
                         {"renamed.txt", "include.txt"})
        self.assertEqual(len({entry.rename_group for entry in rename}), 1)
        r = commit_selected(self.repo, ["renamed.txt", "include.txt"], "umbenannt", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        changed = subprocess.run(
            ["git", "-C", str(self.repo), "diff-tree", "--no-commit-id",
             "--name-status", "-r", "HEAD"],
            check=True, capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(changed), ["A", "D", "include.txt", "renamed.txt"])
        self.assertEqual(self._status(), "")

    def test_plain_deletion_can_be_committed(self):
        """Löschungen konnte die Hilfe nie committen: das zweite Race-Check-
        Staging fand den Pfad weder im Index noch im Arbeitsbaum und `git add`
        brach mit "pathspec did not match" ab (GitReadError, Exit 128)."""
        (self.repo / "include.txt").unlink()
        r = commit_selected(self.repo, ["include.txt"], "geloescht", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        changed = subprocess.run(
            ["git", "-C", str(self.repo), "diff-tree", "--no-commit-id",
             "--name-status", "-r", "HEAD"],
            check=True, capture_output=True, text=True).stdout.split()
        self.assertEqual(changed, ["D", "include.txt"])
        self.assertEqual(self._status(), "")


    def test_initial_commit_in_fresh_repository(self):
        """Die Commit-Hilfe muss auch den allerersten Commit eines Repos können.

        Ohne HEAD scheiterte `git read-tree HEAD` mit GitReadError — ausgerechnet
        beim ersten Commit, den der alte direkte Pfad noch konnte.
        """
        fresh = Path(self.tmp.name) / "frisch"; fresh.mkdir()
        git(fresh, "init", "-q", "-b", "main")
        git(fresh, "config", "user.email", "t@example.invalid")
        git(fresh, "config", "user.name", "T")
        (fresh / "erste.txt").write_text("hallo\n")
        r = commit_selected(fresh, ["erste.txt"], "initial", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        committed_head = gmf_module.current_head(fresh, 10)
        self.assertEqual(r.committed_head, committed_head)
        log = subprocess.run(["git", "-C", str(fresh), "log", "--format=%s"],
                             check=True, capture_output=True, text=True).stdout
        self.assertEqual(log.strip(), "initial")
        status = subprocess.run(["git", "-C", str(fresh), "status", "--porcelain"],
                                check=True, capture_output=True, text=True).stdout
        self.assertEqual(status, "")

        undo = gmf_module.commit_undo_command(
            None, r.committed_head, r.approved_ref)
        git(fresh, *shlex.split(undo)[1:])
        self.assertIsNone(gmf_module.current_head(fresh, 10))
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(fresh), "status", "--porcelain"],
                check=True, capture_output=True, text=True,
            ).stdout,
            "A  erste.txt\n",
        )

    def test_initial_commit_keeps_a_private_index_mode(self):
        fresh = Path(self.tmp.name) / "private-index"
        fresh.mkdir()
        git(fresh, "init", "-q", "-b", "main")
        git(fresh, "config", "user.email", "t@example.invalid")
        git(fresh, "config", "user.name", "T")
        (fresh / "first.txt").write_text("content\n")
        previous_umask = os.umask(0o077)
        try:
            result = commit_selected(fresh, ["first.txt"], "initial", 10)
        finally:
            os.umask(previous_umask)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            stat.S_IMODE((fresh / ".git" / "index").stat().st_mode), 0o600)

    def test_oserror_after_real_commit_is_reported_as_unknown(self):
        before = gmf_module.current_head(self.repo, 10)
        (self.repo / "include.txt").write_text("approved\n")
        original = gmf_module.run_git

        def commit_then_lose_result(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if "commit" in args:
                self.assertEqual(result.returncode, 0, result.stderr)
                raise OSError("lost commit result")
            return result

        with mock.patch.object(
                gmf_module, "run_git", side_effect=commit_then_lose_result), \
                self.assertRaises(gmf_module.CommitOutcomeUnknownError):
            commit_selected(self.repo, ["include.txt"], "selected", 10)

        self.assertNotEqual(gmf_module.current_head(self.repo, 10), before)
        self.assertTrue(any(
            line.startswith("…") and "git commit" in line
            for line in gmf_module.COMMAND_LOG))

    def test_command_log_shows_the_terminal_equivalent_commit(self):
        """Das Protokoll verspricht terminal-ausführbare Zeilen. Ein nacktes
        `git add`/`git commit` liefe dort aber gegen den ECHTEN Index (ohne das
        GIT_INDEX_FILE der Hilfe) — deshalb steht stattdessen der äquivalente,
        pfadbegrenzte Befehl im Protokoll."""
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        (self.repo / "include.txt").write_text("approved\n")
        r = commit_selected(self.repo, ["include.txt"], "selected", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        joined = "\n".join(gmf_module.COMMAND_LOG)
        self.assertIn("git commit -m selected -- ':(literal)include.txt'", joined)
        self.assertIn(
            f"git -c core.hooksPath=/dev/null reset -q "
            f"{r.committed_head} -- ':(literal)include.txt'",
            joined)
        self.assertNotIn("git add", joined)

    def test_conflicts_block_commit(self):
        git(self.repo, "checkout", "-qb", "other")
        (self.repo / "include.txt").write_text("other\n"); git(self.repo, "commit", "-qam", "other")
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "include.txt").write_text("main\n"); git(self.repo, "commit", "-qam", "main")
        merge = subprocess.run(["git", "-C", str(self.repo), "merge", "other"],
                               capture_output=True, text=True)
        self.assertNotEqual(merge.returncode, 0)
        with self.assertRaises(CommitSafetyError):
            commit_selected(self.repo, ["include.txt"], "must fail", 10)

class CommitWizardSafetyTests(unittest.TestCase):
    class Screen:
        def __init__(self, keys=()): self.keys = iter(keys)
        def getmaxyx(self): return (30, 120)
        def erase(self): pass
        def refresh(self): pass
        def addstr(self, *_args): pass
        def move(self, *_args): pass
        def clrtoeol(self): pass
        def getch(self): return next(self.keys)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        for name in ("one.txt", "two.txt"):
            (self.repo / name).write_text(name + "\n")
        (self.repo / ".gitignore").write_text("base-rule\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")

    def tearDown(self): self.tmp.cleanup()

    def test_commit_helper_never_changes_gitignore_implicitly(self):
        before = (self.repo / ".gitignore").read_text()
        (self.repo / "debug.log").write_text("log\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "U", "path": "debug.log", "include": False,
                  "rename_group": ""}]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)

        self.assertTrue(ui._commit_step2(st, items))

        self.assertEqual(ui.message, gmf_module.t("nothing_selected"))
        self.assertEqual((self.repo / ".gitignore").read_text(), before)

    def test_timeout_before_git_commit_never_reads_unset_approval_state(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "M", "path": "one.txt", "ignore": False,
                  "pattern": None, "include": True, "rename_group": ""}]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        timeout = subprocess.TimeoutExpired(["git", "symbolic-ref"], 10)
        with mock.patch.object(ui, "prompt_line", return_value="selected"), \
                mock.patch.object(ui, "refresh_one", return_value=st), \
                mock.patch.object(gmf_module, "current_symbolic_head_ref",
                                  side_effect=timeout), \
                mock.patch("gitmaster_flash.curses.flushinp"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            self.assertTrue(ui._commit_step2(st, items))

        self.assertEqual(
            ui.message,
            gmf_module.t("commit_timeout_none", s=DEFAULT_CONFIG["commit_timeout"]))

    def test_head_read_error_before_commit_is_reported_inside_the_tui(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "M", "path": "one.txt", "ignore": False,
                  "pattern": None, "include": True, "rename_group": ""}]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with mock.patch.object(ui, "prompt_line", return_value="selected"), \
                mock.patch.object(gmf_module, "current_head",
                                  side_effect=gmf_module.GitReadError("broken HEAD")), \
                mock.patch("gitmaster_flash.curses.flushinp"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            self.assertTrue(ui._commit_step2(st, items))
        self.assertIn("broken HEAD", ui.message)

    def test_checkout_during_the_commit_dialog_never_changes_the_target_branch(self):
        git(self.repo, "branch", "other")
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "M", "path": "one.txt", "ignore": False,
                  "pattern": None, "include": True, "rename_group": ""}]
        main_before = git_output(self.repo, "rev-parse", "refs/heads/main")
        other_before = git_output(self.repo, "rev-parse", "refs/heads/other")
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        real_commit = gmf_module.commit_selected

        def switch_before_commit(*args, **kwargs):
            git(self.repo, "switch", "-q", "other")
            return real_commit(*args, **kwargs)

        with mock.patch.object(ui, "prompt_line", return_value="selected"), \
                mock.patch.object(gmf_module, "commit_selected",
                                  side_effect=switch_before_commit), \
                mock.patch.object(ui, "show_busy"), \
                mock.patch("gitmaster_flash.curses.flushinp"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            self.assertTrue(ui._commit_step2(st, items))

        self.assertIn("HEAD changed after UI approval", ui.message)
        self.assertEqual(git_output(self.repo, "rev-parse", "refs/heads/main"),
                         main_before)
        self.assertEqual(git_output(self.repo, "rev-parse", "refs/heads/other"),
                         other_before)

    def test_existing_commit_with_failed_index_adoption_is_named_clearly(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "M", "path": "one.txt", "ignore": False,
                  "pattern": None, "include": True, "rename_group": ""}]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        error = gmf_module.CommitAdoptionError("a" * 40, OSError("locked"))
        with mock.patch.object(ui, "prompt_line", return_value="selected"), \
                mock.patch.object(gmf_module, "commit_selected", side_effect=error), \
                mock.patch.object(ui, "show_busy"), \
                mock.patch("gitmaster_flash.curses.flushinp"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            self.assertTrue(ui._commit_step2(st, items))
        self.assertIn("aaaaaaaaaaaa", ui.message)
        self.assertIn("index", ui.message.lower())
        self.assertIsNot(ui.statuses[0], st)

    def test_failed_commit_never_creates_an_ignore_rule(self):
        (self.repo / "debug.log").write_text("log\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "U", "path": "debug.log",
                  "include": True, "rename_group": ""}]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        failed = subprocess.CompletedProcess(["git", "commit"], 1, "", "hook failed")
        with mock.patch.object(ui, "prompt_line", return_value="logs"), \
                mock.patch.object(gmf_module, "commit_selected", return_value=failed), \
                mock.patch.object(ui, "show_busy"), \
                mock.patch("gitmaster_flash.curses.flushinp"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            self.assertTrue(ui._commit_step2(st, items))

        self.assertEqual((self.repo / ".gitignore").read_text(), "base-rule\n")
        self.assertIsNot(ui.statuses[0], st)

    def test_post_success_timeout_never_says_nothing_was_committed(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "M", "path": "one.txt", "ignore": False,
                  "pattern": None, "include": True, "rename_group": ""}]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        error = gmf_module.CommitOutcomeUnknownError("synthetic timeout")
        with mock.patch.object(ui, "prompt_line", return_value="selected"), \
                mock.patch.object(gmf_module, "commit_selected", side_effect=error), \
                mock.patch.object(ui, "refresh_one", return_value=st), \
                mock.patch.object(ui, "show_busy"), \
                mock.patch("gitmaster_flash.curses.flushinp"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            self.assertTrue(ui._commit_step2(st, items))
        self.assertEqual(ui.message, gmf_module.t("commit_outcome_unknown"))
        self.assertNotIn("nothing was committed", ui.message)

    def test_unlock_oserror_after_real_commit_is_outcome_unknown(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "M", "path": "one.txt", "ignore": False,
                  "pattern": None, "include": True, "rename_group": ""}]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        original_release = gmf_module._release_owned_git_lock

        def release_then_fail(path, fd, identity):
            if path.name != "index.lock":
                return original_release(path, fd, identity)
            if fd is not None:
                os.close(fd)
            path.unlink(missing_ok=True)
            raise OSError("lost unlock result")

        with mock.patch.object(ui, "prompt_line", return_value="selected"), \
                mock.patch.object(gmf_module, "_release_owned_git_lock",
                                  side_effect=release_then_fail), \
                mock.patch.object(ui, "show_busy"), \
                mock.patch("gitmaster_flash.curses.flushinp"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            self.assertTrue(ui._commit_step2(st, items))

        self.assertEqual(ui.message, gmf_module.t("commit_outcome_unknown"))
        self.assertEqual(
            git_output(self.repo, "show", "HEAD:one.txt"), "changed")

    def test_timeout_during_interrupted_commit_head_check_is_unknown(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "M", "path": "one.txt", "ignore": False,
                  "pattern": None, "include": True, "rename_group": ""}]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        commit_timeout = subprocess.TimeoutExpired(["git", "commit"], 120)
        commit_timeout.approved_head = gmf_module.current_head(self.repo, 10)
        commit_timeout.approved_ref = "refs/heads/main"
        commit_timeout.approved_tree = "a" * 40
        commit_timeout.reflog_action = "gmf test interrupted commit"
        verify_timeout = subprocess.TimeoutExpired(["git", "rev-parse"], 10)
        with mock.patch.object(ui, "prompt_line", return_value="selected"), \
                mock.patch.object(gmf_module, "commit_selected",
                                  side_effect=commit_timeout), \
                mock.patch.object(gmf_module, "current_head",
                                  side_effect=[commit_timeout.approved_head,
                                               verify_timeout]), \
                mock.patch.object(ui, "refresh_one", return_value=st), \
                mock.patch.object(ui, "show_busy"), \
                mock.patch("gitmaster_flash.curses.flushinp"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            self.assertTrue(ui._commit_step2(st, items))
        self.assertEqual(ui.message, gmf_module.t("commit_outcome_unknown"))

    def test_timeout_with_failed_commit_proof_is_outcome_unknown(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "M", "path": "one.txt", "ignore": False,
                  "pattern": None, "include": True, "rename_group": ""}]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        commit_timeout = subprocess.TimeoutExpired(["git", "commit"], 120)
        commit_timeout.approved_head = gmf_module.current_head(self.repo, 10)
        commit_timeout.approved_ref = "refs/heads/main"
        commit_timeout.approved_tree = "a" * 40
        commit_timeout.reflog_action = "gmf test interrupted commit"
        with mock.patch.object(ui, "prompt_line", return_value="selected"), \
                mock.patch.object(gmf_module, "commit_selected",
                                  side_effect=commit_timeout), \
                mock.patch.object(gmf_module, "finish_interrupted_commit",
                                  side_effect=CommitSafetyError("tree differs")), \
                mock.patch.object(ui, "refresh_one", return_value=st), \
                mock.patch.object(ui, "show_busy"), \
                mock.patch("gitmaster_flash.curses.flushinp"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            self.assertTrue(ui._commit_step2(st, items))
        self.assertEqual(ui.message, gmf_module.t("commit_outcome_unknown"))

    def test_timeout_with_os_error_during_proof_is_outcome_unknown(self):
        (self.repo / "one.txt").write_text("changed\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        items = [{"code": "M", "path": "one.txt", "ignore": False,
                  "pattern": None, "include": True, "rename_group": ""}]
        ui = TUI(self.Screen(), self.root, DEFAULT_CONFIG, None)
        commit_timeout = subprocess.TimeoutExpired(["git", "commit"], 120)
        commit_timeout.approved_head = gmf_module.current_head(self.repo, 10)
        commit_timeout.approved_ref = "refs/heads/main"
        commit_timeout.approved_tree = "a" * 40
        commit_timeout.reflog_action = "gmf test interrupted commit"
        with mock.patch.object(ui, "prompt_line", return_value="selected"), \
                mock.patch.object(gmf_module, "commit_selected",
                                  side_effect=commit_timeout), \
                mock.patch.object(gmf_module, "finish_interrupted_commit",
                                  side_effect=OSError("cannot read repo")), \
                mock.patch.object(ui, "refresh_one", return_value=st), \
                mock.patch.object(ui, "show_busy"), \
                mock.patch("gitmaster_flash.curses.flushinp"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            self.assertTrue(ui._commit_step2(st, items))
        self.assertEqual(ui.message, gmf_module.t("commit_outcome_unknown"))

    def test_rename_pair_toggle_never_affects_a_second_pair(self):
        git(self.repo, "mv", "one.txt", "one-new.txt")
        git(self.repo, "mv", "two.txt", "two-new.txt")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        groups = {entry.rename_group for entry in st.files if entry.rename_group}
        self.assertEqual(len(groups), 2)
        captured = []
        ui = TUI(self.Screen([ord(" "), 10]), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]

        def capture(_status, items):
            captured.extend(dict(item) for item in items)
            return True

        with mock.patch.object(ui, "_commit_step2", side_effect=capture), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_commit_wizard()
        first_group = captured[0]["rename_group"]
        first = [item for item in captured if item["rename_group"] == first_group]
        other = [item for item in captured if item["rename_group"] != first_group]
        self.assertEqual(len(first), 2)
        self.assertFalse(any(item["include"] for item in first))
        self.assertTrue(all(item["include"] for item in other))


class HookInterferenceTests(unittest.TestCase):
    """`git commit` vererbt GIT_INDEX_FILE an seine Hooks.

    Ein pre-commit-Hook kann darüber mit `git add` zusätzliche, bewusst NICHT
    freigegebene Pfade in den temporären Index stagen — genau so landete eine
    abgewählte Datei im Commit. Die Hilfe muss das nach dem Commit erkennen und
    den Commit zurücknehmen, statt Erfolg zu melden.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"; self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)

    def tearDown(self):
        self.tmp.cleanup()

    def _install_hook(self, script: str) -> None:
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\n" + script + "\n")
        hook.chmod(0o755)

    def _base_commit(self) -> None:
        (self.repo / "gut.txt").write_text("base\n")
        (self.repo / "geheim.txt").write_text("base\n")
        git(self.repo, "add", "gut.txt", "geheim.txt")
        git(self.repo, "commit", "-qm", "base")

    def test_hook_staged_path_rolls_the_commit_back(self):
        self._base_commit()
        (self.repo / "gut.txt").write_text("freigegeben\n")
        (self.repo / "geheim.txt").write_text("abgewählt\n")
        self._install_hook("git add geheim.txt")
        head_before = gmf_module.current_head(self.repo, 10)
        with self.assertRaisesRegex(CommitSafetyError, "rolled back"):
            commit_selected(self.repo, ["gut.txt"], "nur gut", 10)
        # Der Branch steht wieder auf dem alten Commit; nichts wurde publiziert.
        self.assertEqual(gmf_module.current_head(self.repo, 10), head_before)
        show = subprocess.run(
            ["git", "-C", str(self.repo), "show", "HEAD:geheim.txt"],
            check=True, capture_output=True, text=True).stdout
        self.assertEqual(show, "base\n")
        # Arbeitsbaum unangetastet: beide Änderungen liegen weiter vor.
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True, capture_output=True, text=True).stdout
        self.assertIn("gut.txt", status)
        self.assertIn("geheim.txt", status)

    def test_commit_rollback_does_not_run_reference_transaction_hook(self):
        self._base_commit()
        marker = Path(self.tmp.name) / "reference-hook-ran"
        armed = Path(self.tmp.name) / "rollback-armed"
        ref_hook = self.repo / ".git" / "hooks" / "reference-transaction"
        ref_hook.write_text(
            "#!/bin/sh\n"
            f"test ! -e {shlex.quote(str(armed))} || "
            f"printf ran >> {shlex.quote(str(marker))}\n")
        ref_hook.chmod(0o755)
        post_commit = self.repo / ".git" / "hooks" / "post-commit"
        post_commit.write_text(
            "#!/bin/sh\n"
            f": > {shlex.quote(str(armed))}\n")
        post_commit.chmod(0o755)
        (self.repo / "gut.txt").write_text("freigegeben\n")
        (self.repo / "geheim.txt").write_text("abgewählt\n")
        self._install_hook("git add geheim.txt")

        with self.assertRaisesRegex(CommitSafetyError, "rolled back"):
            commit_selected(self.repo, ["gut.txt"], "nur gut", 10)

        self.assertTrue(armed.exists())
        self.assertFalse(marker.exists())

    def test_index_adoption_does_not_run_post_index_change_hook(self):
        self._base_commit()
        marker = Path(self.tmp.name) / "index-hook-ran"
        armed = Path(self.tmp.name) / "adoption-armed"
        post_commit = self.repo / ".git" / "hooks" / "post-commit"
        post_commit.write_text(
            "#!/bin/sh\n"
            f": > {shlex.quote(str(armed))}\n")
        post_commit.chmod(0o755)
        index_hook = self.repo / ".git" / "hooks" / "post-index-change"
        index_hook.write_text(
            "#!/bin/sh\n"
            f"if test -e {shlex.quote(str(armed))}; then\n"
            f"  printf ran >> {shlex.quote(str(marker))}\n"
            "  git add geheim.txt\n"
            "fi\n")
        index_hook.chmod(0o755)
        (self.repo / "gut.txt").write_text("freigegeben\n")
        (self.repo / "geheim.txt").write_text("bleibt unstaged\n")

        # Belegen, dass dieses Git den Hook beim betroffenen Reset wirklich
        # ausführt; sonst könnte ein bloß ignorierter Hook den Test grün machen.
        probe_index = Path(self.tmp.name) / "probe-index"
        probe_index.write_bytes((self.repo / ".git" / "index").read_bytes())
        armed.touch()
        probe_env = {**os.environ, "GIT_INDEX_FILE": str(probe_index)}
        probe = subprocess.run(
            ["git", "-C", str(self.repo), "reset", "-q", "HEAD", "--",
             "gut.txt"], env=probe_env, capture_output=True, text=True)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertTrue(marker.exists())
        marker.unlink()
        armed.unlink()

        result = commit_selected(self.repo, ["gut.txt"], "nur gut", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(
            git_output(self.repo, "diff", "--cached", "--name-only", "--",
                       "geheim.txt"), "")
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain=v1", "--",
             "geheim.txt"], check=True, capture_output=True,
            text=True).stdout
        self.assertIn(" M geheim.txt", status)

    def test_failed_hook_rollback_reports_an_unknown_existing_commit(self):
        self._base_commit()
        (self.repo / "gut.txt").write_text("freigegeben\n")
        (self.repo / "geheim.txt").write_text("abgewählt\n")
        self._install_hook("git add geheim.txt")
        head_before = gmf_module.current_head(self.repo, 10)
        original = gmf_module.run_git

        def fail_rollback(repo, *args, **kwargs):
            if "update-ref" in args:
                return subprocess.CompletedProcess(
                    ["git", *args], 1, "", "simulated lock race")
            return original(repo, *args, **kwargs)

        with mock.patch.object(
                gmf_module, "run_git_logged", side_effect=fail_rollback), \
                self.assertRaises(gmf_module.CommitOutcomeUnknownError):
            commit_selected(self.repo, ["gut.txt"], "nur gut", 10)

        self.assertNotEqual(gmf_module.current_head(self.repo, 10), head_before)

    def test_wrong_branch_rollback_restores_that_branch_own_parent(self):
        self._base_commit()
        approved_head = git_output(self.repo, "rev-parse", "HEAD")
        tree = git_output(self.repo, "rev-parse", "HEAD^{tree}")
        other_parent = git_output(
            self.repo, "commit-tree", tree, "-m", "other root")
        committed = git_output(
            self.repo, "commit-tree", tree, "-p", other_parent,
            "-m", "misdirected")
        git(self.repo, "update-ref", "refs/heads/other", committed)

        with self.assertRaisesRegex(CommitSafetyError, "rolled back"):
            gmf_module._verify_hooks_kept_approved_tree(
                self.repo, approved_head, tree, 10, "refs/heads/main",
                committed, "refs/heads/other")

        self.assertEqual(
            git_output(self.repo, "rev-parse", "refs/heads/main"),
            approved_head)
        self.assertEqual(
            git_output(self.repo, "rev-parse", "refs/heads/other"),
            other_parent)

    def test_same_branch_parent_race_preserves_the_foreign_parent(self):
        self._base_commit()
        approved_head = git_output(self.repo, "rev-parse", "HEAD")
        tree = git_output(self.repo, "rev-parse", "HEAD^{tree}")
        foreign = git_output(
            self.repo, "commit-tree", tree, "-p", approved_head,
            "-m", "foreign")
        committed = git_output(
            self.repo, "commit-tree", tree, "-p", foreign,
            "-m", "gmf after race")
        git(self.repo, "update-ref", "refs/heads/main", committed)

        with self.assertRaisesRegex(CommitSafetyError, "rolled back"):
            gmf_module._verify_hooks_kept_approved_tree(
                self.repo, approved_head, tree, 10, "refs/heads/main",
                committed, "refs/heads/main")

        self.assertEqual(
            git_output(self.repo, "rev-parse", "refs/heads/main"), foreign)

    def test_replace_ref_cannot_hide_a_hook_changed_tree(self):
        self._base_commit()
        head_before = gmf_module.current_head(self.repo, 10)
        (self.repo / "gut.txt").write_text("freigegeben\n")
        (self.repo / "geheim.txt").write_text("abgewählt\n")
        git(self.repo, "add", "gut.txt")
        approved_tree = subprocess.run(
            ["git", "-C", str(self.repo), "write-tree"], check=True,
            capture_output=True, text=True).stdout.strip()
        git(self.repo, "reset", "-q", "HEAD", "--", "gut.txt")
        self._install_hook("git add geheim.txt")
        post = self.repo / ".git" / "hooks" / "post-commit"
        post.write_text(
            "#!/bin/sh\n"
            f"replacement=$(printf 'replacement\\n' | git commit-tree {approved_tree} "
            f"-p {head_before})\n"
            "git replace -f \"$(git rev-parse HEAD)\" \"$replacement\"\n")
        post.chmod(0o755)

        with self.assertRaisesRegex(CommitSafetyError, "rolled back"):
            commit_selected(self.repo, ["gut.txt"], "nur gut", 10)
        self.assertEqual(gmf_module.current_head(self.repo, 10), head_before)

    def test_hook_cannot_make_the_new_commit_look_parentless_via_shallow(self):
        self._base_commit()
        head_before = gmf_module.current_head(self.repo, 10)
        (self.repo / "gut.txt").write_text("approved\n")
        post = self.repo / ".git" / "hooks" / "post-commit"
        post.write_text(
            "#!/bin/sh\n"
            "git rev-parse HEAD > \"$(git rev-parse --git-dir)/shallow\"\n")
        post.chmod(0o755)

        result = commit_selected(
            self.repo, ["gut.txt"], "shallow hook", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            git_output(self.repo, "rev-parse", "refs/heads/main"),
            result.committed_head)
        self.assertEqual(
            gmf_module.commit_parents(self.repo, result.committed_head, 10),
            [head_before])

    def test_replace_ref_for_new_commit_cannot_change_index_adoption(self):
        self._base_commit()
        head_before = gmf_module.current_head(self.repo, 10)
        base_tree = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", head_before + "^{tree}"],
            check=True, capture_output=True, text=True).stdout.strip()
        (self.repo / "gut.txt").write_text("freigegeben\n")
        post = self.repo / ".git" / "hooks" / "post-commit"
        post.write_text(
            "#!/bin/sh\n"
            f"replacement=$(printf 'replacement\\n' | git commit-tree {base_tree} "
            f"-p {head_before})\n"
            "git replace -f \"$(git rev-parse HEAD)\" \"$replacement\"\n")
        post.chmod(0o755)

        result = commit_selected(self.repo, ["gut.txt"], "nur gut", 10)

        self.assertEqual(result.returncode, 0, result.stderr)
        cached = subprocess.run(
            ["git", "-C", str(self.repo), "show", ":gut.txt"], check=True,
            capture_output=True, text=True).stdout
        self.assertEqual(cached, "freigegeben\n")

    def test_hook_interference_on_initial_commit_restores_unborn_state(self):
        (self.repo / "gut.txt").write_text("a\n")
        (self.repo / "geheim.txt").write_text("b\n")
        self._install_hook("git add geheim.txt")
        with self.assertRaisesRegex(CommitSafetyError, "rolled back"):
            commit_selected(self.repo, ["gut.txt"], "initial", 10)
        # Das Repo ist wieder ohne Commit (unborn branch), nichts ging verloren.
        self.assertIsNone(gmf_module.current_head(self.repo, 10))
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True, capture_output=True, text=True).stdout
        self.assertIn("gut.txt", status)
        self.assertIn("geheim.txt", status)

    def test_harmless_hook_does_not_disturb_the_commit(self):
        self._base_commit()
        (self.repo / "gut.txt").write_text("freigegeben\n")
        self._install_hook("echo pre-commit lief > /dev/null")
        r = commit_selected(self.repo, ["gut.txt"], "mit Hook", 10)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_hook_commit_then_failure_is_reported_as_unknown_outcome(self):
        self._base_commit()
        before = gmf_module.current_head(self.repo, 10)
        (self.repo / "gut.txt").write_text("freigegeben\n")
        self._install_hook("git commit --no-verify -m nested\nexit 1")

        with self.assertRaises(gmf_module.CommitOutcomeUnknownError):
            commit_selected(self.repo, ["gut.txt"], "outer", 10)

        self.assertNotEqual(gmf_module.current_head(self.repo, 10), before)
        subject = subprocess.run(
            ["git", "-C", str(self.repo), "log", "-1", "--format=%s"],
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(subject, "nested")
        changed = subprocess.run(
            ["git", "-C", str(self.repo), "diff-tree", "--no-commit-id",
             "--name-only", "-r", "HEAD"],
            check=True, capture_output=True, text=True).stdout.split()
        self.assertEqual(changed, ["gut.txt"])

    def test_hook_commit_on_other_branch_cannot_hide_behind_restored_head(self):
        self._base_commit()
        before = gmf_module.current_head(self.repo, 10)
        (self.repo / "gut.txt").write_text("freigegeben\n")
        self._install_hook(
            "git branch other HEAD\n"
            "git symbolic-ref HEAD refs/heads/other\n"
            "git commit --no-verify -m nested-other\n"
            "git symbolic-ref HEAD refs/heads/main\n"
            "exit 1")

        with self.assertRaises(gmf_module.CommitOutcomeUnknownError):
            commit_selected(self.repo, ["gut.txt"], "outer", 10)

        self.assertEqual(gmf_module.current_head(self.repo, 10), before)
        self.assertNotEqual(
            git_output(self.repo, "rev-parse", "refs/heads/other"), before)


class SlowPreCommitHookTests(unittest.TestCase):
    """Ein langsamer pre-commit-Hook ist der Alltagsfall, an dem gmf abstürzte.

    `git commit` führt den Hook des Repos aus; startet der Linter oder Tests, ist der
    kurze `git_timeout` von zehn Sekunden längst um. Früher flog der `TimeoutExpired`
    dann bis in `main()` durch und beendete die TUI mit einem Traceback.

    Die Tests unten erzwingen diesen Timeout über `HOOK_TIMEOUT` statt über den
    Produktionswert `commit_timeout` von 120 Sekunden. Der Wert muss zwei Dinge
    zugleich leisten: kurz genug, um die Suite nicht zu bremsen, und lang genug,
    dass der Hook seine ersten Schritte (Marker schreiben, Fremd-Commit anlegen)
    davor sicher schafft. Gemessen braucht er dafür rund 0,4 Sekunden; mit den
    früheren fest verdrahteten 1 Sekunde blieb unter der Last der vollen Suite
    zu wenig Rand, und drei Tests dieser Klasse schlugen sporadisch fehl
    (2026-08-19: `hook-pids` fehlte, der Fremd-Commit des Hooks entstand nie).
    """

    # Rand gegenüber den gemessenen ~0,4 s Hook-Vorlauf; kostet je Test genau
    # diese Wartezeit, weil der Hook danach absichtlich weiterhängt.
    HOOK_TIMEOUT = 3

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"; self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "file.txt").write_text("base\n")
        git(self.repo, "add", "file.txt")
        git(self.repo, "commit", "-qm", "base")
        (self.repo / "file.txt").write_text("changed\n")
        self.marker = Path(self.tmp.name) / "hook-pids"

    def tearDown(self):
        self.tmp.cleanup()

    def _install_hook(self, seconds: float) -> None:
        """pre-commit-Hook, der `seconds` lang beschäftigt ist.

        Er notiert seine eigene PID und die eines Enkelprozesses; daran prüft der
        Test, dass ein Timeout wirklich die ganze Prozessgruppe abräumt.
        """
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            f"sleep {seconds} &\n"
            "child=$!\n"
            f"echo \"$$ $child\" > {shlex.quote(str(self.marker))}\n"
            "wait $child\n")
        hook.chmod(0o755)

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def test_timeout_kills_hook_and_its_children_and_leaves_no_commit(self):
        self._install_hook(30)
        head_before = gmf_module.current_head(self.repo, 10)
        with self.assertRaises(subprocess.TimeoutExpired):
            commit_selected(self.repo, ["file.txt"], "hängt", 10,
                            commit_timeout=self.HOOK_TIMEOUT)
        hook_pid, child_pid = (int(p) for p in self.marker.read_text().split())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (self._alive(hook_pid) or self._alive(child_pid)):
            time.sleep(0.05)
        self.assertFalse(self._alive(hook_pid), "pre-commit-Hook läuft weiter")
        self.assertFalse(self._alive(child_pid), "Enkelprozess des Hooks läuft weiter")
        self.assertEqual(gmf_module.current_head(self.repo, 10), head_before)
        self.assertFalse((self.repo / ".git" / "index.lock").exists())

    def test_timeout_kills_a_background_child_after_git_has_exited(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            "trap '' HUP\n"
            "sleep 30 &\n"
            f"echo $! > {shlex.quote(str(self.marker))}\n"
            "exit 0\n")
        hook.chmod(0o755)

        with self.assertRaises(subprocess.TimeoutExpired):
            commit_selected(
                self.repo, ["file.txt"], "hintergrund", 10,
                commit_timeout=self.HOOK_TIMEOUT)

        child_pid = int(self.marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self._alive(child_pid):
            time.sleep(0.05)
        self.assertFalse(self._alive(child_pid),
                         "Hintergrundprozess des beendeten Hooks läuft weiter")

    def test_commit_timeout_covers_the_hook_while_git_timeout_stays_short(self):
        # Genau der gemeldete Fall: Vorbereitungsschritte sind schnell, nur der
        # Commit selbst braucht wegen des Hooks länger als git_timeout.
        self._install_hook(2)
        r = commit_selected(self.repo, ["file.txt"], "mit Hook", 1, commit_timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(gmf_module.current_head(self.repo, 10), None)

    def test_timeout_message_names_the_subcommand_and_the_limit(self):
        exc = subprocess.TimeoutExpired(["git", "-C", str(self.repo), "commit", "-m", "x"], 42)
        text = gmf_module.timeout_message(exc)
        self.assertIn("commit", text)
        self.assertIn("42", text)

    def test_timeout_message_skips_global_url_binding_options(self):
        push = safe_push_args("/tmp/approved.git", "main", "a" * 40, "b" * 40)
        exc = subprocess.TimeoutExpired(
            ["git", "-C", str(self.repo), *push], 17)
        text = gmf_module.timeout_message(exc)
        self.assertIn("push", text)
        self.assertNotIn("git -c", text)
        self.assertIn("outcome is unknown", text)

    def test_commit_written_before_a_hanging_post_commit_hook_is_not_adopted(self):
        """Hängt erst der post-commit-Hook, existiert der Commit längst.

        Ohne synchronen Rückgabekanal lässt sich seine Herkunft aber nicht sicher
        beweisen. Der Commit bleibt deshalb bestehen, der echte Index wird nicht
        übernommen und `git status` zeigt die manuell zu prüfende Änderung weiter.
        """
        hook = self.repo / ".git" / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\nsleep 30\n")
        hook.chmod(0o755)
        head_before = gmf_module.current_head(self.repo, 10)
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            commit_selected(self.repo, ["file.txt"], "haengt danach", 10,
                            commit_timeout=self.HOOK_TIMEOUT)
        # Die Ausnahme trägt den freigegebenen Baum — derselbe Weg, den auch
        # der Timeout-Zweig der Commit-Hilfe nimmt.
        done = gmf_module.finish_interrupted_commit(
            self.repo, head_before, 10, cm.exception.approved_tree)
        self.assertTrue(done)
        self.assertNotEqual(gmf_module.current_head(self.repo, 10), head_before)
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True, capture_output=True, text=True).stdout
        self.assertEqual(status, "MM file.txt\n")

    def test_timeout_cannot_hide_a_hook_commit_on_another_branch(self):
        head_before = gmf_module.current_head(self.repo, 10)
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            "git branch other HEAD\n"
            "git symbolic-ref HEAD refs/heads/other\n"
            "git commit --no-verify -m nested-other\n"
            "git symbolic-ref HEAD refs/heads/main\n"
            "sleep 30\n")
        hook.chmod(0o755)

        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            commit_selected(
                self.repo, ["file.txt"], "outer", 10,
                commit_timeout=self.HOOK_TIMEOUT)

        self.assertEqual(gmf_module.current_head(self.repo, 10), head_before)
        # Erst dieser Nachweis trennt "die Erkennung greift nicht" von "der Hook
        # kam vor dem Timeout gar nicht bis zu seinem Commit". Ohne ihn las sich
        # ein zu knapper HOOK_TIMEOUT wie ein Fehler in der Erkennung.
        foreign = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "-q", "--verify",
             "refs/heads/other"], capture_output=True, text=True).stdout.strip()
        self.assertTrue(
            foreign and foreign != head_before,
            "der pre-commit-Hook schrieb seinen Fremd-Commit nicht vor dem Timeout")
        with self.assertRaisesRegex(CommitSafetyError, "another branch"):
            gmf_module.finish_interrupted_commit(
                self.repo, raised.exception.approved_head, 10,
                raised.exception.approved_tree, raised.exception.approved_ref,
                raised.exception.reflog_action)

    def test_interrupted_commit_without_result_reports_false(self):
        head_before = gmf_module.current_head(self.repo, 10)
        self.assertFalse(gmf_module.finish_interrupted_commit(
            self.repo, head_before, 10))

    def test_timeout_never_rolls_back_a_foreign_commit(self):
        head_before = gmf_module.current_head(self.repo, 10)
        approved_tree = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD^{tree}"], check=True,
            capture_output=True, text=True).stdout.strip()
        (self.repo / "foreign.txt").write_text("parallel\n")
        git(self.repo, "add", "foreign.txt")
        git(self.repo, "commit", "-qm", "foreign")
        foreign_head = gmf_module.current_head(self.repo, 10)

        with self.assertRaisesRegex(CommitSafetyError, "approved tree"):
            gmf_module.finish_interrupted_commit(
                self.repo, head_before, 10, approved_tree)

        self.assertEqual(gmf_module.current_head(self.repo, 10), foreign_head)

    def test_hook_staged_extras_during_timeout_are_not_rolled_back(self):
        """Der Timeout-Zweig prüft den Commit gegen den freigegebenen Baum.

        Ein pre-commit-Hook stagt hier heimlich eine weitere Datei in den
        geerbten temporären Index; der post-commit-Hook hängt, sodass der
        Commit zwar entsteht, `git commit` aber in den Timeout läuft. Vorher
        verglich finish_interrupted_commit nur Pfadnamen — der Commit mit der
        geschmuggelten Datei blieb ungeprüft stehen. Jetzt wird sein Baum gegen
        die Freigabe geprüft und der Fehler gemeldet; ohne Eigentumsnachweis
        bleibt der Commit selbst bewusst unangetastet.
        """
        (self.repo / "geschmuggelt.txt").write_text("nicht freigegeben\n")
        pre = self.repo / ".git" / "hooks" / "pre-commit"
        pre.write_text("#!/bin/sh\ngit add geschmuggelt.txt\n")
        pre.chmod(0o755)
        post = self.repo / ".git" / "hooks" / "post-commit"
        post.write_text("#!/bin/sh\nsleep 30\n")
        post.chmod(0o755)
        head_before = gmf_module.current_head(self.repo, 10)
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            commit_selected(self.repo, ["file.txt"], "schmuggelt", 10,
                            commit_timeout=3)
        with self.assertRaises(gmf_module.CommitSafetyError):
            gmf_module.finish_interrupted_commit(
                self.repo, head_before, 10,
                cm.exception.approved_tree)
        # Ohne Eigentumsnachweis bleibt der Commit erreichbar; gmf mutiert HEAD
        # nach einem Timeout niemals auf Verdacht.
        self.assertNotEqual(gmf_module.current_head(self.repo, 10), head_before)

    def test_timeout_keeps_the_commit_on_its_actual_base(self):
        """Der Aufrufer liest HEAD, BEVOR er commit_selected startet.

        Bewegt sich HEAD dazwischen — ein anderes Programm committet im selben
        Repo —, passt sein Wert nicht mehr zum Elternteil unseres Commits. Der
        atomare Rollback prüft aber genau diesen Elternwert: Er unterblieb, und
        ein vom Hook erweiterter Commit blieb trotz erkanntem Fehler stehen.
        Deshalb hängt commit_selected seine EIGENE HEAD-Basis an die Ausnahme.
        """
        veralteter_stand = gmf_module.current_head(self.repo, 10)
        # Der fremde Commit dazwischen.
        (self.repo / "fremd.txt").write_text("von woanders\n")
        git(self.repo, "add", "fremd.txt")
        git(self.repo, "commit", "-qm", "fremder Commit")
        fremder_stand = gmf_module.current_head(self.repo, 10)
        self.assertNotEqual(fremder_stand, veralteter_stand)

        (self.repo / "geschmuggelt.txt").write_text("nicht freigegeben\n")
        pre = self.repo / ".git" / "hooks" / "pre-commit"
        pre.write_text("#!/bin/sh\ngit add geschmuggelt.txt\n")
        pre.chmod(0o755)
        post = self.repo / ".git" / "hooks" / "post-commit"
        post.write_text("#!/bin/sh\nsleep 30\n")
        post.chmod(0o755)
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            commit_selected(self.repo, ["file.txt"], "schmuggelt", 10,
                            commit_timeout=3)

        # Genau der Ausdruck, den der Timeout-Zweig der Commit-Hilfe benutzt.
        basis = getattr(cm.exception, "approved_head", veralteter_stand)
        self.assertEqual(basis, fremder_stand)
        with self.assertRaises(gmf_module.CommitSafetyError):
            gmf_module.finish_interrupted_commit(
                self.repo, basis, 10, cm.exception.approved_tree)
        # Der neue Commit bleibt stehen; insbesondere verschwindet der fremde
        # Eltern-Commit nicht durch einen spekulativen Rollback.
        head_after = gmf_module.current_head(self.repo, 10)
        self.assertNotEqual(head_after, fremder_stand)
        self.assertEqual(subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD^"], check=True,
            capture_output=True, text=True).stdout.strip(), fremder_stand)


class StashAndReadFailureTests(unittest.TestCase):
    def test_stash_preview_includes_untracked_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp); git(repo, "init", "-q")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "base").write_text("base\n"); git(repo, "add", "base"); git(repo, "commit", "-qm", "base")
            (repo / "untracked.bin").write_bytes(bytes(range(256)) * 2)
            git(repo, "stash", "push", "-qu")
            ok, preview = stash_preview(repo, 10)
            self.assertTrue(ok)
            self.assertIn("untracked.bin", preview)
            self.assertIn("GIT binary patch", preview)

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
            driver.write_text(
                "#!/bin/sh\nprintf ran > " + shlex.quote(str(marker)) + "\n")
            driver.chmod(0o755)

            with mock.patch.dict(
                    os.environ, {"GIT_EXTERNAL_DIFF": str(driver)}):
                ok, preview = stash_preview(repo, 10)

            self.assertTrue(ok, preview)
            self.assertIn("+changed", preview)
            self.assertFalse(marker.exists())

    def test_stash_view_uses_one_oid_bound_snapshot_for_title_and_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            path = repo / "a"; path.write_text("base\n")
            git(repo, "add", "a"); git(repo, "commit", "-qm", "base")
            path.write_text("old\n"); git(repo, "stash", "push", "-qm", "old")

            class Screen:
                def getmaxyx(self): return (30, 100)

            ui = TUI(Screen(), root, DEFAULT_CONFIG, None)
            ui.statuses = [collect_status(repo, root, DEFAULT_CONFIG)]
            path.write_text("new\n"); git(repo, "stash", "push", "-qm", "new")
            shown = {}

            def capture(_self, title, lines):
                shown["title"] = title
                shown["text"] = "\n".join(lines)

            with mock.patch.object(TUI, "show_pager", new=capture):
                ui.action_stash_show()

            self.assertIn("new", shown["title"])
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
            original = gmf_module.latest_stash(repo, 10)[0]
            path.write_text("replacement\n")
            git(repo, "stash", "push", "-qm", "replacement")
            replacement = gmf_module.latest_stash(repo, 10)[0]
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
            original = gmf_module.latest_stash(repo, 10)[0]
            path.write_text("other\n")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "other base")
            grafts = repo / ".git" / "info" / "grafts"
            grafts.write_text(f"{original} {git_output(repo, 'rev-parse', 'HEAD')}\n")

            ok, preview = stash_preview(repo, 10)
            self.assertTrue(ok, preview)
            self.assertIn("+original", preview)

    def test_broken_index_sets_error_instead_of_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            git(repo, "init", "-q"); git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a"); git(repo, "add", "a"); git(repo, "commit", "-qm", "base")
            (repo / ".git" / "index").write_bytes(b"broken")
            st = collect_status(repo, root, DEFAULT_CONFIG)
            self.assertTrue(st.error)
            self.assertEqual(st.remote_state, "error")
            self.assertFalse(st.clean_and_synced)

    def test_stash_mutation_keys_cannot_change_a_new_checkout(self):
        """U/D bleiben ohne atomare Bindung des Zielzustands unerreichbar."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            path = repo / "a"; path.write_text("base\n")
            git(repo, "add", "a"); git(repo, "commit", "-qm", "base")
            path.write_text("approved\n"); git(repo, "stash", "push", "-qm", "approved")
            ui_status = collect_status(repo, root, DEFAULT_CONFIG)
            git(repo, "switch", "-qc", "other")
            path.write_text("other branch edit\n")
            before_head = git_output(repo, "rev-parse", "HEAD")
            before_status = git_output(repo, "status", "--porcelain=v1", "-z",
                                       "--untracked-files=all")
            before_stack = git_output(repo, "stash", "list", "--format=%H %gd %gs")

            class Screen:
                def getmaxyx(self): return (30, 100)

            ui = TUI(Screen(), root, DEFAULT_CONFIG, None)
            ui.statuses = [ui_status]
            with mock.patch.object(gmf_module, "run_git_logged") as logged:
                ui.dispatch_action("U")
                ui.dispatch_action("D")

            logged.assert_not_called()
            self.assertEqual(git_output(repo, "rev-parse", "HEAD"), before_head)
            self.assertEqual(
                git_output(repo, "status", "--porcelain=v1", "-z",
                           "--untracked-files=all"), before_status)
            self.assertEqual(
                git_output(repo, "stash", "list", "--format=%H %gd %gs"),
                before_stack)
            self.assertEqual(path.read_text(), "other branch edit\n")


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
            r = gmf_module.run_git(repo, "fetch", "--prune", "--quiet", "--", "faux",
                                   timeout=30)
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
            with mock.patch.object(gmf_module, "keychain_session",
                                   return_value=True):
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
            with mock.patch.object(gmf_module, "keychain_session",
                                   return_value=True):
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
            with mock.patch.object(gmf_module, "keychain_session",
                                   return_value=True):
                st = collect_status(repo, root, DEFAULT_CONFIG, fetch=True)

        remotes = {remote.name: remote for remote in st.remotes}
        self.assertEqual(remotes["login"].fetch_outcome, "auth")
        self.assertEqual(remotes["gone"].fetch_outcome, "gone")
        self.assertFalse(remotes["valid"].fetch_failed)
        self.assertIn("login", remotes["login"].fetch_error_long.lower())
        self.assertIn("no repository", remotes["gone"].fetch_error_long.lower())
        payloads = {remote["name"]: remote
                    for remote in status_dict(st)["remotes"]}
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
                "#!/bin/sh\n"
                f"printf ran > {shlex.quote(str(marker))}\n"
                "printf 'test-token\\n'\n")
            hook.chmod(0o755)
            git(repo, "config", "core.fsmonitor", str(hook))
            git(repo, "config", "core.fsmonitorHookVersion", "2")

            # Die Gegenprobe belegt, dass die Repo-Konfiguration ohne unseren
            # Guard tatsächlich ausführbaren Code starten würde.
            probe_env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
            probe = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                env=probe_env, capture_output=True, text=True)
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
            oid, _ = gmf_module.latest_stash(repo, 10)
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
            staged_before = git_output(repo, *staged)
            ok_file, _ = file_diff(repo, "M", "tracked.txt", 10)
            self.assertTrue(ok_file)
            self.assertFalse(marker.exists())
            self.assertEqual(git_output(repo, *staged), staged_before)

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
            parent = git_output(repo, "rev-parse", "HEAD")
            tree = git_output(repo, "rev-parse", "HEAD^{tree}")
            identity = "T <t@example.invalid> 0 +0000"
            raw_commit = (
                f"tree {tree}\nparent {parent}\nauthor {identity}\n"
                f"committer {identity}\n"
                "gpgsig -----BEGIN PGP SIGNATURE-----\n"
                " fake\n -----END PGP SIGNATURE-----\n\n"
                "synthetically signed\n")
            signed = subprocess.run(
                ["git", "-C", str(repo), "hash-object", "-t", "commit",
                 "-w", "--stdin"], input=raw_commit, check=True,
                capture_output=True, text=True).stdout.strip()
            git(repo, "update-ref", "refs/heads/main", signed, parent)
            marker = root / "gpg-verifier-ran"
            verifier = root / "fake-gpg"
            verifier.write_text(
                "#!/bin/sh\n"
                f"printf ran > {shlex.quote(str(marker))}\n"
                "exit 1\n")
            verifier.chmod(0o755)
            git(repo, "config", "gpg.program", str(verifier))
            git(repo, "config", "log.showSignature", "true")

            probe = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%H"],
                capture_output=True, text=True)
            self.assertTrue(marker.exists(), probe.stderr)
            marker.unlink()

            status = collect_status(repo, root, DEFAULT_CONFIG)
            repo_info_lines(status, DEFAULT_CONFIG)
            shown = gmf_module.run_git(
                repo, "show", "--format=%H", "--no-patch", "HEAD")

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
                    ["/bin/sleep", "30"], stdin=kwargs.get("stdin"),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=kwargs["start_new_session"])
                self.first = True
                self.pids.append(self.proc.pid)

            @property
            def pid(self): return self.proc.pid

            @property
            def returncode(self): return self.proc.returncode

            def communicate(self, timeout=None):
                if self.first:
                    self.first = False
                    raise OSError("lost process result")
                return self.proc.communicate(timeout=timeout)

            def kill(self): return self.proc.kill()
            def __enter__(self): return self
            def __exit__(self, *args): return self.proc.__exit__(*args)

        for operation in (
                lambda: gmf_module.run_git(Path.cwd(), "status", timeout=10),
                lambda: gmf_module._run_process_group(
                    ["ssh", "example"], stdin=subprocess.DEVNULL, timeout=10)):
            with self.subTest(operation=operation), \
                    mock.patch("gitmaster_flash.subprocess.Popen",
                               LostResultProcess), \
                    self.assertRaises(OSError):
                operation()
            pid = LostResultProcess.pids[-1]
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_logged_fetch_and_ref_update_keep_unclear_oserrors_in_history(self):
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for args in (
                ("fetch", "--", "example"),
                (*gmf_module.safe_update_ref_args(
                    "--no-deref", "refs/remotes/origin/main", "a" * 40),)):
            with self.subTest(args=args), \
                    mock.patch.object(
                        gmf_module, "run_git", side_effect=OSError("lost")), \
                    self.assertRaises(OSError):
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
                ["git", "-C", str(repos[1]), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True).stdout.strip()
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
                committed = commit_selected(
                    repos[1], ["tracked"], "only repo b", 10)
            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repos[0]), "log", "-1", "--format=%s"],
                    check=True, capture_output=True, text=True).stdout.strip(),
                "a",
            )

    def test_credentials_missing_only_for_login_errors(self):
        def result(stderr):
            return subprocess.CompletedProcess(["git"], 128, "", stderr)

        # `keychain_session` festnageln: Ohne das haengt der Test an der Sitzung,
        # in der die Suite laeuft — in der GUI-Sitzung gruen, ueber ssh rot. Genau
        # solche Geisterfehler will man nicht.
        with mock.patch.object(gmf_module, "keychain_session",
                                        return_value=True):
            self.assertTrue(gmf_module.credentials_missing(result(
                "fatal: could not read Username for 'https://github.com': "
                "terminal prompts disabled")))
            self.assertTrue(gmf_module.credentials_missing(result(
                "git@github.com: Permission denied (publickey).")))
            self.assertFalse(gmf_module.credentials_missing(result(
                "fatal: couldn't find remote ref main")))

    def test_auth_error_without_keychain_is_its_own_cause(self):
        """Dieselbe Git-Meldung, zwei sehr verschiedene Ursachen. In einer Sitzung
        ohne Schluesselbund (ssh, LaunchDaemon, cron) fehlen keine Zugangsdaten —
        sie sind nur nicht lesbar. "Login fehlt" schickte am 2026-08-05 in die
        falsche Richtung: Eine erneute Anmeldung würde empfohlen, obwohl alles in
        Ordnung war."""
        r = subprocess.CompletedProcess(["git"], 128, "", (
            "fatal: could not read Username for 'https://github.com': "
            "terminal prompts disabled"))
        with mock.patch.object(gmf_module, "keychain_session",
                                        return_value=False):
            # Ohne positiven Helper-Nachweis ist das ein echter Auth-Fehler.
            self.assertEqual(gmf_module.classify_remote_check(r), "auth")
            self.assertEqual(gmf_module.classify_remote_check(
                r, keychain_helper=True), "nokeychain")
            # Und es gilt ausdruecklich NICHT als fehlender Login.
            self.assertFalse(gmf_module.credentials_missing(
                r, keychain_helper=True))
            satz = gmf_module.remote_check_message("github", "nokeychain", 0, "", 30)
            self.assertIn("github", satz)
        with mock.patch.object(gmf_module, "keychain_session",
                                        return_value=True):
            self.assertEqual(gmf_module.classify_remote_check(r), "auth")

    def test_keychain_helper_must_be_effective_for_the_remote_url(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "origin",
                "https://example.invalid/org/repo.git")
            with mock.patch.dict(os.environ, {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
            }):
                self.assertFalse(gmf_module.remote_uses_keychain_helper(
                    repo, "origin", 10))
                git(repo, "config", "credential.helper", "osxkeychain")
                self.assertTrue(gmf_module.remote_uses_keychain_helper(
                    repo, "origin", 10))

    def test_keychain_remote_read_uses_the_callers_timeout(self):
        with mock.patch.object(gmf_module, "read_remote_configs",
                               return_value={}) as read:
            self.assertFalse(gmf_module.remote_uses_keychain_helper(
                Path("/unused"), "origin", 3))
        self.assertEqual(read.call_args.args[1]["git_timeout"], 3)

    def test_keychain_diagnosis_fails_closed_on_secondary_read_errors(self):
        for error in (
                subprocess.TimeoutExpired(["git", "remote"], 1),
                ValueError("invalid URL port"),
                RuntimeError("unknown home directory")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(gmf_module, "read_remote_configs",
                                      side_effect=error):
                self.assertFalse(gmf_module.remote_uses_keychain_helper(
                    Path("/unused"), "origin", 1))

    def test_keychain_match_timeout_does_not_replace_the_transfer_error(self):
        remote = gmf_module.RemoteConfig(
            "origin", ["https://example.invalid/repo.git"],
            ["https://example.invalid/repo.git"], [], [], settings=[])
        with mock.patch.object(
                gmf_module, "read_remote_configs",
                return_value={"origin": remote}), \
                mock.patch.object(
                    gmf_module, "run_git",
                    side_effect=subprocess.TimeoutExpired(["git", "config"], 1)):
            self.assertFalse(gmf_module.remote_uses_keychain_helper(
                Path("/unused"), "origin", 1))

    def test_push_checks_the_helper_for_the_push_url(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "origin",
                "git@github.com:example/repo.git")
            git(repo, "remote", "set-url", "--push", "origin",
                "https://github.com/example/repo.git")
            with mock.patch.dict(os.environ, {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
            }):
                git(repo, "config", "credential.https://github.com.helper",
                    "osxkeychain")
                remote = gmf_module.read_remote_configs(
                    repo, DEFAULT_CONFIG)["origin"]
                self.assertTrue(remote.transfer_safe)
                self.assertFalse(gmf_module.remote_uses_keychain_helper(
                    repo, "origin", 10))
                self.assertTrue(gmf_module.remote_uses_keychain_helper(
                    repo, "origin", 10, for_push=True))

    def test_keychain_probe_never_puts_a_secret_url_in_argv(self):
        secret_url = ("https://" + "user:" + "token@" +
                      "example.invalid/repo.git?token=query")
        remote = gmf_module.RemoteConfig(
            "origin", [secret_url], [secret_url], [], [], settings=[])
        with mock.patch.object(
                gmf_module, "read_remote_configs",
                return_value={"origin": remote}), \
                mock.patch.object(gmf_module, "run_git") as run:
            self.assertFalse(gmf_module.remote_uses_keychain_helper(
                Path("/unused"), "origin", 10))
            self.assertFalse(gmf_module.remote_uses_keychain_helper(
                Path("/unused"), "origin", 10, for_push=True))
        run.assert_not_called()

    def test_external_remote_helper_is_never_used_as_a_keychain_match_argv(self):
        helper_url = "ext::helper credential=" + "opaque-value"
        remote = gmf_module.RemoteConfig(
            "origin", [helper_url], [helper_url], [], [], settings=[])
        with mock.patch.object(
                gmf_module, "read_remote_configs",
                return_value={"origin": remote}), \
                mock.patch.object(gmf_module, "run_git") as run:
            self.assertFalse(gmf_module.remote_uses_keychain_helper(
                Path("/unused"), "origin", 10))
        run.assert_not_called()

    def test_keychain_helper_name_must_not_be_a_substring(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "origin",
                "https://example.invalid/org/repo.git")
            with mock.patch.dict(os.environ, {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
            }):
                git(repo, "config", "credential.helper",
                    "store --file=/tmp/osxkeychain-credentials")
                self.assertFalse(gmf_module.remote_uses_keychain_helper(
                    repo, "origin", 10))

    def test_keychain_helper_must_be_the_exact_osxkeychain_token(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "origin",
                "https://example.invalid/org/repo.git")
            with mock.patch.dict(os.environ, {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
            }):
                for helper in ("/tmp/osxkeychain", "!osxkeychain", "OSXKEYCHAIN"):
                    with self.subTest(helper=helper):
                        git(repo, "config", "credential.helper", helper)
                        self.assertFalse(gmf_module.remote_uses_keychain_helper(
                            repo, "origin", 10))

    def test_gh_keychain_helper_must_be_an_executable_command(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "origin",
                "https://example.invalid/org/repo.git")
            with mock.patch.dict(os.environ, {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
            }):
                git(repo, "config", "credential.helper",
                    "gh auth git-credential")
                with mock.patch("gitmaster_flash.shutil.which",
                                return_value="/usr/local/bin/gh"):
                    self.assertFalse(gmf_module.remote_uses_keychain_helper(
                        repo, "origin", 10))

                git(repo, "config", "credential.helper",
                    "!gh auth git-credential")
                with mock.patch("gitmaster_flash.shutil.which",
                                return_value="/usr/local/bin/gh"):
                    self.assertTrue(gmf_module.remote_uses_keychain_helper(
                        repo, "origin", 10))
                with mock.patch("gitmaster_flash.shutil.which", return_value=None):
                    self.assertFalse(gmf_module.remote_uses_keychain_helper(
                        repo, "origin", 10))

                absolute = repo / "gh"
                absolute.write_text("#!/bin/sh\nexit 0\n")
                absolute.chmod(0o755)
                git(repo, "config", "credential.helper",
                    f"{absolute} auth git-credential")
                self.assertTrue(gmf_module.remote_uses_keychain_helper(
                    repo, "origin", 10))

                git(repo, "config", "credential.helper",
                    "!gh AUTH GIT-CREDENTIAL")
                with mock.patch("gitmaster_flash.shutil.which",
                                return_value="/usr/local/bin/gh"):
                    self.assertFalse(gmf_module.remote_uses_keychain_helper(
                        repo, "origin", 10))
                upper = repo / "GH"
                upper.write_text("#!/bin/sh\nexit 0\n")
                upper.chmod(0o755)
                git(repo, "config", "credential.helper",
                    f"{upper} auth git-credential")
                self.assertFalse(gmf_module.remote_uses_keychain_helper(
                    repo, "origin", 10))

                git(repo, "config", "credential.helper",
                    "!gh auth git-credential extra")
                with mock.patch("gitmaster_flash.shutil.which",
                                return_value="/usr/local/bin/gh"):
                    self.assertFalse(gmf_module.remote_uses_keychain_helper(
                        repo, "origin", 10))

    def test_only_the_primary_fetch_url_can_explain_a_keychain_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "remote", "add", "origin",
                "https://one.invalid/org/repo.git")
            git(repo, "remote", "set-url", "--add", "origin",
                "https://two.invalid/org/repo.git")
            with mock.patch.dict(os.environ, {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
            }):
                git(repo, "config", "credential.https://two.invalid.helper",
                    "osxkeychain")
                self.assertFalse(gmf_module.remote_uses_keychain_helper(
                    repo, "origin", 10))

    def test_a_rejected_ssh_key_is_never_excused_as_a_keychain_problem(self):
        """Gegenprobe zum Fall darueber: "Permission denied (publickey)" kommt von
        SSH und nicht von einem Credential-Helper. Ohne Helfer ist auch kein
        Schluesselbund im Spiel — die Meldung "Login ist in Ordnung, nur aus dieser
        Sitzung nicht messbar" schickte die Fehlersuche in die falsche Richtung,
        obwohl der Schluessel wirklich fehlt oder abgelehnt wird."""
        r = subprocess.CompletedProcess(["git"], 128, "", (
            "git@example.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository."))
        for sitzung in (True, False):
            with self.subTest(keychain=sitzung):
                with mock.patch.object(gmf_module, "keychain_session",
                                                return_value=sitzung):
                    self.assertEqual(gmf_module.classify_remote_check(r), "auth")
                    self.assertTrue(gmf_module.credentials_missing(r))

    def test_keychain_session_only_true_in_gui_session(self):
        """`launchctl managername` nennt den Unterschied: "Aqua" ist die GUI-Sitzung,
        alles andere lebt daneben. Ausserhalb von macOS gibt es das Problem nicht."""
        def lauf(name):
            return subprocess.CompletedProcess(["launchctl"], 0, name + "\n", "")

        for ausgabe, platform, erwartet in (("Aqua", "darwin", True),
                                            ("Background", "darwin", False),
                                            ("System", "darwin", False),
                                            ("Background", "linux", True)):
            with self.subTest(ausgabe=ausgabe, platform=platform):
                gmf_module._KEYCHAIN_SESSION = None
                with mock.patch.object(gmf_module.sys, "platform", platform), \
                     mock.patch.object(gmf_module.subprocess, "run",
                                                return_value=lauf(ausgabe)):
                    self.assertIs(gmf_module.keychain_session(), erwartet)
        # Faellt der Aufruf selbst aus, gilt der Schluesselbund als erreichbar —
        # lieber einen echten Auth-Fehler zeigen als ihn stillschweigend entschuldigen.
        gmf_module._KEYCHAIN_SESSION = None
        with mock.patch.object(gmf_module.sys, "platform", "darwin"), \
             mock.patch.object(gmf_module.subprocess, "run",
                                        side_effect=OSError("weg")):
            self.assertIs(gmf_module.keychain_session(), True)
        # Dasselbe fuer einen Aufruf, der zwar startet, aber mit Fehler endet:
        # Sein leeres stdout ist nicht "nicht Aqua", sondern gar keine Antwort.
        # Der Wert wird gemerkt — sonst gaelte fuer den REST des Prozesses jeder
        # Auth-Fehler als entschuldigt.
        gmf_module._KEYCHAIN_SESSION = None
        with mock.patch.object(gmf_module.sys, "platform", "darwin"), \
             mock.patch.object(gmf_module.subprocess, "run",
                                        return_value=subprocess.CompletedProcess(
                                            ["launchctl"], 1, "", "nope")):
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
            wrapper.write_text(
                "#!/bin/sh\n"
                f"printf ran >> {shlex.quote(str(marker))}\n"
                "exit 1\n")
            wrapper.chmod(0o755)
            git(repo, "config", "core.sshCommand", str(wrapper))
            # Port 1 nimmt niemand an: Das echte ssh scheitert sofort, ohne Netz.
            url = "ssh://127.0.0.1:1/x.git"
            environment = {"GIT_SSH_COMMAND": str(wrapper),
                           "GIT_SSH": str(wrapper)}

            pin_config, pinned = gmf_module._pinned_url_config(url)
            with mock.patch.dict(os.environ, environment):
                bound = gmf_module.run_git(
                    repo, *pin_config, "ls-remote", "--heads", "--", pinned,
                    timeout=30)
            self.assertNotEqual(bound.returncode, 0)
            self.assertFalse(
                marker.exists(),
                "ein Wrapper entschied über das Ziel eines gebundenen Transfers")

            # Gegenprobe: Ohne Bindung greift der Wrapper weiterhin. Nur so ist
            # belegt, dass oben wirklich die Bindung gewirkt hat.
            with mock.patch.dict(os.environ, environment):
                gmf_module.run_git(
                    repo, "ls-remote", "--heads", "--", url, timeout=30)
            self.assertTrue(marker.exists())


class ErrorRedactionTests(unittest.TestCase):
    """Git zitiert in Fehlermeldungen die komplette URL — inklusive Login/Query.

    Diese Zeilen erscheinen in TUI, Info-Seite und (als error_long) in --json;
    Tokens müssen deshalb schon an der Eingangsgrenze verschwinden.
    """

    @staticmethod
    def _result(stderr: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 128, "", stderr)

    def test_http_userinfo_query_and_fragment_are_removed(self):
        stderr = ("fatal: unable to access "
                  "'https://user:s3cr3t@example.com/org/repo.git?private_token=abc123#frag': "
                  "The requested URL returned error: 403")
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
        for url in ("ssh://user:s3cr3t@example.com/x.git",
                    "git://user:s3cr3t@example.com/x.git",
                    "ftps://user:s3cr3t@example.com/x.git"):
            with self.subTest(url=url):
                line = gmf_module.last_error_line(
                    self._result(f"fatal: unable to access '{url}'"))
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
        for url in ("git+ssh://user:s3cr3t@example.com/x.git",
                    "foo+://user:s3cr3t@example.com/x.git",
                    "my-helper://user:s3cr3t@example.com/x.git"):
            with self.subTest(url=url):
                line = gmf_module.last_error_line(
                    self._result(f"fatal: unable to access '{url}'"))
                self.assertNotIn("s3cr3t", line)
                if url.startswith("git+ssh" + "://"):
                    self.assertIn("user@example.com", line)
                else:
                    self.assertNotIn("user@", line)
        # Query und Fragment fallen unter denselben Schemata ebenfalls weg.
        line = gmf_module.last_error_line(self._result(
            "fatal: unable to access 'foo+://example.com/x.git?token=abc123'"))
        self.assertNotIn("abc123", line)

    def test_a_port_is_not_mistaken_for_a_password(self):
        stderr = "fatal: unable to access 'ssh://git@example.com:2222/x.git'"
        line = gmf_module.last_error_line(self._result(stderr))
        self.assertIn("git@example.com:2222/x.git", line)

    def test_plain_messages_pass_unchanged(self):
        stderr = "ssh: connect to host example.com port 22: Connection refused"
        self.assertEqual(gmf_module.last_error_line(self._result(stderr)), stderr)


class RemoteCheckTests(unittest.TestCase):
    """`T` auf der Info-Seite: existiert das Remote-Repo — und wenn nicht, warum?"""

    @staticmethod
    def _result(stderr, code=128):
        return subprocess.CompletedProcess(["git"], code, "", stderr)

    def test_unreadable_remote_config_counts_as_a_missing_remote(self):
        """Fünf Sicherheitsentscheidungen hängen an dieser einen Antwort.

        Ist die Config noch dieselbe wie bei der Freigabe? Hängt der Login am
        Schlüsselbund? Darf gefetcht werden? Eine Config, die gerade nicht
        lesbar ist, darf keine davon stützen — egal, woran das Lesen scheitert.
        """
        errors = (
            subprocess.TimeoutExpired(["git"], 1),
            gmf_module.GitReadError("git config kaputt"),
            OSError("kein Dateideskriptor"),
            ValueError("krumme Ausgabe"),
            RuntimeError("unklarer Zustand"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(gmf_module, "read_remote_configs",
                                       side_effect=error):
                    self.assertIsNone(gmf_module.read_remote_config_or_none(
                        Path("/tmp"), DEFAULT_CONFIG, "origin"))

    def test_timeout_config_changes_only_the_git_timeout(self):
        cfg = gmf_module.timeout_config(7)
        self.assertEqual(cfg["git_timeout"], 7)
        self.assertEqual(
            {key: value for key, value in cfg.items() if key != "git_timeout"},
            {key: value for key, value in DEFAULT_CONFIG.items()
             if key != "git_timeout"})
        # Die Vorlage darf dabei nicht verändert werden: sie ist prozessweit.
        self.assertEqual(DEFAULT_CONFIG["git_timeout"], 10)

    def test_causes_are_told_apart(self):
        cases = {
            "gone": "remote: Repository not found.\nfatal: repository 'https://x/y.git' not found",
            "auth": "fatal: could not read Username for 'https://github.com': "
                    "terminal prompts disabled",
            "dns": "ssh: Could not resolve hostname nirgendwo: nodename nor servname "
                   "provided, or not known",
            "unreachable": "ssh: connect to host 10.0.0.9 port 22: Connection refused",
            # Ein unbekannter Hostschlüssel ist KEIN fehlender Login: man muss ihn
            # einmal bestätigen, nicht Zugangsdaten einrichten.
            "hostkey": "Host key verification failed.\n"
                       "fatal: Could not read from remote repository.",
            "server": "fatal: unable to access 'https://x/y.git/': "
                      "The requested URL returned error: 503",
            "unknown": "fatal: something entirely new happened",
        }
        # `keychain_session` festnageln: In einer Sitzung ohne Schlüsselbund
        # (ssh, cron) würde der "auth"-Fall sonst als "nokeychain" landen und
        # der Test hinge an der Umgebung statt an der Meldung.
        with mock.patch.object(gmf_module, "keychain_session", return_value=True):
            for expected, stderr in cases.items():
                with self.subTest(expected):
                    self.assertEqual(classify_remote_check(self._result(stderr)),
                                     expected)

    def test_unknown_host_key_is_not_reported_as_a_missing_login(self):
        result = self._result("Host key verification failed.")
        self.assertFalse(gmf_module.credentials_missing(result))
        message = remote_check_message("github", classify_remote_check(result), 0, "", 30)
        self.assertIn("host key", message.lower())
        self.assertNotIn("login", message.lower())

    def test_fetch_failure_reports_the_real_cause_per_remote(self):
        # Konkreter Regressionfall: Das GitHub-Repo ist weg, der Fetch soll das sagen —
        # nicht pauschal "Login fehlt".
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
            git(repo, "remote", "add", "github", str(root / "auf-github-geloescht.git"))
            st = collect_status(repo, root, DEFAULT_CONFIG, fetch=True)
        # In der Repo-Zeile nur das Stichwort — sonst verdrängt der Satz die Badges.
        self.assertEqual(st.error, "github: repository gone")
        self.assertNotIn("login", st.error.lower())
        # Ganzer Satz und Gits Wortlaut stehen auf der Info-Seite.
        self.assertIn("no repository", st.error_long)
        info = "\n".join(repo_info_lines(st, DEFAULT_CONFIG))
        self.assertIn("no repository", info)
        self.assertTrue(st.error_detail)
        self.assertIn("Git said", info)

    def test_deleted_repo_and_broken_network_do_not_look_alike(self):
        gone = remote_check_message("github", "gone", 0, "", 30)
        offline = remote_check_message("github", "dns", 0, "", 30)
        down = remote_check_message("github", "unreachable", 0, "", 30)
        self.assertNotEqual(gone, offline)
        self.assertNotEqual(gone, down)
        self.assertNotEqual(offline, down)

    def test_check_reports_existing_and_missing_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            origin = root / "origin.git"
            git(root, "init", "-q", "--bare", str(origin))
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            (repo / "a").write_text("a")
            git(repo, "add", "a")
            git(repo, "commit", "-qm", "base")
            git(repo, "remote", "add", "origin", str(origin))
            git(repo, "push", "-q", "origin", "HEAD")
            git(repo, "remote", "add", "weg", str(root / "gibt-es-nicht.git"))

            self.assertEqual(check_remote(repo, "origin", 10)[:2], ("ok", 1))
            outcome, _, detail = check_remote(repo, "weg", 10)
        self.assertEqual(outcome, "gone", detail)

    def test_check_of_empty_repository_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            empty = root / "empty.git"
            git(root, "init", "-q", "--bare", str(empty))
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "remote", "add", "leer", str(empty))
            self.assertEqual(check_remote(repo, "leer", 10)[0], "empty")

    def test_check_never_starts_an_unknown_remote_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            marker = root / "helper-ran"
            binary = root / "git-remote-evil"
            binary.write_text(
                "#!/bin/sh\nprintf ran > \"$GMF_HELPER_MARKER\"\nexit 1\n")
            binary.chmod(0o755)
            git(repo, "remote", "add", "origin", "evil://example/repo")
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       GMF_HELPER_MARKER=str(marker))

            with mock.patch.dict(os.environ, env, clear=True):
                outcome = check_remote(repo, "origin", 10)[0]

            self.assertEqual(outcome, "unsafe_url")
            self.assertFalse(marker.exists())


class RemoteAndCommandLogTests(unittest.TestCase):
    """Remote-Schutz, Info-Ansicht und Befehlsprotokoll gegen echte Repos."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "a.md").write_text("hallo\n")
        git(self.repo, "add", "a.md")
        git(self.repo, "commit", "-qm", "erster Commit")
        self.origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", str(self.origin))
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-q", "-u", "origin", "HEAD")
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)

    def tearDown(self):
        self.tmp.cleanup()


    def test_fetch_blocks_refspecs_that_target_local_branches(self):
        git(self.repo, "branch", "victim")
        before = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "refs/heads/victim"],
            check=True, capture_output=True, text=True).stdout.strip()
        git(self.repo, "config", "--unset-all", "remote.origin.fetch")
        git(self.repo, "config", "--add", "remote.origin.fetch",
            "+refs/heads/*:refs/heads/*")

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        remote = next(item for item in status.remotes if item.name == "origin")
        self.assertEqual(remote.fetch_outcome, "unsafe_refspec")
        after = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "refs/heads/victim"],
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(after, before)
        self.assertFalse(any("git fetch" in line for line in gmf_module.COMMAND_LOG))

    def test_fetch_requires_a_tracking_destination_not_only_fetch_head(self):
        git(self.repo, "config", "--unset-all", "remote.origin.fetch")
        git(self.repo, "config", "--add", "remote.origin.fetch",
            "refs/heads/main")

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        remote = next(item for item in status.remotes if item.name == "origin")
        self.assertEqual(remote.fetch_outcome, "unsafe_refspec")
        self.assertFalse(any("git fetch" in line for line in gmf_module.COMMAND_LOG))

    def test_fetch_rejects_mismatched_refspec_wildcards(self):
        for refspec in (
                "refs/heads/main:refs/remotes/origin/*",
                "refs/heads/*:refs/remotes/origin/main"):
            with self.subTest(refspec=refspec):
                remote = gmf_module.RemoteConfig(
                    "origin", [str(self.origin)], [str(self.origin)], [], [],
                    settings=[("fetch", refspec)])
                self.assertFalse(gmf_module.fetch_refspecs_safe(remote))
                self.assertFalse(
                    gmf_module.fetch_maps_branch_exactly(remote, "main"))

    def test_fetch_rejects_invalid_ref_components(self):
        for refspec in (
                "+refs/heads/foo..bar:refs/remotes/origin/main",
                "+refs/heads/.hidden:refs/remotes/origin/main",
                "+refs/heads/topic.lock:refs/remotes/origin/main"):
            with self.subTest(refspec=refspec):
                remote = gmf_module.RemoteConfig(
                    "origin", [str(self.origin)], [str(self.origin)], [], [],
                    settings=[("fetch", refspec)])
                self.assertFalse(gmf_module.fetch_refspecs_safe(remote))

    def test_fetch_never_places_a_secret_remote_url_in_argv(self):
        secret_url = ("https://" + "user:" + "credential@" +
                      "example.invalid/repo.git?access=query")
        remote = gmf_module.RemoteConfig(
            "origin", [secret_url], [secret_url], [], [], settings=[
                ("fetch", "+refs/heads/*:refs/remotes/origin/*"),
            ])
        self.assertIsNone(gmf_module.approved_fetch_args(
            remote, "main", "a" * 40))

    def test_fetch_reports_a_secret_remote_url_as_unsafe(self):
        secret_url = ("https://" + "user:credential@" +
                      "example.invalid/repo.git")
        git(self.repo, "remote", "set-url", "origin", secret_url)

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        remote = next(item for item in status.remotes if item.name == "origin")
        self.assertEqual(remote.fetch_outcome, "unsafe_url")
        self.assertNotIn("credential", status.error_long)

        class Screen:
            def getmaxyx(self): return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [status]
        self.assertIsNone(ui._fetch_remote(status, "origin"))
        self.assertIn("blocked", ui.message.lower())
        self.assertNotIn("credential", ui.message)

    def test_tracking_update_timeout_is_reported_as_unknown_fetch_outcome(self):
        config = gmf_module.read_remote_configs(
            self.repo, DEFAULT_CONFIG)["origin"]
        branch = git_output(self.repo, "symbolic-ref", "--short", "HEAD")
        original = gmf_module.run_git_logged

        def timeout_update(repo, *args, **kwargs):
            if "update-ref" in args:
                raise subprocess.TimeoutExpired(
                    ["git", "-C", str(repo), "update-ref"], 1)
            return original(repo, *args, **kwargs)

        with mock.patch.object(
                gmf_module, "run_git_logged", side_effect=timeout_update), \
                self.assertRaises(subprocess.TimeoutExpired) as caught:
            gmf_module.fetch_remote_safely(self.repo, config, branch, 10)

        self.assertEqual(caught.exception.cmd[-1], "fetch")
        self.assertIn("unknown", gmf_module.timeout_message(
            caught.exception).lower())

    def test_tracking_update_oserror_is_reported_as_unknown_fetch_outcome(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        original = gmf_module.run_git_logged

        def fail_update(repo, *args, **kwargs):
            if "update-ref" in args:
                raise OSError("cannot start update-ref")
            return original(repo, *args, **kwargs)

        class Screen:
            def getmaxyx(self): return (30, 100)
            def addstr(self, *a): pass
            def refresh(self): pass

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with mock.patch.object(
                gmf_module, "run_git_logged", side_effect=fail_update), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            result = ui._fetch_remote(st, "origin")

        self.assertIsNone(result)
        self.assertEqual(
            ui.message, gmf_module.t("fetch_outcome_unknown", r="origin"))
        self.assertIsNot(ui.statuses[0], st)

    def test_one_invalid_remote_does_not_hide_or_block_the_safe_remote(self):
        git(self.repo, "remote", "add", "broken",
            "https://example.invalid:not-a-port/repo.git")

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        origin = next(item for item in status.remotes if item.name == "origin")
        broken = next(item for item in status.remotes if item.name == "broken")
        self.assertFalse(origin.fetch_failed)
        self.assertTrue(broken.fetch_failed)
        self.assertEqual(broken.fetch_outcome, "unsafe_url")

    def test_one_fetch_oserror_does_not_skip_the_next_remote(self):
        branch = git_output(self.repo, "symbolic-ref", "--short", "HEAD")
        before = git_output(self.repo, "rev-parse", "HEAD")
        tree = git_output(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = git_output(
            self.repo, "commit-tree", tree, "-p", before, "-m", "remote tip")
        git(self.repo, "push", "-q", str(self.origin),
            f"{remote_tip}:refs/heads/{branch}")
        backup = self.root / "backup.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.origin), str(backup)],
            check=True, capture_output=True, text=True)
        git(self.repo, "remote", "add", "backup", str(backup))
        original = gmf_module.fetch_remote_safely
        fetched_names = []

        def fetch_then_fail(repo, remote, current_branch, timeout):
            result = original(repo, remote, current_branch, timeout)
            fetched_names.append(remote.name)
            if remote.name == "origin":
                self.assertEqual(result.returncode, 0, result.stderr)
                raise OSError("lost fetch result")
            return result

        with mock.patch.object(
                gmf_module, "fetch_remote_safely", side_effect=fetch_then_fail):
            status = collect_status(
                self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        self.assertEqual(set(fetched_names), {"origin", "backup"})
        remotes = {remote.name: remote for remote in status.remotes}
        self.assertEqual(
            remotes["origin"].fetch_outcome, "outcome_unknown")
        self.assertIn("may already have changed",
                      remotes["origin"].fetch_error_long)
        self.assertTrue(remotes["origin"].fetch_failed)
        self.assertFalse(remotes["backup"].fetch_failed)
        self.assertEqual(
            git_output(self.repo, "rev-parse",
                       f"refs/remotes/origin/{branch}"), remote_tip)
        payload = status_dict(status)
        origin_payload = next(
            remote for remote in payload["remotes"]
            if remote["name"] == "origin")
        self.assertEqual(
            origin_payload["fetch_outcome"], "outcome_unknown")
        self.assertIn("origin", [item.name for item in status.remotes])

    def test_invalid_push_url_does_not_block_a_safe_fetch(self):
        branch = git_output(self.repo, "symbolic-ref", "--short", "HEAD")
        head = git_output(self.repo, "rev-parse", "HEAD")
        tree = git_output(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", head],
            input="remote tip\n", capture_output=True, text=True,
            check=True).stdout.strip()
        git(self.repo, "push", "-q", str(self.origin),
            f"{remote_tip}:refs/heads/{branch}")
        git(self.repo, "remote", "set-url", "--push", "origin",
            "https://example.invalid:not-a-port/repo.git")

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        remote = next(item for item in status.remotes if item.name == "origin")

        self.assertFalse(remote.fetch_failed)
        self.assertTrue(remote.fetch_url_safe)
        self.assertFalse(remote.push_url_safe)
        self.assertEqual(
            git_output(self.repo, "rev-parse", f"refs/remotes/origin/{branch}"),
            remote_tip)
        self.assertEqual(
            inspect_transfer(self.repo, "origin", branch, "push").reason,
            "remote-unsafe")

    def test_url_execution_safety_is_serialized_without_the_url(self):
        unsafe_url = ("https://" + "token@" + "example.invalid/repo.git")
        git(self.repo, "remote", "set-url", "origin", unsafe_url)

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        payload = status_dict(status)
        remote = next(item for item in payload["remotes"]
                      if item["name"] == "origin")

        self.assertFalse(remote["fetch_url_safe"])
        self.assertFalse(remote["push_url_safe"])
        self.assertNotIn("token", json.dumps(payload))

    def test_fetch_uses_the_approved_refspec_after_a_config_race(self):
        before = gmf_module.current_head(self.repo, 10)
        git(self.repo, "branch", "victim", before)
        (self.repo / "remote.txt").write_text("new\n")
        git(self.repo, "add", "remote.txt")
        git(self.repo, "commit", "-qm", "remote victim")
        git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/victim")
        approved_remote_tip = git_output(self.repo, "rev-parse", "HEAD")
        redirected = self.root / "redirected.git"
        git(self.root, "init", "-q", "--bare", str(redirected))
        original = gmf_module.run_git_logged
        raced = False

        def race(target_repo, *args, **kwargs):
            nonlocal raced
            if "fetch" in args and not raced:
                raced = True
                git(self.repo, "config", "--replace-all", "remote.origin.fetch",
                    "+refs/heads/*:refs/heads/*")
                git(self.repo, "remote", "set-url", "origin", str(redirected))
            return original(target_repo, *args, **kwargs)

        with mock.patch.object(gmf_module, "run_git_logged", side_effect=race):
            status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        self.assertTrue(raced)
        remote = next(item for item in status.remotes if item.name == "origin")
        self.assertTrue(remote.fetch_failed)
        self.assertEqual(remote.fetch_outcome, "changed")
        self.assertEqual(subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "refs/heads/victim"],
            check=True, capture_output=True, text=True).stdout.strip(), before)
        self.assertEqual(
            git_output(self.repo, "rev-parse", "refs/remotes/origin/victim"),
            approved_remote_tip)

    def test_fetch_cannot_rewrite_the_approved_url_a_second_time(self):
        original_parent = self.root / "configured"
        safe_parent = self.root / "approved"
        redirected_parent = self.root / "redirected"
        for parent in (original_parent, safe_parent, redirected_parent):
            parent.mkdir()
        safe = safe_parent / "repo.git"
        redirected = redirected_parent / "repo.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.origin), str(safe)],
            check=True)
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.origin), str(redirected)],
            check=True)
        branch = git_output(self.repo, "symbolic-ref", "--short", "HEAD")
        head = git_output(self.repo, "rev-parse", "HEAD")
        tree = git_output(self.repo, "rev-parse", "HEAD^{tree}")
        approved_oid = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", head],
            input="approved remote tip\n", check=True,
            capture_output=True, text=True).stdout.strip()
        git(self.repo, "push", "-q", str(safe),
            f"{approved_oid}:refs/heads/{branch}")
        git(self.repo, "remote", "set-url", "origin",
            str(original_parent / "repo.git"))
        git(self.repo, "config", f"url.{safe_parent}/.insteadOf",
            str(original_parent) + "/")
        git(self.repo, "config", f"url.{redirected_parent}/.insteadOf",
            str(safe_parent) + "/")
        config = gmf_module.read_remote_configs(
            self.repo, DEFAULT_CONFIG)["origin"]
        self.assertEqual(config.fetch_urls, [str(safe)])

        status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        remote = next(item for item in status.remotes if item.name == "origin")
        self.assertFalse(remote.fetch_failed)
        self.assertEqual(
            git_output(self.repo, "rev-parse",
                       f"refs/remotes/origin/{branch}"),
            git_output(safe, "rev-parse", f"refs/heads/{branch}"))
        self.assertNotEqual(
            git_output(self.repo, "rev-parse", f"refs/remotes/origin/{branch}"),
            git_output(redirected, "rev-parse", f"refs/heads/{branch}"))




    def test_command_log_shows_the_real_git_syntax(self):
        gmf_module.run_git_logged(
            self.repo, "config", "gmf.command-log-test", "value", timeout=10)
        self.assertEqual(len(gmf_module.COMMAND_LOG), 1)
        entry = gmf_module.COMMAND_LOG[0]
        self.assertIn("git config gmf.command-log-test value", entry)
        self.assertTrue(entry.startswith("✔"), entry)
        self.assertIn("repo", entry)

    def test_failed_command_is_logged_with_exit_code(self):
        gmf_module.run_git_logged(self.repo, "remote", "remove", "gibtsnicht", timeout=10)
        entry = gmf_module.COMMAND_LOG[0]
        self.assertTrue(entry.startswith("✘"), entry)
        self.assertIn("Exit", entry)

    def test_known_fetch_failure_survives_a_local_refresh(self):
        # Nach einer Aktion (oder einem abgebrochenen Dialog) wird nur lokal neu
        # gelesen. Der tote Remote darf dabei nicht scheinbar heil werden.
        git(self.repo, "remote", "add", "github", str(self.root / "geloescht.git"))
        broken = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        self.assertTrue(broken.error)

        class Screen:
            def getmaxyx(self): return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [broken]
        fresh = ui.refresh_one(broken)
        self.assertEqual(fresh.error, broken.error)
        self.assertEqual(fresh.error_detail, broken.error_detail)
        self.assertTrue(any(r.fetch_failed for r in fresh.remotes if r.name == "github"))
        self.assertFalse(fresh.clean_and_synced)

    def test_successful_refetch_clears_the_known_failure(self):
        """Nach einem bewiesen erfolgreichen Fetch darf der alte Fehler nicht
        bis zum kompletten Reload weiterleuchten."""
        git(self.repo, "remote", "add", "github", str(self.root / "geloescht.git"))
        broken = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        self.assertTrue(broken.error)
        self.assertTrue(broken.fetch_error)
        fresh = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        gmf_module.carry_fetch_failure(broken, fresh, refetched="github")
        self.assertEqual(fresh.error, "")
        self.assertFalse(any(r.fetch_failed for r in fresh.remotes))

    def test_fetch_failure_is_not_carried_to_a_new_url_with_the_same_name(self):
        git(self.repo, "remote", "add", "github", str(self.root / "missing.git"))
        broken = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        self.assertTrue(any(r.fetch_failed for r in broken.remotes
                            if r.name == "github"))
        git(self.repo, "remote", "set-url", "github", str(self.origin))
        fresh = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        gmf_module.carry_fetch_failure(broken, fresh)

        self.assertFalse(any(r.fetch_failed for r in fresh.remotes
                             if r.name == "github"))
        self.assertEqual(fresh.error, "")

    def test_local_read_error_is_not_carried_over(self):
        """Nur Fetch-Fehler überleben einen lokalen Refresh: ein reparierter
        Index-/Lesefehler muss nach dem Neu-Einlesen verschwinden."""
        old = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        old.error = "cannot read Git index: kaputt"
        old.remote_state = "error"          # fetch_error bleibt False
        fresh = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        gmf_module.carry_fetch_failure(old, fresh)
        self.assertEqual(fresh.error, "")
        self.assertEqual(fresh.remote_state, "ok")

    def test_interactive_fetch_appears_in_the_command_log(self):
        # R bzw. --fetch ist eine bewusst ausgelöste Aktion und gehört ins
        # Protokoll — sonst erklärt das Protokoll genau die eine Netz-Aktion nicht.
        collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        fetch_logs = [line for line in gmf_module.COMMAND_LOG if " fetch " in line]
        self.assertEqual(len(fetch_logs), 1, gmf_module.COMMAND_LOG)
        logged = fetch_logs[0].split(": ", 1)[1]
        argv = shlex.split(logged)
        self.assertIn("fetch", argv)
        self.assertIn("--no-tags", argv)
        self.assertIn("--no-prune-tags", argv)
        self.assertIn("--no-recurse-submodules", argv)
        self.assertIn("--refmap=", argv)
        self.assertIn("--no-write-fetch-head", argv)

    def test_nonzero_push_exit_is_reported_as_unknown_not_unpublished(self):
        remote = gmf_module.RemoteStatus(name="origin", branch_exists=True)
        st = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remote="origin",
            remotes=[remote])
        check = gmf_module.TransferCheck(
            "ready", ahead=1, remote_ref="refs/remotes/origin/main",
            branch="main", head_oid="a" * 40, target_oid="b" * 40,
            transfer_url=str(self.origin))

        class Screen:
            def getmaxyx(self): return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        failed = subprocess.CompletedProcess(["git", "push"], 1, "", "rejected")
        with mock.patch.object(ui, "_fetch_remote", return_value=st), \
                mock.patch.object(gmf_module, "inspect_transfer", return_value=check), \
                mock.patch.object(ui, "confirm", return_value=True), \
                mock.patch.object(gmf_module, "run_git_logged", return_value=failed), \
                mock.patch.object(gmf_module, "credentials_missing", return_value=False), \
                mock.patch.object(ui, "refresh_one", return_value=st):
            ui.action_sync_push()

        self.assertEqual(ui.message, gmf_module.t("push_outcome_unknown", code=1))

    def test_oserror_after_real_push_is_reported_as_unknown(self):
        (self.repo / "a.md").write_text("pushed\n")
        git(self.repo, "commit", "-qam", "outgoing")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        original = gmf_module.run_git

        def push_then_lose_result(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if "push" in args:
                self.assertEqual(result.returncode, 0, result.stderr)
                raise OSError("lost push result")
            return result

        class Screen:
            def getmaxyx(self): return (30, 100)
            def addstr(self, *a): pass
            def refresh(self): pass

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with mock.patch.object(ui, "confirm", return_value=True), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch.object(gmf_module, "run_git",
                                  side_effect=push_then_lose_result):
            ui.action_sync_push()

        self.assertEqual(ui.message, gmf_module.t("push_io_unknown"))
        self.assertEqual(
            git_output(self.origin, "rev-parse", "refs/heads/master"),
            git_output(self.repo, "rev-parse", "HEAD"))
        self.assertTrue(any(
            line.startswith("…") and " push " in line
            for line in gmf_module.COMMAND_LOG))

    def test_cancelled_private_push_is_translated_in_both_languages(self):
        remote = gmf_module.RemoteStatus(name="origin", branch_exists=True)
        st = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remote="origin",
            remotes=[remote])
        check = gmf_module.TransferCheck(
            "ready", ahead=1, branch="main", head_oid="a" * 40,
            target_oid="b" * 40, transfer_url=str(self.origin))

        class Screen:
            def getmaxyx(self): return (30, 100)

        for lang, expected in (("en", "Cancelled."), ("de", "Abgebrochen.")):
            with self.subTest(lang=lang):
                ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
                ui.statuses = [st]
                with mock.patch.object(gmf_module, "UI_LANG", lang), \
                        mock.patch.object(ui, "_fetch_remote", return_value=st), \
                        mock.patch.object(gmf_module, "inspect_transfer",
                                          return_value=check), \
                        mock.patch.object(ui, "confirm", return_value=False):
                    ui.action_sync_push()
                self.assertEqual(ui.message, expected)

    def test_private_push_blocks_a_branch_change_during_the_first_fetch(self):
        remote = gmf_module.RemoteStatus(name="origin", branch_exists=True)
        shown = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remote="origin",
            remotes=[remote])
        fresh = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="other", remote="origin",
            remotes=[remote])

        class Screen:
            def getmaxyx(self): return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [shown]
        with mock.patch.object(ui, "_fetch_remote", return_value=fresh), \
                mock.patch.object(gmf_module, "inspect_transfer") as inspect, \
                mock.patch.object(ui, "confirm") as confirm:
            ui.action_sync_push()

        self.assertEqual(ui.message, gmf_module.t("transfer_changed"))
        inspect.assert_not_called()
        confirm.assert_not_called()

    def test_private_push_blocks_a_remote_change_since_the_list_scan(self):
        shown_remote = gmf_module.RemoteStatus(
            name="origin", branch_exists=True,
            fetch_fingerprints=["target-a"], push_fingerprints=["target-a"],
            fetch_refspecs_safe=True, branch_mapping_safe=True)
        fresh_remote = gmf_module.RemoteStatus(
            name="origin", branch_exists=True,
            fetch_fingerprints=["target-b"], push_fingerprints=["target-b"],
            fetch_refspecs_safe=True, branch_mapping_safe=True)
        shown = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remote="origin",
            remotes=[shown_remote])
        fresh = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remote="origin",
            remotes=[fresh_remote])

        class Screen:
            def getmaxyx(self): return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [shown]
        with mock.patch.object(ui, "_fetch_remote", return_value=fresh), \
                mock.patch.object(gmf_module, "inspect_transfer") as inspect:
            ui.action_sync_push()

        self.assertEqual(ui.message, gmf_module.t("transfer_changed"))
        inspect.assert_not_called()

    def test_public_push_blocks_a_remote_change_since_the_list_scan(self):
        shown_remote = gmf_module.RemoteStatus(
            name="github", public=True, branch_exists=True,
            fetch_fingerprints=["target-a"], push_fingerprints=["target-a"],
            fetch_refspecs_safe=True, branch_mapping_safe=True)
        fresh_remote = gmf_module.RemoteStatus(
            name="github", public=True, branch_exists=True,
            fetch_fingerprints=["target-b"], push_fingerprints=["target-b"],
            fetch_refspecs_safe=True, branch_mapping_safe=True)
        shown = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remotes=[shown_remote])
        fresh = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remotes=[fresh_remote])

        class Screen:
            def getmaxyx(self): return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [shown]
        with mock.patch.object(ui, "_fetch_remote", return_value=fresh), \
                mock.patch.object(gmf_module, "inspect_transfer") as inspect:
            ui.action_github_push()

        self.assertEqual(ui.message, gmf_module.t("github_changed"))
        inspect.assert_not_called()

    def _public_push_fixture(self):
        remote = gmf_module.RemoteStatus(
            name="github", public=True, branch_exists=True,
            fetch_fingerprints=["target-a"], push_fingerprints=["target-a"],
            fetch_refspecs_safe=True, branch_mapping_safe=True)
        st = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remotes=[remote])
        check = gmf_module.TransferCheck(
            "ready", ahead=1, remote_ref="refs/remotes/github/main",
            branch="main", head_oid="a" * 40, target_oid="b" * 40,
            transfer_url="https://github.com/example/repo.git",
            commits=["abc1234 add feature"], files=["src/app.py"])

        class Screen:
            def getmaxyx(self): return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        return ui, st, check

    def test_public_push_asks_inside_the_preview_and_pushes_on_yes(self):
        ui, st, check = self._public_push_fixture()
        asked = {}

        def preview(title, lines, question):
            asked.update(title=title, lines=lines, question=question)
            return True

        pushed = subprocess.CompletedProcess(["git", "push"], 0, "", "")
        with mock.patch.object(ui, "_fetch_remote", return_value=st), \
                mock.patch.object(gmf_module, "inspect_transfer", return_value=check), \
                mock.patch.object(ui, "confirm_in_pager", side_effect=preview), \
                mock.patch.object(ui, "prompt_line") as typed, \
                mock.patch.object(gmf_module, "run_git_logged",
                                  return_value=pushed) as run, \
                mock.patch.object(gmf_module, "update_tracking_after_push",
                                  return_value=True), \
                mock.patch.object(ui, "refresh_one", return_value=st):
            ui.action_github_push()

        self.assertEqual(ui.message, gmf_module.t("github_pushed", r="github"))
        # Kein getippter Satz mehr: Die Rückfrage steht in der Vorschau selbst.
        typed.assert_not_called()
        self.assertEqual(asked["title"], gmf_module.t(
            "github_preview", rel="repo", r="github", b="main"))
        self.assertEqual(asked["question"],
                         gmf_module.t("github_confirm", r="github"))
        self.assertIn("abc1234 add feature", asked["lines"])
        self.assertIn("src/app.py", asked["lines"])
        run.assert_called_once()
        self.assertIn("push", run.call_args.args)

    def test_public_push_is_cancelled_when_the_preview_question_is_declined(self):
        ui, st, check = self._public_push_fixture()
        with mock.patch.object(ui, "_fetch_remote", return_value=st), \
                mock.patch.object(gmf_module, "inspect_transfer", return_value=check), \
                mock.patch.object(ui, "confirm_in_pager", return_value=False), \
                mock.patch.object(gmf_module, "run_git_logged") as run, \
                mock.patch.object(gmf_module, "log_cancelled") as cancelled:
            ui.action_github_push()

        self.assertEqual(ui.message, gmf_module.t("github_cancelled"))
        run.assert_not_called()
        cancelled.assert_called_once()

    def test_successful_push_keeps_success_visible_when_tracking_update_times_out(self):
        remote = gmf_module.RemoteStatus(name="origin", branch_exists=True)
        st = gmf_module.RepoStatus(
            path=self.repo, rel="repo", branch="main", remote="origin",
            remotes=[remote])
        config = gmf_module.read_remote_configs(
            self.repo, DEFAULT_CONFIG)["origin"]
        check = gmf_module.TransferCheck(
            "ready", ahead=1, remote_ref="refs/remotes/origin/main",
            branch="main", head_oid="a" * 40, target_oid="b" * 40,
            transfer_url=str(self.origin), remote_name="origin",
            remote_config_signature=(tuple(config.fetch_urls),
                                     tuple(config.push_urls),
                                     tuple(config.settings)))

        class Screen:
            def getmaxyx(self): return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        pushed = subprocess.CompletedProcess(["git", "push"], 0, "", "")
        timeout = subprocess.TimeoutExpired(["git", "update-ref"], 10)
        with mock.patch.object(ui, "_fetch_remote", return_value=st), \
                mock.patch.object(gmf_module, "inspect_transfer", return_value=check), \
                mock.patch.object(ui, "confirm", return_value=True), \
                mock.patch.object(gmf_module, "run_git_logged",
                                  side_effect=[pushed, timeout]), \
                mock.patch.object(ui, "refresh_one", return_value=st):
            ui.action_sync_push()

        self.assertEqual(ui.message, gmf_module.t("push_tracking_changed"))

    def test_repo_line_with_error_still_shows_branch_and_remotes(self):
        """Der Fehlertext darf Branch und Remote-Badges nicht verdrängen —
        gerade dann muss sichtbar sein, WELCHES Remote das ✘ trägt."""
        git(self.repo, "remote", "add", "github", str(self.root / "geloescht.git"))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        self.assertTrue(st.error)

        class Screen:
            def __init__(self):
                self.texts = []

            def getmaxyx(self):
                return (30, 200)

            def addstr(self, y, x, text, attr=0):
                self.texts.append(text)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw_repo_line(0, st, False)
        drawn = " ".join(ui.scr.texts)
        self.assertIn(st.error, drawn)
        self.assertIn("[" + st.branch + "]", drawn)
        self.assertIn("github", drawn)
        self.assertIn("origin", drawn)

    def test_action_timeout_rereads_the_repo(self):
        """Eine Aktion kann vor dem hängenden Schritt schon mutiert haben —
        nach dem Timeout muss die TUI den echten Zustand neu einlesen."""
        class Screen:
            def getmaxyx(self):
                return (30, 100)

        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        exc = subprocess.TimeoutExpired(["git", "-C", str(self.repo), "merge"], 7)
        with mock.patch("gitmaster_flash.curses.flushinp"):
            ui.handle_action_timeout(exc)
        self.assertIsNot(ui.statuses[0], st)
        self.assertIn("merge", ui.message)

    def test_info_view_paging_survives_the_redraw(self):
        """Der Footer verspricht freie Seitennavigation: ein PgDn-Sprung darf im
        nächsten Zeichendurchlauf nicht auf den gewählten Block zurückschnappen."""
        for i in range(8):
            git(self.repo, "branch", f"zweig-{i}")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        view = build_info_view(st, DEFAULT_CONFIG)
        body_h = 30 - 4
        self.assertGreater(len(view.lines), body_h)   # Paging hat etwas zu tun
        expected_top = min(len(view.lines) - body_h, body_h)

        class Screen:
            def __init__(self):
                self.keys = iter([curses.KEY_NPAGE, ord("q")])
                self.frames = []

            def erase(self):
                self.frames.append([])

            def getmaxyx(self):
                return (30, 100)

            def addstr(self, y, x, text, attr=0):
                if self.frames:
                    self.frames[-1].append((y, text))

            def refresh(self): pass

            def getch(self):
                return next(self.keys)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_repo_info()
        last_frame = ui.scr.frames[-1]
        first_body_line = next(text for y, text in last_frame if y == 1)
        self.assertEqual(first_body_line, view.lines[expected_top])

    def test_repo_line_keeps_the_remote_badges_visible(self):
        # Die Fehlermeldung teilt sich die Zeile mit den Badges: bleibt sie kurz,
        # ist das ✘ am Remote noch zu sehen.
        git(self.repo, "remote", "add", "github", str(self.root / "geloescht.git"))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)
        self.assertLess(len(st.error), 40, st.error)
        self.assertIn("✘", "".join(r.badge() for r in st.remotes))

    def test_log_quotes_arguments_so_the_line_can_be_pasted(self):
        self.assertEqual(
            gmf_module.format_git_command(("commit", "-m", "zwei Wörter")),
            "git commit -m 'zwei Wörter'")

    def test_log_preserves_control_characters_as_executable_zsh_escapes(self):
        name = "line\ncontrol\x1b\u0085.txt"
        (self.repo / name).write_text("content\n")
        command = gmf_module.format_git_command(("add", "--", name))

        self.assertNotIn("\n", command)
        self.assertNotIn("\x1b", command)
        added = subprocess.run(
            ["zsh", "-fc", command], cwd=self.repo,
            capture_output=True, text=True)
        self.assertEqual(added.returncode, 0, added.stderr)
        cached = subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--cached", "--name-only",
             "-z"], capture_output=True, text=True, check=True)
        self.assertIn(name, cached.stdout.split("\0"))

    def test_log_keeps_only_the_newest_entries(self):
        for i in range(gmf_module.COMMAND_LOG_MAX + 5):
            gmf_module.log_command(self.repo, ("status", str(i)), 0)
        self.assertEqual(len(gmf_module.COMMAND_LOG), gmf_module.COMMAND_LOG_MAX)
        self.assertIn(str(gmf_module.COMMAND_LOG_MAX + 4), gmf_module.COMMAND_LOG[-1])

    def _info_screen(self, keys):
        """Fake-Screen fuer die Info-Ansicht; liefert die gedrueckten Tasten der Reihe."""
        class Screen:
            def __init__(self):
                self.keys = iter(keys)
                self.written = []

            def erase(self):
                pass

            def getmaxyx(self):
                return (30, 100)

            def addstr(self, y, x, text, *rest):
                self.written.append(text)

            def refresh(self):
                pass

            def getch(self):
                return next(self.keys)

        return Screen()

    def test_tab_selects_the_next_remote_before_acting_on_it(self):
        git(self.repo, "remote", "add", "github", str(self.root / "geloescht.git"))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        ui = TUI(self._info_screen([9, ord("t"), ord("q")]), self.root,
                 DEFAULT_CONFIG, None)
        ui.statuses = [st]
        # origin (Sync) steht an erster Stelle, github zuletzt — Tab muss weiterspringen.
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_repo_info()
        self.assertIn("github", ui.message)
        self.assertIn("no repository", ui.message)

    def test_info_view_has_no_destructive_x_action(self):
        git(self.repo, "branch", "keep-me")
        before = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        ui = TUI(self._info_screen([ord("x"), ord("q")]), self.root,
                 DEFAULT_CONFIG, None)
        ui.statuses = [before]

        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_repo_info()

        after = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual([remote.name for remote in after.remotes], ["origin"])
        self.assertIn("keep-me", {
            branch.name for branch in read_branches(self.repo, DEFAULT_CONFIG)})
        self.assertFalse(any(" remote remove " in line or " branch -d " in line
                             for line in gmf_module.COMMAND_LOG))

    def test_info_view_maps_remote_blocks_to_lines(self):
        git(self.repo, "remote", "add", "github", "https://github.com/example/demo.git")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        view = build_info_view(st, DEFAULT_CONFIG)
        self.assertEqual(view.remote_names, ["origin", "github"])
        for name, first, last in view.remote_blocks:
            self.assertIn(name, view.lines[first])
            self.assertLessEqual(first, last)
            # Der Block endet vor dem naechsten Remote-Kopf.
            self.assertTrue(all(not line.startswith("  " + other)
                                for other in view.remote_names if other != name
                                for line in view.lines[first:last + 1]))
        # Der Textmodus bleibt unveraendert nutzbar (CLI/Tests).
        self.assertEqual(repo_info_lines(st, DEFAULT_CONFIG), view.lines)


class BranchAndDiffTests(unittest.TestCase):
    """Branch-Übersicht und Datei-Diffs — beides rein lesend gegen echte Repos."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "a.md").write_text("eins\n")
        git(self.repo, "add", "a.md")
        git(self.repo, "commit", "-qm", "erster Commit")
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)

    def tearDown(self):
        self.tmp.cleanup()

    def test_branches_report_head_merge_state_and_upstream(self):
        git(self.repo, "branch", "fertig")               # gemergt (zeigt auf HEAD)
        git(self.repo, "checkout", "-q", "-b", "offen")
        (self.repo / "b.md").write_text("zwei\n")
        git(self.repo, "add", "b.md")
        git(self.repo, "commit", "-qm", "zweiter Commit")
        git(self.repo, "checkout", "-q", "main")

        branches = {b.name: b for b in read_branches(self.repo, DEFAULT_CONFIG)}
        self.assertEqual(set(branches), {"main", "fertig", "offen"})
        self.assertTrue(branches["main"].is_head)
        self.assertTrue(branches["fertig"].merged)
        self.assertFalse(branches["offen"].merged)
        self.assertEqual(branches["main"].subject, "erster Commit")
        self.assertEqual(branches["main"].upstream, "")

    def test_branch_upstream_delta_is_read(self):
        origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", str(origin))
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "-u", "origin", "main")
        (self.repo / "a.md").write_text("zwei\n")
        git(self.repo, "commit", "-qam", "lokaler Vorsprung")
        branch = next(b for b in read_branches(self.repo, DEFAULT_CONFIG)
                      if b.name == "main")
        self.assertEqual(branch.upstream, "origin/main")
        self.assertEqual((branch.ahead, branch.behind), (1, 0))

    def test_info_page_lists_branches_as_selectable_blocks(self):
        git(self.repo, "branch", "fertig")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        view = build_info_view(st, DEFAULT_CONFIG)
        kinds = {kind for kind, _, _, _ in view.blocks}
        names = [name for kind, name, _, _ in view.blocks if kind == "branch"]
        self.assertIn("branch", kinds)
        self.assertEqual(sorted(names), ["fertig", "main"])
        self.assertIn("Local branches:", view.lines)
        # Auch über Remotes und Branches hinweg steht alles in einer Spalte.
        details = [line for line in view.lines if line.startswith("    ")]
        columns = {len(line) - len(line.partition(":")[2].lstrip()) for line in details}
        self.assertEqual(len(columns), 1, details)

    def test_file_diff_covers_modified_deleted_and_untracked(self):
        (self.repo / "a.md").write_text("geändert\n")
        (self.repo / "neu.txt").write_text("frisch\n")
        (self.repo / "weg.md").write_text("x\n")
        git(self.repo, "add", "weg.md")
        git(self.repo, "commit", "-qm", "zweite Datei")
        (self.repo / "weg.md").unlink()

        ok, diff = file_diff(self.repo, "M", "a.md", 10)
        self.assertTrue(ok)
        self.assertIn("-eins", diff)
        self.assertIn("+geändert", diff)

        ok, diff = file_diff(self.repo, "U", "neu.txt", 10)
        self.assertTrue(ok, diff)
        self.assertIn("+frisch", diff)

        ok, diff = file_diff(self.repo, "D", "weg.md", 10)
        self.assertTrue(ok)
        self.assertIn("-x", diff)

    def test_file_diff_treats_a_magic_filename_literally(self):
        magic = ":(glob)*.txt"
        other = "other.txt"
        (self.repo / magic).write_text("magic base\n")
        (self.repo / other).write_text("other base\n")
        git(self.repo, "add", "--", magic, other)
        git(self.repo, "commit", "-qm", "magic names")
        (self.repo / magic).write_text("magic changed\n")
        (self.repo / other).write_text("other changed\n")

        ok, diff = file_diff(self.repo, "M", magic, 10)

        self.assertTrue(ok, diff)
        self.assertIn("magic changed", diff)
        self.assertNotIn("other changed", diff)

    def test_file_diff_never_runs_external_or_textconv_drivers(self):
        marker = self.root / "external-driver-ran"
        driver = self.root / "external-driver"
        driver.write_text(
            "#!/bin/sh\nprintf ran > " + shlex.quote(str(marker)) + "\n")
        driver.chmod(0o755)
        (self.repo / ".gitattributes").write_text("a.md diff=danger\n")
        git(self.repo, "add", ".gitattributes")
        git(self.repo, "commit", "-qm", "diff attributes")
        git(self.repo, "config", "diff.danger.textconv", str(driver))
        (self.repo / "a.md").write_text("geändert\n")

        with mock.patch.dict(
                os.environ, {"GIT_EXTERNAL_DIFF": str(driver)}):
            ok, diff = file_diff(self.repo, "M", "a.md", 10)

        self.assertTrue(ok, diff)
        self.assertIn("+geändert", diff)
        self.assertFalse(marker.exists())

    def test_unborn_diff_matches_the_worktree_content_that_will_be_committed(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "unborn"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.invalid")
            git(repo, "config", "user.name", "T")
            path = repo / "new.txt"
            path.write_text("staged version\n")
            git(repo, "add", "new.txt")
            path.write_text("current version\nlatest line\n")

            ok, preview = file_diff(repo, "M", "new.txt", 10)
            result = commit_selected(repo, ["new.txt"], "initial", 10)

            self.assertTrue(ok, preview)
            self.assertIn("+current version", preview)
            self.assertIn("+latest line", preview)
            self.assertNotIn("staged version", preview)
            self.assertEqual(result.returncode, 0, result.stderr)
            committed = subprocess.run(
                ["git", "-C", str(repo), "show", "HEAD:new.txt"], check=True,
                capture_output=True, text=True).stdout
            self.assertEqual(committed, "current version\nlatest line\n")

    def test_file_diff_ignores_a_replacement_for_head(self):
        (self.repo / "a.md").write_text("changed\n")
        git(self.repo, "add", "a.md")
        git(self.repo, "commit", "-qm", "replacement tree")
        replacement = git_output(self.repo, "rev-parse", "HEAD")
        git(self.repo, "reset", "--mixed", "-q", "HEAD^")
        real_head = git_output(self.repo, "rev-parse", "HEAD")
        git(self.repo, "replace", real_head, replacement)

        ok, preview = file_diff(self.repo, "M", "a.md", 10)
        result = commit_selected(self.repo, ["a.md"], "real change", 10)

        self.assertTrue(ok, preview)
        self.assertIn("+changed", preview)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git_output(self.repo, "show", "HEAD:a.md"), "changed")

    def test_untracked_directory_is_expanded_to_committable_files(self):
        """Der Hauptscan bindet die Freigabe an einzelne neue Dateien."""
        (self.repo / "neu-dir").mkdir()
        (self.repo / "neu-dir" / "datei.txt").write_text("inhalt\n")
        git(self.repo, "config", "status.showUntrackedFiles", "no")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertIn(ChangedFile("U", "neu-dir/datei.txt", "??"), st.files)
        self.assertEqual(status_dict(st)["untracked"], 1)
        result = commit_selected(
            self.repo, ["neu-dir/datei.txt"], "add nested file", 10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changes_view_shows_diff_for_the_selected_file(self):
        (self.repo / "a.md").write_text("geändert\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        shown = {}

        class Screen:
            def __init__(self):
                self.keys = iter([10, ord("q")])   # ⏎ Diff, dann zurück
            def erase(self): pass
            def getmaxyx(self): return (30, 100)
            def addstr(self, *a): pass
            def refresh(self): pass
            def getch(self): return next(self.keys)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch.object(TUI, "show_pager",
                                  side_effect=lambda title, lines: shown.update(
                                      title=title, lines=lines)):
            ui.action_file_changes()
        self.assertIn("a.md", shown["title"])
        self.assertTrue(any(line.startswith("+geändert") for line in shown["lines"]))

    def test_app_open_is_refused_over_ssh(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        cfg = {**DEFAULT_CONFIG, "apps": {"E": {"name": "Editor", "path": "/"}}}
        ui = TUI(None, self.root, cfg, None)
        ui.statuses = [st]
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "1.2.3.4 1 5.6.7.8 22"}), \
                mock.patch("gitmaster_flash.subprocess.run") as run:
            ui.action_open_app("E")
        run.assert_not_called()
        self.assertIn("SSH", ui.message)


class CompactViewTests(unittest.TestCase):
    """Kompakte Übersicht: Layout, Marken und Navigation — alles headless."""

    @staticmethod
    def _status(rel, **kw):
        kw.setdefault("remote_state", "ok")
        return RepoStatus(path=Path("/tmp") / rel, rel=rel, **kw)

    def test_marks_show_the_most_urgent_state(self):
        cases = [
            (self._status("a", error="kaputt"), "✘"),
            (self._status("b", conflicts=1), "⚠"),
            (self._status("c", modified=2), "●"),
            (self._status("d", stashes=["stash@{0} x"]), "⚑"),
            (self._status("e", ahead=2), "↑2"),
            (self._status("f", behind=1), "↓1"),
            (self._status("g", remote_state="no-remote"), "?"),
            (self._status("h"), "✔"),
        ]
        for st, expected in cases:
            with self.subTest(expected):
                self.assertEqual(gmf_module.compact_mark(st)[0], expected)
        # Ein toter Remote schlägt auch ohne st.error durch.
        st = self._status("i", remotes=[RemoteStatus(name="github", fetch_failed=True)])
        self.assertEqual(gmf_module.compact_mark(st)[0], "✘")

    def test_layout_keeps_three_columns_despite_one_long_name(self):
        rows, columns, width = gmf_module.compact_layout(24, 110, 20, 40)
        self.assertGreaterEqual(columns, 3)
        self.assertLessEqual(width * columns, 110)
        self.assertEqual(rows, math.ceil(24 / columns))
        # Schmales Fenster: lieber weniger Spalten als unlesbare Namen.
        _, narrow_columns, narrow_width = gmf_module.compact_layout(24, 40, 20, 40)
        self.assertGreaterEqual(narrow_width, gmf_module.COMPACT_MIN_WIDTH)
        self.assertLessEqual(narrow_columns, 3)

    def test_layout_fills_columns_like_ls(self):
        rows, columns, _ = gmf_module.compact_layout(9, 68, 10, 20)
        self.assertEqual((rows, columns), (3, 3))
        # Erste Spalte = erste drei Einträge (spaltenweise gefüllt).
        self.assertEqual(gmf_module.compact_position(0, rows), (0, 0))
        self.assertEqual(gmf_module.compact_position(2, rows), (2, 0))
        self.assertEqual(gmf_module.compact_position(3, rows), (0, 1))
        self.assertEqual(gmf_module.compact_position(8, rows), (2, 2))

    def test_scroll_hint_gets_its_own_reserved_row(self):
        # 40 Spalten, viele Repos: der Hinweis begann früher in der letzten
        # Rasterzeile bei Spalte 18 und überschrieb dort die zweite Repo-Spalte
        # (Beginn Spalte 17). Jetzt gibt das Raster eine Zeile an ihn ab.
        rows, columns, width, hint = gmf_module.compact_plan(60, 40, 10, 20)
        self.assertTrue(hint)
        self.assertLess(rows, 10)
        # Passt alles ins Fenster, wird keine Zeile reserviert.
        _, _, _, no_hint = gmf_module.compact_plan(6, 110, 10, 20)
        self.assertFalse(no_hint)

    def test_scroll_hint_does_not_overwrite_a_repo_cell(self):
        ui = self._ui(60, width=40, height=20)
        ui.view_mode = "compact"
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        hint_rows = {y for y, _, text in ui.scr.lines
                     if "columns" in text or "Spalten" in text}
        name_rows = {y for y, _, text in ui.scr.lines if "repo-" in text}
        self.assertTrue(hint_rows)
        self.assertFalse(hint_rows & name_rows, (hint_rows, name_rows))

    def test_long_names_are_shortened_visibly(self):
        self.assertEqual(gmf_module.ellipsize("kurz", 10), "kurz")
        shortened = gmf_module.ellipsize("firefox-tabs-save-and-restore", 12)
        self.assertEqual(cell_width(shortened), 12)
        self.assertTrue(shortened.endswith("…"))

    def test_counted_and_symbol_marks_align_repository_names(self):
        marks = ("●", "⚑", "✔", "⚠", "↑2", "↓3", "⇅", "?")
        prefixes = [pad_cells(mark, gmf_module.COMPACT_MARK_WIDTH) + " "
                    for mark in marks]
        # macOS curses führt diese Statuszeichen einzellig. Jede Marke muss daher
        # im ausgegebenen Text exakt dieselbe Namensspalte ergeben.
        self.assertEqual({len(prefix) for prefix in prefixes},
                         {gmf_module.COMPACT_MARK_WIDTH + 1})

    def _ui(self, count, keys=(), width=100, height=30):
        class Screen:
            def __init__(self):
                self.keys = iter(list(keys) + [ord("q")])
                self.lines = []

            def erase(self): pass
            def clear(self): self.lines.clear()
            def getmaxyx(self): return (height, width)
            def addstr(self, y, x, text, *rest): self.lines.append((y, x, text))
            def refresh(self): pass
            def getch(self): return next(self.keys)

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.statuses = [self._status(f"repo-{i:02d}") for i in range(count)]
        return ui

    def test_view_switch_keeps_the_selected_repository(self):
        ui = self._ui(24)
        ui.view_mode = "compact"
        ui.selected = 17
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.scr.keys = iter([ord("m"), ord("q")])
            ui.run()
        self.assertEqual(ui.view_mode, "detail")
        self.assertEqual(ui.selected, 17)
        self.assertEqual(ui.current().rel, "repo-17")

    def test_left_right_move_by_one_column(self):
        ui = self._ui(24)
        ui.view_mode = "compact"
        ui.selected = 0
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.scr.keys = iter([curses.KEY_RIGHT, ord("q")])
            ui.run()
        rows, _, _, _ = ui.compact_geometry(max(1, 30 - 5 - ui.log_height(30)), 100)
        self.assertEqual(ui.selected, rows)
        # Und zurück.
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.scr.keys = iter([curses.KEY_LEFT, ord("q")])
            ui.run()
        self.assertEqual(ui.selected, 0)

    def test_many_repositories_start_compact(self):
        for count, expected in ((25, "compact"), (5, "detail")):
            with self.subTest(count=count):
                ui = self._ui(0)
                ui.statuses = []
                with mock.patch("gitmaster_flash.collect_all",
                                return_value=[self._status(f"r{i}")
                                              for i in range(count)]), \
                        mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                        mock.patch("gitmaster_flash.safe_addstr"):
                    ui.reload()
                self.assertEqual(ui.view_mode, expected)

    def test_only_the_focused_pane_shows_a_selection_bar(self):
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for i in range(6):
            gmf_module.log_command(Path("/tmp/repo"), ("status", str(i)), 0)
        class Recorder(list):
            pass

        for focus, expect_repo_bar in (("repos", True), ("log", False)):
            with self.subTest(focus):
                ui = self._ui(9, height=26, width=100)
                ui.view_mode = "compact"
                ui.focus = focus
                ui.log_selected = 2
                marked = Recorder()

                def addstr(y, x, text, attr=0, _marked=marked):
                    _marked.append((text, bool(attr & curses.A_REVERSE)))

                ui.scr.addstr = addstr
                with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
                    ui.draw()
                repo_bar = any(mark for text, mark in marked if "repo-00" in text)
                log_bar = any(mark for text, mark in marked if "git status 2" in text)
                self.assertEqual(repo_bar, expect_repo_bar, marked)
                self.assertEqual(log_bar, not expect_repo_bar, marked)

    def test_log_selection_and_repo_selection_survive_switching(self):
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for i in range(10):
            gmf_module.log_command(Path("/tmp/repo"), ("status", str(i)), 0)
        ui = self._ui(9, height=26, width=100)
        ui.selected = 4
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            # Tab nach unten, zweimal hoch, Tab zurück, einmal runter, wieder Tab.
            ui.scr.keys = iter([9, curses.KEY_UP, curses.KEY_UP, 9,
                                curses.KEY_DOWN, 9, ord("q")])
            ui.run()
        self.assertEqual(ui.focus, "log")
        self.assertEqual(ui.log_selected, 7)   # 9 (neuester) minus zwei nach oben
        self.assertEqual(ui.selected, 5)       # oben eins weiter, blieb erhalten

    def test_first_visit_to_the_log_starts_at_the_newest_entry(self):
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for i in range(4):
            gmf_module.log_command(Path("/tmp/repo"), ("status", str(i)), 0)
        ui = self._ui(3)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.scr.keys = iter([9, ord("q")])
            ui.run()
        self.assertEqual(ui.log_selected, 3)

    def test_tab_moves_focus_to_the_log_and_arrows_then_scroll(self):
        ui = self._ui(5)
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for i in range(30):
            gmf_module.log_command(Path("/tmp/repo"), ("status", str(i)), 0)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.scr.keys = iter([9, curses.KEY_UP, curses.KEY_UP, ord("q")])
            ui.run()
        self.assertEqual(ui.focus, "log")
        self.assertEqual(ui.selected, 0)      # Auswahl blieb unberührt
        self.assertGreater(ui.log_height(30), 3)   # im Fokus größer

    def test_layout_does_not_overlap_and_fits_the_window(self):
        """Kein Bereich darf in einen anderen hineinzeichnen (h=26, w=100)."""
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for i in range(5):
            gmf_module.log_command(Path("/tmp/repo"), ("status", str(i)), 0)
        for mode in ("compact", "detail"):
            with self.subTest(mode):
                ui = self._ui(24, height=26, width=100)
                ui.view_mode = mode
                with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
                    ui.draw()
                calls = ui.scr.lines
                self.assertTrue(all(0 <= y < 26 for y, _, _ in calls), calls)
                repo_ys = {y for y, _, text in calls
                           if any(name in text for name in ("repo-0", "repo-1"))}
                log_ys = {y for y, _, text in calls
                          if "Befehle" in text or "Commands" in text
                          or "git status" in text}
                footer_ys = {y for y, _, text in calls
                             if any(marker in text for marker in
                                    ("M view", "M Ansicht", "A changes",
                                     "A Änderungen", "Q quit", "Q Beenden"))}
                self.assertTrue(repo_ys and log_ys and footer_ys, calls)
                self.assertLess(max(repo_ys), min(log_ys))
                self.assertLess(max(log_ys), min(footer_ys))
                # Die drei Fußzeilen stehen ganz unten und nirgends sonst.
                self.assertEqual(sorted(footer_ys), [23, 24, 25])

    def test_compact_uses_the_leftover_height_for_the_log(self):
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        ui = self._ui(6, height=30, width=100)      # 6 Repos = 2 Zeilen bei 3 Spalten
        ui.view_mode = "compact"
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        log_header = next(y for y, _, text in ui.scr.lines
                          if "Befehle" in text or "Commands" in text)
        # Der Protokollkopf rückt direkt unter die kurze Liste, statt unten zu kleben.
        self.assertLessEqual(log_header, 5)

    def test_log_pane_is_always_drawn_with_at_least_three_lines(self):
        ui = self._ui(3)
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        gmf_module.log_command(
            Path("/tmp/repo"), ("config", "gmf.test", "value"), 0)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        drawn = "\n".join(text for _, _, text in ui.scr.lines)
        self.assertIn("git config gmf.test value", drawn)
        self.assertEqual(ui.log_height(30), 3)
        self.assertEqual(ui.log_height(12), 0)     # winziges Fenster: Repos gehen vor


class RepoFilterLogicTests(unittest.TestCase):
    """Die reine Filterlogik — ohne curses, ohne Git."""

    @staticmethod
    def _st(rel):
        return RepoStatus(path=Path("/tmp") / rel, rel=rel)

    def test_empty_query_keeps_every_repo_and_its_order(self):
        names = ["zeta", "alpha", "mitte"]
        statuses = [self._st(n) for n in names]
        for query in ("", "   ", "\t"):
            with self.subTest(query=repr(query)):
                self.assertEqual([s.rel for s in
                                  gmf_module.filter_statuses(statuses, query)], names)

    def test_query_is_a_substring_of_the_path_not_a_prefix(self):
        statuses = [self._st("arbeit/kunde/api-server"), self._st("privat/blog")]
        hit = gmf_module.filter_statuses(statuses, "api")
        self.assertEqual([s.rel for s in hit], ["arbeit/kunde/api-server"])

    def test_all_terms_must_match_in_any_order(self):
        statuses = [self._st("arbeit/kunde/api-server"), self._st("arbeit/blog"),
                    self._st("privat/api-spielwiese")]
        # Beide Begriffe zusammen treffen nur das eine Repo — und die Reihenfolge
        # der Begriffe darf daran nichts ändern.
        for query in ("arbeit api", "api arbeit"):
            with self.subTest(query=query):
                self.assertEqual(
                    [s.rel for s in gmf_module.filter_statuses(statuses, query)],
                    ["arbeit/kunde/api-server"])

    def test_matching_ignores_case_including_the_german_sharp_s(self):
        statuses = [self._st("Straße"), self._st("ANDERES")]
        self.assertEqual([s.rel for s in gmf_module.filter_statuses(statuses, "STRASSE")],
                         ["Straße"])
        self.assertEqual([s.rel for s in gmf_module.filter_statuses(statuses, "anderes")],
                         ["ANDERES"])

    def test_no_match_yields_an_empty_list_not_the_whole_list(self):
        statuses = [self._st("alpha"), self._st("beta")]
        self.assertEqual(gmf_module.filter_statuses(statuses, "gibtsnicht"), [])

    def test_filtering_returns_a_new_list_and_never_mutates_the_input(self):
        statuses = [self._st("alpha"), self._st("beta")]
        result = gmf_module.filter_statuses(statuses, "")
        self.assertIsNot(result, statuses)
        result.clear()
        self.assertEqual(len(statuses), 2)

    def test_the_dict_form_filters_by_the_same_rule(self):
        repos = [{"rel": "arbeit/api"}, {"rel": "privat/blog"}, {}]
        self.assertEqual(gmf_module.filter_repo_dicts(repos, "api"),
                         [{"rel": "arbeit/api"}])
        # Ein Repo ohne "rel" darf den Filter nicht sprengen — die Gegenseite
        # eines --diff liefert fremdes JSON, auf dessen Felder man sich nicht
        # verlassen kann.
        self.assertEqual(len(gmf_module.filter_repo_dicts(repos, "")), 3)


class TuiFilterTests(unittest.TestCase):
    """Verhalten des Filters in der TUI: Auswahl, Kopfzeile, Esc."""

    class Screen:
        def __init__(self, keys=()):
            self.keys = iter(keys)
            self.drawn = []

        def getmaxyx(self): return (24, 100)
        def erase(self): pass
        def clear(self): pass
        def addstr(self, y, x, text, *a): self.drawn.append((y, text))
        def move(self, *_a): pass
        def refresh(self): pass
        def getch(self): return next(self.keys)
        def get_wch(self): return next(self.keys)

    def _ui(self, names, keys=(), dirty=()):
        ui = TUI(self.Screen(keys), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.all_statuses = [RepoStatus(path=Path("/tmp") / n, rel=n,
                                      modified=1 if n in dirty else 0)
                           for n in names]
        ui.statuses = list(ui.all_statuses)
        return ui

    def test_applying_a_filter_narrows_the_visible_list_only(self):
        ui = self._ui(["api-gateway", "blog", "api-docs"])
        ui.filter_query = "api"
        ui.apply_filter()
        self.assertEqual([s.rel for s in ui.statuses], ["api-gateway", "api-docs"])
        # Der Filter ist reine Anzeige: der Scan bleibt vollständig, sonst
        # müsste ein Aufheben des Filters neu einlesen.
        self.assertEqual(len(ui.all_statuses), 3)

    def test_a_new_filter_moves_the_selection_to_the_first_hit(self):
        ui = self._ui(["blog", "api-gateway", "api-docs"])
        ui.selected = 0                      # steht auf "blog"
        ui.filter_query = "api"
        ui.apply_filter()
        self.assertEqual(ui.current().rel, "api-gateway")

    def test_reload_keeps_the_selected_repo_under_an_active_filter(self):
        ui = self._ui(["api-gateway", "blog", "api-docs"])
        ui.filter_query = "api"
        ui.apply_filter()
        ui.selected = 1                      # "api-docs"
        ui.apply_filter(keep_selection=True)
        self.assertEqual(ui.current().rel, "api-docs")

    def test_selection_falls_back_to_the_top_when_its_repo_vanishes(self):
        ui = self._ui(["api-gateway", "blog"])
        ui.selected = 1                      # "blog"
        ui.filter_query = "api"
        ui.apply_filter(keep_selection=True)
        self.assertEqual(ui.selected, 0)
        self.assertEqual(ui.current().rel, "api-gateway")

    def test_current_is_none_when_nothing_matches(self):
        ui = self._ui(["alpha", "beta"])
        ui.filter_query = "gibtsnicht"
        ui.apply_filter()
        self.assertIsNone(ui.current())

    def test_hidden_dirty_counts_only_repos_the_filter_removed(self):
        ui = self._ui(["api-gateway", "blog", "notizen"],
                      dirty=("blog", "notizen", "api-gateway"))
        ui.filter_query = "api"
        ui.apply_filter()
        # Zwei schmutzige Repos sind ausgeblendet; das sichtbare zählt nicht mit.
        self.assertEqual(ui.hidden_dirty(), 2)

    def test_the_header_names_the_filter_and_the_hidden_dirty_repos(self):
        ui = self._ui(["api-gateway", "blog"], dirty=("blog",))
        ui.filter_query = "api"
        ui.apply_filter()
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        head = ui.scr.drawn[0][1]
        self.assertIn("1/2", head)
        self.assertIn("api", head)
        # Ein Filter, der das einzige zu prüfende Repo versteckt, muss das sagen.
        self.assertIn("1", head.split("+")[-1])
        self.assertIn("+", head)

    def test_an_empty_result_never_claims_everything_is_clean(self):
        ui = self._ui(["alpha", "beta"], dirty=("alpha",))
        ui.filter_query = "gibtsnicht"
        ui.apply_filter()
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        head = ui.scr.drawn[0][1]
        self.assertNotIn(gmf_module.TR["hdr_clean"]["en"], head)
        self.assertIn(gmf_module.TR["filter_no_match"]["en"], head)

    def test_an_empty_result_draws_a_hint_instead_of_an_empty_area(self):
        ui = self._ui(["alpha"])
        ui.filter_query = "gibtsnicht"
        ui.apply_filter()
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        self.assertTrue(any("gibtsnicht" in text for _, text in ui.scr.drawn[1:]))

    def test_clear_filter_restores_every_repo_and_holds_the_selection(self):
        ui = self._ui(["blog", "api-gateway", "api-docs"])
        ui.filter_query = "api"
        ui.apply_filter()
        ui.selected = 1                      # "api-docs"
        ui.clear_filter()
        self.assertEqual(ui.filter_query, "")
        self.assertEqual(len(ui.statuses), 3)
        self.assertEqual(ui.current().rel, "api-docs")

    def test_the_prompt_starts_from_the_current_filter_and_can_extend_it(self):
        # Esc im Dialog lässt den bestehenden Filter unangetastet …
        ui = self._ui(["api-gateway"], keys=["\x1b"])
        ui.filter_query = "api"
        with mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_filter()
        self.assertEqual(ui.filter_query, "api")
        # … und das Feld ist mit ihm vorbelegt, sodass ⏎ ihn unverändert bestätigt.
        ui = self._ui(["api-gateway", "blog"], keys=["\n"])
        ui.filter_query = "api"
        with mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_filter()
        self.assertEqual(ui.filter_query, "api")
        self.assertEqual([s.rel for s in ui.statuses], ["api-gateway"])

    def test_an_emptied_prompt_clears_the_filter(self):
        ui = self._ui(["api-gateway", "blog"], keys=["\x7f", "\x7f", "\x7f", "\n"])
        ui.filter_query = "api"
        ui.apply_filter()
        with mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_filter()
        self.assertEqual(ui.filter_query, "")
        self.assertEqual(len(ui.statuses), 2)

    def test_slash_reaches_the_filter_action(self):
        ui = self._ui(["alpha"])
        with mock.patch.object(TUI, "action_filter") as action:
            ui.dispatch_action("/")
        action.assert_called_once_with()

    def test_refresh_one_updates_both_lists_so_the_filter_can_be_lifted(self):
        ui = self._ui(["api-gateway", "blog"])
        ui.filter_query = "api"
        ui.apply_filter()
        old = ui.statuses[0]
        fresh = RepoStatus(path=old.path, rel=old.rel, modified=7)
        with mock.patch("gitmaster_flash.collect_status", return_value=fresh):
            ui.refresh_one(old)
        ui.clear_filter()
        # Ohne Nachziehen in all_statuses zeigte das Aufheben des Filters wieder
        # den alten Stand.
        self.assertEqual([s.modified for s in ui.statuses if s.rel == "api-gateway"],
                         [7])


class FilterCliTests(unittest.TestCase):
    """--filter auf der nicht-interaktiven Schnittstelle."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="gmf-narrow-cli-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for name in ("api-gateway", "blog"):
            repo = self.root / name
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        # `main()` setzt die modulweite Sprache. Ohne diese Klammer traegt der
        # Test seine Umgebungssprache in alle folgenden Tests weiter, die auf der
        # englischen Basis bestehen. `--lang en` haelt zusaetzlich die hier
        # geprueften Texte unabhaengig von $LANG.
        with mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err), \
                mock.patch.object(gmf_module, "UI_LANG", "en"):
            code = gmf_module.main([str(self.root), "--lang", "en", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_list_shows_only_matching_repos_and_names_the_filter(self):
        _, out, _ = self._run("--list", "--filter", "api")
        self.assertIn("api-gateway", out)
        self.assertNotIn("blog", out)
        self.assertIn("api", out.splitlines()[0])

    def test_json_carries_only_matching_repos(self):
        _, out, _ = self._run("--json", "--filter", "api")
        payload = json.loads(out)
        self.assertEqual([r["rel"] for r in payload["repos"]], ["api-gateway"])

    def test_a_filter_without_hits_says_so_on_stderr_not_on_stdout(self):
        code, out, err = self._run("--json", "--filter", "gibtsnicht")
        self.assertEqual(json.loads(out)["repos"], [])
        self.assertIn("gibtsnicht", err)
        # Der Exit-Code bleibt skriptbar: kein sichtbares Repo braucht etwas.
        self.assertEqual(code, 0)

    def test_without_a_filter_nothing_changes_in_the_header(self):
        _, out, err = self._run("--list")
        self.assertNotIn("filter", out.splitlines()[0].lower())
        self.assertEqual(err, "")


class SettingsLogicTests(unittest.TestCase):
    """Prüfregeln der Einstellungen — reine Logik, ohne curses."""

    def _setting(self, key):
        return next(s for s in gmf_module.EDITABLE_SETTINGS if s.key == key)

    def test_language_accepts_its_three_options_in_any_case(self):
        lang = self._setting("lang")
        self.assertEqual(gmf_module.parse_setting(lang, "de"), ("de", None))
        self.assertEqual(gmf_module.parse_setting(lang, "  EN "), ("en", None))
        # "auto" bedeutet in der Datei `null`: dann entscheidet $LANG.
        self.assertEqual(gmf_module.parse_setting(lang, "Auto"), (None, None))

    def test_an_unknown_language_is_refused_with_the_options(self):
        value, error = gmf_module.parse_setting(self._setting("lang"), "klingon")
        self.assertIsNone(value)
        self.assertIn("auto", error)
        self.assertIn("de", error)

    def test_numbers_must_be_plain_digits(self):
        setting = self._setting("git_timeout")
        self.assertEqual(gmf_module.parse_setting(setting, " 42 "), (42, None))
        for bad in ("", "abc", "1.5", "-3", "1e3", "10 20"):
            with self.subTest(bad=bad):
                value, error = gmf_module.parse_setting(setting, bad)
                self.assertIsNone(value)
                self.assertTrue(error)

    def test_a_superscript_digit_is_refused_although_str_isdigit_likes_it(self):
        # "²".isdigit() ist True, int("²") wirft aber — eine Prüfung mit isdigit()
        # ließe den Wert durch und der nächste Git-Aufruf scheiterte.
        self.assertTrue("²".isdigit())
        value, error = gmf_module.parse_setting(self._setting("git_timeout"), "²")
        self.assertIsNone(value)
        self.assertTrue(error)

    def test_numbers_outside_their_range_are_refused(self):
        setting = self._setting("git_timeout")
        self.assertEqual(gmf_module.parse_setting(setting, "0")[0], None)
        self.assertEqual(gmf_module.parse_setting(setting, "3600"), (3600, None))
        self.assertIsNone(gmf_module.parse_setting(setting, "3601")[0])

    def test_compact_from_may_be_zero_so_the_compact_view_is_always_on(self):
        self.assertEqual(gmf_module.parse_setting(self._setting("compact_from"), "0"),
                         (0, None))

    def test_skip_dirs_splits_on_commas_and_drops_blanks(self):
        setting = self._setting("skip_dirs")
        self.assertEqual(gmf_module.parse_setting(setting, " a , ,b,, c "),
                         (["a", "b", "c"], None))
        self.assertEqual(gmf_module.parse_setting(setting, "   "), ([], None))

    def test_skip_dirs_refuses_a_path_because_it_could_never_match(self):
        # find_repos() vergleicht mit einzelnen Pfadsegmenten — "a/b" träfe nie
        # zu, und der Anwender suchte den Fehler woanders.
        setting = self._setting("skip_dirs")
        for bad in ("a/b", ".", ".."):
            with self.subTest(bad=bad):
                value, error = gmf_module.parse_setting(setting, bad)
                self.assertIsNone(value)
                self.assertIn(bad, error)

    def test_folder_names_with_spaces_stay_allowed(self):
        self.assertEqual(
            gmf_module.parse_setting(self._setting("skip_dirs"), "My Folder"),
            (["My Folder"], None))

    def test_display_shows_auto_empty_lists_and_apps_readably(self):
        cfg = {"lang": None, "skip_dirs": [], "sync_remote_names": ["origin"],
               "apps": {"E": {"name": "Editor", "path": "/x"}}}
        self.assertEqual(gmf_module.setting_display(cfg, "lang"), "auto")
        self.assertEqual(gmf_module.setting_display(cfg, "skip_dirs"),
                         gmf_module.TR["set_value_none"]["en"])
        self.assertEqual(gmf_module.setting_display(cfg, "sync_remote_names"), "origin")
        self.assertEqual(gmf_module.setting_display(cfg, "apps"), "E Editor")

    def test_remote_identity_and_apps_are_never_editable_in_the_ui(self):
        editable = {s.key for s in gmf_module.EDITABLE_SETTINGS}
        for key in ("sync_remote_names", "sync_remote_hosts", "apps"):
            with self.subTest(key=key):
                self.assertNotIn(key, editable)
                self.assertIn(key, gmf_module.READ_ONLY_SETTINGS)

    def test_every_editable_setting_has_a_label_and_a_hint_in_both_languages(self):
        for setting in gmf_module.EDITABLE_SETTINGS:
            for key in (f"set_{setting.key}", f"set_{setting.key}_hint"):
                with self.subTest(key=key):
                    self.assertEqual(set(gmf_module.TR[key]), {"en", "de"})


class SaveConfigTests(unittest.TestCase):
    """Schreiben der Config: atomar und verlustfrei."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="gmf-cfg-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = self.dir / "nested" / "config.json"

    def test_it_creates_the_folder_and_writes_readable_json(self):
        gmf_module.save_config({"lang": "de", "git_timeout": 11}, self.path)
        self.assertEqual(json.loads(self.path.read_text()),
                         {"lang": "de", "git_timeout": 11})

    def test_unknown_keys_survive_a_save(self):
        # Eine Config kann Schlüssel enthalten, die diese Fassung nicht kennt —
        # etwa von einer neueren Version auf einem anderen Mac.
        cfg = {"lang": "de", "zukunft": {"a": 1}}
        gmf_module.save_config(cfg, self.path)
        self.assertEqual(json.loads(self.path.read_text())["zukunft"], {"a": 1})

    def test_a_failed_write_leaves_neither_a_temp_file_nor_a_broken_config(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"lang": "en"}\n')
        with mock.patch("gitmaster_flash.os.replace", side_effect=OSError("voll")):
            with self.assertRaises(OSError):
                gmf_module.save_config({"lang": "de"}, self.path)
        # Die alte Datei steht unveraendert, und daneben liegt kein Rest.
        self.assertEqual(json.loads(self.path.read_text()), {"lang": "en"})
        self.assertEqual(list(self.path.parent.glob("*.tmp*")), [])

    def test_non_ascii_stays_readable_instead_of_being_escaped(self):
        gmf_module.save_config({"skip_dirs": ["Bücher"]}, self.path)
        self.assertIn("Bücher", self.path.read_text())


class TuiSettingsTests(unittest.TestCase):
    """Die Einstellungsansicht: Navigation, Speichern, Grenzen."""

    class Screen:
        def __init__(self, keys=()):
            self.keys = iter(keys)
            self.drawn = []

        def getmaxyx(self): return (24, 100)
        def erase(self): pass
        def clear(self): pass
        def addstr(self, y, x, text, *a): self.drawn.append((y, text))
        def move(self, *_a): pass
        def refresh(self): pass
        def getch(self): return next(self.keys)
        def get_wch(self): return next(self.keys)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="gmf-setui-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = self.dir / "config.json"

    def _ui(self, keys=(), config_path="real"):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        where = self.path if config_path == "real" else None
        return TUI(self.Screen(keys), Path("/tmp"), cfg, None, "", where)

    # Das Eingabefeld ist mit dem aktuellen Wert vorbelegt — wie ein Mensch muss
    # ein Test ihn erst loeschen, bevor er einen neuen tippt.
    CLEAR = ["\x7f"] * 12

    def _run(self, ui):
        with mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_settings()

    def test_the_rows_list_every_setting_and_mark_only_some_editable(self):
        ui = self._ui()
        rows = ui.settings_rows()
        editable = [row[1].key for row in rows if row[0] == "edit"]
        shown_only = [row[1] for row in rows if row[0] == "info"]
        self.assertEqual(editable, [s.key for s in gmf_module.EDITABLE_SETTINGS])
        self.assertEqual(shown_only, list(gmf_module.READ_ONLY_SETTINGS))

    def test_q_closes_the_view_without_touching_the_config(self):
        ui = self._ui([ord("q")])
        self._run(ui)
        self.assertFalse(self.path.exists())

    def test_changing_a_number_saves_it_and_takes_effect_at_once(self):
        # runter zu compact_from, Enter, "7", Enter, dann q
        ui = self._ui([curses.KEY_DOWN, 10, *self.CLEAR, "7", "\n", ord("q")])
        self._run(ui)
        self.assertEqual(ui.cfg["compact_from"], 7)
        self.assertEqual(json.loads(self.path.read_text())["compact_from"], 7)

    def test_a_refused_value_changes_nothing_and_explains_why(self):
        before = DEFAULT_CONFIG["compact_from"]
        ui = self._ui([curses.KEY_DOWN, 10, *self.CLEAR, "x", "\n", ord("q")])
        self._run(ui)
        self.assertEqual(ui.cfg["compact_from"], before)
        self.assertFalse(self.path.exists())
        self.assertTrue(any(gmf_module.TR["set_err_number"]["en"] in text
                            for _, text in ui.scr.drawn))

    def test_esc_in_the_prompt_keeps_the_old_value(self):
        before = DEFAULT_CONFIG["compact_from"]
        ui = self._ui([curses.KEY_DOWN, 10, "\x1b", ord("q")])
        self._run(ui)
        self.assertEqual(ui.cfg["compact_from"], before)
        self.assertFalse(self.path.exists())

    def test_choosing_a_language_switches_the_interface_immediately(self):
        ui = self._ui([10, *self.CLEAR, "d", "e", "\n", ord("q")])
        with mock.patch.object(gmf_module, "UI_LANG", "en"):
            self._run(ui)
            self.assertEqual(gmf_module.UI_LANG, "de")
        self.assertEqual(ui.cfg["lang"], "de")

    def test_auto_language_is_stored_as_null_not_as_the_word(self):
        ui = self._ui([10, *self.CLEAR, "a", "u", "t", "o", "\n", ord("q")])
        with mock.patch.object(gmf_module, "UI_LANG", "en"):
            self._run(ui)
        self.assertIsNone(json.loads(self.path.read_text())["lang"])

    def test_without_a_config_path_nothing_is_written(self):
        # Genau der Demo-Fall: Ein Screenshot-Lauf darf die echte Datei nie anfassen.
        ui = self._ui([curses.KEY_DOWN, 10, *self.CLEAR, "7", "\n", ord("q")],
                      config_path=None)
        self._run(ui)
        self.assertEqual(ui.cfg["compact_from"], 7)     # gilt für die Sitzung
        self.assertFalse(self.path.exists())
        self.assertTrue(any(gmf_module.TR["set_not_saved_hint"]["en"] in text
                            for _, text in ui.scr.drawn))

    def test_a_failed_save_says_so_instead_of_claiming_success(self):
        ui = self._ui([curses.KEY_DOWN, 10, *self.CLEAR, "7", "\n", ord("q")])
        with mock.patch("gitmaster_flash.save_config",
                        side_effect=OSError("Platte voll")):
            self._run(ui)
        self.assertEqual(ui.cfg["compact_from"], 7)     # gilt für die Sitzung
        self.assertTrue(any("Platte voll" in text for _, text in ui.scr.drawn))
        self.assertFalse(any(gmf_module.TR["set_saved"]["en"] == text.strip()
                             for _, text in ui.scr.drawn))

    def test_the_selection_never_leaves_the_editable_rows(self):
        # Zwanzig Mal runter, dann zwanzig Mal hoch: die Auswahl muss innerhalb
        # der editierbaren Zeilen bleiben, sonst zeigte ⏎ auf eine Infozeile.
        keys = ([curses.KEY_DOWN] * 20 + [curses.KEY_UP] * 20
                + [10, *self.CLEAR, "3", "\n", ord("q")])
        ui = self._ui(keys)
        self._run(ui)
        # Die erste editierbare Zeile ist die Sprache — "3" ist dort ungültig.
        self.assertEqual(ui.cfg["lang"], DEFAULT_CONFIG["lang"])
        self.assertTrue(any(gmf_module.TR["set_err_choice"]["en"].split("{")[0] in text
                            for _, text in ui.scr.drawn))

    def test_the_comma_key_reaches_the_settings_view(self):
        ui = self._ui()
        with mock.patch.object(TUI, "action_settings") as action:
            ui.dispatch_action(",")
        action.assert_called_once_with()

    def test_changing_skip_dirs_says_that_a_rescan_is_needed(self):
        rows = self._ui().settings_rows()
        index = next(i for i, row in enumerate(rows)
                     if row[0] == "edit" and row[1].key == "skip_dirs")
        # An die vorbelegte Liste einen weiteren Ordner anhaengen — so, wie man
        # es in der Oberflaeche taete.
        keys = [curses.KEY_DOWN] * index + [10] + list(",bau") + ["\n", ord("q")]
        ui = self._ui(keys)
        self._run(ui)
        self.assertTrue(any(gmf_module.TR["set_saved_rescan"]["en"] in text
                            for _, text in ui.scr.drawn))


class GitCancelTests(unittest.TestCase):
    """Abbruch laufender Git-Aufrufe — die Grundlage des Hintergrund-Fetch.

    Wird die Oberflaeche waehrend eines Fetch beendet, darf kein git und kein
    davon gestartetes ssh weiterlaufen.
    """

    def setUp(self):
        self.addCleanup(gmf_module.resume_git_calls)
        gmf_module.resume_git_calls()

    def test_a_running_call_is_killed_and_its_whole_group_with_it(self):
        # Ein Kommando, das selbst ein langlebiges Enkelkind in derselben Gruppe
        # startet — genau die Konstellation git + ssh.
        script = ("import subprocess, sys, time\n"
                  "child = subprocess.Popen(['/bin/sleep', '60'])\n"
                  "sys.stdout.write(str(child.pid) + chr(10))\n"
                  "sys.stdout.flush()\n"
                  "time.sleep(60)\n")
        started = threading.Event()
        result = {}

        def call():
            started.set()
            try:
                result["run"] = gmf_module._run_process_group(
                    [sys.executable, "-c", script], stdin=subprocess.DEVNULL,
                    timeout=60)
            except BaseException as exc:            # noqa: BLE001
                result["error"] = exc

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        started.wait(5)
        # Warten, bis der Prozess wirklich registriert ist.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not gmf_module._LIVE_PROCESSES:
            time.sleep(0.02)
        self.assertTrue(gmf_module._LIVE_PROCESSES, "Prozess wurde nie registriert")

        gmf_module.cancel_git_calls()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "Der Aufruf haengt trotz Abbruch")
        # Das Enkelkind darf den Abbruch nicht ueberleben.
        grandchild = int((result.get("run").stdout if result.get("run")
                          else "0").strip() or 0)
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
        gmf_module.cancel_git_calls()
        with mock.patch("gitmaster_flash.subprocess.Popen") as popen:
            with self.assertRaises(gmf_module.GitCancelled):
                gmf_module._run_process_group(["/bin/echo", "hi"],
                                              stdin=subprocess.DEVNULL, timeout=5)
        # Nicht "gestartet und dann getoetet", sondern gar nicht erst gestartet:
        # ein Fetch, der jetzt losliefe, haenge bis zu seinem eigenen Timeout.
        popen.assert_not_called()

    def test_resume_lets_calls_through_again(self):
        gmf_module.cancel_git_calls()
        gmf_module.resume_git_calls()
        done = gmf_module._run_process_group(["/bin/echo", "hi"],
                                             stdin=subprocess.DEVNULL, timeout=10)
        self.assertEqual(done.stdout.strip(), "hi")

    def test_the_registry_is_empty_again_after_a_normal_call(self):
        gmf_module._run_process_group(["/bin/echo", "hi"],
                                      stdin=subprocess.DEVNULL, timeout=10)
        self.assertFalse(gmf_module._LIVE_PROCESSES)


class BackgroundScanTests(unittest.TestCase):
    """Der Hintergrund-Scan als solcher: Meldungen, Reihenfolge, Abbruch."""

    def setUp(self):
        self.addCleanup(gmf_module.resume_git_calls)
        gmf_module.resume_git_calls()
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
        self.assertEqual([st.rel for st in results],
                         [st.rel for st in gmf_module.sort_statuses(results)])

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
        scan = gmf_module.BackgroundScan(self.root,
                                         {**DEFAULT_CONFIG, "fetch_timeout": 120,
                                          "git_timeout": 120})
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
                live = [proc.args for proc in gmf_module._LIVE_PROCESSES]
                time.sleep(0.02)
            self.assertTrue(live, "Es lief nie ein Git-Aufruf")
            self.assertEqual(live[0][:1], ["git"])
            started = time.monotonic()
            scan.stop()
            elapsed = time.monotonic() - started
        self.assertFalse(scan._thread.is_alive(), "Der Arbeiter laeuft weiter")
        # Deutlich unter dem Timeout von 120 s — sonst waere nichts abgebrochen.
        self.assertLess(elapsed, 15)
        self.assertFalse(gmf_module._LIVE_PROCESSES)


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

        class Screen:
            """R startet den Fetch, q beendet MITTEN darin.

            Der Zeitgeber-Tick wartet, bis wirklich ein Git-Prozess laeuft —
            sonst waere das q womoeglich schon durch, bevor ueberhaupt etwas zu
            beenden war, und der Test bewiese nichts.
            """

            def __init__(self):
                self.keys = iter([ord("R"), -1, ord("q")])

            def getmaxyx(self): return (24, 100)
            def erase(self): pass
            def clear(self): pass
            def addstr(self, *a): pass
            def refresh(self): pass
            def timeout(self, *_a): pass

            def getch(self):
                key = next(self.keys)
                if key == -1:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if gmf_module._LIVE_PROCESSES:
                            seen_live.append(True)
                            break
                        time.sleep(0.02)
                return key

        cfg = {**DEFAULT_CONFIG, "git_timeout": 300, "fetch_timeout": 300}
        ui = TUI(Screen(), self.root, cfg, None)
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"PATH": f"{slow}:{os.environ['PATH']}"}), \
                mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.run()
        elapsed = time.monotonic() - started
        self.assertTrue(seen_live, "Beim Beenden lief gar kein Git-Aufruf")
        # Ohne Abbruch haette das Beenden bis zum Timeout von 300 s gedauert.
        self.assertLess(elapsed, 20)
        self.assertIsNone(ui.scan)
        self.assertFalse(gmf_module._LIVE_PROCESSES)
        # Und kein sleep aus unserem Ersatz-git laeuft noch.
        leftovers = subprocess.run(["pgrep", "-f", f"{slow}/git"],
                                   capture_output=True, text=True).stdout.strip()
        self.assertEqual(leftovers, "", f"uebrige Prozesse: {leftovers}")


class TuiBackgroundFetchTests(unittest.TestCase):
    """Wie die Oberflaeche mit den Ergebnissen des Hintergrund-Fetch umgeht."""

    class Screen:
        def __init__(self, keys=()):
            self.keys = iter(keys)
            self.drawn = []

        def getmaxyx(self): return (24, 120)
        def erase(self): pass
        def clear(self): pass
        def addstr(self, y, x, text, *a): self.drawn.append((y, text))
        def move(self, *_a): pass
        def refresh(self): pass
        def timeout(self, *_a): pass
        def getch(self): return next(self.keys)
        def get_wch(self): return next(self.keys)

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

        def stop(self): self.stopped = True

    def test_a_finished_repo_is_merged_without_reordering_the_list(self):
        ui = self._ui(["alpha", "beta", "gamma"])
        ui.scan = self.FakeScan()
        # "gamma" wird problematisch — beim Sortieren stuende es ganz oben.
        ui.scan.queue.put(("one", self._st("gamma", modified=3)))
        ui.drain_background_scan()
        self.assertEqual([s.rel for s in ui.all_statuses],
                         ["alpha", "beta", "gamma"])
        self.assertEqual(ui.all_statuses[2].modified, 3)

    def test_the_final_result_sorts_and_takes_new_repos_along(self):
        ui = self._ui(["alpha", "beta"])
        ui.scan = self.FakeScan()
        results = gmf_module.sort_statuses(
            [self._st("alpha"), self._st("beta"), self._st("neu", modified=1)])
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
        ui.locally_refreshed.add("alpha")
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
        self.assertEqual(ui.locally_refreshed, {"alpha"})

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
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.run()
        self.assertTrue(stopped.stopped, "Der Scan lief nach dem Beenden weiter")
        self.assertIsNone(ui.scan)

    def test_an_exception_in_the_loop_still_stops_the_scan(self):
        ui = self._ui(["alpha"], keys=[])          # StopIteration beim ersten getch
        ui.scan = self.FakeScan()
        stopped = ui.scan
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            with self.assertRaises(StopIteration):
                ui.run()
        self.assertTrue(stopped.stopped)

    def test_the_timer_tick_redraws_without_wiping_the_message(self):
        # -1 heisst "keine Taste, nur der Zeitgeber". Eine Meldung, die dabei
        # verschwaende, waere nach 200 ms weg und nie lesbar.
        ui = self._ui(["alpha"], keys=[-1, ord("q")])
        ui.scan = self.FakeScan()
        ui.message = "wichtig"
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "reload"):
            ui.run()
        self.assertTrue(any("wichtig" in text for _, text in ui.scr.drawn))

    def test_without_a_scan_the_wait_stays_unbounded(self):
        # Nur waehrend eines Scans darf getch() begrenzt warten: Die
        # Unteransichten lesen mit get_wch(), das bei Zeitablauf wirft.
        ui = self._ui(["alpha"])
        ui.scr.timeout = mock.Mock()
        ui.scr.keys = iter([ord("x")])
        ui._wait_for_key()
        ui.scr.timeout.assert_not_called()

    def test_during_a_scan_the_wait_is_bounded_and_reset_right_after(self):
        ui = self._ui(["alpha"])
        ui.scan = self.FakeScan()
        ui.scr.timeout = mock.Mock()
        ui.scr.keys = iter([-1])
        ui._wait_for_key()
        self.assertEqual([call.args[0] for call in ui.scr.timeout.call_args_list],
                         [gmf_module.BACKGROUND_POLL_MS, -1])


class CommitGroupTests(unittest.TestCase):
    """Vorschlaege fuer die Dateiauswahl — reine Logik."""

    @staticmethod
    def _f(code, path, group=""):
        return ChangedFile(code, path, " M", group)

    def _labels(self, files):
        return [TUI.commit_group_label(g) for g in gmf_module.commit_groups(files)]

    def test_a_single_file_gets_no_suggestion(self):
        self.assertEqual(gmf_module.commit_groups([self._f("M", "a.py")]), [])

    def test_it_groups_by_change_kind_folder_and_extension(self):
        files = [self._f("M", "src/app.py"), self._f("M", "src/util.py"),
                 self._f("U", "docs/neu.md"), self._f("M", "docs/guide.md"),
                 self._f("M", "Makefile")]
        found = {(g.kind, g.key): set(g.paths)
                 for g in gmf_module.commit_groups(files)}
        self.assertEqual(found[("dir", "src")], {"src/app.py", "src/util.py"})
        self.assertEqual(found[("dir", "docs")], {"docs/neu.md", "docs/guide.md"})
        # Nach Art der Aenderung: die vier geaenderten ohne die eine neue Datei.
        self.assertEqual(found[("code", "M")],
                         {"src/app.py", "src/util.py", "docs/guide.md", "Makefile"})
        # Die einzelne neue Datei ergibt keine Gruppe, und die Endung .py trifft
        # dieselben Dateien wie der Ordner src/ — beides faellt weg.
        self.assertNotIn(("code", "U"), found)
        self.assertNotIn(("ext", ".py"), found)

    def test_a_suggestion_covering_everything_is_dropped(self):
        # Alle Dateien sind geaendert und liegen in src/ — beides waere nur ein
        # umstaendliches "alle", das es schon gibt.
        files = [self._f("M", "src/a.py"), self._f("M", "src/b.py")]
        self.assertEqual(gmf_module.commit_groups(files), [])

    def test_identical_subsets_appear_only_once(self):
        # "Ordner docs/" und "Typ .md" treffen dieselben zwei Dateien.
        files = [self._f("M", "docs/a.md"), self._f("M", "docs/b.md"),
                 self._f("M", "src/c.py")]
        groups = gmf_module.commit_groups(files)
        self.assertEqual([set(g.paths) for g in groups].count(
            {"docs/a.md", "docs/b.md"}), 1)

    def test_a_group_of_one_file_is_no_group(self):
        files = [self._f("M", "src/a.py"), self._f("M", "src/b.py"),
                 self._f("M", "einzeln/c.py")]
        self.assertNotIn("einzeln", [g.key for g in gmf_module.commit_groups(files)])

    def test_files_in_the_root_produce_no_folder_group(self):
        files = [self._f("M", "a.py"), self._f("M", "b.py"), self._f("U", "src/c.py")]
        self.assertEqual([g.key for g in gmf_module.commit_groups(files)
                          if g.kind == "dir"], [])

    def test_a_dotfile_is_a_name_not_an_extension(self):
        files = [self._f("M", ".gitignore"), self._f("M", ".npmrc"),
                 self._f("M", "src/a.py"), self._f("M", "src/b.py")]
        self.assertEqual([g.key for g in gmf_module.commit_groups(files)
                          if g.kind == "ext"], [])

    def test_extensions_are_matched_regardless_of_case(self):
        files = [self._f("M", "a/One.PY"), self._f("M", "b/two.py"),
                 self._f("M", "c/three.md")]
        by_ext = {g.key: set(g.paths) for g in gmf_module.commit_groups(files)
                  if g.kind == "ext"}
        self.assertEqual(by_ext.get(".py"), {"a/One.PY", "b/two.py"})

    def test_a_rename_pair_is_never_split_across_a_suggestion(self):
        # Quelle und Ziel muessen zusammen committet werden; ein Vorschlag, der
        # nur eine Haelfte traefe, hinterliesse eine halbe Umbenennung.
        files = [self._f("D", "src/alt.py", "r1"), self._f("U", "docs/neu.md", "r1"),
                 self._f("M", "src/x.py"), self._f("M", "src/y.py")]
        for group in gmf_module.commit_groups(files):
            with self.subTest(group=group.key):
                inside = {"src/alt.py", "docs/neu.md"} & set(group.paths)
                self.assertIn(len(inside), (0, 2))

    def test_a_completed_group_says_so_in_its_label(self):
        files = [self._f("D", "src/alt.py", "r1"), self._f("U", "docs/neu.md", "r1"),
                 self._f("M", "src/x.py"), self._f("M", "src/y.py")]
        deleted = next(g for g in gmf_module.commit_groups(files)
                       if g.kind == "code" and g.key == "D")
        self.assertTrue(deleted.completed)
        # Ohne den Zusatz hiesse die Gruppe "deleted files (2)", obwohl eine der
        # beiden Dateien neu ist.
        self.assertIn(gmf_module.TR["group_with_rename"]["en"],
                      TUI.commit_group_label(deleted))

    def test_bigger_suggestions_come_first_and_the_order_is_stable(self):
        files = [self._f("M", "src/a.py"), self._f("M", "src/b.py"),
                 self._f("M", "src/c.py"), self._f("U", "docs/d.md"),
                 self._f("U", "docs/e.md")]
        groups = gmf_module.commit_groups(files)
        sizes = [len(g.paths) for g in groups if g.kind == "dir"]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual([g.key for g in gmf_module.commit_groups(files)],
                         [g.key for g in groups])

    def test_every_label_exists_in_both_languages(self):
        for code in ("M", "U", "D", "C"):
            with self.subTest(code=code):
                self.assertEqual(set(gmf_module.TR[f"group_code_{code}"]),
                                 {"en", "de"})


class CommitWizardSelectionTests(unittest.TestCase):
    """Die neuen Auswahltasten der Commit-Hilfe: A, N und G."""

    class Screen:
        def __init__(self, keys):
            self.keys = iter(keys)
            self.drawn = []

        def getmaxyx(self): return (24, 100)
        def erase(self): pass
        def clear(self): pass
        def addstr(self, y, x, text, *a): self.drawn.append((y, text))
        def move(self, *_a): pass
        def refresh(self): pass
        def getch(self): return next(self.keys)
        def get_wch(self): return next(self.keys)

    FILES = [ChangedFile("M", "src/app.py", " M"),
             ChangedFile("M", "src/util.py", " M"),
             ChangedFile("U", "docs/neu.md", "??")]

    def _run(self, keys):
        st = RepoStatus(path=Path("/tmp/x"), rel="x", files=list(self.FILES),
                        modified=2, untracked=1)
        ui = TUI(self.Screen(keys), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.all_statuses = ui.statuses = [st]
        taken = {}

        def step2(_self, _st, items):
            taken["paths"] = [it["path"] for it in items if it["include"]]
            return True

        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "_commit_step2", step2):
            ui.action_commit_wizard()
        return taken.get("paths"), ui

    def test_n_deselects_everything_and_a_brings_it_back(self):
        taken, _ = self._run([ord("n"), 10])
        self.assertEqual(taken, [])
        taken, _ = self._run([ord("n"), ord("a"), 10])
        self.assertEqual(len(taken), 3)

    def test_g_replaces_the_selection_with_the_chosen_suggestion(self):
        # G öffnet die Liste, ⏎ nimmt den ersten Vorschlag, ⏎ committet.
        taken, _ = self._run([ord("g"), 10, 10])
        groups = gmf_module.commit_groups(self.FILES)
        self.assertEqual(sorted(taken), sorted(groups[0].paths))
        # "Ersetzen" heisst ersetzen: nichts von vorher bleibt zusaetzlich drin.
        self.assertLess(len(taken), 3)

    def test_esc_in_the_suggestion_list_leaves_the_selection_untouched(self):
        taken, _ = self._run([ord("g"), 27, 10])
        self.assertEqual(len(taken), 3)

    def test_without_a_sensible_subset_g_says_so_instead_of_an_empty_list(self):
        st = RepoStatus(path=Path("/tmp/x"), rel="x",
                        files=[ChangedFile("M", "src/a.py", " M"),
                               ChangedFile("M", "src/b.py", " M")])
        ui = TUI(self.Screen([]), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.all_statuses = ui.statuses = [st]
        note = ui._apply_commit_group(st, [])
        self.assertEqual(note, gmf_module.TR["group_none"]["en"])

    def test_g_without_suggestions_shows_the_reason_in_the_helper_itself(self):
        # Drei neue Dateien in der Wurzel: keine Teilmenge, die etwas taugt.
        # Die Meldung muss IM Bild der Hilfe stehen — die Zeile der Repo-Liste
        # ist hier nicht sichtbar.
        st = RepoStatus(path=Path("/tmp/x"), rel="x",
                        files=[ChangedFile("U", "a.log", "??"),
                               ChangedFile("U", "b.sh", "??"),
                               ChangedFile("U", "c.md", "??")])
        ui = TUI(self.Screen([ord("g"), ord("n"), 10]), Path("/tmp"),
                 DEFAULT_CONFIG, None)
        ui.all_statuses = ui.statuses = [st]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0), \
                mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(TUI, "_commit_step2", lambda *a: True):
            ui.action_commit_wizard()
        self.assertTrue(any(gmf_module.TR["group_none"]["en"] in text
                            for _, text in ui.scr.drawn))

    def test_the_applied_suggestion_is_named_in_the_helper(self):
        taken, ui = self._run([ord("g"), 10, 10])
        self.assertTrue(taken)
        self.assertTrue(any(gmf_module.TR["group_applied"]["en"][:12] in text
                            for _, text in ui.scr.drawn))

    def test_the_title_counts_how_many_are_selected(self):
        _, ui = self._run([ord("n"), 10])
        self.assertTrue(any("0/3" in text for _, text in ui.scr.drawn))


class DisplayAndIntegrationSafetyTests(unittest.TestCase):
    def test_i_key_opens_repo_info_case_insensitively(self):
        class Screen:
            def __init__(self):
                self.keys = iter((ord("i"), ord("Q")))

            def getch(self):
                return next(self.keys)

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        with mock.patch("gitmaster_flash.curses.curs_set"), \
                mock.patch.object(ui, "reload"), \
                mock.patch.object(ui, "draw"), \
                mock.patch.object(ui, "action_repo_info") as action:
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
        self.assertEqual(
            escaped, "a\\u202eb\\u2066c\\u2028d\\u2029")
        self.assertEqual(terminal_text("byte\udcff"), "byte\\udcff")

    def test_surrogateescaped_repo_name_is_safe_in_json_and_terminal_text(self):
        raw = b"repo-\xff"
        rel = os.fsdecode(raw)
        st = RepoStatus(path=Path("/tmp") / rel, rel=rel)

        payload = status_dict(st)

        self.assertEqual(payload["rel"], "repo-\\udcff")
        self.assertNotIn("\udcff", json.dumps(payload, ensure_ascii=True))

    def test_direct_cd_output_is_safe_and_shell_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "line\n;echo nope\x1b\u0085\u202e"
            path.mkdir()

            class Screen:
                def getmaxyx(self): return (30, 100)

            ui = TUI(Screen(), Path(temp), DEFAULT_CONFIG, None)
            ui.statuses = [RepoStatus(path=path, rel="unsafe")]
            with mock.patch("builtins.print") as printed:
                self.assertTrue(ui.action_cd_and_quit())

            command = printed.call_args_list[0].args[0].strip()
            self.assertNotIn("\x1b", command)
            self.assertNotIn("\n", command)
            self.assertTrue(command.startswith("cd -- $'"), command)
            executed = subprocess.run(
                ["zsh", "-fc", command + "; pwd -P"],
                capture_output=True, text=True)
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

        with mock.patch("gitmaster_flash.subprocess.Popen", FakePopen), \
                mock.patch.dict(os.environ,
                                {key: "/tmp/wrapper"
                                 for key in gmf_module.TRANSPORT_GIT_ENV}):
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
        unset = {words[index + 1]
                 for index, word in enumerate(words[env_at:], env_at)
                 if word == "-u"}
        self.assertEqual(unset, gmf_module.TRANSPORT_GIT_ENV)
        # Der tatsächliche Aufruf muss sie ebenfalls losgeworden sein, sonst
        # entschiede ein geerbter SSH-Wrapper über das wahre Transferziel.
        for key in gmf_module.TRANSPORT_GIT_ENV:
            self.assertNotIn(key, seen)
        actual = {key: value for key, value in seen.items()
                  if key.startswith("GIT_CONFIG")}
        self.assertEqual(len(assignments), len(actual))
        for key, value in actual.items():
            self.assertEqual(assignments[key], value, key)
        # Die Anzahl muss zu den tatsächlich gesetzten Paaren passen; sonst
        # ignoriert Git den Rest der Liste stillschweigend.
        self.assertEqual(int(actual["GIT_CONFIG_COUNT"]), (len(actual) - 1) // 2)

    def test_long_pinned_command_wraps_without_losing_text(self):
        command = gmf_module.format_git_command(safe_push_args(
            "/tmp/a-very-long-approved-remote-name.git", "main",
            "a" * 40, "b" * 40)) + "\u202e"
        visible = terminal_text(command)
        wrapped = gmf_module.wrap_cells(visible, 80)
        self.assertGreater(len(wrapped), 1)
        self.assertEqual("".join(wrapped), visible)
        self.assertNotIn("\u202e", "".join(wrapped))
        self.assertIn("\\u202e", "".join(wrapped))
        self.assertTrue(all(cell_width(line) <= 80 for line in wrapped))

    def test_commit_picker_survives_39_columns(self):
        class Screen:
            def erase(self): pass
            def getmaxyx(self): return (10, 39)
            def addstr(self, *args): pass
            def refresh(self): pass
            def getch(self): return 27

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.statuses = [RepoStatus(path=Path("/tmp/repo"), rel="r",
                                  files=[ChangedFile("M", "very-long-name.txt", " M")],
                                  modified=1)]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_commit_wizard()
        self.assertEqual(ui.message, "Commit helper cancelled.")

    def test_remote_root_is_one_shell_argument(self):
        root = "~/My Projects/x;touch PWNED"
        completed = subprocess.CompletedProcess([], 0, stdout='{"version":"x","repos":[]}', stderr="")
        with mock.patch("gitmaster_flash._run_process_group",
                        return_value=completed) as run:
            fetch_remote_status("example", root, fetch=False)
        command = run.call_args.args[0][-1]
        self.assertEqual(shlex.split(command)[-1], root)
        self.assertEqual(shlex.split(command).count(root), 1)
        self.assertEqual(shlex.split(command)[-2], "--")

    def test_remote_side_gets_the_local_ui_language(self):
        """Ohne --lang wählte die Gegenseite ihre Sprache selbst — derselbe
        lokalisierte Fehlertext sähe im --diff dann wie DRIFT aus."""
        completed = subprocess.CompletedProcess(
            [], 0, stdout='{"version":"x","repos":[]}', stderr="")
        with mock.patch("gitmaster_flash._run_process_group",
                        return_value=completed) as run:
            fetch_remote_status("example", "~/git", fetch=False)
        command = shlex.split(run.call_args.args[0][-1])
        self.assertIn("--lang", command)
        self.assertEqual(command[command.index("--lang") + 1], gmf_module.UI_LANG)

    def test_remote_json_with_attention_exit_is_accepted(self):
        """Exit 1 ist bei --json ein Befund, kein fehlgeschlagener SSH-Aufruf."""
        payload = {"version": "x", "repos": [{"rel": "needs-attention"}]}
        completed = subprocess.CompletedProcess(
            [], 1, stdout=json.dumps(payload), stderr="")
        with mock.patch("gitmaster_flash._run_process_group",
                        return_value=completed):
            self.assertEqual(fetch_remote_status("example", "~/git", fetch=True), payload)

    def test_remote_process_uses_the_derived_timeout(self):
        payload = {"version": "x", "repos": []}
        completed = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(payload), stderr="")
        with mock.patch("gitmaster_flash._run_process_group",
                        return_value=completed) as run:
            fetch_remote_status(
                "example", "~/git", fetch=True, process_timeout=777)
        self.assertEqual(run.call_args.kwargs["timeout"], 777)

    def test_run_diff_uses_the_configured_hard_wall_clock_timeout(self):
        payload = {"version": __version__, "repos": []}
        cfg = {**DEFAULT_CONFIG, "git_timeout": 10, "fetch_timeout": 30}
        with mock.patch.object(gmf_module, "fetch_remote_status",
                               return_value=payload) as remote, \
                mock.patch.object(gmf_module, "collect_all", return_value=[]), \
                mock.patch("builtins.print"):
            result = gmf_module.run_diff(
                "example", Path("/work"), cfg, fetch=True, as_json=False)
        self.assertEqual(result, 0)
        remote.assert_called_once()
        self.assertTrue(remote.call_args.kwargs["fetch"])
        self.assertEqual(remote.call_args.kwargs["process_timeout"],
                         DEFAULT_CONFIG["diff_timeout"])

    def test_a_connected_but_stuck_remote_process_is_reported(self):
        timeout = subprocess.TimeoutExpired(["ssh", "example"], 1)
        with mock.patch("gitmaster_flash._run_process_group", side_effect=timeout):
            with self.assertRaisesRegex(RuntimeError, "Cannot reach"):
                fetch_remote_status(
                    "example", "~/git", fetch=False, process_timeout=1)

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
                "printf '%s\\n' \"$child\" > \"$GMF_SSH_CHILD_PID\"\n"
                "wait \"$child\"\n")
            ssh.chmod(0o755)
            env = {
                "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
                "GMF_SSH_CHILD_PID": str(child_file),
            }
            with mock.patch.dict(os.environ, env):
                with self.assertRaisesRegex(RuntimeError, "Cannot reach"):
                    fetch_remote_status(
                        "example", "~/git", fetch=False, process_timeout=1)

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
        for payload in ([], {"version": "x", "repos": {}},
                        {"version": "x", "repos": [{"rel": "x", "remotes": [{}]}]}):
            with self.subTest(payload=payload):
                completed = subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps(payload), stderr="")
                with mock.patch("gitmaster_flash._run_process_group",
                                return_value=completed):
                    with self.assertRaisesRegex(RuntimeError, "JSON structure"):
                        fetch_remote_status("example", "~/git", fetch=False)

    def test_remote_process_failure_without_stderr_reports_exit_code(self):
        completed = subprocess.CompletedProcess([], 255, stdout="", stderr="")
        with mock.patch("gitmaster_flash._run_process_group",
                        return_value=completed):
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
        grid = module.replay("\x1b[H\x1b[2Jlist\x1b[4dfooter"      # ESC[4d: 4. Zeile
                             "\x1b[?25l"                            # privater Modus
                             "\x1b[2;1Hab\x08X",                    # Backspace
                             cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in grid]
        self.assertEqual(lines[0], "list")
        # ESC[<n>d wechselt nur die Zeile, die Spalte bleibt hinter "list" stehen.
        self.assertEqual(lines[3], "    footer")
        self.assertEqual(lines[1], "aX")
        self.assertNotIn("25l", "".join(lines))
        # Diese Symbole sind für macOS-curses einzellig. Würde der Nachbau sie
        # breiter zählen, wanderten spätere ↑n-/↓n-Zeilen im SVG seitlich.
        self.assertEqual(module._cell_width("✔"), 1)
        self.assertEqual(module._cell_width("⚑"), 1)

    def test_screen_replay_scrolls_the_region_curses_asks_it_to(self):
        """ncurses schiebt Zeilen, statt sie neu zu malen — das muss nachgebildet sein.

        Beobachtet am 2026-08-23 beim Filter ohne Treffer: ncurses setzte den
        Bereich (ESC[2;30r), sprang ans Ende und scrollte ihn (ESC[6S). Der
        Nachbau kannte weder das eine noch das andere und behielt die alte
        Repo-Liste im Bild — ringsum stimmte alles, das Ergebnis sah deshalb
        plausibel aus und war falsch.
        """
        module, _ = self._make_screens_module()
        painted = "\x1b[H\x1b[2J" + "".join(
            f"\x1b[{n};1Hzeile{n}" for n in range(1, 6))
        # Bereich Zeile 2-4, ans Ende springen, um zwei Zeilen hochschieben.
        grid = module.replay(painted + "\x1b[2;4r\x1b[4;1H\x1b[2S",
                             cols=12, rows=5)
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
        painted = "\x1b[H\x1b[2J" + "".join(
            f"\x1b[{n};1Hzeile{n}" for n in range(1, 6))
        grid = module.replay(painted + "\x1b[2;4r\x1b[2;1H\x1b[1T",
                             cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in grid]
        self.assertEqual(lines[0], "zeile1")
        self.assertEqual(lines[1], "")
        self.assertEqual(lines[2], "zeile2")
        self.assertEqual(lines[3], "zeile3")
        self.assertEqual(lines[4], "zeile5")

    def test_screen_replay_scrolling_further_than_the_region_clears_it(self):
        module, _ = self._make_screens_module()
        painted = "\x1b[H\x1b[2J" + "".join(
            f"\x1b[{n};1Hzeile{n}" for n in range(1, 6))
        grid = module.replay(painted + "\x1b[2;4r\x1b[4;1H\x1b[9S",
                             cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in grid]
        self.assertEqual(lines[1:4], ["", "", ""])
        self.assertEqual([lines[0], lines[4]], ["zeile1", "zeile5"])

    def test_screen_replay_inserts_and_deletes_lines_inside_the_region(self):
        module, _ = self._make_screens_module()
        painted = "\x1b[H\x1b[2J" + "".join(
            f"\x1b[{n};1Hzeile{n}" for n in range(1, 6))
        # ESC[M loescht die Cursorzeile, alles darunter rueckt hoch.
        deleted = module.replay(painted + "\x1b[2;1H\x1b[1M", cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in deleted]
        self.assertEqual(lines, ["zeile1", "zeile3", "zeile4", "zeile5", ""])
        # ESC[L schiebt sie nach unten und laesst eine leere Zeile zurueck.
        inserted = module.replay(painted + "\x1b[2;1H\x1b[1L", cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in inserted]
        self.assertEqual(lines, ["zeile1", "", "zeile2", "zeile3", "zeile4"])

    def test_setting_the_region_puts_the_cursor_home_like_a_terminal(self):
        module, _ = self._make_screens_module()
        grid = module.replay("\x1b[H\x1b[2J\x1b[5;1Hunten\x1b[1;5rX",
                             cols=12, rows=5)
        lines = ["".join(cell.ch for cell in row).rstrip() for row in grid]
        self.assertEqual(lines[0], "X")

    def test_screen_replay_normalizes_the_random_transfer_alias(self):
        module, _ = self._make_screens_module()
        grid = module.replay(
            "gmf-pin-" + "a" * 32 + "://approved", cols=64, rows=1)
        line = "".join(cell.ch for cell in grid[0]).rstrip()
        self.assertEqual(
            line, "gmf-pin-" + "0" * 32 + "://approved")

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
        self.assertIsNone(grid[0][4].bg)                 # nach ESC[0m wieder normal
        self.assertIn(f'fill="{module.ANSI_BG[41]}"', module.to_svg(grid, "t"))

    def test_screenshot_settle_and_owned_tmpdir_are_wired(self):
        module, source = self._make_screens_module()
        owned = []

        def fake_render(args, keys, settle, tmpdir, cols, rows,
                        ready_marker, key_markers):
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
        self.assertNotIn('glob("gmf-demo-', source.read_text())

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
                read_fd, quiet=0.01, cap=1.0, first_wait=0.2,
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
        with mock.patch.object(module.select, "select",
                               return_value=([123], [], [])), \
                mock.patch.object(module.os, "read", side_effect=read_error):
            with self.assertRaisesRegex(OSError, "bad file descriptor"):
                module._read_pty_until(123, cap=0.1, first_wait=0.1)

    def test_pty_ioctl_failure_still_closes_and_reaps_the_child(self):
        module, _ = self._make_screens_module()
        with mock.patch.object(module.pty, "fork", return_value=(123, 456)), \
                mock.patch.object(module.fcntl, "ioctl", side_effect=OSError("ioctl")), \
                mock.patch.object(module.os, "write", side_effect=OSError), \
                mock.patch.object(module.os, "close") as close, \
                mock.patch.object(module.os, "kill") as kill, \
                mock.patch.object(module.os, "waitpid", return_value=(123, 0)) as waitpid, \
                mock.patch.object(module, "_descendant_pids", return_value=[]), \
                mock.patch.object(module.time, "sleep"):
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
        with mock.patch.object(module.os, "kill") as kill, \
                mock.patch.object(module.os, "waitpid", side_effect=waits) as waitpid, \
                mock.patch.object(module.time, "sleep"):
            module._terminate_pty_child(123)

        self.assertEqual(kill.call_args_list, [
            mock.call(123, module.signal.SIGTERM),
            mock.call(123, module.signal.SIGKILL),
        ])
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
            [sys.executable, "-c",
             "import subprocess, sys, time;"
             "child = subprocess.Popen(['/bin/sleep', '30'],"
             " start_new_session=True);"
             "print(child.pid, flush=True);"
             "time.sleep(30)"],
            stdout=subprocess.PIPE, text=True, start_new_session=True)
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
            "time.sleep(30)\n")

        grandchild = None
        try:
            with mock.patch.object(module, "GMF", stand_in):
                module._render_in_pty([pid_file], b"", 0.2, owned_tmp,
                                      ready_marker=b"READY")
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
                ("compact.de.svg", "de"),
                ("command-log.de.svg", "de"),
                ("overview.de.svg", "de"),
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

            status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                    capture_output=True, text=True).stdout
            self.assertIn("UU README.md", status)          # Konflikt im richtigen Repo
            stashes = subprocess.run(["git", "-C", str(repo), "stash", "list"],
                                     capture_output=True, text=True).stdout
            self.assertEqual(len(stashes.splitlines()), 1)  # Stash bleibt erhalten

    def test_demo_conflict_call_reports_every_other_exit_code(self):
        """Erwartet ist genau Exit 1 (Konflikt). Läuft der Aufruf glatt durch
        oder scheitert er anders, ist die Demo-Szene kaputt und muss auffallen."""
        with tempfile.TemporaryDirectory() as temp:
            repo, _ = gmf_module._demo_repo(Path(temp), "sauber")
            with self.assertRaises(subprocess.CalledProcessError):
                gmf_module._dgit_conflict(repo, "stash", "list")     # Exit 0
            with self.assertRaises(subprocess.CalledProcessError):
                gmf_module._dgit_conflict(repo, "rev-parse", "--verify",
                                          "gibtesnicht")             # Exit 128

    def test_demo_commits_ignore_the_callers_git_environment(self):
        """Demo-Commit-IDs sind ein Vertrag (Bild-Check): GIT_*-Variablen der
        aufrufenden Shell dürfen Identität und damit die IDs nicht verschieben."""
        sabotage = {"GIT_AUTHOR_NAME": "Evil", "GIT_AUTHOR_EMAIL": "evil@example.invalid",
                    "GIT_COMMITTER_NAME": "Evil", "GIT_COMMITTER_EMAIL": "evil@example.invalid"}
        heads = []
        for env in (sabotage, {}):
            with tempfile.TemporaryDirectory() as temp:
                with mock.patch.dict(os.environ, env):
                    repo, _ = gmf_module._demo_repo(Path(temp), "probe")
                head = subprocess.run(
                    ["git", "-C", str(repo), "log", "-1", "--format=%H %an %ae"],
                    check=True, capture_output=True, text=True).stdout.split()
                heads.append(head)
        self.assertEqual(heads[0], heads[1])
        self.assertEqual(heads[0][1:], ["Demo", "demo@example.invalid"])

    @unittest.skipUnless(shutil.which("zsh"), "zsh unavailable")
    def test_installer_quotes_weird_clone_path(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo = base / 'My Projects; "quoted"'
            repo.mkdir()
            shutil.copy2(source / "install.sh", repo / "install.sh")
            shutil.copy2(source / "gmf.zsh", repo / "gmf.zsh")
            fakebin = base / "bin"; fakebin.mkdir()
            fake_python = fakebin / "python3"
            fake_python.write_text("#!/bin/sh\nexit 0\n"); fake_python.chmod(0o755)
            zdot = base / "zdot"; zdot.mkdir()
            env = dict(os.environ, HOME=str(base / "home"), ZDOTDIR=str(zdot),
                       PATH=str(fakebin) + os.pathsep + os.environ["PATH"])
            result = subprocess.run(["zsh", str(repo / "install.sh")], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            sourced = subprocess.run(
                ["zsh", "-fc", 'source "$1"; print -r -- "$GMF_SCRIPT"', "zsh", str(zdot / ".zshrc")],
                env=env, capture_output=True, text=True)
            self.assertEqual(sourced.returncode, 0, sourced.stderr)
            self.assertEqual(Path(sourced.stdout.strip()).resolve(),
                             (repo / "gitmaster_flash.py").resolve())


class VersionInOutputTests(unittest.TestCase):
    """Die Version muss in JEDER Ausgabe stehen (seit 0.6.0).

    Zweck: Wer zwei Ausgaben von verschiedenen Macs vergleicht, muss sehen, ob
    dieselbe Fassung dahintersteckt — sonst haelt man einen Versionsunterschied fuer
    einen echten Repo-Unterschied. Betrifft auch eine laufende Instanz, die den Code
    von ihrem Start zeigt, waehrend der Fleet-Sync im Hintergrund schon aktualisiert hat.
    """

    SCRIPT = str(Path(__file__).resolve().parent.parent / "gitmaster_flash.py")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = Path(cls.tmp.name) / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "f.txt").write_text("x")
        git(repo, "add", "f.txt")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_gmf(self, *args):
        return subprocess.run([sys.executable, self.SCRIPT, *args, self.tmp.name],
                              capture_output=True, text=True)

    def test_version_flag(self):
        r = subprocess.run([sys.executable, self.SCRIPT, "--version"],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), __version__)

    def test_list_kopfzeile_nennt_version(self):
        out = self.run_gmf("--list").stdout
        self.assertIn(f"gitmaster_flash {__version__}", out.splitlines()[0])

    def test_json_traegt_version_und_root(self):
        d = json.loads(self.run_gmf("--json").stdout)
        self.assertEqual(d["version"], __version__)
        # resolve() auf beiden Seiten: auf macOS ist /var ein Symlink auf /private/var,
        # das Tool loest den Pfad auf — beide meinen dasselbe Verzeichnis.
        self.assertEqual(Path(d["root"]).resolve(), Path(self.tmp.name).resolve())
        self.assertIsInstance(d["repos"], list)

    def test_json_repo_felder_unveraendert(self):
        """Das Wrapper-Objekt darf die Repo-Eintraege selbst nicht veraendert haben."""
        d = json.loads(self.run_gmf("--json").stdout)
        self.assertTrue(d["repos"])
        for feld in ("rel", "path", "branch", "clean_and_synced", "remotes"):
            self.assertIn(feld, d["repos"][0])


if __name__ == "__main__":
    unittest.main()
