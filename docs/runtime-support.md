# Runtime support and repeatable checks

[English](runtime-support.md) · [Deutsch](runtime-support.de.md)

The full feature set targets Python 3.9 or later and Git 2.36 or later on macOS
and Linux. Python 3.9 supplies the string operations used by the single-file
program; Git 2.36 supplies NUL-delimited worktree registration output. See the
[Python string API](https://docs.python.org/3.9/library/stdtypes.html#str.removeprefix)
and [Git 2.36 worktree format](https://git-scm.com/docs/git-worktree/2.36.0).
An older Git that cannot prove an inventory produces an unavailable state;
there is no guessed fallback to newline-delimited paths.

The TUI needs standard-library curses, a POSIX terminal and a UTF-8 locale.
No third-party Python modules are required. macOS app shortcuts use `open -a`;
Linux starts a configured executable launcher in a local desktop session,
passing the selected repository as one folder argument. SSH sessions remain blocked. The Linux installer (`install-linux.sh`) supports Bash, Fish and Zsh, including
Enter-to-cd. The original macOS installer and build/release scripts need Zsh.
On a Linux host without Zsh its Zsh-specific tests are skipped. The optional
release-checksum verification also needs `shasum` and is skipped when it is absent.
Windows and OS versions beyond the recorded checks have not been validated.

## Reproduce a check

```sh
python3 tools/check-compatibility.py
python3 tools/check-compatibility.py --python python3.9 --python python3.14 --suite core
python3 tools/check-compatibility.py --python python3.15 --git /usr/bin/git --screens
```

Each combination records interpreter, Git and product versions, the source hash,
exit codes, duration and test count. Full is the default and includes the existing
safety, CLI and release fixtures. Core selects only the separately maintained
feature-contract suites; it does not substitute for the full safety suite.
`--screens` verifies the six generated images. No runtime is installed or updated.
Temporary PATH wrappers select the requested Git without changing global settings.
An unavailable or below-floor runtime is a failed check, not a skipped success.

## Validation record, updated 2026-11-06

### 0.32.0 — Linux installation

| System | Python | Git | Result |
|---|---|---|---|
| macOS, arm64 | 3.15.0 | 2.56.0 | 868 executed, one Fish check skipped; six identical images |
| CachyOS, x86_64 | 3.14.9 | 2.56.0 | 868 executed, one optional shasum check skipped |
| Linux Mint 22.2, x86_64 | 3.12.3 | 2.43.0 | 813 executed, 56 Zsh/Fish checks skipped |

The complete installer ran on both Linux systems with temporary user homes:
automatic Fish/Bash selection, followed by real `gmf --version` and JSON calls.
Repeated registration left existing files unchanged. Shell checks preserve paths
with trailing newlines and the original exit code; conflicting startup files
stay protected. No real user configuration was installed or changed.


### 0.30.3

| System | Python | Git | Result |
|---|---|---|---|
| macOS 26.7.1, arm64 | 3.15.0 | 2.56.0 | 842 tests and six identical images |
| macOS 26.7.1, arm64 | 3.9.6 | 2.56.0 | 157 core checks |
| Linux, x86_64 | 3.12.3 | 2.43.0 | 788 executed, 54 zsh checks skipped |

Four regressions were reproduced before correction. Tests cover HEAD changes
including unborn/detached states, ref changes across graph reads, and overflowing
JSON exponents. Real English/German terminal flows retain the original repo
during an all-remote fetch when the work view changes selection. Real CLI runs
report a raced scan with exit 1 and a JSON overflow with exit 2.

### 0.30.2

| System | Python | Git | Result |
|---|---|---|---|
| macOS 26.7.1, arm64 | 3.15.0 | 2.56.0 | 828 tests and six identical images |
| macOS 26.7.1, arm64 | 3.9.6 | 2.56.0 | 144 core checks including 13 integration cases |
| macOS 26.7.1, arm64 | 3.14.8 | 2.56.0 | 13 integration cases |
| Linux Mint 22.2, x86_64 | 3.12.3 | 2.43.0 | 774 executed, 54 zsh checks skipped; six identical images |

The HEAD OIDs of all 27 demo repositories also match between macOS and Linux.
Each regression builds two complete sandboxes to check independence from the
temporary root path. The committed submodule URL is relative; local clone setup
stays offline.

### 0.30.1

| System | Python | Git | Result |
|---|---|---|---|
| macOS 26.7.1, arm64 | 3.15.0 | 2.56.0 | 827 tests and six identical images |
| macOS 26.7.1, arm64 | 3.9.6 | 2.56.0 | 143 core checks including twelve integration cases |
| macOS 26.7.1, arm64 | 3.14.8 | 2.56.0 | twelve new integration cases |
| Linux Mint 22.2, x86_64 | 3.12.3 | 2.43.0 | 773 executed, 54 zsh checks skipped; six identical images |

Real terminal flows ran in English and German: a selected LF-path commit, an
unchanged neighboring literal-backslash-n path, preserved foreign staging, an
offline comparison with Git/SSH disabled, and a confirmed push to a temporary
local bare remote. Commit suggestions retained disabled module contents. Only
test repositories were changed.

### 0.30.0

| System | Python | Git | Result |
|---|---|---|---|
| macOS 26.7.1, arm64 | 3.9.6 | 2.56.0 | 815 tests; Python also pinned for shell subprocesses; six images identical |
| macOS 26.7.1, arm64 | 3.15.0 | 2.56.0 | 815 tests and `build.sh --check` pass |
| macOS 26.7.1, arm64 | 3.9.6 / 3.14.8 | 2.54.0 Apple / 2.56.0 | 131 core checks each, four combinations |
| Linux Mint 22.2, x86_64 | 3.12.3 | 2.43.0 | 761 executed, 54 skipped because zsh is absent; six images identical |

The zsh checks ran on macOS. Matrix output records skipped tests explicitly. A version floor
is a code/feature requirement, not evidence that every intermediate release
has been run. The JSON reports distinguish full checks from core checks.

## Scan measurement

`python3 tools/benchmark-scan.py` creates one temporary repo, three local bare
remotes and either one or 25 branches on the same commit. It records five local
scans and five fetch scans for each case; setup is excluded. It contacts no
external host and removes the temporary repos. Results include individual times,
medians and Git call counts. Use the same interpreter/Git and comparable host
load for timing comparisons. [Recorded before/after data](scan-benchmark.json)
shows 57 → 33 calls locally and 160 → 136 with fetch for 25 identical tips.
The one-branch case stays at 33 / 136. Timing varied with host load; process counts
are the stable result. Ref/HEAD rechecks and per-transfer configuration checks
are retained. Counts are shared only inside one pinned ref inventory.

The recorded `consistency_guard` measurement for 0.30.3 adds four checks across
the complete graph measurement. Both the one- and 25-branch cases use 37 local
or 140 fetch-scan calls. These checks detect changes between remote counters,
branch inventory and upstream reads; the scan reports a local measurement error.
