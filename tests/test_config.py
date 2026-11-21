import contextlib
import io
import json
import shutil
import string
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gmf_test_support import FakeScreen

import gitmaster_flash as gmf_module
from gitmaster_flash import (
    DEFAULT_CONFIG,
    TUI,
    RepoStatus,
)


class TranslationContractTests(unittest.TestCase):
    def test_every_user_text_has_matching_english_and_german_placeholders(self):
        formatter = string.Formatter()

        def placeholders(text):
            return {field for _, field, _, _ in formatter.parse(text) if field is not None}

        for key, translations in gmf_module.TR.items():
            with self.subTest(key=key):
                self.assertEqual(set(translations), {"en", "de"})
                self.assertEqual(placeholders(translations["en"]), placeholders(translations["de"]))


class AppEntryTests(unittest.TestCase):
    """Ein von Hand ergänzter `apps`-Eintrag darf die TUI nie beenden.

    Die App-Tasten trägt man selbst in die config.json ein. Ein vergessenes
    `name` warf bis 2026-09-03 schon im ersten `draw()` einen KeyError — und
    den fängt niemand ab: `_loop()` kennt nur Timeouts. gmf endete mit einem
    Traceback, bevor überhaupt eine Repo-Zeile stand.
    """

    class Screen(FakeScreen):
        def __init__(self):
            self.drawn = []

        def getmaxyx(self):
            return (24, 100)

        def addstr(self, y, x, text, *a):
            self.drawn.append((y, text))

    def test_a_missing_name_becomes_a_visible_placeholder(self):
        self.assertEqual(gmf_module.app_label({"path": "/A.app"}), "?")
        self.assertEqual(gmf_module.app_label({"name": "Editor"}), "Editor")
        self.assertEqual(gmf_module.app_label(None), "?")
        self.assertEqual(gmf_module.app_path({"name": "Editor"}), "")
        self.assertEqual(gmf_module.app_path({"path": "/A.app"}), "/A.app")

    def test_the_footer_draws_instead_of_raising(self):
        cfg = {**DEFAULT_CONFIG, "apps": {"Z": {"path": "/A.app"}}}
        ui = TUI(self.Screen(), Path("/tmp"), cfg, None)
        ui.all_statuses = [RepoStatus(path=Path("/tmp/x"), rel="x")]
        ui.statuses = list(ui.all_statuses)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        self.assertTrue(any("Z ?" in text for _, text in ui.scr.drawn))

    def test_an_entry_without_a_path_reports_it_instead_of_raising(self):
        cfg = {**DEFAULT_CONFIG, "apps": {"Z": {"name": "Zed"}}}
        ui = TUI(self.Screen(), Path("/tmp"), cfg, None)
        ui.all_statuses = [RepoStatus(path=Path("/tmp/x"), rel="x")]
        ui.statuses = list(ui.all_statuses)

        ui.action_open_app("Z")

        self.assertEqual(ui.message, gmf_module.t("app_not_found", p="?"))

    def test_the_settings_page_shows_the_same_placeholder(self):
        cfg = {**DEFAULT_CONFIG, "apps": {"Z": {"path": "/A.app"}}}
        self.assertEqual(gmf_module.setting_display(cfg, "apps"), "Z ?")

    def test_an_entry_that_is_not_an_object_at_all_stays_a_placeholder(self):
        """`"Z": 5` statt `"Z": {…}` ist derselbe Tippfehler eine Stufe hoeher."""
        self.assertEqual(gmf_module.app_label(5), "?")
        self.assertEqual(gmf_module.app_path(5), "")
        self.assertEqual(gmf_module.app_label("Zed"), "?")
        cfg = {**DEFAULT_CONFIG, "apps": {"Z": 5}}
        ui = TUI(self.Screen(), Path("/tmp"), cfg, None)
        ui.all_statuses = [RepoStatus(path=Path("/tmp/x"), rel="x")]
        ui.statuses = list(ui.all_statuses)
        with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
            ui.draw()
        self.assertTrue(any("Z ?" in text for _, text in ui.scr.drawn))
        ui.action_open_app("Z")
        self.assertEqual(ui.message, gmf_module.t("app_not_found", p="?"))
        self.assertEqual(gmf_module.setting_display(cfg, "apps"), "Z ?")


