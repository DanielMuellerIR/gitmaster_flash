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
uses generic app names + English, exactly like `--demo`.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import termios
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GMF = HERE.parent / "gitmaster_flash.py"

COLS, ROWS = 100, 26

# xterm-ish palette. Only what the TUI actually uses.
FG = "#d8d8d8"
BG = "#1c1c1c"
ANSI = {
    30: "#3b3b3b", 31: "#e06c75", 32: "#98c379", 33: "#e5c07b",
    34: "#61afef", 35: "#c678dd", 36: "#56b6c2", 37: "#d8d8d8",
    90: "#7f7f7f", 91: "#e06c75", 92: "#98c379", 93: "#e5c07b",
    94: "#61afef", 95: "#c678dd", 96: "#56b6c2", 97: "#ffffff",
}


def _cleanup_sandboxes() -> None:
    """Remove the throwaway demo sandboxes this script created.

    `--demo` builds one per run under $TMPDIR (we pin TMPDIR=/tmp to keep the path
    short) and never deletes it — fine for a human trying it out, litter for a
    generator. Only ever touches /tmp/gmf-demo-* : unambiguous, ours, disposable."""
    for d in Path("/tmp").glob("gmf-demo-*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


def _split_keys(keys: bytes) -> list:
    """Split a key string into single keypresses: b"\x1b[B\x1b[Bc" -> [down, down, c].

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
        else:
            out.append(keys[i:i + 1])
            i += 1
    return out


class Cell:
    __slots__ = ("ch", "fg", "bold", "rev")

    def __init__(self):
        self.ch, self.fg, self.bold, self.rev = " ", FG, False, False


def render_in_pty(args: list, keys: bytes = b"", settle: float = 1.8) -> list:
    """Run the program in a pty, feed `keys`, return the final screen as a Cell grid.

    We parse only the escape sequences curses actually emits here (absolute cursor
    moves, SGR colours, erase). That is far less than a full terminal emulator, but it
    is exactly what we need — and it keeps this script readable.
    """
    grid = [[Cell() for _ in range(COLS)] for _ in range(ROWS)]
    cy = cx = 0
    cur_fg, cur_bold, cur_rev = FG, False, False

    pid, fd = pty.fork()
    if pid == 0:                                    # child
        os.environ.update(TERM="xterm-256color", LINES=str(ROWS), COLUMNS=str(COLS),
                          LANG="en_US.UTF-8", TMPDIR="/tmp")
        os.execvp(sys.executable, [sys.executable, str(GMF)] + args)

    # Window size on the pty master. curses also honours LINES/COLUMNS (set in the
    # child env above) — belt and braces, because initscr() may run before we get here.
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    def read_until_quiet(quiet: float = 0.5, cap: float = 20.0,
                         first_wait: float = 15.0) -> bytes:
        """Read until the program stops drawing for `quiet` seconds.

        `first_wait` is generous on purpose: before the first byte appears, `--demo`
        builds a sandbox of nine git repos, which takes seconds. Giving up after
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
    buf = read_until_quiet()
    for chunk in _split_keys(keys):
        os.write(fd, chunk)
        buf += read_until_quiet()
    try:
        os.write(fd, b"q")                          # quit the TUI
    except OSError:
        pass                                        # already gone — fine, we have the screen
    time.sleep(0.2)
    try:
        os.close(fd)
    except OSError:
        pass
    # Reap properly. A half-reaped child from the previous screen left the next capture
    # writing into a dead pty ("[Errno 5] Input/output error").
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    _cleanup_sandboxes()

    text = buf.decode("utf-8", "replace")
    i = 0
    while i < len(text):
        m = re.compile(r"\x1b\[([0-9;]*)([A-Za-z])").match(text, i)
        if m:
            params, cmd = m.group(1), m.group(2)
            nums = [int(x) for x in params.split(";") if x.isdigit()]
            if cmd == "H":                          # cursor home / absolute
                cy = (nums[0] - 1) if nums else 0
                cx = (nums[1] - 1) if len(nums) > 1 else 0
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
                    grid = [[Cell() for _ in range(COLS)] for _ in range(ROWS)]
                elif mode == 0:                     # cursor -> end of screen
                    # curses sends the parameterless ESC[J when a new view (commit
                    # helper, pager) replaces the list. Ignoring it left the old
                    # overview bleeding through the new screen.
                    for x in range(cx, COLS):
                        grid[cy][x] = Cell()
                    for y in range(cy + 1, ROWS):
                        grid[y] = [Cell() for _ in range(COLS)]
                elif mode == 1:                     # start of screen -> cursor
                    for y in range(0, cy):
                        grid[y] = [Cell() for _ in range(COLS)]
                    for x in range(0, cx + 1):
                        grid[cy][x] = Cell()
            elif cmd == "K":                        # erase line
                mode = nums[0] if nums else 0
                rng = (range(cx, COLS) if mode == 0 else
                       range(0, cx + 1) if mode == 1 else range(COLS))
                for x in rng:
                    grid[cy][x] = Cell()
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
            cy, cx = min(cy + 1, ROWS - 1), 0
        elif ch == "\x1b":
            i += 1                                  # unknown escape: skip the byte
        elif ch >= " " and 0 <= cy < ROWS and 0 <= cx < COLS:
            c = grid[cy][cx]
            c.ch, c.fg, c.bold, c.rev = ch, cur_fg, cur_bold, cur_rev
            cx += 1
        i += 1
    return grid


def _set_line(row: list, text: str) -> None:
    text = text.ljust(len(row))[:len(row)]
    for x, ch in enumerate(text):
        row[x].ch = ch


def _tidy(grid: list) -> None:
    """Two cosmetic repairs. Everything else stays exactly as the program drew it.

    1. The demo sandbox's random tmp path -> `~/git`. Needed for reproducibility
       (otherwise `--check` can never pass) and because
       `/var/folders/gn/vs63.../gmf-demo-8a2f/gmf-demo` in a header is pure noise.
    2. Leftovers of the scan progress counter ("6/97/98/99/9"). While scanning, the
       program prints `6/9`, `7/9` … on the first list line; the repo line then drawn
       over it is shorter, and curses does not bother clearing the tail because it
       knows the terminal already shows the right thing. Our reconstruction starts
       from an empty grid and cannot know that, so the digits stick around.
    """
    for row in grid:
        line = "".join(c.ch for c in row)
        m = re.search(r"(/var/folders/\S*?gmf-demo|/tmp/gmf-demo\S*?)(/gmf-demo)?(?=\s|$)",
                      line)
        if m:
            _set_line(row, line[:m.start()] + "~/git" + line[m.end():])
            line = "".join(c.ch for c in row)
        m = re.search(r"(?:\d+/\d+){2,}\s*$", line)
        if m:
            _set_line(row, line[:m.start()])


def to_svg(grid: list, title: str) -> str:
    """Cell grid -> SVG with selectable text."""
    cw, ch, pad = 8.4, 17.0, 12
    w, h = int(COLS * cw + 2 * pad), int(ROWS * ch + 2 * pad)
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
        while x < COLS:
            c = row[x]
            x2 = x
            while x2 < COLS and row[x2].rev == c.rev and row[x2].fg == c.fg \
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


# Only the first screen is captured. The commit helper is shown as a plain code block
# in the README instead — deliberately:
#
# The list view is drawn onto a freshly cleared screen, so replaying the escape codes
# reproduces it exactly. Any view opened LATER (commit helper, pager) is painted OVER
# the list, and curses only sends the cells it believes changed. Reconstructing that
# needs real cell-width accounting: ⏎, ⚑ and ✔ occupy two terminal columns but one
# string index, so our grid drifts by one and the old line bleeds through. Getting that
# right means writing a full terminal emulator — not worth it for a screenshot.
SCREENS = [
    ("overview.svg", [], b"", "gitmaster_flash overview — problem repos sorted to the top"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the files would change (do not write)")
    args = ap.parse_args()

    rc = 0
    for name, extra, keys, title in SCREENS:
        grid = render_in_pty(["--demo", "--lang", "en"] + extra, keys=keys)
        _tidy(grid)
        svg = to_svg(grid, title)
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
