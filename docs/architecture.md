# Architecture and development

[English](architecture.md) · [Deutsch](architecture.de.md) · [Overview](../README.md)

## Program and shell function

The command line and the curses interface share one Python program,
[gitmaster_flash.py](../gitmaster_flash.py). The program reads Git status,
refs and configuration into repository records. List output, saved
snapshots and the TUI use those records. The SSH comparison sends the same
file to the other machine over standard input, which lets both ends use
the same version without installing it remotely.

Git calls are non-interactive and use captured output, bounded execution
time and separate process groups. A stopped background fetch cancels that
scan's processes. If a local tracking-ref update already started, its
configuration check and any rollback continue with bounded local Git calls.
The interface waits at most five seconds for its scan worker. Commands that
change Git state enter the session log. Git's machine-readable path
protocols preserve bytes, including newlines. The terminal view escapes
control characters, while `path_key` in the [JSON format](json-format.md)
preserves the original identity of supported paths.

The installer entry points delegate to [tools/install.py](../tools/install.py).
The shell function stays in the calling shell for Enter-to-cd. It forwards
normal CLI arguments and exit status and reads the selected path from a
temporary file. See the [installation guide](installation.md) for file
locations and supported shells.

## Tests and demo images

```sh
python3 -m unittest discover -s tests
python3 docs/make-screens.py --check
```

The tests exercise pure logic, real temporary Git repositories, the CLI
and shell integration. The repository's GitHub Actions workflow runs the
suite and generated-image check on macOS and Linux. See the
[runtime record](runtime-support.md) for dated results and commands to
repeat the compatibility checks.

The image generator runs the real program in its own pseudo-terminal.
Fixed commit metadata and isolated Git settings make the demo inventory
repeatable. The generator waits for the expected command-log entry after
confirmed actions and renders the list from the terminal output.
Overlaid views such as the commit helper use checked text examples in the
[interface guide](interface.md).

Run `./build.sh` after a UI change to test and regenerate the images.
`./build.sh --check` runs the tests and compares the generated output with
the committed images. The build and release scripts use Zsh.

## Static checks

The development tools are pinned in `requirements-dev.txt`. Run them with
Python 3.11 in a virtual environment. Mypy 0.991 checks the application, Python
tools and screenshot generator against Python 3.9. Ruff 0.14.14 checks imports,
common errors and formatting across the Python source.

```sh
python3.11 -m venv /tmp/gmf-checks
. /tmp/gmf-checks/bin/activate
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
mypy
shellcheck install.sh install-linux.sh
for file in build.sh release.sh gmf.zsh tools/check-zshrc.zsh; do zsh -n "$file"; done
```

For a shorter functional check, run
`python3 tools/check-compatibility.py --suite core`. Installation and release
still run the full suite to cover the Shell and Git safety contracts.

## Scan measurement

`python3 tools/benchmark-scan.py --baseline /path/to/previous/gitmaster_flash.py`
runs both versions alternately on the same temporary repositories and checks
the results for differences other than measurement times. The
[recorded measurements](scan-benchmark.json) use three local bare remotes and
one or 25 branches at the same tip. From 0.34.1 to 0.34.2, local scans went
from 37 to 35 Git calls and fetch scans went from 140 to 88 in both cases.
Git still provides the effective URLs, and the HEAD, ref and configuration
checks run at the same points. Timings depend on host load.

## Source release

```sh
./release.sh
```

The release script requires all tracked changes to be committed. It
checks tests and images with `./build.sh --check`, archives the commit
it checked, then unpacks the result to verify the program version.
It writes `dist/gitmaster_flash-<version>.tar.gz` and a matching
`.sha256` file without overwriting an existing result. The archive
includes the tests and documentation needed to run the installer
and build checks from the extracted files.
