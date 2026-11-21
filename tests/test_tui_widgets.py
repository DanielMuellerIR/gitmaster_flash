import curses
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gmf_test_support import FakeScreen

import gitmaster_flash as gmf_module
from gitmaster_flash import (
    DEFAULT_CONFIG,
    TUI,
    RemoteStatus,
    RepoStatus,
    cell_width,
    pad_cells,
)


class ConfirmDialogTests(unittest.TestCase):
    """Die Rückfrage kennt genau zwei Antworten und zeichnet sie als Balken."""

    def ask(self, keys):
        drawn = []

        class Screen(FakeScreen):
            def __init__(self):
                self.keys = iter(keys)

            def getmaxyx(self):
                return (24, 80)

            def addstr(self, y, x, text, *a):
                drawn.append((y, text))

            def getch(self):
                return next(self.keys)

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set") as curs_set,
        ):
            result = ui.confirm("Wirklich?")
        # Cursor sichtbar während der Frage, danach wieder aus.
        self.assertEqual([c.args for c in curs_set.call_args_list], [(1,), (0,)])
        return result, drawn

    def test_yes_and_no_are_unchanged(self):
        self.assertIs(self.ask([ord("j")])[0], True)
        self.assertIs(self.ask([ord("y")])[0], True)
        self.assertIs(self.ask([ord("n")])[0], False)
        self.assertIs(self.ask([27])[0], False)

    def test_any_other_key_neither_confirms_nor_cancels(self):
        # Der Dialog kennt nur Ja und Nein: Ein A darf weder zustimmen noch
        # abbrechen, sondern wartet weiter — hier bis zum folgenden N.
        self.assertIs(self.ask([ord("a"), ord("n")])[0], False)

    def test_question_is_a_bar_with_a_blank_line_above(self):
        # 24 Zeilen: Fußzeile 21-23, Frage-Balken in 20, Leerzeile in 19.
        # Der Balken beginnt in Spalte 0 mit einem Leerzeichen und füllt die
        # Breite; ohne Leerzeile ging die Frage unter einer langen Liste unter.
        _, drawn = self.ask([ord("n")])
        self.assertEqual(drawn[0], (19, " " * 79))
        self.assertEqual(drawn[1], (20, (" Wirklich?" + gmf_module.t("yesno")).ljust(79)))
        # Die Ja-Taste wird danach rot nachgezeichnet.
        self.assertEqual(drawn[2], (20, gmf_module.t("yes_key")))

    def test_the_accent_is_part_of_the_hint_in_both_languages(self):
        # Sonst zeichnet draw_question() nichts rot nach — oder das Falsche.
        for lang in ("en", "de"):
            self.assertIn(gmf_module.TR["yes_key"][lang], gmf_module.TR["yesno"][lang])
            self.assertIn(gmf_module.TR["yes_accent"][lang], gmf_module.TR["github_confirm"][lang])


