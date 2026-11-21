# Configuration

[English](configuration.md) · [Deutsch](configuration.de.md) · [Overview](../README.md)

Use `,` in the interface or edit the JSON configuration file.

## Settings in the interface (`,`)

`,` opens a list of the settings, `↑`/`↓` picks one, `⏎` changes it. The
list scrolls in small terminals, including the read-only entries at the end.
The line above the footer explains whatever you are standing on. A changed value takes
effect at once and is written back to `config.json` — atomically, via a
neighbouring temporary file, so an interrupted write cannot leave you with half
a config and none of your settings.

Editable here: language, when the compact view starts, the four timeouts, and
the folders the scan skips. Every input is checked before it is stored — a
timeout of `abc` never reaches the file, and a skipped folder must be a *name*,
because the scan compares it against single path segments and an entry like
`a/b` could never match.

Two groups stay in the file on purpose, and the view shows them read-only with
that note:

- `sync_remote_names` / `sync_remote_hosts` decide which remote counts as your
  private sync remote — and therefore where `P` pushes. Changing a push target
  does not belong behind one keystroke in a list you are scrolling through.
- `apps` holds program paths that gmf starts with `open -a`. A path typed into a
  one-line field is not a path anyone has verified.

Showing them read-only beats hiding them: a setting you cannot find is worse
than one you have to edit in a file.

## Configuration file

`~/.config/gitmaster_flash/config.json`, created on first run:

Values loaded from this file use the same ranges and choices as the settings
interface. Invalid entries fall back individually with a warning; the file is
preserved for correction. Folder and remote lists must contain strings only.

- `apps` — key → application used to open a repository (macOS `open -a`, Linux executable launcher). The key
  shows up in the footer automatically, so `{"Z": {"name": "Zed", "path":
  "/Applications/Zed.app"}}` gives you `Z Zed`. Pick a key that is not already
  taken by the table above. Inside an SSH session gmf refuses this action with a
  clear explanation: an app can only open in your local desktop session.
- `sync_remote_names` / `sync_remote_hosts` — how the private sync remote is
  recognized: by remote name, or by an exact normalized host in the remote URL
  (substring matches are never accepted). Defaults to `origin`
  for a generic installation. All remotes are displayed regardless; GitHub is
  recognized from its URL and sorted last.
- `skip_dirs` — directories the scan does not descend into.
- `publication_excludes` — explicit literal paths omitted from the GitHub content
  comparison, per `owner/repo`, for example
  `{"example/project": ["internal/reviews", "docs/private-notes.md"]}`.
  Directory paths include their contents; wildcards have no special meaning.
  No paths are excluded by default. These rules only affect the comparison;
  they never remove files from a push or relax its approval checks. JSON records
  `content_equal` (`true`, `false`, or `null` when unknown), `content_excludes`,
  `content_error` and `history_differs` per remote, alongside the raw commit counts.
- `lang` — `"en"`, `"de"`, or `null` to follow `$LANG`.
- `compact_from` — when the repository count exceeds this number, gmf starts in
  the compact view (default 20: up to 20 use the detail view; `M` switches at any
  time).
- `git_timeout` — seconds per local git call (default 10).
- `fetch_timeout` — seconds per fetch call (default 30).
- `diff_timeout` — hard wall-clock limit for the complete SSH comparison
  (default 3600 seconds). Individual Git calls still use `git_timeout` or
  `fetch_timeout`; SSH connection setup has a separate ten-second limit.
- `commit_timeout` — seconds for `git commit` alone (default 120). It runs the
  repository's pre-commit hook, which often starts linters or tests and needs far
  longer than `git_timeout`. When the limit is hit, git *and* everything the hook
  started are terminated, and gmf reports it instead of aborting.
