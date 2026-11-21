# Installation

[English](installation.md) · [Deutsch](installation.de.md) · [Overview](../README.md)

Gitmaster Flash requires Python 3.9+, Git 2.36+ and a UTF-8 terminal with curses.
The program uses the standard library. Keep the checkout or unpacked source
archive at a stable path.

Run directly from that directory with `python3 gitmaster_flash.py ~/projects`.
Enter prints the selected repository path. The optional `gmf` shell function
starts the same program and uses that path to change the calling shell's
directory.

## Register the shell function

Run the appropriate command as your own user.

```sh
./install.sh          # Zsh by default on macOS
./install-linux.sh    # Bash, Fish or Zsh from $SHELL on Linux
```

The entry points share an installer. On Linux, `./install.sh` also selects
the shell from `$SHELL`. Override the selection with `--shell bash`,
`--shell fish` or `--shell zsh`. `--shell auto` selects via `$SHELL` on
macOS as well.

Both entry points accept `--check` to run runtime checks and self-tests alone.
A normal install runs these checks before writing any registration files.
Install any missing system packages through your system's package manager.

| Shell | Files used |
|---|---|
| Zsh | A marked block in `${ZDOTDIR:-$HOME}/.zshrc` sources the project's `gmf.zsh`. |
| Bash | A marked block in `$HOME/.bashrc` sources `${XDG_CONFIG_HOME:-$HOME/.config}/gitmaster_flash/shell/gmf.bash`. |
| Fish | Autoloads `${XDG_CONFIG_HOME:-$HOME/.config}/fish/functions/gmf.fish`. |

Start a new shell to load the function. Verify the registration with
`gmf --version`, then try `gmf ~/projects`. The wrapper preserves the
program's exit status for scripts.

Bash reads `.bashrc` in interactive non-login shells. A login profile needs
to source `.bashrc` for `gmf` to be available in login shells.

## Existing installations and moved checkouts

The installer keeps working registrations for the same checkout, including
recognized direct Zsh source lines and managed Linux Zsh wrappers from
previous versions. Repeating installation leaves identical files untouched.
A startup file changed by the installer gets a dated `.gmf-backup-*` copy.
Use absolute paths for the home and configuration directories.

The installer reads startup files without running their commands. It stops
when it finds a foreign registration, a conflicting wrapper or a position
where it cannot establish that its new block will run. If registration depends
on dynamically sourced files, check availability with `gmf --version` in a new
shell. Read the diagnostic and inspect the named file before editing it. For a moved checkout, update a
direct Zsh source line to the new path. For a managed installation, remove
the corresponding installer block and its generated wrapper, if present,
before running the installer from the new path. Fish uses just the autoload
file. Keep any unrelated shell configuration intact.

## Desktop app shortcuts

App shortcuts use macOS `open -a` or an absolute path to a Linux executable
launcher. The selected repository is passed as one folder argument. The
shortcut reports the launcher exit status and a bounded diagnostic.
A successful exit describes the launcher process. App shortcuts require a
local desktop session. See [configuration](configuration.md) for the
`apps` setting.
