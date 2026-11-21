# Command-line reference

[English](cli.md) · [Deutsch](cli.de.md) · [Overview](../README.md)

Use `--help` for a short flag reference and `--demo` for temporary example repositories.
`--view` narrows the inventory to a work view alongside the path filter.
See the [interface guide](interface.md) for the view names and selection rules.
`--fetch-remote NAME` implies `--fetch` and refreshes the named remote in each
scanned repository. Pass a repository as the root to refresh that one repository.

## Repository scans

```sh
python3 gitmaster_flash.py --list          # colored text list
python3 gitmaster_flash.py --json          # machine-readable
python3 gitmaster_flash.py --json --fetch  # fetch each repo first
python3 gitmaster_flash.py --filter api    # only repos whose path matches
python3 gitmaster_flash.py --lang de       # force the output language
python3 gitmaster_flash.py --version       # bare version number, nothing else

# The list header and JSON snapshot include the program version.
```

`--filter` takes the same terms as `/` in the UI and works with `--list`,
`--json` and `--diff`; started interactively it preselects the filter, which `/`
then changes. With `--diff` it narrows **both** machines by the same terms —
filtering only one side would report "only here" differences that do not exist.
Every output names the active filter: the `--list` header, a structured `filter`
object in JSON (query plus matching and total repository counts), and a leading
line in the `--diff` text report that gives both machines' counts. A filter
without a single hit says so on stderr — on stdout an empty list,
or "no differences", together with exit code 0 would otherwise look exactly like
"everything is fine". The `--list` header also counts the hidden repositories
that would need attention (`(+7 hidden)`), the same way the UI header does: a
single `✔` line with exit code 0 must not look like a clean estate while the
filter is quietly hiding fifty dirty repositories.

Exit code 0 means everything is clean and in sync, 1 means at least one
repository needs attention; it is computed over the repositories that passed the
filter. Without a TTY the tool prints the list instead of
starting the UI, so a pipe does the sensible thing.

One further flag exists but is not meant to be typed: `--cd-file` names the file
the `gmf` shell wrapper reads the selected repository path from. The wrapper
passes it; calling it by hand has no use.

## Comparing two machines over SSH

If you keep the same repos on more than one machine (laptop + desktop, Mac + Linux),
they drift apart in ways git never warns you about. **Remotes live in `.git/config`
and git never transfers them** — add a `github` remote on one machine and the other
simply doesn't have it, so a pending push is invisible there. Same for branches you
don't currently have checked out.

```sh
gmf --diff buildbox            # compare ~/git here with ~/git on buildbox
gmf --diff buildbox --json     # machine-readable
gmf --diff buildbox:~/code     # different directory on the other side
gmf --diff buildbox --fetch    # refresh ahead/behind counts first
```

It prints **only the differences**, split into classes — that split is the point,
a report that lists everything gets ignored:

```
DRIFT  website: remote 'github' only here (git never transfers remotes)
DRIFT  notes: origin is 4 ahead/2 behind here, 0/0 on buildbox
SYNC   music: origin 0 ahead/3 behind on both machines
local  webapp: [main] here, [feature/x] on buildbox
local  blog: 3 changed/new file(s) here
only on buildbox: experiment
```

`DRIFT` also covers errors, conflicts, stashes, branch availability, remote safety
classification (including whether fetch/push URLs are executable without exposing
credentials) and credential-free endpoint fingerprints. A remote's configured URL
is never serialized as such — only its fingerprint. Credentials never enter JSON
at all: a quoted Git error line keeps the target address (host and repository
path) as evidence, but its user info, query and fragment are removed before it
gets there. `DRIFT` = should be identical but isn't (worth
acting on). `SYNC` = both machines agree, but together they sit ahead/behind the
sync remote — invisible in a pure two-machine comparison, yet usually the number
you actually care about. `local` = explainable (different branch checked out,
dirty working tree). Exit code **0** =
nothing to report, **1** = findings (note: a `SYNC` line means the machines agree
with each other, so "identical machines" alone no longer guarantees exit 0),
**2** = the other machine could not be reached.

**Requirements:** `ssh HOST` has to work — that's it. gitmaster_flash does **not** need
to be installed on the other machine: the script is piped over stdin, so the remote
only needs `python3` and `git`, and both sides always run the exact same version (no
version drift to reason about). Works against Linux too.

For the same checked-out branch, gmf also compares the full commit IDs. Equal
ahead/behind counts can describe different commits; those are reported as `DRIFT`,
not as a shared `SYNC` state. Older JSON without commit IDs remains readable but
cannot prove commit equality. Active Git operations are reported as local work,
even with a clean working tree. JSON carries `head_oid` (null before the first
commit, empty when unknown) and the stable `operation` code.