class PagerConfirmTests(unittest.TestCase):
    """Die GitHub-Vorschau fragt in ihrer eigenen Ansicht nach: J/Y + ⏎
    bestätigt, alles andere bricht ab, und die Liste bleibt dabei scrollbar.
    Seit 2026-08-22 ersetzt das den getippten Satz „PUSH <remote>" nach dem
    Schließen des Pagers, der unten auf der Liste leicht übersehen wurde."""

    LINES = [f"line {i}" for i in range(1, 31)]

    def run_dialog(self, keys, size=(12, 60)):
        drawn = []  # (Zeile, Text) in Zeichenreihenfolge

        class Screen(FakeScreen):
            def __init__(self):
                self.keys = iter(keys)

            def getmaxyx(self):
                return size

            def erase(self):
                drawn.append((None, "<erase>"))

            def addstr(self, y, x, text, *a):
                drawn.append((y, text))

            def get_wch(self):
                return next(self.keys)

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set") as curs_set,
        ):
            result = ui.confirm_in_pager("Preview", self.LINES, "Publish? ")
        # Der Cursor ist nur während der Frage sichtbar, am Ende wieder aus.
        self.assertEqual(curs_set.call_args_list[-1].args, (0,))
        self.assertIn((1,), [c.args for c in curs_set.call_args_list])
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
        # 12 Zeilen hoch: Titel, 8 Textzeilen, Leerzeile, Rückfrage, Fußzeile.
        result, drawn = self.run_dialog(
            [curses.KEY_DOWN, curses.KEY_NPAGE, "j", curses.KEY_UP, "\n"]
        )
        self.assertIs(result, True)
        footers = [text for y, text in drawn if y == 11]
        self.assertIn(gmf_module.t("pager_footer_confirm", a=10, b=17, n=30).ljust(59), footers)
        self.assertIn(gmf_module.t("pager_footer_confirm", a=9, b=16, n=30).ljust(59), footers)
        # Die Antwort bleibt beim Scrollen stehen und steht als Balken in der
        # vorletzten Zeile; die Zeile darüber ist leer (Abgrenzung zur Liste).
        self.assertIn(" Publish? j".ljust(59), [text for y, text in drawn if y == 10])
        # Die Frage der GitHub-Vorschau hebt „Y ⏎ = yes" rot hervor; die
        # Testfrage enthält das nicht, also wird hier nichts nachgezeichnet.
        self.assertNotIn(gmf_module.t("yes_accent"), [text for y, text in drawn if y == 10])
        self.assertEqual({text for y, text in drawn if y == 9}, {" " * 59})
        body_rows = {y for y, text in drawn if y is not None and 1 <= y <= 8}
        self.assertEqual(body_rows, set(range(1, 9)))


class SelectedLineColourTests(unittest.TestCase):
    """Die markierte Zeile darf ihre wichtigste Angabe nicht unlesbar machen."""

    def test_red_uses_light_text_instead_of_being_inverted(self):
        # Umkehren hieße bei Rot: schwarze Schrift auf sattem Rot — praktisch nicht
        # zu lesen, und ausgerechnet Rot trägt die dringenden Angaben (M:, D:, ↓).
        self.assertEqual(gmf_module.selected_pair(gmf_module.C_RED), (gmf_module.C_SEL, False))

    def test_light_colours_keep_the_plain_inversion(self):
        for pair in (gmf_module.C_GREEN, gmf_module.C_YELLOW, gmf_module.C_CYAN, gmf_module.C_DIM):
            self.assertEqual(gmf_module.selected_pair(pair), (pair, True))


class CursorVisibilityTests(unittest.TestCase):
    """Genau eine Stelle spricht mit `curses.curs_set()`.

    Auf einem Terminal ohne Cursor-Steuerung wirft curses dort `curses.error`.
    Filter (`/`), Einstellungen (`,`) und der Programmstart riefen bis
    2026-09-03 direkt auf und beendeten die Oberfläche mit einem Traceback.
    """

    def test_the_helper_swallows_terminals_without_cursor_control(self):
        with mock.patch("gitmaster_flash.curses.curs_set", side_effect=curses.error):
            gmf_module.set_cursor_visible(True)  # darf nicht werfen
            gmf_module.set_cursor_visible(False)

    def test_the_filter_prompt_survives_such_a_terminal(self):
        class Screen(FakeScreen):
            def getmaxyx(self):
                return (24, 100)

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        with (
            mock.patch("gitmaster_flash.curses.curs_set", side_effect=curses.error),
            mock.patch.object(ui, "prompt_line", return_value=None),
        ):
            ui.action_filter()


class FileRowTests(unittest.TestCase):
    """Dieselbe Datei sieht in beiden Ansichten gleich aus."""

    def test_every_code_parse_porcelain_emits_has_a_colour(self):
        codes = {"M", "D", "U", "C"}
        self.assertEqual(set(gmf_module.FILE_CODE_COLORS), codes)
        for code in sorted(codes):
            with self.subTest(code=code):
                text, pair = gmf_module.file_row(code, "src/app.py")
                self.assertTrue(text.startswith(f"{code}  "))
                self.assertIn("src/app.py", text)
                self.assertEqual(pair, gmf_module.FILE_CODE_COLORS[code])

    def test_a_conflict_is_named_in_the_row(self):
        text, pair = gmf_module.file_row("C", "a.txt")
        self.assertIn(gmf_module.t("conflict_label"), text)
        self.assertEqual(pair, gmf_module.C_RED)
        self.assertNotIn(gmf_module.t("conflict_label"), gmf_module.file_row("M", "a.txt")[0])

    def test_an_unknown_code_dims_instead_of_raising(self):
        """Vorher stand die Zuordnung zweimal als Dict-Zugriff im Zeichencode.

        Ein Code, den `parse_porcelain()` heute nicht liefert, hätte die
        jeweilige Ansicht mit einem KeyError beendet.
        """
        text, pair = gmf_module.file_row("X", "a.txt")
        self.assertEqual(pair, gmf_module.C_DIM)
        self.assertIn("a.txt", text)


