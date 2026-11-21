# Changelog

This file records selected changes by version. For the full history, see the
Git log.

## [0.34.3]

### Fixed

- Scans report damaged worktree paths and use the same stash count as the detail view.
- File diffs show staged deletions in combined mode. Snapshot comparisons accept
  unknown branch coverage with an omitted count.
- Remote checks preserve URL boundaries, reject overlapping removal namespaces,
  and bind fetch updates to the tracking ref observed before the network request.
- Index cleanup releases each lock once. The editor launcher reports exit status
  even when diagnostics are unavailable.
- Shell registration protects continued Bash definitions and prints a lossless
  activation command. The Zsh wrapper cleans up after a failed invocation.
- The compact view hints at GitHub publication only for a unique public remote.

## [0.34.2]

### Changed

- The macOS and Linux entry points use the same installer. Both support
  `--check`, and Linux Zsh uses the project's shared Zsh wrapper for new
  registrations. Recognized existing registrations continue to work.
- The project overview now links to separate installation, interface,
  configuration, command-line and developer guides in English and German.
- Source releases retain the installer, its self-tests and the related
  documentation. Repository workflow metadata is excluded from the archive.

### Fixed

- The `gmf` shell function preserves the repository and file arguments for
  command-line diff previews.
- Transfer checks detect dirty submodules even when the repository is
  configured to ignore them.

## [0.34.1] - 2026-11-15

### Fixed

- Machine comparisons treat matching GitHub content as current even when the
  commit history differs. Raw counts remain available.
- Failed GitHub content checks report the affected side, remote and error,
  including when a work-view filter is active or a fetch also failed.

## [0.22.17] - 2026-10-03

### Fixed

- Remote removal rejects custom or shared tracking namespaces. Remote names
  and refs retain Unicode line separators.
- Settings keep the selected entry visible on five-line terminals.
- A hangup during release installation completes or rolls back the archive
  and checksum pair before cleanup.
- JSON escapes unpaired surrogates while keeping ordinary Unicode readable.

## [0.22.16] - 2026-10-02

### Fixed

- Transfer checks use the effective Git configuration. Remote-removal
  previews count the original commit graph and use the true upstream remote.
- Small terminals keep the selected setting on screen, and compact status
  marks show full ahead/behind counts when space permits.
- Invalid UTF-8 in the configuration falls back to defaults. GitHub links
  preserve valid URL escapes. The Zsh wrapper preserves paths ending in
  newlines.
- The release script archives the commit it actually checked and protects
  the archive and checksum against interruption during placement.