The comparison also checks every local branch name and full commit ID, including
branches checked out on neither machine. A shared non-current branch with commits
outside known remote refs produces a `SYNC` finding. Differences in cached coverage
are labelled local; they do not prove different server contents. JSON adds
`branches`, `branches_error` and `branch_coverage_basis: "local-remote-tracking-refs"`.
Each branch has its name, full `oid`, `current` flag, full upstream ref, upstream
remote, ahead/behind counts, merge state, and `coverage` (`covered`, `uncovered`,
`unknown`) with `uncovered_commits` (null when unknown). An unmeasured inventory
is omitted; older payloads without it cannot prove branch equality and do not
invent missing branches. Neither comparison nor inventory changes any ref.

**Branch, index, and working tree stay untouched** — no remotes are added and
nothing is committed or pushed. It tells you what differs; fixing is yours.
Two things it does write: `--fetch` runs a real fetch on both machines — that is
what the flag is for — with tag fetching, tag pruning, and submodule recursion
disabled. For each remote it fetches only the advertised object ID of the current
branch, without a local target ref, then updates only the matching
`refs/remotes/<remote>/<branch>` ref with `--no-deref`. The checked URL is passed
through a one-time alias; unsafe remotes are reported instead of run. The
very first run also creates
`~/.config/gitmaster_flash/config.json`, on the queried machine as well, because the
same script runs there.

**Tip:** put your machines in `~/.ssh/config` and add
`ControlMaster auto` / `ControlPath ~/.ssh/cm-%C` / `ControlPersist 60s`. A `--fetch`
scan may open up to eight ssh connections across repositories at once (each Git fetch
itself uses one job), and the sshd default (`MaxStartups 10:30:100`) can otherwise drop
some of them at random — which looks like a broken repo but isn't.

## Saved comparisons and color output

```sh
gmf --json > before.json
# ... later ...
gmf --json > after.json
gmf --compare before.json after.json
gmf --compare before.json after.json --json --view changes
gmf --list --color never
```

`--compare` emits a text report by default or JSON with `--json`. It reads only
the two files, without scanning, SSH, fetch or config creation. Saved path searches must match. Save complete snapshots and apply
work views when comparing, so an excluded counterpart does not become a false
missing repo. Creation times, local measurement times and unknown capabilities
remain visible; time differences alone are not drift. Incompatible files exit 2.

JSON carries `schema_version: 1`, independently of the program version.
The JSON comparison includes readable `differences` and stable `findings`
with types, categories, sides and values. Older object snapshots remain readable
with explicit limits. The complete contract is in
[JSON format and finding types](json-format.md).

`--color auto` is the default for `--list`: colors only at a terminal and without
`NO_COLOR` in the environment. `always` forces them even through a pipe and takes
precedence over `NO_COLOR`. `never` suppresses them. JSON and comparison reports
use their own colorless formats. The curses UI continues to use the terminal
palette.

## Diff layers and command-line previews

Within `A`, use `1` for all changes, `2` for staged changes (HEAD → index),
and `3` for unstaged changes (index → working tree, including new files). The
header names the active layer and the file list follows it. Empty layers remain
switchable. A staged-and-edited file can show different changes in each layer. `C` continues
to commit selected current working-tree paths and preserves staging elsewhere;
the diff layer affects previews.
Before the first commit, staged compares against the empty starting tree.
Submodules show Gitlink references and a dirty-content marker; `O` explains the
contents. Diff previews do not run external diff or text-conversion programs.
They preserve index entries and file contents; Git may update its index stat cache.

The same reader is available without a scan or config creation:

```sh
gmf /path/to/repo --file-diff src/main.py --diff-mode staged
gmf /path/to/repo --file-diff src/main.py --diff-mode unstaged --json
```

The root must be the working-tree root, and the file path relative to it.
`--diff-mode` accepts `combined` (default), `staged` or `unstaged` and requires
`--file-diff`. This command cannot be combined with fetches, comparisons or scan
filters. Text escapes terminal controls while retaining line breaks. JSON is a
separate `kind: "file_diff"` result containing `schema_version`, `version`,
`root`, `path`, `path_key`, `mode`, `ok`, `diff` and `error`; it is not a saved repo snapshot.
The `diff` field contains escaped preview text; it is unsuitable for `git apply`.
Git administration paths such as `.git/config` are excluded. Exit codes: 0 for
no difference, 1 for a difference, 2 for failure.

## Stash report

For a read-only report with the same file grouping, use `gmf REPO --stashes`
(or `python3 gitmaster_flash.py REPO --stashes --json`). It starts no scan or
network access and applies/deletes nothing. Exit codes: 0 for a report (including
an empty stash list), 2 for a read error or invalid options. JSON uses the result
kind `stashes`, with stash OIDs, selectors and subjects plus `files` containing
`path`, byte-preserving `path_key`, working/staged status codes, `untracked` and
labelled `diff` lines. The usual `--list`/snapshot `--json` stash counts and exit
codes remain unchanged.
