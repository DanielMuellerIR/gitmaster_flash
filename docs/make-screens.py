#!/usr/bin/env python3
"""Generate the README screens as SVG — from the REAL TUI, no screenshot needed.

Why: hand-made terminal screenshots were fiddly (window transparency, fonts, cropping)
and went stale on every UI change — the ones in this repo still showed a header without
the version. This runs the actual program in a pseudo-terminal on the `--demo` sandbox,
reads the rendered screen back and turns it into an SVG. So the picture is genuinely
what the program draws, but reproducible: no camera, no window manager, no luck.

The SVG contains selectable text (nice for screen readers, and tiny: a few KB instead of
several hundred), and it renders identically on GitHub in light and dark mode because it
brings its own background.

Usage:
    python3 docs/make-screens.py           # regenerate docs/*.svg
    python3 docs/make-screens.py --check   # fail if they would change (CI/pre-release)

Requires a Unix pty (macOS/Linux). Deterministic: the demo sandbox is built fresh and
uses generic app names; every README screen is captured once in English and once in
German.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import tempfile
import termios
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
GMF = HERE.parent / "gitmaster_flash.py"

# Default terminal for a capture. Every screen may ask for its own size (see SCREENS):
# the window height decides how the TUI divides list, command log and footer, so a
# picture only looks like everyday use if the window fits its contents.
# 110 columns and not 100, because the header carries version, path, count and summary:
# at 100 the sandbox path pushed the last word out of the window ("21 to r").
COLS, ROWS = 110, 34

# The escape sequences ncurses emits. The "?" belongs into the parameter part so that
# private modes (ESC[?25l) are swallowed instead of being printed as text.
CSI = re.compile(r"\x1b\[([?0-9;]*)([A-Za-z`@])")

# xterm-ish palette. Only what the TUI actually uses.
FG = "#d8d8d8"
BG = "#1c1c1c"
ANSI = {
    30: "#3b3b3b", 31: "#e06c75", 32: "#98c379", 33: "#e5c07b",
    34: "#61afef", 35: "#c678dd", 36: "#56b6c2", 37: "#d8d8d8",
    90: "#7f7f7f", 91: "#e06c75", 92: "#98c379", 93: "#e5c07b",
    94: "#61afef", 95: "#c678dd", 96: "#56b6c2", 97: "#ffffff",
}


# Cursor keys as the TUI expects them. ncurses puts the terminal into "application
# cursor" mode (ESC[?1h), and from then on it only recognises ESC O A/B/C/D. Sending
# the ESC [ A/B/C/D form instead delivered a bare ESC — which the TUI reads as "quit",
# so the capture died with "[Errno 5] Input/output error" halfway through the keys.
UP, DOWN, RIGHT, LEFT = b"\x1bOA", b"\x1bOB", b"\x1bOC", b"\x1bOD"
TAB = b"\t"


def _split_keys(keys: bytes) -> list:
    """Split a key string into single keypresses: DOWN + DOWN + b"c" -> [down, down, c].

    Each has to arrive as its own read() — curses assembles an escape sequence into one
    KEY_DOWN, but only if it is not glued to the next keypress."""
    out, i = [], 0
    while i < len(keys):
        if keys[i:i + 2] == b"\x1b[":
            j = i + 2
            while j < len(keys) and not (0x40 <= keys[j] <= 0x7E):
                j += 1
            out.append(keys[i:j + 1])
            i = j + 1
        elif keys[i:i + 2] == b"\x1bO":              # application cursor keys
            out.append(keys[i:i + 3])
            i += 3
        else:
            out.append(keys[i:i + 1])
            i += 1
    return out


class Cell:
    __slots__ = ("ch", "fg", "bold", "rev")

    def __init__(self):
        self.ch, self.fg, self.bold, self.rev = " ", FG, False, False


WIDE_TERMINAL_SYMBOLS = {"⏎", "⚑", "✔", "⚠"}


def _cell_width(ch: str) -> int:
    if unicodedata.combining(ch) or ch in ("\ufe0e", "\ufe0f"):
        return 0
    return 2 if (ch in WIDE_TERMINAL_SYMBOLS
                 or unicodedata.east_asian_width(ch) in ("W", "F")) else 1


def _render_in_pty(args: list, keys: bytes, settle: float, owned_tmp: str,
                   cols: int = COLS, rows: int = ROWS) -> list:
    """Run the program in a pty, feed `keys`, return the final screen as a Cell grid."""
    pid, fd = pty.fork()
    if pid == 0:                                    # child
        os.environ.update(TERM="xterm-256color", LINES=str(rows), COLUMNS=str(cols),
                          LANG="en_US.UTF-8", TMPDIR=owned_tmp)
        os.execvp(sys.executable, [sys.executable, str(GMF)] + args)

    # Window size on the pty master. curses also honours LINES/COLUMNS (set in the
    # child env above) — belt and braces, because initscr() may run before we get here.
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    def read_until_quiet(quiet: float = 0.5, cap: float = 90.0,
                         first_wait: float = 60.0) -> bytes:
        """Read until the program stops drawing for `quiet` seconds.

        `first_wait` is generous on purpose: before the first byte appears, `--demo`
        builds a sandbox of 27 git repos, which takes seconds. Giving up after
        `quiet` there captured an empty screen."""
        got = b""
        start = time.time()
        last = None
        while time.time() - start < cap:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                got += chunk
                last = time.time()
                continue
            if last is None:
                if time.time() - start > first_wait:
                    break                            # nothing ever came
                continue
            if time.time() - last >= quiet:
                break                                # drawing has settled
        return got

    # Phase 1: let it draw the first screen. Phase 2: type, let it redraw. Doing this
    # explicitly (instead of "send keys whenever select is idle") is what makes the
    # capture reliable — the earlier version raced the drawing and caught a blank screen.
    buf = b""
    try:
        buf = read_until_quiet(quiet=settle)
        for chunk in _split_keys(keys):
            os.write(fd, chunk)
            buf += read_until_quiet(quiet=settle)
    finally:
        try:
            os.write(fd, b"q")                      # quit the TUI
        except OSError:
            pass                                    # already gone — fine
        time.sleep(0.2)
        try:
            os.close(fd)
        except OSError:
            pass
        # Always reap the exact child. Otherwise a failed capture can leave the next
        # one writing into a dead pty ("[Errno 5] Input/output error").
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    return replay(buf.decode("utf-8", "replace"), cols, rows)


def replay(text: str, cols: int = COLS, rows: int = ROWS) -> list:
    """Terminal output -> Cell grid, i.e. the screen the program would have painted.

    We handle only the escape sequences ncurses actually emits (cursor moves, SGR
    colours, erase). That is far less than a full terminal emulator, but it is exactly
    what we need — and it keeps this script readable. Everything the loop does not
    understand has to be *swallowed* rather than ignored, otherwise it lands on the
    screen as text or, worse, the following line is drawn in the wrong row.

    Kept separate from the pty plumbing on purpose: this way the replay is testable
    without a child process (see tests/test_gitmaster_flash.py).
    """
    grid = [[Cell() for _ in range(cols)] for _ in range(rows)]
    cy = cx = 0
    cur_fg, cur_bold, cur_rev = FG, False, False
    i = 0
    while i < len(text):
        m = CSI.match(text, i)
        if m:
            params, cmd = m.group(1), m.group(2)
            # Private sequences (ESC[?1049h, ESC[?25l …) only switch terminal modes we
            # do not model. They must still be *consumed*: the older regex did not match
            # the "?", so the ESC was skipped and "[?1049h" landed on the screen as text.
            if params.startswith("?"):
                i = m.end()
                continue
            nums = [int(x) for x in params.split(";") if x.isdigit()]
            first = nums[0] if nums else 1           # most commands default to 1
            if cmd in ("H", "f"):                   # cursor home / absolute row;col
                cy = (nums[0] - 1) if nums else 0
                cx = (nums[1] - 1) if len(nums) > 1 else 0
            elif cmd == "d":                        # absolute row, column unchanged
                # ncurses reaches for this one a lot (ESC[34d for the footer). Without
                # it the cursor stayed put and whole lines were drawn over each other —
                # the footer used to end up right below the list instead of at the
                # bottom of the window.
                cy = first - 1
            elif cmd in ("G", "`"):                 # absolute column, row unchanged
                cx = first - 1
            elif cmd == "A":                        # cursor up / down / right / left
                cy -= first
            elif cmd == "B":
                cy += first
            elif cmd == "C":
                cx += first
            elif cmd == "D":
                cx -= first
            elif cmd == "X":                        # erase n cells, cursor stays
                for x in range(cx, min(cols, cx + first)):
                    grid[cy][x] = Cell()
            elif cmd == "m":                        # colours / attributes
                for n in (nums or [0]):
                    if n == 0:
                        cur_fg, cur_bold, cur_rev = FG, False, False
                    elif n == 1:
                        cur_bold = True
                    elif n == 7:
                        cur_rev = True
                    elif n in ANSI:
                        cur_fg = ANSI[n]
            elif cmd == "J":                        # erase display
                mode = nums[0] if nums else 0
                if mode == 2:                       # whole screen
                    grid = [[Cell() for _ in range(cols)] for _ in range(rows)]
                elif mode == 0:                     # cursor -> end of screen
                    # curses sends the parameterless ESC[J when a new view (commit
                    # helper, pager) replaces the list. Ignoring it left the old
                    # overview bleeding through the new screen.
                    for x in range(cx, cols):
                        grid[cy][x] = Cell()
                    for y in range(cy + 1, rows):
                        grid[y] = [Cell() for _ in range(cols)]
                elif mode == 1:                     # start of screen -> cursor
                    for y in range(0, cy):
                        grid[y] = [Cell() for _ in range(cols)]
                    for x in range(0, cx + 1):
                        grid[cy][x] = Cell()
            elif cmd == "K":                        # erase line
                mode = nums[0] if nums else 0
                rng = (range(cx, cols) if mode == 0 else
                       range(0, cx + 1) if mode == 1 else range(cols))
                for x in rng:
                    grid[cy][x] = Cell()
            # A terminal parks the cursor at the edge instead of leaving the screen;
            # without this, a move beyond the last row would index past the grid.
            cy = max(0, min(rows - 1, cy))
            cx = max(0, min(cols - 1, cx))
            i = m.end()
            continue
        # ESC ( B / ESC ) 0 etc.: charset selection, 3 bytes. Skipping only two left
        # the trailing letter behind as literal text ("api-gatewayB").
        if text.startswith("\x1b(", i) or text.startswith("\x1b)", i):
            i += 3
            continue
        ch = text[i]
        if ch == "\r":
            cx = 0
        elif ch == "\n":
            cy, cx = min(cy + 1, rows - 1), 0
        elif ch == "\x08":                          # backspace: one cell to the left
            cx = max(0, cx - 1)
        elif ch == "\x1b":
            i += 1                                  # unknown escape: skip the byte
        elif ch >= " " and 0 <= cy < rows and 0 <= cx < cols:
            width = _cell_width(ch)
            if width == 0 and cx > 0:
                grid[cy][cx - 1].ch += ch
            else:
                c = grid[cy][cx]
                c.ch, c.fg, c.bold, c.rev = ch, cur_fg, cur_bold, cur_rev
                # SVG monospace text does not reliably reserve the second terminal
                # cell of emoji-style symbols, so represent continuation cells.
                for extra in range(1, min(width, cols - cx)):
                    c = grid[cy][cx + extra]
                    c.ch, c.fg, c.bold, c.rev = " ", cur_fg, cur_bold, cur_rev
                cx += width
        i += 1
    return grid


def render_in_pty(args: list, keys: bytes = b"", settle: float = 1.8,
                  cols: int = COLS, rows: int = ROWS) -> list:
    """Render inside one owned TMPDIR and clean exactly that directory in all cases."""
    # Keep the securely created unique path short enough that the complete demo
    # root and status summary both fit into the captured header before _tidy().
    with tempfile.TemporaryDirectory(prefix="gmfs-", dir="/tmp") as owned_tmp:
        return _render_in_pty(args, keys, settle, owned_tmp, cols, rows)


def _set_line(row: list, text: str) -> None:
    text = text.ljust(len(row))[:len(row)]
    for x, ch in enumerate(text):
        row[x].ch = ch


def _tidy(grid: list) -> None:
    """Two cosmetic repairs. Everything else stays exactly as the program drew it.

    1. The demo sandbox's random tmp path -> `~/git`. Needed for reproducibility
       (otherwise `--check` can never pass) and because
       `/tmp/gmfs-.../gmf-demo-.../gmf-demo` in a header is pure noise.
    2. Leftovers of the scan progress counter ("6/97/98/99/9"). While scanning, the
       program prints `6/9`, `7/9` … on the first list line; the repo line then drawn
       over it is shorter, and curses does not bother clearing the tail because it
       knows the terminal already shows the right thing. Our reconstruction starts
       from an empty grid and cannot know that, so the digits stick around.
    """
    for row in grid:
        line = "".join(c.ch for c in row)
        m = re.search(r"/\S*?gmf-demo-\S*?(?:/gmf-demo)?(?=\s|$)", line)
        if m:
            _set_line(row, line[:m.start()] + "~/git" + line[m.end():])
            line = "".join(c.ch for c in row)
        m = re.search(r"(?:\d+/\d+){2,}\s*$", line)
        if m:
            _set_line(row, line[:m.start()])


def _trim(grid: list) -> list:
    """Drop blank rows below the last drawn line.

    The terminal is a fixed rectangle, so the program leaves the rest of the screen
    empty below the help bar. On GitHub that turned into a tall grey void under the
    picture; an SVG has no reason to keep it."""
    last = 0
    for y, row in enumerate(grid):
        if any(c.ch.strip() or c.rev for c in row):
            last = y
    return grid[:last + 1]


def to_svg(grid: list, title: str) -> str:
    """Cell grid -> SVG with selectable text."""
    cw, ch, pad = 8.4, 17.0, 12
    rows, cols = len(grid), len(grid[0])
    w, h = int(cols * cw + 2 * pad), int(rows * ch + 2 * pad)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" '
        f'height="{h}" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" '
        f'font-size="13">',
        f'<title>{title}</title>',
        f'<rect width="{w}" height="{h}" rx="8" fill="{BG}"/>',
    ]
    for y, row in enumerate(grid):
        # selected line (curses A_REVERSE) -> draw the highlight bar
        runs = []
        x = 0
        while x < cols:
            c = row[x]
            x2 = x
            while x2 < cols and row[x2].rev == c.rev and row[x2].fg == c.fg \
                    and row[x2].bold == c.bold:
                x2 += 1
            runs.append((x, x2, c))
            x = x2
        for x0, x1, c in runs:
            s = "".join(row[i].ch for i in range(x0, x1))
            if not s.strip():
                continue
            px, py = pad + x0 * cw, pad + (y + 1) * ch - 4
            if c.rev:
                out.append(f'<rect x="{px:.1f}" y="{pad + y * ch:.1f}" '
                           f'width="{(x1 - x0) * cw:.1f}" height="{ch}" fill="{c.fg}"/>')
            fill = BG if c.rev else c.fg
            esc = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            weight = ' font-weight="bold"' if c.bold else ""
            out.append(f'<text x="{px:.1f}" y="{py:.1f}" fill="{fill}"'
                       f'{weight} xml:space="preserve">{esc}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


# Only list screens are captured. The commit helper, the pager and the info view are
# shown as plain code blocks in the README instead — deliberately:
#
# The list view is drawn onto a freshly cleared screen, so replaying the escape codes
# reproduces it exactly. Any view opened LATER (commit helper, pager, info) is painted
# OVER the list, and curses only sends the cells it believes changed. Reconstructing
# that needs far more terminal-state reconstruction than the overview — going through
# the info view left stray lines of it behind on the list underneath. The renderer does
# account for the wide symbols used here, but deliberately remains a small replay tool
# rather than a complete terminal emulator.

# Two real actions before every capture, so the command log shows what it is for
# instead of "(none yet)": pop the stash of the third repo, then pull the one that is
# a commit behind. Both are plain list-view keys — no overlay view involved. `L` and
# not `P` on purpose: the fast-forward merge fits into the pane in full, while the
# leased push line would be cut off mid-hash at the right edge.
ACTIONS = DOWN + DOWN + b"Uy" + RIGHT + RIGHT + UP + b"Ly"

# Die Demo-Sandbox hat mehr Repos als `compact_from`, startet also kompakt. Für das
# Detailbild schaltet ein "m" zurück — beide Ansichten sollen dokumentiert sein. Die
# Höhe je Bild ist bewusst knapp gewählt: das Fenster soll gefüllt aussehen, nicht
# halb leer.
SCREENS = [
    ("compact.svg", "en", [], ACTIONS,
     "gitmaster_flash compact view — the whole collection at a glance", COLS, 20),
    ("command-log.svg", "en", [], ACTIONS + TAB,
     "gitmaster_flash — the command log with the focus on it", COLS, 20),
    ("overview.svg", "en", [], ACTIONS + b"m",
     "gitmaster_flash detail view — problem repos sorted to the top", COLS, 35),
    ("compact.de.svg", "de", [], ACTIONS,
     "gitmaster_flash Kompaktansicht — alle Repos auf einen Blick", COLS, 20),
    ("command-log.de.svg", "de", [], ACTIONS + TAB,
     "gitmaster_flash — das Befehlsprotokoll mit Fokus", COLS, 20),
    ("overview.de.svg", "de", [], ACTIONS + b"m",
     "gitmaster_flash Detailansicht — problematische Repos zuerst", COLS, 35),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the files would change (do not write)")
    args = ap.parse_args()

    rc = 0
    for name, lang, extra, keys, title, cols, rows in SCREENS:
        grid = render_in_pty(["--demo", "--lang", lang] + extra, keys=keys,
                             cols=cols, rows=rows)
        _tidy(grid)
        svg = to_svg(_trim(grid), title)
        if len([1 for row in grid for c in row if c.ch.strip()]) < 50:
            print(f"{name}: screen looks empty — pty capture failed", file=sys.stderr)
            return 2
        p = HERE / name
        if args.check:
            if not p.exists() or p.read_text() != svg:
                print(f"{name}: would change", file=sys.stderr)
                rc = 1
            continue
        p.write_text(svg)
        print(f"wrote {p.relative_to(HERE.parent)} ({len(svg) // 1024} KB)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