class ScrollWindowTests(unittest.TestCase):
    """Die eine Regel, nach der vier Listen ihren Ausschnitt verschieben."""

    def test_the_selection_stays_inside_the_window(self):
        for selection in range(0, 12):
            offset = 0
            for _ in range(3):  # mehrfach zeichnen ändert nichts
                offset = gmf_module.scroll_window(selection, offset, 5)
            with self.subTest(auswahl=selection):
                self.assertLessEqual(offset, selection)
                self.assertLess(selection, offset + 5)

    def test_the_window_moves_by_the_smallest_step(self):
        self.assertEqual(gmf_module.scroll_window(7, 0, 5), 3)  # nach unten
        self.assertEqual(gmf_module.scroll_window(2, 5, 5), 2)  # nach oben
        self.assertEqual(gmf_module.scroll_window(4, 3, 5), 3)  # schon sichtbar

    def test_a_degenerate_height_never_produces_a_negative_offset(self):
        """Die Commit-Hilfe rechnete früher mit `h - 4` ohne untere Schranke.

        In einem sehr kleinen Fenster sprang ihr Ausschnitt dann bei jedem
        Neuzeichnen zwischen 0 und 1 hin und her.
        """
        for height in (0, -3):
            with self.subTest(hoehe=height):
                self.assertEqual(gmf_module.scroll_window(0, 0, height), 0)
                self.assertEqual(gmf_module.scroll_window(0, 1, height), 0)


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
        hint_rows = {y for y, _, text in ui.scr.lines if "columns" in text or "Spalten" in text}
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
        prefixes = [pad_cells(mark, gmf_module.COMPACT_MARK_WIDTH) + " " for mark in marks]
        # macOS curses führt diese Statuszeichen einzellig. Jede Marke muss daher
        # im ausgegebenen Text exakt dieselbe Namensspalte ergeben.
        self.assertEqual({len(prefix) for prefix in prefixes}, {gmf_module.COMPACT_MARK_WIDTH + 1})

    def test_large_ahead_behind_counts_remain_complete_and_aligned(self):
        ui = self._ui(3)
        ui.statuses[0].ahead = 99
        ui.statuses[1].ahead = 100
        ui.statuses[2].behind = 12345
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw_compact(2, 10, 100)
        cells = [text for _, _, text in ui.scr.lines if "repo-" in text]
        self.assertEqual(len(cells), 3)
        for cell, mark in zip(cells, ("↑99", "↑100", "↓12345")):
            self.assertTrue(cell.startswith(mark + " "), cell)
        self.assertEqual(len({cell.index("repo-") for cell in cells}), 1)

    def _ui(self, count, keys=(), width=100, height=30):
        class Screen(FakeScreen):
            def __init__(self):
                self.keys = iter(list(keys) + [ord("q")])
                self.lines = []

            def clear(self):
                self.lines.clear()

            def getmaxyx(self):
                return (height, width)

            def addstr(self, y, x, text, *rest):
                self.lines.append((y, x, text))

            def getch(self):
                return next(self.keys)

        ui = TUI(Screen(), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.statuses = [self._status(f"repo-{i:02d}") for i in range(count)]
        return ui

    def test_view_switch_keeps_the_selected_repository(self):
        ui = self._ui(24)
        ui.view_mode = "compact"
        ui.selected = 17
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "reload"),
        ):
            ui.scr.keys = iter([ord("m"), ord("q")])
            ui.run()
        self.assertEqual(ui.view_mode, "detail")
        self.assertEqual(ui.selected, 17)
        self.assertEqual(ui.current().rel, "repo-17")

    def test_left_right_move_by_one_column(self):
        ui = self._ui(24)
        ui.view_mode = "compact"
        ui.selected = 0
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "reload"),
        ):
            ui.scr.keys = iter([curses.KEY_RIGHT, ord("q")])
            ui.run()
        rows, _, _, _ = ui.compact_geometry(max(1, 30 - 5 - ui.log_height(30)), 100)
        self.assertEqual(ui.selected, rows)
        # Und zurück.
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "reload"),
        ):
            ui.scr.keys = iter([curses.KEY_LEFT, ord("q")])
            ui.run()
        self.assertEqual(ui.selected, 0)

    def test_many_repositories_start_compact(self):
        for count, expected in ((25, "compact"), (5, "detail")):
            with self.subTest(count=count):
                ui = self._ui(0)
                ui.statuses = []
                with (
                    mock.patch(
                        "gitmaster_flash.collect_all",
                        return_value=[self._status(f"r{i}") for i in range(count)],
                    ),
                    mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
                    mock.patch("gitmaster_flash.safe_addstr"),
                ):
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
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "reload"),
        ):
            # Tab nach unten, zweimal hoch, Tab zurück, einmal runter, wieder Tab.
            ui.scr.keys = iter([9, curses.KEY_UP, curses.KEY_UP, 9, curses.KEY_DOWN, 9, ord("q")])
            ui.run()
        self.assertEqual(ui.focus, "log")
        self.assertEqual(ui.log_selected, 7)  # 9 (neuester) minus zwei nach oben
        self.assertEqual(ui.selected, 5)  # oben eins weiter, blieb erhalten

    def test_first_visit_to_the_log_starts_at_the_newest_entry(self):
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for i in range(4):
            gmf_module.log_command(Path("/tmp/repo"), ("status", str(i)), 0)
        ui = self._ui(3)
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "reload"),
        ):
            ui.scr.keys = iter([9, ord("q")])
            ui.run()
        self.assertEqual(ui.log_selected, 3)

    def test_tab_moves_focus_to_the_log_and_arrows_then_scroll(self):
        ui = self._ui(5)
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        for i in range(30):
            gmf_module.log_command(Path("/tmp/repo"), ("status", str(i)), 0)
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch.object(TUI, "reload"),
        ):
            ui.scr.keys = iter([9, curses.KEY_UP, curses.KEY_UP, ord("q")])
            ui.run()
        self.assertEqual(ui.focus, "log")
        self.assertEqual(ui.selected, 0)  # Auswahl blieb unberührt
        self.assertGreater(ui.log_height(30), 3)  # im Fokus größer

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
                repo_ys = {
                    y
                    for y, _, text in calls
                    if any(name in text for name in ("repo-0", "repo-1"))
                    and not (mode == "compact" and y == 21)
                }
                log_ys = {
                    y
                    for y, _, text in calls
                    if "Befehle" in text or "Commands" in text or "git status" in text
                }
                footer_ys = {
                    y
                    for y, _, text in calls
                    if any(
                        marker in text
                        for marker in (
                            "M layout",
                            "M Layout",
                            "A changes",
                            "A Änderungen",
                            "N reread",
                            "N neu lesen",
                        )
                    )
                }
                self.assertTrue(repo_ys and log_ys and footer_ys, calls)
                self.assertLess(max(repo_ys), min(log_ys))
                self.assertLess(max(log_ys), min(footer_ys))
                if mode == "compact":
                    self.assertLess(max(log_ys), 21)
                    self.assertTrue(any(y == 21 and "repo-00" in text for y, _, text in calls))
                # Die drei Fußzeilen stehen ganz unten und nirgends sonst.
                self.assertEqual(sorted(footer_ys), [23, 24, 25])

    def test_compact_uses_the_leftover_height_for_the_log(self):
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        ui = self._ui(6, height=30, width=100)  # 6 Repos = 2 Zeilen bei 3 Spalten
        ui.view_mode = "compact"
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        log_header = next(
            y for y, _, text in ui.scr.lines if "Befehle" in text or "Commands" in text
        )
        # Der Protokollkopf rückt direkt unter die kurze Liste, statt unten zu kleben.
        self.assertLessEqual(log_header, 5)

    def test_log_pane_is_always_drawn_with_at_least_three_lines(self):
        ui = self._ui(3)
        gmf_module.COMMAND_LOG.clear()
        self.addCleanup(gmf_module.COMMAND_LOG.clear)
        gmf_module.log_command(Path("/tmp/repo"), ("config", "gmf.test", "value"), 0)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        drawn = "\n".join(text for _, _, text in ui.scr.lines)
        self.assertIn("git config gmf.test value", drawn)
        self.assertEqual(ui.log_height(30), 3)
        self.assertEqual(ui.log_height(12), 0)  # winziges Fenster: Repos gehen vor


