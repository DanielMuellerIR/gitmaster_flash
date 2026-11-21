"""Remote-Konfiguration aus echten Git-Repos ohne Netz lesen."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gitmaster_flash as g


class RemoteConfigurationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gmf-remote-config-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.global_config = self.root / "global.cfg"
        env = {**g._demo_env(), "GIT_CONFIG_GLOBAL": str(self.global_config)}
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.repo = self.root / "project"
        g._dgit(self.root, "init", "-q", "-b", "main", str(self.repo))

    def test_targeted_and_bulk_reads_keep_includes_order_and_effective_urls(self):
        self.global_config.write_text(
            '[url "https://example.invalid/general/"]\n insteadOf = alias:\n'
            '[url "https://example.invalid/specific/"]\n insteadOf = alias:deep/\n'
            '[url "ssh://example.invalid/"]\n pushInsteadOf = alias:\n'
        )
        included = self.root / "remotes.cfg"
        first_fetch = "+refs/heads/*:refs/remotes/Team.Core/*"
        second_fetch = "^refs/heads/ignored"
        included.write_text(
            f'[remote "Team.Core"]\n url = alias:deep/first.git\n fetch = {first_fetch}\n'
            f" url = alias:second.git\n fetch = {second_fetch}\n"
            '[remote "team.core"]\n url = alias:lowercase.git\n'
            '[remote "Team.Core.extra"]\n url = alias:neighbor.git\n'
        )
        g._dgit(self.repo, "config", "include.path", str(included))

        remote = g.read_remote_config_or_none(self.repo, g.DEFAULT_CONFIG, "Team.Core")
        self.assertIsNotNone(remote)
        self.assertEqual(remote, g.read_remote_configs(self.repo, g.DEFAULT_CONFIG)["Team.Core"])
        self.assertEqual(
            remote.settings,
            [
                ("url", "alias:deep/first.git"),
                ("fetch", first_fetch),
                ("url", "alias:second.git"),
                ("fetch", second_fetch),
            ],
        )
        self.assertEqual(
            remote.fetch_urls,
            [
                "https://example.invalid/specific/first.git",
                "https://example.invalid/general/second.git",
            ],
        )
        self.assertEqual(
            remote.push_urls,
            [
                "ssh://example.invalid/deep/first.git",
                "ssh://example.invalid/second.git",
            ],
        )

        g._dgit(self.repo, "config", "remote.Team.Core.pushurl", "alias:deep/explicit.git")
        remote = g.read_remote_config_or_none(self.repo, g.DEFAULT_CONFIG, "Team.Core")
        self.assertIsNotNone(remote)
        self.assertEqual(remote.push_urls, ["https://example.invalid/specific/explicit.git"])

    def test_unusable_neighbor_url_does_not_hide_requested_remote(self):
        g._dgit(self.repo, "remote", "add", "origin", "../remote.git")
        g._dgit(self.repo, "config", "remote.other.url", "\n")

        with self.assertRaises(g.GitReadError):
            g.read_remote_configs(self.repo, g.DEFAULT_CONFIG)
        remote = g.read_remote_config_or_none(self.repo, g.DEFAULT_CONFIG, "origin")
        self.assertIsNotNone(remote)
        self.assertEqual(remote.fetch_urls, ["../remote.git"])

    def test_dotted_neighbor_does_not_authorize_a_legacy_remote(self):
        g._dgit(self.repo, "remote", "add", "origin.extra", "../neighbor.git")
        legacy = self.repo / ".git" / "remotes"
        legacy.mkdir()
        (legacy / "origin").write_text("URL: ../legacy.git\n")

        self.assertIsNone(g.read_remote_config_or_none(self.repo, g.DEFAULT_CONFIG, "origin"))
        self.assertEqual(list(g.read_remote_configs(self.repo, g.DEFAULT_CONFIG)), ["origin.extra"])

    def test_transport_guard_prioritizes_git_proxy_over_earlier_http_proxy(self):
        g._dgit(self.repo, "config", "http.proxy", "http://proxy.example.invalid:9")
        g._dgit(self.repo, "config", "core.gitProxy", "none")

        self.assertEqual(g.repo_transport_override(self.repo, 10), "core.gitProxy")


if __name__ == "__main__":
    unittest.main()
