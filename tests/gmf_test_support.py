"""Gemeinsame Test-Fixtures ohne Demo-Daten und Benutzerkonfiguration."""

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import gitmaster_flash as gmf_module


def isolated_git_environment(root: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.pop("SSH_CONNECTION", None)
    env.pop("SSH_TTY", None)
    return dict(
        env,
        HOME=str(root),
        XDG_CONFIG_HOME=str(root / ".config"),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_ATTR_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL="file",
        GIT_AUTHOR_NAME="Test User",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test User",
        GIT_COMMITTER_EMAIL="test@example.invalid",
        LC_ALL="C",
    )


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return result.stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q", "--template=", "-b", "main")
    return path


def commit_file(repo: Path, path: str, content: str, message: str) -> None:
    (repo / path).write_text(content, encoding="utf-8")
    git(repo, "--literal-pathspecs", "add", "--", path)
    git(repo, "commit", "-qm", message)


def create_repo(root: Path, name: str = "project") -> Path:
    repo = init_repo(root / name)
    commit_file(repo, "README.md", f"# {name}\n", "initial commit")
    return repo


def add_local_remote(repo: Path, bare: Path, name: str = "origin", *, upstream=False) -> None:
    bare.mkdir(parents=True)
    git(bare, "init", "-q", "--template=", "--bare", "-b", "main")
    git(repo, "remote", "add", name, str(bare))
    options = ("-u",) if upstream else ()
    git(repo, "push", "-q", *options, name, "main")


def mock_screen(*, size=(24, 80), keys=()):
    screen = Mock()
    screen.getmaxyx.return_value = size
    screen.getch.side_effect = keys
    return screen


class FakeScreen:
    """Gemeinsame curses-Methoden für Bildschirme mit einem eigenen Testverhalten."""

    def erase(self):
        pass

    def clear(self):
        pass

    def addstr(self, *args):
        pass

    def move(self, *args):
        pass

    def refresh(self):
        pass

    def timeout(self, *args):
        pass

    def keypad(self, *args):
        pass

    def attrset(self, *args):
        pass


def latest_stash(repo, timeout):
    entries = gmf_module.read_stashes(repo, timeout)
    return (entries[0].oid, entries[0].label) if entries else None


def stash_preview(repo, timeout, oid=None):
    """Bestehende Sicherheitsproben über die aktuellen Dateileser ausführen."""
    if oid is None:
        entry = latest_stash(repo, timeout)
        if entry is None:
            return False, "no stash"
        oid = entry[0]
    try:
        files = gmf_module.stash_files(repo, oid, timeout)
        return True, "\n".join(
            line for item in files for line in gmf_module.stash_file_diff(repo, oid, item, timeout)
        )
    except (gmf_module.GitReadError, OSError) as error:
        return False, str(error)