class ListNavigationBoundsTests(unittest.TestCase):
    """Die Auswahl der Repo-Liste bleibt ein gültiger Index.

    Bis 2026-09-03 hielt sie nur die obere Schranke ein. Bei leerer
    Trefferliste ergab `min(len - 1, selected + 1)` genau -1 — in Python der
    LETZTE Eintrag. Solange die Liste leer blieb, fiel das nicht auf; sobald
    ein Aufrufer sie wieder füllte, ohne über `apply_filter()` zu gehen, träfe
    jede Aktion still ein anderes Repo als das gemeinte. Kein Test fuhr die
    Liste überhaupt bis an ihr Ende.
    """

    class Screen(FakeScreen):
        def __init__(self, keys=()):
            self.keys = iter(keys)
            self.drawn = []

        def getmaxyx(self):
            return (24, 100)

        def addstr(self, y, x, text, *a):
            self.drawn.append((y, text))

        def getch(self):
            return next(self.keys)

        def get_wch(self):
            return next(self.keys)

    def _run(self, names, keys, view_mode="detail", query=""):
        ui = TUI(self.Screen([*keys, ord("q")]), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.all_statuses = [RepoStatus(path=Path("/tmp") / n, rel=n) for n in names]
        ui.view_mode = view_mode
        ui.filter_query = query
        ui.apply_filter()
        with (
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
            mock.patch("gitmaster_flash.curses.curs_set"),
        ):
            ui._loop()
        return ui

    def test_arrow_down_stops_at_the_last_repo(self):
        names = [f"repo-{i}" for i in range(6)]
        ui = self._run(names, [curses.KEY_DOWN] * 12)
        self.assertEqual(ui.selected, len(names) - 1)
        self.assertEqual(ui.current().rel, names[-1])

    def test_arrow_right_in_the_compact_view_stops_at_the_last_repo(self):
        names = [f"repo-{i}" for i in range(6)]
        ui = self._run(names, [curses.KEY_RIGHT] * 12, view_mode="compact")
        self.assertEqual(ui.selected, len(names) - 1)
        self.assertEqual(ui.current().rel, names[-1])

    def test_arrow_down_on_an_empty_result_keeps_a_valid_index(self):
        ui = self._run(["alpha", "beta"], [curses.KEY_DOWN] * 3, query="gibtsnicht")
        self.assertEqual(ui.statuses, [])
        self.assertEqual(ui.selected, 0)
        self.assertIsNone(ui.current())

    def test_an_out_of_range_index_never_points_at_another_repo(self):
        """Gegenprobe: `current()` prüft den Index, nicht nur die Länge."""
        ui = self._run(["alpha", "beta"], [])
        ui.selected = -1
        self.assertIsNone(ui.current())
        ui.selected = 2
        self.assertIsNone(ui.current())


class TuiFilterTests(unittest.TestCase):
    """Verhalten des Filters in der TUI: Auswahl, Kopfzeile, Esc."""

    class Screen(FakeScreen):
        def __init__(self, keys=()):
            self.keys = iter(keys)
            self.drawn = []

        def getmaxyx(self):
            return (24, 100)

        def addstr(self, y, x, text, *a):
            self.drawn.append((y, text))

        def getch(self):
            return next(self.keys)

        def get_wch(self):
            return next(self.keys)

    def _ui(self, names, keys=(), dirty=()):
        ui = TUI(self.Screen(keys), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.all_statuses = [
            RepoStatus(path=Path("/tmp") / n, rel=n, modified=1 if n in dirty else 0) for n in names
        ]
        ui.statuses = list(ui.all_statuses)
        return ui

    def test_a_blank_filter_is_no_filter_at_all(self):
        """Review-Fund 2026-08-25: Eine Eingabe aus lauter Leerzeichen blieb aktiv.

        `filter_terms` zerlegt mit `split()` und zeigt dann alle Repos, waehrend
        Kopfzeile, Meldungen und die Esc-Bedeutung den nichtleeren Rohstring als
        aktiven Filter lasen. Kanonisiert wird jetzt an der Eingabegrenze.
        """
        self.assertEqual(gmf_module.canonical_filter_query("   "), "")
        self.assertEqual(gmf_module.canonical_filter_query("  api   docs "), "api docs")
        ui = TUI(self.Screen(()), Path("/tmp"), DEFAULT_CONFIG, None, "   ")
        self.assertEqual(ui.filter_query, "")

    def test_the_prompt_survives_a_window_too_small_for_its_row(self):
        """Ein drei Zeilen hohes Fenster liess `/` mit einem Traceback enden.

        `action_filter()` rechnet `h - 4`, `_edit_setting()` `h - 2`. Beide
        Zeilen sind dort negativ, und `prompt_line()` klemmte nur nach oben.
        Echtes curses wirft bei `move(-1, x)` eine `curses.error`, die durch
        `_loop` (faengt nur TimeoutExpired) bis aus `curses.wrapper` lief.
        `draw_question()` klemmt seit jeher nach BEIDEN Seiten und faengt den
        Sprung ab (Review-Fund 2026-09-10).
        """

        class TinyScreen(TuiFilterTests.Screen):
            def getmaxyx(self):
                return (3, 40)

            def move(self, y, x):
                # Wie echtes curses: eine Zeile ausserhalb des Fensters ist ERR.
                if not 0 <= y < 3:
                    raise curses.error("wmove() returned ERR")

        ui = TUI(TinyScreen(iter(["a", "\n"])), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.all_statuses = [RepoStatus(path=Path("/tmp/a"), rel="a")]
        ui.statuses = list(ui.all_statuses)

        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.action_filter()

        self.assertEqual(ui.filter_query, "a")

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
        ui.selected = 0  # steht auf "blog"
        ui.filter_query = "api"
        ui.apply_filter()
        self.assertEqual(ui.current().rel, "api-gateway")

    def test_reload_keeps_the_selected_repo_under_an_active_filter(self):
        ui = self._ui(["api-gateway", "blog", "api-docs"])
        ui.filter_query = "api"
        ui.apply_filter()
        ui.selected = 1  # "api-docs"
        ui.apply_filter(keep_selection=True)
        self.assertEqual(ui.current().rel, "api-docs")

    def test_selection_falls_back_to_the_top_when_its_repo_vanishes(self):
        ui = self._ui(["api-gateway", "blog"])
        ui.selected = 1  # "blog"
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
        ui = self._ui(["api-gateway", "blog", "notizen"], dirty=("blog", "notizen", "api-gateway"))
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

    def test_the_selection_follows_the_real_path_not_the_display_name(self):
        """Zwei Ordner, die sich nur in NFC/NFD unterscheiden, tragen dasselbe `rel`.

        `collect_status()` normalisiert den Anzeigenamen auf NFC, damit die
        Spaltenbreiten stimmen. Auf einem Linux-Server sind beide Ordner
        nebeneinander erlaubt — die Auswahl darf sie dann nicht verwechseln.
        Derselbe Fehler war am 2026-08-25 schon beim Hintergrund-Fetch behoben
        worden; hier stand er noch.
        """
        nfd, nfc = "cafe\u0301", "caf\u00e9"
        ui = TUI(self.Screen(()), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.all_statuses = [
            RepoStatus(path=Path("/tmp") / nfd, rel=nfc),
            RepoStatus(path=Path("/tmp") / nfc, rel=nfc),
        ]
        ui.statuses = list(ui.all_statuses)
        ui.selected = 1  # der zweite der beiden Ordner
        ui.apply_filter(keep_selection=True)
        self.assertEqual(ui.current().path, Path("/tmp") / nfc)

    def test_expansion_follows_the_real_path_not_the_display_name(self):
        """Gleiche NFC-Anzeigenamen duerfen nicht gemeinsam aufklappen."""
        nfd, nfc = "cafe\u0301", "caf\u00e9"
        first = RepoStatus(path=Path("/tmp") / nfd, rel=nfc)
        second = RepoStatus(path=Path("/tmp") / nfc, rel=nfc)
        ui = TUI(self.Screen(()), Path("/tmp"), DEFAULT_CONFIG, None)
        ui.all_statuses = [first, second]
        ui.statuses = list(ui.all_statuses)
        ui.expanded.add(first.path)

        rows = ui.build_rows()

        self.assertEqual([row for row in rows if row[0] == "empty"], [("empty", 0)])
        with (
            mock.patch("gitmaster_flash.safe_addstr") as draw,
            mock.patch("gitmaster_flash.color_attr", return_value=0),
        ):
            ui.draw_repo_line(0, first, False)
            first_arrow = draw.call_args_list[0].args[3]
        with (
            mock.patch("gitmaster_flash.safe_addstr") as draw,
            mock.patch("gitmaster_flash.color_attr", return_value=0),
        ):
            ui.draw_repo_line(0, second, False)
            second_arrow = draw.call_args_list[0].args[3]
        self.assertEqual((first_arrow, second_arrow), ("▼ ", "▶ "))

    def test_clear_filter_restores_every_repo_and_holds_the_selection(self):
        ui = self._ui(["blog", "api-gateway", "api-docs"])
        ui.filter_query = "api"
        ui.apply_filter()
        ui.selected = 1  # "api-docs"
        ui.clear_filter()
        self.assertEqual(ui.filter_query, "")
        self.assertEqual(len(ui.statuses), 3)
        self.assertEqual(ui.current().rel, "api-docs")

    def test_the_prompt_starts_from_the_current_filter_and_can_extend_it(self):
        # Esc im Dialog lässt den bestehenden Filter unangetastet …
        ui = self._ui(["api-gateway"], keys=["\x1b"])
        ui.filter_query = "api"
        with (
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            ui.action_filter()
        self.assertEqual(ui.filter_query, "api")
        # … und das Feld ist mit ihm vorbelegt, sodass ⏎ ihn unverändert bestätigt.
        ui = self._ui(["api-gateway", "blog"], keys=["\n"])
        ui.filter_query = "api"
        with (
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
            ui.action_filter()
        self.assertEqual(ui.filter_query, "api")
        self.assertEqual([s.rel for s in ui.statuses], ["api-gateway"])

    def test_an_emptied_prompt_clears_the_filter(self):
        ui = self._ui(["api-gateway", "blog"], keys=["\x7f", "\x7f", "\x7f", "\n"])
        ui.filter_query = "api"
        ui.apply_filter()
        with (
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
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
        self.assertEqual([s.modified for s in ui.statuses if s.rel == "api-gateway"], [7])


class TuiSettingsTests(unittest.TestCase):
    """Die Einstellungsansicht: Navigation, Speichern, Grenzen."""

    class Screen(FakeScreen):
        def __init__(self, keys=()):
            self.keys = iter(keys)
            self.drawn = []

        def getmaxyx(self):
            return (24, 100)

        def addstr(self, y, x, text, *a):
            self.drawn.append((y, text))

        def getch(self):
            return next(self.keys)

        def get_wch(self):
            return next(self.keys)

    def test_five_row_terminal_preserves_selected_setting(self):
        ui = self._ui()
        ui.scr.getmaxyx = lambda: (5, 100)
        rows = ui.settings_rows()
        selected = next(
            i for i, row in enumerate(rows) if row[0] == "edit" and row[1].key == "skip_dirs"
        )
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui._draw_settings_page(rows, selected, "", 0)
        line = [text for y, text in ui.scr.drawn if y == 2]
        self.assertTrue(any(gmf_module.t("set_skip_dirs") in text for text in line))
        self.assertNotIn(rows[selected][4], line)

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
        with (
            mock.patch("gitmaster_flash.curses.curs_set"),
            mock.patch("gitmaster_flash.curses.color_pair", return_value=0),
        ):
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

    def test_small_settings_view_keeps_the_selected_row_visible(self):
        ui = self._ui([curses.KEY_DOWN] * 6 + [ord("q")])
        ui.scr.getmaxyx = lambda: (10, 100)
        ui.scr.erase = ui.scr.drawn.clear
        self._run(ui)
        selected = [(y, text) for y, text in ui.scr.drawn if "›" in text]
        self.assertEqual(len(selected), 1, ui.scr.drawn)
        self.assertIn(gmf_module.t("set_skip_dirs"), selected[0][1])
        self.assertTrue(2 <= selected[0][0] < 7)

    def test_small_settings_view_reaches_read_only_rows_without_editing(self):
        ui = self._ui([curses.KEY_DOWN] * 20 + [10, ord("q")])
        ui.scr.getmaxyx = lambda: (10, 100)
        ui.scr.erase = ui.scr.drawn.clear
        with mock.patch.object(ui, "_edit_setting") as edit:
            self._run(ui)
        edit.assert_not_called()
        self.assertTrue(any("apps" in text for _, text in ui.scr.drawn))
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
        self.assertTrue(
            any(gmf_module.TR["set_err_number"]["en"] in text for _, text in ui.scr.drawn)
        )

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
        ui = self._ui([curses.KEY_DOWN, 10, *self.CLEAR, "7", "\n", ord("q")], config_path=None)
        self._run(ui)
        self.assertEqual(ui.cfg["compact_from"], 7)  # gilt für die Sitzung
        self.assertFalse(self.path.exists())
        self.assertTrue(
            any(gmf_module.TR["set_not_saved_hint"]["en"] in text for _, text in ui.scr.drawn)
        )

    def test_a_failed_save_says_so_instead_of_claiming_success(self):
        ui = self._ui([curses.KEY_DOWN, 10, *self.CLEAR, "7", "\n", ord("q")])
        with mock.patch("gitmaster_flash.save_config", side_effect=OSError("Platte voll")):
            self._run(ui)
        self.assertEqual(ui.cfg["compact_from"], 7)  # gilt für die Sitzung
        self.assertTrue(any("Platte voll" in text for _, text in ui.scr.drawn))
        self.assertFalse(
            any(gmf_module.TR["set_saved"]["en"] == text.strip() for _, text in ui.scr.drawn)
        )

    def test_the_selection_returns_from_read_only_rows_to_the_first_setting(self):
        # Zwanzig Mal runter, dann zwanzig Mal hoch: die Auswahl muss innerhalb
        # der Liste bleiben und wieder auf der ersten Einstellung landen.
        keys = (
            [curses.KEY_DOWN] * 20 + [curses.KEY_UP] * 20 + [10, *self.CLEAR, "3", "\n", ord("q")]
        )
        ui = self._ui(keys)
        self._run(ui)
        # Die erste editierbare Zeile ist die Sprache — "3" ist dort ungültig.
        self.assertEqual(ui.cfg["lang"], DEFAULT_CONFIG["lang"])
        self.assertTrue(
            any(
                gmf_module.TR["set_err_choice"]["en"].split("{")[0] in text
                for _, text in ui.scr.drawn
            )
        )

    def test_the_comma_key_reaches_the_settings_view(self):
        ui = self._ui()
        with mock.patch.object(TUI, "action_settings") as action:
            ui.dispatch_action(",")
        action.assert_called_once_with()

    def test_changing_skip_dirs_says_that_a_rescan_is_needed(self):
        rows = self._ui().settings_rows()
        index = next(
            i for i, row in enumerate(rows) if row[0] == "edit" and row[1].key == "skip_dirs"
        )
        # An die vorbelegte Liste einen weiteren Ordner anhaengen — so, wie man
        # es in der Oberflaeche taete.
        keys = [curses.KEY_DOWN] * index + [10] + list(",bau") + ["\n", ord("q")]
        ui = self._ui(keys)
        self._run(ui)
        self.assertTrue(
            any(gmf_module.TR["set_saved_rescan"]["en"] in text for _, text in ui.scr.drawn)
        )
