import contextlib
import io
import re
import unittest
from pathlib import Path
from unittest import mock

from gmf_test_support import FakeScreen

import gitmaster_flash as gmf_module
from gitmaster_flash import (
    DEFAULT_CONFIG,
    TUI,
    ChangedFile,
    RepoStatus,
)


class DocumentationContractTests(unittest.TestCase):
    """Die beiden READMEs gegen den Code — automatisch statt bei jedem Review.

    Diese Abgleiche wurden bisher in jeder QA-Runde von Hand gefahren: Kommen
    alle argparse-Schalter vor? Alle Config-Schluessel? Alle Tasten? Stimmt die
    abgedruckte Kopfzeile noch? Von Hand heisst: nur dann, wenn jemand
    hinschaut — dazwischen konnte die Doku beliebig auseinanderlaufen, und
    genau das war schon zweimal ein Befund (fehlende Schalter --lang/--version/
    --cd-file am 2026-08-19, falscher Rueckgaengig-Befehl ebenda). Als Test
    laufen sie bei jedem Lauf mit.
    """

    ROOT = Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        cls.docs = {
            name: (cls.ROOT / name).read_text(encoding="utf-8")
            for name in ("README.md", "README.de.md")
        }
        cls.guides = {
            lang: {
                kind: (cls.ROOT / "docs" / f"{kind}{suffix}.md").read_text(encoding="utf-8")
                for kind in ("cli", "configuration", "interface")
            }
            for lang, suffix in (("en", ""), ("de", ".de"))
        }

    def test_every_command_line_switch_is_documented_in_both_languages(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                gmf_module.main(["--help"])
        # `--help` erklaert sich selbst und steht bewusst in keinem README.
        flags = set(re.findall(r"--[a-z][a-z-]+", buf.getvalue())) - {"--help"}
        self.assertIn("--filter", flags)  # Schutz gegen leere Menge
        for lang, guides in self.guides.items():
            for flag in sorted(flags):
                with self.subTest(sprache=lang, schalter=flag):
                    self.assertIn(flag, guides["cli"])

    def test_every_config_key_and_setting_is_documented_in_both_languages(self):
        keys = (
            list(gmf_module.DEFAULT_CONFIG)
            + [s.key for s in gmf_module.EDITABLE_SETTINGS]
            + list(gmf_module.READ_ONLY_SETTINGS)
        )
        for lang, guides in self.guides.items():
            text = guides["configuration"]
            for key in keys:
                with self.subTest(sprache=lang, schluessel=key):
                    self.assertIn(key, text)

    def test_every_documented_default_value_matches_the_code(self):
        for lang, guides in self.guides.items():
            text = guides["configuration"]
            for key, value in gmf_module.DEFAULT_CONFIG.items():
                if isinstance(value, bool) or not isinstance(value, int):
                    continue
                with self.subTest(sprache=lang, schluessel=key):
                    bullet = re.search(
                        rf"(?ms)^- `[^`]*\b{re.escape(key)}\b[^`]*`[^\n]*(?:\n  .*?)*"
                        rf"(?=\n- |\n\n|\Z)",
                        text,
                    )
                    self.assertIsNotNone(bullet, f"kein Abschnitt fuer {key}")
                    self.assertIn(str(value), bullet.group(0))

    def test_every_key_the_interface_handles_is_in_the_key_table(self):
        keys = set(gmf_module.KEY_BINDINGS)
        for action in gmf_module.KEY_BINDINGS.values():
            self.assertTrue(callable(getattr(TUI, action)))
        # Diese Tasten behandelt `_loop()` vor `dispatch_action()`. Die
        # Standard-App-Tasten kommen aus der Config und erreichen den letzten
        # Zweig von `dispatch_action()` deshalb ebenfalls ohne literalen
        # Vergleich im Funktionsrumpf.
        keys.update(gmf_module.DEFAULT_CONFIG["apps"])
        keys.update(("↑ / ↓", "→ / ←", "Tab", "⏎", "Q"))
        self.assertIn("/", keys)  # Schutz gegen leere Menge
        for name, text in self.docs.items():
            table = "\n".join(line for line in text.splitlines() if line.startswith("| "))
            for key in sorted(keys):
                with self.subTest(datei=name, taste=key):
                    self.assertIn(f"| {key} |", table)

    def test_the_printed_header_line_is_what_the_code_really_draws(self):
        """Die abgedruckte Kopfzeile wird nachgebaut, nicht nur ueberflogen.

        Sie traegt Version, Trefferzahl, Filtertext und die Zahl der
        ausgeblendeten Repos — jede davon kann still veralten.
        """
        visible = ["api-1", "api-2", "api-3"]
        hidden_dirty = [f"other-dirty-{i}" for i in range(7)]
        rest = [f"other-{i}" for i in range(61 - 3 - 7)]
        dirty = set(visible[:2]) | set(hidden_dirty)
        for name, lang in (("README.md", "en"), ("README.de.md", "de")):
            with self.subTest(datei=name):
                with mock.patch.object(gmf_module, "UI_LANG", lang):
                    ui = TUI(_HeaderScreen(), Path("~/git"), dict(DEFAULT_CONFIG), None)
                    ui.all_statuses = [
                        RepoStatus(path=Path("~/git") / n, rel=n, modified=1 if n in dirty else 0)
                        for n in visible + hidden_dirty + rest
                    ]
                    ui.statuses = list(ui.all_statuses)
                    ui.filter_query = "api"
                    ui.apply_filter()
                    with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
                        ui.draw()
                drawn = ui.scr.drawn[0][1].rstrip()
                self.assertIn(drawn, self.guides[lang]["interface"])

    def test_the_printed_commit_helper_block_is_what_the_code_really_draws(self):
        """Auch der Textblock der Commit-Hilfe wird nachgebaut, nicht abgeschrieben.

        Seine Spalten richten sich nach der laengsten Beschriftung. Genau das
        liess ihn schon einmal still veralten: Eine dritte Beschriftung
        verschob die Pfadspalte, und im README stand weiter die alte Breite.
        """
        files = [
            ChangedFile("M", "README.md", " M"),
            ChangedFile("U", "notes.txt", "??"),
            ChangedFile("U", "server.py", "??"),
            ChangedFile("U", "build/out.o", "??"),
            # Gestaget und geloescht: gegenueber HEAD gibt es nichts zu committen.
            ChangedFile("D", "old-draft.md", "AD"),
            # Fremdes Git-Repo im Arbeitsbaum: Nur DIESER Fall kommt auch mit
            # --untracked-files=all noch als Ordner heraus, und er hat seine
            # eigene Beschriftung.
            ChangedFile("U", "vendor/lib/", "??"),
        ]

        class Screen(_HeaderScreen):
            def __init__(self):
                super().__init__()
                self.keys = iter([27])  # Esc schliesst die Hilfe sofort

            def getmaxyx(self):
                return (14, 92)

            def addstr(self, y, x, text, *a):
                self.drawn.append((y, (" " * x) + text.rstrip()))

            def getch(self):
                return next(self.keys)

        for name, lang in (("README.md", "en"), ("README.de.md", "de")):
            with self.subTest(datei=name):
                with mock.patch.object(gmf_module, "UI_LANG", lang):
                    st = RepoStatus(path=Path("/tmp/api-gateway"), rel="api-gateway")
                    st.files = files
                    ui = TUI(Screen(), Path("/tmp"), dict(DEFAULT_CONFIG), None)
                    ui.all_statuses = [st]
                    ui.statuses = [st]
                    with mock.patch("gitmaster_flash.curses.color_pair", return_value=0):
                        ui.action_commit_wizard()
                    lines = [text for y, text in ui.scr.drawn if text.strip()]
                for line in lines:
                    with self.subTest(zeile=line):
                        self.assertIn(line, self.guides[lang]["interface"])

    def test_both_readmes_stay_structurally_in_step(self):
        heads = {}
        for name, text in self.docs.items():
            with self.subTest(datei=name):
                self.assertEqual(text.count("```") % 2, 0, "ungerade Zahl von Codeblock-Zaeunen")
                for target in re.findall(r"\]\((?!https?:)([^)#]+)", text):
                    self.assertTrue(
                        (self.ROOT / target).exists(), f"lokaler Link zeigt ins Leere: {target}"
                    )
            heads[name] = [line for line in text.splitlines() if line.startswith("#")]
        # Beide Fassungen muessen inhaltlich synchron bleiben (AGENTS.md); eine
        # zusaetzliche Ueberschrift auf einer Seite ist der erste Hinweis darauf,
        # dass eine Aenderung nur in einer Sprache angekommen ist.
        self.assertEqual(len(heads["README.md"]), len(heads["README.de.md"]))


class _HeaderScreen(FakeScreen):
    """Minimales curses-Fenster: nur breit genug fuer die ungekuerzte Kopfzeile."""

    def __init__(self):
        self.drawn = []

    def getmaxyx(self):
        return (24, 200)

    def addstr(self, y, x, text, *a):
        self.drawn.append((y, text))
