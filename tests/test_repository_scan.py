import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from gmf_test_support import FakeScreen, git, latest_stash, stash_preview

import gitmaster_flash as gmf_module
from gitmaster_flash import (
    DEFAULT_CONFIG,
    TUI,
    ChangedFile,
    RepoStatus,
    __version__,
    build_info_view,
    collect_status,
    commit_selected,
    file_diff,
    find_repos,
    inspect_transfer,
    parse_porcelain,
    read_branches,
    repo_info_lines,
    status_dict,
    upstream_delta,
)


class TestParsePorcelain(unittest.TestCase):
    def test_counts_and_letters(self):
        output = "\0".join(
            [
                " M geändert.py",
                "M  gestaged.py",
                " D geloescht.txt",
                "?? neu.md",
                "?? unterordner/noch-neu.md",
                "A  hinzugefuegt.py",
                "",
            ]
        )
        m, d, u, c, files = parse_porcelain(output)
        self.assertEqual((m, d, u, c), (3, 1, 2, 0))
        self.assertIn(ChangedFile("U", "neu.md", "??"), files)
        self.assertIn(ChangedFile("D", "geloescht.txt", " D"), files)
        self.assertIn(ChangedFile("M", "gestaged.py", "M "), files)

    def test_raw_status_survives_the_simplification(self):
        # Die Anzeige kürzt alles auf M — fürs Zurücksetzen muss aber
        # unterscheidbar bleiben, WO die Änderung liegt.
        output = "\0".join(
            [
                " M nur-arbeitsbaum.py",
                "M  nur-index.py",
                "MM beides.py",
                "A  neu-hinzugefuegt.py",
                "",
            ]
        )
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
        self.assertIn(ChangedFile("U", "übungen/Abendsession 2026-07-21.pdf", "??"), files)
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
        self.assertEqual(files[0]._replace(rename_group=""), ChangedFile("M", "neu ü.txt", "R "))
        self.assertEqual(files[1]._replace(rename_group=""), ChangedFile("D", "alt ü.txt", "R "))
        self.assertEqual(files[2], ChangedFile("M", "danach.txt", " M"))

    def test_copy_does_not_invent_a_deletion(self):
        # Bei einer Kopie bleibt die Quelle unverändert liegen.
        output = "C  kopie.txt\0quelle.txt\0"
        modified, deleted, _, _, files = parse_porcelain(output)
        self.assertEqual((modified, deleted), (1, 0))
        self.assertEqual(files, [ChangedFile("M", "kopie.txt", "C ")])

    def test_empty(self):
        self.assertEqual(parse_porcelain(""), (0, 0, 0, 0, []))


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

    def test_all_five_levels_keep_their_order(self):
        """Die vollstaendige Reihenfolge, nicht nur "dreckig vor sauber".

        Die Namen sind absichtlich GEGEN die Sollreihenfolge gewaehlt: Sonst
        deckte der alphabetische Zweitschluessel eine falsche Stufe zu.
        """
        stufen = [
            ("zz-kaputt", dict(error="boom")),
            ("mm-dreckig", dict(modified=1)),
            ("ll-stash", dict(stashes=["stash@{0} WIP"])),
            ("aa-voraus", dict(ahead=1)),
            ("yy-ohne-remote", dict(remote_state="no-remote")),
            ("bb-sauber", {}),
        ]
        statuses = [RepoStatus(path=Path("/x") / rel, rel=rel, **kw) for rel, kw in stufen]
        self.assertEqual([s.severity() for s in statuses], [0, 1, 1, 2, 3, 4])
        self.assertEqual(
            [s.rel for s in gmf_module.sort_statuses(statuses)],
            ["zz-kaputt", "ll-stash", "mm-dreckig", "aa-voraus", "yy-ohne-remote", "bb-sauber"],
        )


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
        with (
            mock.patch("gitmaster_flash.find_repos", return_value=[]),
            mock.patch("gitmaster_flash.concurrent.futures.ThreadPoolExecutor") as pool,
        ):
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
            check=True,
            capture_output=True,
            text=True,
        )
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
            check=True,
            capture_output=True,
            text=True,
        )
        git(self.repo, "remote", "add", "origin", str(remote))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        drawn = []

        class Screen(FakeScreen):
            def getmaxyx(self):
                return (30, 100)

            def addstr(self, y, x, text, *a):
                drawn.append((y, text))

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            refreshed = ui._fetch_remote(st, "origin")

        self.assertIsNotNone(refreshed)
        busy = gmf_module.t("fetch_busy", r="origin")
        self.assertTrue(any(y == 29 and text.startswith(busy) for y, text in drawn), drawn)

    def test_transfer_fetch_uses_every_fetch_guard(self):
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        git(self.repo, "remote", "add", "origin", str(remote))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        class Screen(FakeScreen):
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        calls = []
        original = gmf_module.run_git

        def remember(repo, *args, **kwargs):
            calls.append(args)
            return original(repo, *args, **kwargs)

        with (
            mock.patch.object(gmf_module, "run_git_logged", side_effect=remember),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            refreshed = ui._fetch_remote(st, "origin")

        self.assertIsNotNone(refreshed)
        fetches = [args for args in calls if "fetch" in args]
        self.assertEqual(len(fetches), 1, calls)
        for option in (
            "--jobs=1",
            "--no-tags",
            "--no-prune-tags",
            "--no-recurse-submodules",
            "--refmap=",
        ):
            self.assertIn(option, fetches[0])

    def test_fetch_never_dereferences_a_symbolic_tracking_ref(self):
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        git(self.repo, "remote", "add", "origin", str(remote))
        branch = git(self.repo, "symbolic-ref", "--short", "HEAD")
        before = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "branch", "victim", before)
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", before],
            input="remote tip\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git(self.repo, "push", "-q", str(remote), f"{remote_tip}:refs/heads/{branch}")
        # Auch der Fixture-Push aktualisiert passende Tracking-Refs. Den
        # Symref erst danach setzen, damit wirklich der Fetch geprüft wird.
        git(self.repo, "symbolic-ref", f"refs/remotes/origin/{branch}", "refs/heads/victim")
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/victim"), before)
        config = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["origin"]

        gmf_module.fetch_remote_safely(self.repo, config, branch, DEFAULT_CONFIG["fetch_timeout"])

        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/victim"), before)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)

    def test_transfer_fetch_blocks_newly_invalid_remote_urls(self):
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        git(self.repo, "remote", "add", "origin", str(remote))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        class Screen:
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        for invalid in (
            "https://example.invalid:not-a-port/repo.git",
            "~gmf-user-that-does-not-exist/repo.git",
        ):
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
                check=True,
                capture_output=True,
                text=True,
            )
        git(self.repo, "remote", "add", "origin", str(first))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)

        class Screen(FakeScreen):
            def getmaxyx(self):
                return (30, 100)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]

        def change_config(*_args, **_kwargs):
            git(self.repo, "remote", "set-url", "origin", str(second))
            return subprocess.CompletedProcess(["git", "fetch"], 0, "", "")

        with (
            mock.patch.object(gmf_module, "fetch_remote_safely", side_effect=change_config),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            refreshed = ui._fetch_remote(st, "origin")

        self.assertIsNone(refreshed)
        self.assertEqual(ui.message, gmf_module.t("transfer_changed"))

    def test_fetch_rolls_back_its_tracking_ref_after_config_change(self):
        first = self.root / "first.git"
        second = self.root / "second.git"
        for remote_path in (first, second):
            subprocess.run(
                ["git", "clone", "-q", "--bare", str(self.repo), str(remote_path)],
                check=True,
                capture_output=True,
                text=True,
            )
        git(self.repo, "remote", "add", "origin", str(first))
        branch = git(self.repo, "symbolic-ref", "--short", "HEAD")
        old_oid = git(self.repo, "rev-parse", "HEAD")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", old_oid],
            input="remote tip\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git(self.repo, "push", "-q", str(first), f"{remote_tip}:refs/heads/{branch}")
        # Ausgangs-Tracking-Ref gehört noch zum alten, freigegebenen Ziel.
        git(self.repo, "update-ref", f"refs/remotes/origin/{branch}", old_oid)
        config = gmf_module.read_remote_configs(self.repo, DEFAULT_CONFIG)["origin"]
        original = gmf_module.run_git_logged
        changed = False
        restored_name = False

        def change_after_update(repo, *args, **kwargs):
            nonlocal changed, restored_name
            result = original(repo, *args, **kwargs)
            if "update-ref" in args and remote_tip in args and not changed:
                changed = True
                git(self.repo, "remote", "set-url", "origin", str(second))
            elif changed and "update-ref" in args and old_oid in args and not restored_name:
                # ABA: Vor der äußeren Nachprüfung sieht die Config wieder wie
                # am Anfang aus. Das interne Changed-Signal muss trotzdem leben.
                restored_name = True
                git(self.repo, "remote", "set-url", "origin", str(first))
            return result

        with (
            mock.patch.object(gmf_module, "run_git_logged", side_effect=change_after_update),
            self.assertRaises(gmf_module.RemoteConfigChangedError),
        ):
            gmf_module.fetch_remote_safely(self.repo, config, branch, 10)

        self.assertTrue(changed)
        self.assertTrue(restored_name)
        self.assertEqual(git(self.repo, "rev-parse", f"refs/remotes/origin/{branch}"), old_oid)

    def test_batch_fetch_marks_a_timed_out_tracking_cas_as_unknown(self):
        remote_path = self.root / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(remote_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        git(self.repo, "remote", "add", "origin", str(remote_path))
        branch = git(self.repo, "symbolic-ref", "--short", "HEAD")
        before = git(self.repo, "rev-parse", "HEAD")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        remote_tip = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", before],
            input="remote tip\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git(self.repo, "push", "-q", str(remote_path), f"{remote_tip}:refs/heads/{branch}")
        original = gmf_module.run_git_logged
        timed_out = False

        def update_then_timeout(repo, *args, **kwargs):
            nonlocal timed_out
            result = original(repo, *args, **kwargs)
            if "update-ref" in args and remote_tip in args and not timed_out:
                timed_out = True
                raise subprocess.TimeoutExpired(["git", "-C", str(repo), "update-ref"], 1)
            return result

        with mock.patch.object(gmf_module, "run_git_logged", side_effect=update_then_timeout):
            status = collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        self.assertTrue(timed_out)
        self.assertEqual(git(self.repo, "rev-parse", f"refs/remotes/origin/{branch}"), remote_tip)
        remote = next(item for item in status.remotes if item.name == "origin")
        self.assertEqual(remote.fetch_outcome, "outcome_unknown")
        self.assertIn("may already have changed", remote.fetch_error_long)

    def test_parent_fetch_never_recurses_into_unchecked_submodule_config(self):
        parent_remote = self.root / "parent.git"
        git(self.root, "init", "-q", "--bare", "--initial-branch=main", str(parent_remote))
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
        git(self.root, "init", "-q", "--bare", "--initial-branch=main", str(sub_remote))
        git(sub_source, "remote", "add", "origin", str(sub_remote))
        git(sub_source, "push", "-qu", "origin", "main")
        subprocess.run(
            [
                "git",
                "-c",
                "protocol.file.allow=always",
                "-C",
                str(self.repo),
                "submodule",
                "add",
                "-q",
                str(sub_remote),
                "sub",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
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
        git(sub, "config", "--add", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
        git(self.repo, "config", "fetch.recurseSubmodules", "true")

        collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(sub), "rev-parse", "refs/heads/victim"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            before,
        )

    def test_fetch_never_prunes_local_tags_from_configuration(self):
        """`remote.*.pruneTags` darf die Zusage "nur Tracking-Refs" nicht umgehen."""
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "tag", "local-only")
        git(self.repo, "config", "remote.origin.pruneTags", "true")

        collect_status(self.repo, self.root, DEFAULT_CONFIG, fetch=True)

        result = gmf_module.run_git(
            self.repo, "show-ref", "--verify", "--quiet", "refs/tags/local-only", timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_scan_keeps_full_parallelism(self):
        with (
            mock.patch("gitmaster_flash.find_repos", return_value=[]),
            mock.patch("gitmaster_flash.concurrent.futures.ThreadPoolExecutor") as pool,
        ):
            gmf_module.collect_all(self.root, DEFAULT_CONFIG, fetch=False)
        pool.assert_called_once_with(max_workers=12)

    def test_status_dirty_untracked_stash(self):
        (self.repo / "a.md").write_text("geändert\n")
        (self.repo / "neu.txt").write_text("neu\n")
        git(self.repo, "stash", "--include-untracked")
        (self.repo / "wieder.txt").write_text("x\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.rel, "demo")
        self.assertEqual(st.untracked, 1)  # wieder.txt
        self.assertEqual(len(st.stashes), 1)  # der Stash von oben
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
            capture_output=True,
            text=True,
        )
        self.assertEqual(tracked.returncode, 0, tracked.stderr)

    def test_stash_pop_conflict_keeps_stash(self):
        # Ein Stash, dessen Änderungen mit dem inzwischen committeten Stand
        # kollidieren -> pop erzeugt einen Konflikt,
        # der Stash bleibt erhalten. Das Tool muss das als Konflikt (C) erkennen,
        # nicht als simple Modifikation (M).
        (self.repo / "a.md").write_text("stash-variante\n")
        git(self.repo, "stash")  # Stash mit Änderung an a.md
        (self.repo / "a.md").write_text("andere-variante\n")  # kollidierende Änderung
        git(self.repo, "commit", "-qam", "kollidierender Commit")
        # pop schlägt fehl (Konflikt); Rückgabecode != 0, Stash bleibt
        res = subprocess.run(
            ["git", "-C", str(self.repo), "stash", "pop"], capture_output=True, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.conflicts, 1)
        self.assertEqual(st.modified, 0)
        self.assertEqual(len(st.stashes), 1)  # Stash NICHT verloren
        self.assertTrue(st.dirty)

    def test_status_clean_without_remote(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertFalse(st.dirty)
        # sauber, aber ohne Sync-Remote -> nicht "synchron"
        self.assertFalse(st.clean_and_synced)
        self.assertEqual(st.branch, "main" if st.branch == "main" else st.branch)

    def test_repo_info_contains_clickable_github_url_and_useful_details(self):
        git(self.repo, "tag", "v1.0")
        git(self.repo, "remote", "add", "origin", "https://github.com/example/demo.git")
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
        tree = git(self.repo, "write-tree")
        head = git(self.repo, "rev-parse", "HEAD")
        index_commit = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", head],
            input="stash index\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        stash_commit = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree, "-p", head, "-p", index_commit],
            input="stash worktree\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git(self.repo, "stash", "store", "-m", "gitlink update", stash_commit)
        git(self.repo, "config", "diff.ignoreSubmodules", "all")
        git(self.repo, "config", "submodule.vendor/sub.ignore", "all")
        oid, _ = latest_stash(self.repo, 10)

        ok, preview = stash_preview(self.repo, 10, oid)

        self.assertTrue(ok, preview)
        self.assertIn("Subproject commit", preview)


class ReplaceRefCountingTests(unittest.TestCase):
    """Ein `refs/replace/*` darf die angezeigten Zahlen nicht verschieben.

    Ein Replace-Ref deutet den Commit-Graph um. Die Zähler der Anzeige lasen
    ihn mit, der Push-Preflight (`inspect_transfer`, `RAW_OBJECT_ENV`) nicht:
    Das Abzeichen sagte „↓3" — also erst pullen —, während `P` zugleich
    „bereit, 2 Commits" meldete. Und `--diff` machte daraus einen Unterschied
    zwischen zwei Rechnern, den nur ein rein lokaler Kunstgriff erzeugt.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.bare = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", str(self.bare))
        self.repo = self.root / "work"
        git(self.root, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        git(self.repo, "remote", "add", "origin", str(self.bare))
        for i in range(1, 5):
            (self.repo / "f.txt").write_text(f"{i}\n")
            git(self.repo, "add", "f.txt")
            git(self.repo, "commit", "-qm", f"c{i}")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        for i in (5, 6):
            (self.repo / "f.txt").write_text(f"{i}\n")
            git(self.repo, "add", "f.txt")
            git(self.repo, "commit", "-qm", f"c{i}")
        git(self.repo, "fetch", "-q", "origin")
        # c5 bekommt den Wurzel-Commit als Elter: Git sieht danach drei
        # Commits auf origin/main, die es hier scheinbar nicht gibt.
        root_commit = git(self.repo, "rev-list", "--max-parents=0", "HEAD")
        git(self.repo, "replace", "--graft", git(self.repo, "rev-parse", "HEAD~1"), root_commit)

    def tearDown(self):
        self.tmp.cleanup()

    def test_git_itself_really_is_fooled_by_the_replace_ref(self):
        """Ohne die Sperre zählt Git tatsächlich falsch — sonst prüft der Test nichts."""
        self.assertEqual(
            git(
                self.repo, "rev-list", "--left-right", "--count", "HEAD...refs/remotes/origin/main"
            ).split(),
            ["2", "3"],
        )

    def test_the_displayed_counts_describe_the_real_history(self):
        self.assertEqual(gmf_module.branch_delta(self.repo, "refs/remotes/origin/main", 10), (2, 0))
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual((st.ahead, st.behind), (2, 0))
        self.assertEqual([(r.ahead, r.behind) for r in st.remotes], [(2, 0)])

    def test_display_and_transfer_preflight_agree(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        check = inspect_transfer(self.repo, "origin", "main", "push", 10)
        self.assertEqual(check.reason, "ready")
        self.assertEqual((st.ahead, st.behind), (check.ahead, check.behind))

    def test_branch_list_and_upstream_delta_use_the_real_history(self):
        self.assertEqual(
            [(b.name, b.ahead, b.behind) for b in read_branches(self.repo, DEFAULT_CONFIG)],
            [("main", 2, 0)],
        )
        self.assertEqual(upstream_delta(self.repo, None, DEFAULT_CONFIG), ("origin/main", 2, 0))

    def test_a_shallow_clone_is_still_counted_instead_of_broken(self):
        """Gegenprobe: Nur die Replace-Sperre, nicht die ganze Härtung.

        `RAW_OBJECT_ENV` leert zusätzlich die Shallow-Datei. In einem flachen
        Klon scheitert dann schon `git rev-list --count HEAD` mit Exit 128 —
        gmf meldete das Repo als kaputt, statt es zu zählen.
        """
        shallow = self.root / "shallow"
        git(
            self.root,
            "clone",
            "-q",
            "--depth",
            "1",
            "--branch",
            "main",
            "file://" + str(self.bare),
            str(shallow),
        )
        self.assertEqual(git(shallow, "rev-parse", "--is-shallow-repository"), "true")
        st = collect_status(shallow, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.error, "")
        self.assertEqual((st.ahead, st.behind), (0, 0))
        self.assertIn("shallow clone", "\n".join(repo_info_lines(st, DEFAULT_CONFIG)))


class UnbornBranchTests(unittest.TestCase):
    """Ein geklontes LEERES Repo, dessen Remote inzwischen Commits hat.

    HEAD zeigt dort auf einen Branch, den es noch nicht gibt; nach einem Fetch
    existiert der Tracking-Ref aber sehr wohl. `git rev-list HEAD...<ref>`
    bricht in dieser Lage mit Exit 128 ab. Bis 2026-09-03 meldete gmf das Repo
    deshalb als kaputt ("ERROR: git rev-list failed (exit 128)"), statt die
    einzig richtige Zahl zu zeigen: null voraus, n zurück.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        bare = self.root / "up.git"
        git(self.root, "init", "-q", "--bare", str(bare))
        self.repo = self.root / "work"
        git(self.root, "clone", "-q", str(bare), str(self.repo))
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        # Zweiter Klon schiebt die ersten beiden Commits hoch; danach fetcht
        # der leere Klon nur den Tracking-Ref, sein HEAD bleibt ungeboren.
        other = self.root / "other"
        git(self.root, "clone", "-q", str(bare), str(other))
        git(other, "config", "user.email", "t@example.invalid")
        git(other, "config", "user.name", "T")
        self.branch = git(other, "symbolic-ref", "--short", "HEAD")
        git(other, "commit", "-q", "--allow-empty", "-m", "eins")
        git(other, "commit", "-q", "--allow-empty", "-m", "zwei")
        git(other, "push", "-q", "origin", f"HEAD:refs/heads/{self.branch}")
        git(self.repo, "fetch", "-q")
        shutil.rmtree(other)

    def tearDown(self):
        self.tmp.cleanup()

    def test_head_is_really_unborn_while_the_tracking_ref_exists(self):
        self.assertEqual(git(self.repo, "symbolic-ref", "--short", "HEAD"), self.branch)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "--verify", "-q", "HEAD"],
                capture_output=True,
            ).returncode,
            1,
        )
        self.assertIn(f"refs/remotes/origin/{self.branch}", git(self.repo, "show-ref"))

    def test_unborn_head_counts_the_remote_commits_as_behind(self):
        ahead, behind = gmf_module.branch_delta(self.repo, f"refs/remotes/origin/{self.branch}", 10)
        self.assertEqual((ahead, behind), (0, 2))

    def test_the_repo_is_reported_as_behind_instead_of_broken(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertEqual(st.error, "")
        self.assertEqual(st.remote_state, "ok")
        self.assertEqual((st.ahead, st.behind), (0, 2))
        self.assertFalse(st.clean_and_synced)

    def test_a_real_read_error_still_counts_as_one(self):
        """Der neue Zweig darf keinen echten Git-Fehler verschlucken.

        Nur ein ungeborener HEAD rechtfertigt die Ersatzzählung. Existiert HEAD,
        bleibt ein fehlgeschlagenes `rev-list` ein Lesefehler.
        """
        failed = subprocess.CompletedProcess(["git", "rev-list"], 128, "", "boom")
        with (
            mock.patch.object(gmf_module, "run_git", return_value=failed),
            mock.patch.object(gmf_module, "repo_has_head", return_value=True),
        ):
            with self.assertRaises(gmf_module.GitReadError) as raised:
                gmf_module.branch_delta(self.repo, f"refs/remotes/origin/{self.branch}", 10)
        self.assertIn("rev-list", str(raised.exception))


class DisplayNameNormalisationTests(unittest.TestCase):
    """`collect_status()` normalisiert den Anzeigenamen auf NFC.

    Darauf beruhen die Spaltenbreiten (bei NFD verrechnet sich `len()`), die
    Vergleichbarkeit der `--json`-Ausgabe zwischen Rechnern und die ganze
    Entscheidung, Repos ueber `path` statt ueber `rel` wiederzuerkennen. Alle
    NFC/NFD-Tests bauten ihre `RepoStatus` bisher von Hand — die Zeile selbst
    hatte keinen.
    """

    def test_a_folder_written_in_nfd_gets_an_nfc_display_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            nfd = "cafe\u0301"
            repo = root / nfd
            git(root, "init", "-q", "-b", "main", str(repo))
            st = collect_status(repo, root, DEFAULT_CONFIG)
            self.assertEqual(st.rel, unicodedata.normalize("NFC", nfd))
            self.assertTrue(unicodedata.is_normalized("NFC", st.rel))
            # Der echte Pfad bleibt unangetastet — er ist der Schluessel.
            self.assertEqual(st.path, repo)


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
        git(self.repo, "branch", "fertig")  # gemergt (zeigt auf HEAD)
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
        branch = next(b for b in read_branches(self.repo, DEFAULT_CONFIG) if b.name == "main")
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
        driver.write_text("#!/bin/sh\nprintf ran > " + shlex.quote(str(marker)) + "\n")
        driver.chmod(0o755)
        (self.repo / ".gitattributes").write_text("a.md diff=danger\n")
        git(self.repo, "add", ".gitattributes")
        git(self.repo, "commit", "-qm", "diff attributes")
        git(self.repo, "config", "diff.danger.textconv", str(driver))
        (self.repo / "a.md").write_text("geändert\n")

        with mock.patch.dict(os.environ, {"GIT_EXTERNAL_DIFF": str(driver)}):
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
                ["git", "-C", str(repo), "show", "HEAD:new.txt"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(committed, "current version\nlatest line\n")

    def test_file_diff_ignores_a_replacement_for_head(self):
        (self.repo / "a.md").write_text("changed\n")
        git(self.repo, "add", "a.md")
        git(self.repo, "commit", "-qm", "replacement tree")
        replacement = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "reset", "--mixed", "-q", "HEAD^")
        real_head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "replace", real_head, replacement)

        ok, preview = file_diff(self.repo, "M", "a.md", 10)
        result = commit_selected(self.repo, ["a.md"], "real change", 10)

        self.assertTrue(ok, preview)
        self.assertIn("+changed", preview)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.repo, "show", "HEAD:a.md"), "changed")

    def test_untracked_directory_is_expanded_to_committable_files(self):
        """Der Hauptscan bindet die Freigabe an einzelne neue Dateien."""
        (self.repo / "neu-dir").mkdir()
        (self.repo / "neu-dir" / "datei.txt").write_text("inhalt\n")
        git(self.repo, "config", "status.showUntrackedFiles", "no")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        self.assertIn(ChangedFile("U", "neu-dir/datei.txt", "??"), st.files)
        self.assertEqual(status_dict(st)["untracked"], 1)
        result = commit_selected(self.repo, ["neu-dir/datei.txt"], "add nested file", 10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changes_view_shows_diff_for_the_selected_file(self):
        (self.repo / "a.md").write_text("geändert\n")
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        shown = {}

        class Screen(FakeScreen):
            def __init__(self):
                self.keys = iter([10, ord("q")])  # ⏎ Diff, dann zurück

            def getmaxyx(self):
                return (30, 100)

            def getch(self):
                return next(self.keys)

        ui = TUI(Screen(), self.root, DEFAULT_CONFIG, None)
        ui.statuses = [st]
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch.object(
                TUI,
                "show_pager",
                side_effect=lambda title, lines: shown.update(title=title, lines=lines),
            ),
        ):
            ui.action_file_changes()
        self.assertIn("a.md", shown["title"])
        self.assertTrue(any(line.startswith("+geändert") for line in shown["lines"]))

    def test_app_open_is_refused_over_ssh(self):
        st = collect_status(self.repo, self.root, DEFAULT_CONFIG)
        cfg = {**DEFAULT_CONFIG, "apps": {"E": {"name": "Editor", "path": "/"}}}
        ui = TUI(None, self.root, cfg, None)
        ui.statuses = [st]
        with (
            mock.patch.dict(os.environ, {"SSH_CONNECTION": "1.2.3.4 1 5.6.7.8 22"}),
            mock.patch("gitmaster_flash.subprocess.run") as run,
        ):
            ui.action_open_app("E")
        run.assert_not_called()
        self.assertIn("SSH", ui.message)


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
                self.assertEqual(
                    [s.rel for s in gmf_module.filter_statuses(statuses, query)], names
                )

    def test_query_is_a_substring_of_the_path_not_a_prefix(self):
        statuses = [self._st("arbeit/kunde/api-server"), self._st("privat/blog")]
        hit = gmf_module.filter_statuses(statuses, "api")
        self.assertEqual([s.rel for s in hit], ["arbeit/kunde/api-server"])

    def test_all_terms_must_match_in_any_order(self):
        statuses = [
            self._st("arbeit/kunde/api-server"),
            self._st("arbeit/blog"),
            self._st("privat/api-spielwiese"),
        ]
        # Beide Begriffe zusammen treffen nur das eine Repo — und die Reihenfolge
        # der Begriffe darf daran nichts ändern.
        for query in ("arbeit api", "api arbeit"):
            with self.subTest(query=query):
                self.assertEqual(
                    [s.rel for s in gmf_module.filter_statuses(statuses, query)],
                    ["arbeit/kunde/api-server"],
                )

    def test_a_query_in_the_other_unicode_normalisation_still_finds_the_repo(self):
        """Anzeigename und Suchtext muessen dieselbe Normalform vergleichen.

        `collect_status()` normalisiert `rel` bewusst auf NFC, damit die
        Spalten stimmen. Der Suchtext kam roh von Tastatur oder Kommandozeile,
        und macOS liefert Ordnernamen je nach Herkunft in NFD: Ein eingefuegter
        Name sah zeichengleich aus und fand sein Repo trotzdem nie — bei
        `--list` zusaetzlich mit Exit-Code 0 (Review-Fund 2026-09-10).
        """
        rel = unicodedata.normalize("NFC", "arbeit/münchen-api")
        statuses = [self._st(rel), self._st("privat/blog")]
        for form in ("NFC", "NFD"):
            with self.subTest(form=form):
                query = unicodedata.normalize(form, "München")
                self.assertEqual(
                    [x.rel for x in gmf_module.filter_statuses(statuses, query)], [rel]
                )
        # Auch andersherum: ein in NFD gespeicherter Anzeigename (etwa aus dem
        # JSON eines aelteren Rechners) muss auf eine NFC-Eingabe passen.
        nfd_repos = [{"rel": unicodedata.normalize("NFD", "arbeit/münchen-api")}]
        self.assertEqual(
            len(gmf_module.filter_repo_dicts(nfd_repos, unicodedata.normalize("NFC", "münchen"))), 1
        )
        # Gegenprobe: Ein wirklich anderer Begriff trifft weiterhin nichts.
        self.assertEqual(gmf_module.filter_statuses(statuses, "hamburg"), [])

    def test_matching_ignores_case_including_the_german_sharp_s(self):
        statuses = [self._st("Straße"), self._st("ANDERES")]
        self.assertEqual(
            [s.rel for s in gmf_module.filter_statuses(statuses, "STRASSE")], ["Straße"]
        )
        self.assertEqual(
            [s.rel for s in gmf_module.filter_statuses(statuses, "anderes")], ["ANDERES"]
        )

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
        self.assertEqual(gmf_module.filter_repo_dicts(repos, "api"), [{"rel": "arbeit/api"}])
        # Ein Repo ohne "rel" darf den Filter nicht sprengen — die Gegenseite
        # eines --diff liefert fremdes JSON, auf dessen Felder man sich nicht
        # verlassen kann.
        self.assertEqual(len(gmf_module.filter_repo_dicts(repos, "")), 3)


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
        with (
            mock.patch.object(sys, "stdout", out),
            mock.patch.object(sys, "stderr", err),
            mock.patch.object(gmf_module, "UI_LANG", "en"),
        ):
            code = gmf_module.main([str(self.root), "--lang", "en", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_list_shows_only_matching_repos_and_names_the_filter(self):
        _, out, _ = self._run("--list", "--filter", "api")
        self.assertIn("api-gateway", out)
        self.assertNotIn("blog", out)
        self.assertIn("api", out.splitlines()[0])

    def test_the_list_header_names_the_hidden_repos_that_need_attention(self):
        """Eine `✔`-Zeile mit Exit-Code 0 sah aus wie ein sauberer Bestand.

        Die Oberflaeche haengt `hdr_hidden_dirty` an ihre Kopfzeile, ausdruecklich
        weil ein Uebersichtswerkzeug nicht gerade die Repos verstecken darf,
        deretwegen man es startet. `--list` tat das nicht: `--filter api` konnte
        eine einzelne saubere Zeile zeigen, waehrend daneben dreckige Repos
        ausgeblendet waren.
        """
        (self.root / "blog" / "schmutz.txt").write_text("offen\n")

        _, out, _ = self._run("--list", "--filter", "api")

        header = out.splitlines()[0]
        self.assertIn("+1 hidden", header)
        self.assertIn("filter", header)
        # Das ausgeblendete Repo bleibt ausgeblendet — genannt wird nur, DASS es
        # da ist und Aufmerksamkeit braeuchte.
        self.assertNotIn("blog", out)

    def test_the_hint_stays_away_without_a_filter_and_without_dirty_repos(self):
        """Gegenprobe von beiden Seiten.

        Ohne Filter wird nichts ausgeblendet, und ein ausgeblendetes SAUBERES
        Repo ist kein Grund fuer den Hinweis — sonst waere die Zahl nur eine
        zweite Trefferanzeige.
        """
        _, plain, _ = self._run("--list")
        self.assertNotIn("hidden", plain.splitlines()[0])

        sauber = RepoStatus(path=Path("/tmp/sauber"), rel="sauber")
        offen = RepoStatus(path=Path("/tmp/offen"), rel="offen", modified=1)
        self.assertTrue(sauber.clean_and_synced)
        self.assertEqual(gmf_module.hidden_dirty([sauber, offen], [offen]), 0)
        self.assertEqual(gmf_module.hidden_dirty([sauber, offen], [sauber]), 1)

    def test_json_carries_only_matching_repos(self):
        _, out, _ = self._run("--json", "--filter", "api")
        payload = json.loads(out)
        self.assertEqual([r["rel"] for r in payload["repos"]], ["api-gateway"])
        self.assertEqual(
            payload["filter"],
            {
                "query": "api",
                "matches": 1,
                "total": 2,
            },
        )

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

    # -- --diff: derselbe Filter, dieselbe Ehrlichkeit ----------------------
    # Der Rechnervergleich bekam den Filter in 0.19.0 mit, aber nicht die beiden
    # Zusagen, die ihn erst lesbar machen: Ein Filter ohne Treffer meldete
    # "keine Unterschiede" mit Exit-Code 0 — nicht zu unterscheiden von zwei
    # wirklich uebereinstimmenden Rechnern — und eine aufgehobene Ausgabe
    # verriet nicht, dass ueberhaupt gefiltert wurde.

    def _diff(self, *argv, remote_repos):
        payload = {"version": gmf_module.__version__, "root": "/dort", "repos": remote_repos}
        with mock.patch.object(gmf_module, "fetch_remote_status", return_value=payload):
            return self._run("--diff", "host:/dort", *argv)

    def _remote_repo(self, rel, **fields):
        return {
            "rel": rel,
            "branch": "main",
            "remote_state": None,
            "modified": 0,
            "untracked": 0,
            "deleted": 0,
            "conflicts": 0,
            "stashes": 0,
            "remotes": [],
            **fields,
        }

    def test_a_diff_filter_without_hits_does_not_claim_agreement(self):
        code, out, err = self._diff(
            "--filter",
            "gibtsnicht",
            remote_repos=[self._remote_repo("api-gateway", branch="andere")],
        )
        # Ohne den Hinweis stuende hier nur "No differences to host." — dasselbe
        # Bild, das zwei tatsaechlich gleiche Rechner erzeugen.
        self.assertIn("gibtsnicht", err)
        self.assertEqual(code, 0)

    def test_the_diff_output_names_an_active_filter(self):
        code, out, err = self._diff(
            "--filter",
            "api",
            remote_repos=[
                self._remote_repo("api-gateway", branch="andere"),
                self._remote_repo("blog"),
            ],
        )
        # Die ganze Zeile pruefen, nicht nur "api": Der Suchtext steckt auch im
        # Repo-Namen der DRIFT-Zeile darunter — ein `assertIn` blieb gruen,
        # obwohl der Hinweis gar nicht mehr gedruckt wird.
        self.assertEqual(
            out.splitlines()[0],
            gmf_module.t("diff_filter_note", q="api", n=1, total=2, m=1, mt=2, h="host"),
        )
        # Der Hinweis ist kein Unterschied: Der Exit-Code beschreibt weiterhin
        # nur die Vergleichszeilen.
        self.assertEqual(code, 1)
        self.assertEqual(err, "")

    def test_diff_json_carries_the_filter_and_both_result_counts(self):
        code, out, err = self._diff(
            "--json",
            "--filter",
            "api",
            remote_repos=[
                self._remote_repo("api-gateway", branch="andere"),
                self._remote_repo("blog"),
            ],
        )
        payload = json.loads(out)
        self.assertEqual(
            payload["filter"],
            {
                "query": "api",
                "here_matches": 1,
                "here_total": 2,
                "host_matches": 1,
                "host_total": 2,
            },
        )
        self.assertEqual(code, 1)
        self.assertEqual(err, "")

    def test_an_unfiltered_diff_prints_no_extra_line(self):
        code, out, err = self._diff(
            remote_repos=[self._remote_repo("api-gateway"), self._remote_repo("blog")]
        )
        self.assertNotIn("filter", out.lower())
        self.assertEqual(err, "")

    def test_the_filter_text_cannot_carry_escapes_into_the_terminal(self):
        """Der Suchtext kommt roh von der Kommandozeile.

        `print_list()` schickt ihn seit jeher durch `terminal_text()`; die
        beiden Hinweiszeilen taten das nicht. Ein Filtertext mit der
        Loeschsequenz ESC[2J haette den Bildschirm des Lesers geleert.
        """
        _, out, err = self._run("--list", "--filter", "\x1b[2Japi")
        self.assertNotIn("\x1b", out + err)
        _, out, err = self._diff(
            "--filter", "\x1b[2Jgibtsnicht", remote_repos=[self._remote_repo("api-gateway")]
        )
        self.assertNotIn("\x1b", out + err)
        self.assertIn("gibtsnicht", err)

    def test_a_whitespace_only_filter_reaches_the_comparison_as_no_filter(self):
        """Kanonisiert wird einmal in `main()` — auch fuer `--diff`."""
        with mock.patch.object(gmf_module, "run_diff", return_value=0) as run:
            self._run("--diff", "host:/dort", "--filter", "   ")
        self.assertEqual(run.call_args.kwargs["query"], "")


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
        return subprocess.run(
            [sys.executable, self.SCRIPT, *args, self.tmp.name], capture_output=True, text=True
        )

    def test_version_flag(self):
        r = subprocess.run(
            [sys.executable, self.SCRIPT, "--version"], capture_output=True, text=True
        )
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