class LoadConfigShapeTests(unittest.TestCase):
    """Eine von Hand geschriebene config.json kann gueltiges JSON und trotzdem
    krumm sein. `load_config()` versprach dafuer die Rueckfallebene auf die
    Defaults, endete aber mit einem Traceback: `dict.update(None)` bei einer
    Datei aus `null`, `[…]` oder `"text"`, und `.items()` bei einem
    `"apps": null`. Ein `"skip_dirs": null` riss spaeter den ganzen Scan mit
    (`find_repos` -> `set(None)`), ein `"sync_remote_names": null` jedes
    einzelne Repo. Form und Werte werden an der Eingabegrenze geprueft.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self._real = gmf_module.CONFIG_PATH
        gmf_module.CONFIG_PATH = self.path

    def tearDown(self):
        gmf_module.CONFIG_PATH = self._real
        self.tmp.cleanup()

    def _load(self, content: str) -> tuple[dict, str]:
        self.path.write_text(content)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = gmf_module.load_config()
        return cfg, err.getvalue()

    def test_a_file_that_is_no_object_falls_back_and_says_so(self):
        for content in ("null", "[]", "[1, 2]", '"text"', "42"):
            with self.subTest(inhalt=content):
                cfg, err = self._load(content)
                self.assertEqual(cfg["apps"], DEFAULT_CONFIG["apps"])
                self.assertEqual(cfg["skip_dirs"], DEFAULT_CONFIG["skip_dirs"])
                self.assertIn("not a JSON object", err)

    def test_a_single_entry_of_the_wrong_type_falls_back_and_names_itself(self):
        for content, key in (
            ('{"apps": null}', "apps"),
            ('{"apps": [1]}', "apps"),
            ('{"skip_dirs": null}', "skip_dirs"),
            ('{"sync_remote_names": null}', "sync_remote_names"),
            ('{"git_timeout": "20"}', "git_timeout"),
        ):
            with self.subTest(inhalt=content):
                cfg, err = self._load(content)
                self.assertEqual(cfg[key], DEFAULT_CONFIG[key])
                self.assertIn(f'"{key}"', err)

    def test_a_correct_entry_still_wins_over_the_default(self):
        """Gegenprobe: Die Formpruefung darf keine gueltige Config verwerfen."""
        cfg, err = self._load(
            '{"lang": "de", "git_timeout": 42, "skip_dirs": ["x"],'
            ' "apps": {"z": {"name": "Zed", "path": "/Z.app"}},'
            ' "eigener_schluessel": 1}'
        )
        self.assertEqual(err, "")
        self.assertEqual(cfg["lang"], "de")
        self.assertEqual(cfg["git_timeout"], 42)
        self.assertEqual(cfg["skip_dirs"], ["x"])
        # App-Tasten intern immer gross.
        self.assertEqual(cfg["apps"], {"Z": {"name": "Zed", "path": "/Z.app"}})
        self.assertEqual(cfg["eigener_schluessel"], 1)

    def test_lang_may_carry_null_because_that_means_auto(self):
        cfg, err = self._load('{"lang": null}')
        self.assertEqual(err, "")
        self.assertIsNone(cfg["lang"])
        self.assertIn(gmf_module.resolve_lang(cfg), ("en", "de"))

    def test_lang_with_a_wrong_type_falls_back_like_every_other_entry(self):
        """Ein Default `None` ist kein Freibrief fuer jeden Typ.

        Die Formpruefung sprang bisher ueber jeden Eintrag mit Default `None`,
        damit `lang: null` als "automatisch" durchkommt. Damit landete aber
        auch `lang: ["de"]` ungeprueft in der Config, und `resolve_lang()`
        rief darauf `.lower()` auf — AttributeError statt Rueckfallebene, auch
        auf der Gegenseite von `--diff` (Review-Fund 2026-09-10).
        """
        for content in ('{"lang": 5}', '{"lang": true}', '{"lang": ["de"]}', '{"lang": {"a": 1}}'):
            with self.subTest(inhalt=content):
                cfg, err = self._load(content)
                self.assertIsNone(cfg["lang"])
                self.assertIn('"lang"', err)
                self.assertIn(gmf_module.resolve_lang(cfg), ("en", "de"))

    def test_a_config_that_cannot_be_created_still_yields_the_defaults(self):
        """Die Erstanlage ist Komfort, keine Voraussetzung.

        Ein read-only $HOME oder eine Datei an der Stelle von ~/.config
        beendete gmf mit einem Traceback — bei `--diff` auf der befragten
        Gegenseite, wo der lokale Lauf nur "keine Ausgabe" sieht.
        """
        blocker = Path(self.tmp.name) / "datei"
        blocker.write_text("x")
        gmf_module.CONFIG_PATH = blocker / "gitmaster_flash" / "config.json"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = gmf_module.load_config()
        self.assertEqual(cfg["skip_dirs"], DEFAULT_CONFIG["skip_dirs"])
        self.assertIn("cannot create", err.getvalue())

    def test_a_broken_json_file_still_reports_the_read_error(self):
        cfg, err = self._load("{nope")
        self.assertEqual(cfg["skip_dirs"], DEFAULT_CONFIG["skip_dirs"])
        self.assertIn("cannot read", err)

    def test_non_utf8_config_falls_back_without_rewriting_the_file(self):
        content = b'{"lang": "\xff"}'
        self.path.write_bytes(content)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = gmf_module.load_config()
        self.assertEqual(cfg["skip_dirs"], DEFAULT_CONFIG["skip_dirs"])
        self.assertIn("cannot read", err.getvalue())
        self.assertEqual(self.path.read_bytes(), content)

    def test_a_missing_file_is_created_with_the_defaults(self):
        self.assertFalse(self.path.exists())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = gmf_module.load_config()
        self.assertEqual(err.getvalue(), "")
        self.assertTrue(self.path.exists())
        self.assertEqual(json.loads(self.path.read_text()), DEFAULT_CONFIG)
        self.assertEqual(cfg["apps"], DEFAULT_CONFIG["apps"])


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
        self.assertEqual(gmf_module.parse_setting(self._setting("compact_from"), "0"), (0, None))

    def test_skip_dirs_splits_on_commas_and_drops_blanks(self):
        setting = self._setting("skip_dirs")
        self.assertEqual(gmf_module.parse_setting(setting, " a , ,b,, c "), (["a", "b", "c"], None))
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
            gmf_module.parse_setting(self._setting("skip_dirs"), "My Folder"), (["My Folder"], None)
        )

    def test_display_shows_auto_empty_lists_and_apps_readably(self):
        cfg = {
            "lang": None,
            "skip_dirs": [],
            "sync_remote_names": ["origin"],
            "apps": {"E": {"name": "Editor", "path": "/x"}},
        }
        self.assertEqual(gmf_module.setting_display(cfg, "lang"), "auto")
        self.assertEqual(
            gmf_module.setting_display(cfg, "skip_dirs"), gmf_module.TR["set_value_none"]["en"]
        )
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
        self.assertEqual(json.loads(self.path.read_text()), {"lang": "de", "git_timeout": 11})

    def test_unpaired_surrogate_round_trips_as_json_escape(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"lang":"en","future":"\\ud800"}')
        with mock.patch.object(gmf_module, "CONFIG_PATH", self.path):
            cfg = gmf_module.load_config()
        cfg["git_timeout"] = 11
        gmf_module.save_config(cfg, self.path)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["future"], "\ud800")
        self.assertEqual(saved["git_timeout"], 11)

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
