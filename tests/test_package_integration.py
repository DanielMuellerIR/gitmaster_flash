"""Grenzen zwischen Pfadanzeige, Snapshots, Diff-Ebenen und Commit-Auswahl."""
import copy
import curses
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import gitmaster_flash as g


class PackageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gmf-integration-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        environment = patch.dict(os.environ, g._demo_env(), clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        language = patch.object(g, 'UI_LANG', 'en')
        language.start()
        self.addCleanup(language.stop)
        self.cfg = copy.deepcopy(g.DEFAULT_CONFIG)
        self.repo, _ = g._demo_repo(self.root, 'project')
        g.COMMAND_LOG.clear()
        self.addCleanup(g.COMMAND_LOG.clear)

    def status(self):
        return g.collect_status(self.repo, self.root, self.cfg)

    def snapshot(self):
        return g.make_snapshot(self.root, [self.status()])

    def gitlinks(self, paths):
        oid = g.current_head(self.repo, 10)
        for path in paths:
            (self.repo / path).mkdir(parents=True)
            g._dgit(self.repo, 'update-index', '--add', '--cacheinfo', '160000', oid, path)
        g._dgit(self.repo, 'commit', '-qm', 'module references')

    def ui(self, status, keys=()):
        screen = Mock()
        screen.getmaxyx.return_value = (30, 110)
        screen.getch.side_effect = keys
        ui = g.TUI(screen, self.root, self.cfg, None)
        ui.all_statuses = [status]
        ui.statuses = [status]
        return ui

    def test_two_gitlinks_with_identical_old_display_names_keep_valid_identities(self):
        self.gitlinks(('deps/line\nname', 'deps/line\\nname'))
        payload = self.snapshot()
        modules = payload['repos'][0]['submodules']
        self.assertEqual(len(modules), 2)
        self.assertEqual(modules[0]['path'], modules[1]['path'])
        self.assertNotEqual(modules[0]['path_key'], modules[1]['path_key'])
        self.assertTrue(g._valid_remote_payload(payload))
        for module in modules:
            raw = bytes.fromhex(module['path_key']).decode('utf-8', 'surrogateescape')
            self.assertEqual(g.terminal_text(raw), module['path'])
        json.dumps(payload, ensure_ascii=False).encode('utf-8')

    def test_demo_commit_ids_are_identical_under_different_roots(self):
        inventories = []
        for name in ('one', 'two'):
            base = self.root / name
            base.mkdir()
            root = g.build_demo_sandbox(base)
            repos = g.find_repos(root, self.cfg['skip_dirs'])
            self.assertEqual(len(repos), 27)
            inventories.append({str(repo.relative_to(root)): g.current_head(repo, 10) for repo in repos})
            module = root / 'iot-firmware'
            self.assertEqual(g.run_git(module, 'config', '-f', '.gitmodules', '--get',
                                       'submodule.library.url').stdout.strip(), '../notes-vault.git')
            g._dgit(module, 'submodule', 'sync', '--', 'deps/library')
            self.assertEqual(Path(g.run_git(module, 'config', '--get',
                                            'submodule.library.url').stdout.strip()).resolve(),
                             (root / '_remotes/notes-vault.git').resolve())
        self.assertEqual(inventories[0], inventories[1])

    def test_worktree_registry_with_display_aliases_is_not_rejected(self):
        for branch, path in (('one', 'line\nname'), ('two', 'line\\nname')):
            g._dgit(self.repo, 'worktree', 'add', '-qb', branch, str(self.root / path))
        status = self.status()
        self.assertEqual(status.worktrees_error, '')
        members = g.status_dict(status)['worktree_family']['members']
        self.assertEqual(len(members), 3)
        self.assertEqual(len({entry['path_key'] for entry in members}), 3)
        self.assertTrue(g._valid_remote_payload(g.make_snapshot(self.root, [status])))
        shown = '\n'.join(g.worktree_lines(status, [status]))
        self.assertIn('line\\nname', shown)
        self.assertIn('line\\\\nname', shown)

    def test_comparison_pairs_the_changed_gitlink_by_bytes(self):
        self.gitlinks(('deps/line\nname', 'deps/line\\nname'))
        before = self.snapshot()
        source, _ = g._demo_repo(self.root, 'source')
        oid = g.current_head(source, 10)
        g._dgit(self.repo, 'update-index', '--cacheinfo', '160000', oid, 'deps/line\nname')
        after = self.snapshot()
        findings = [row for row in g.diff_findings(before, after, 'here', 'there')
                    if row['type'] == 'submodule_state']
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]['values']['path_key'], g.snapshot_path_key('deps/line\nname'))
        self.assertEqual(findings[0]['values']['here_state']['index_oid'],
                         before['repos'][0]['submodules'][0]['index_oid'])
        self.assertEqual(findings[0]['values']['there_state']['index_oid'], oid)

    def test_legacy_ambiguous_paths_limit_the_comparison_instead_of_guessing(self):
        self.gitlinks(('deps/line\nname', 'deps/line\\nname'))
        current = self.snapshot()
        legacy = copy.deepcopy(current)
        legacy['repos'][0]['submodules'] = legacy['repos'][0]['submodules'][:1]
        legacy['repos'][0]['submodules'][0].pop('path_key')
        self.assertTrue(g._valid_remote_payload(legacy))
        findings = g.diff_findings(legacy, current, 'here', 'there')
        self.assertFalse(any(row['type'] == 'submodule_state' for row in findings))
        limits = g.snapshot_limitations(legacy, 'here', current)
        self.assertIn({'side': 'here', 'code': 'submodule_path_identity_unknown', 'count': 1}, limits)

    def test_limited_empty_comparison_does_not_claim_complete_equality(self):
        self.gitlinks(('deps/line\nname',))
        current = self.snapshot()
        legacy = copy.deepcopy(current)
        legacy['repos'][0]['submodules'][0].pop('path_key')
        for payload in (legacy, current):
            payload['repos'][0]['remotes'] = []
        output = io.StringIO()
        with redirect_stdout(output):
            code = g.print_comparison(legacy, current, 'before', 'after', as_json=False, offline=True)
        self.assertEqual(code, 0)
        self.assertIn(g.t('snapshot_same_limited'), output.getvalue())
        self.assertIn('ambiguous legacy path identity', output.getvalue())

    def test_ordinary_legacy_path_still_pairs_without_an_extra_limit(self):
        self.gitlinks(('deps/lib',))
        current = self.snapshot()
        legacy = copy.deepcopy(current)
        legacy['repos'][0]['submodules'][0].pop('path_key')
        self.assertTrue(g._valid_remote_payload(legacy))
        self.assertFalse(any(row['type'] == 'submodule_state' for row in
                             g.diff_findings(legacy, current, 'here', 'there')))
        self.assertFalse(any(row['code'] == 'submodule_path_identity_unknown' for row in
                             g.snapshot_limitations(legacy, 'here', current)))

    def test_path_keys_must_describe_the_displayed_relative_git_path(self):
        self.gitlinks(('deps/lib',))
        payload = self.snapshot()
        for value in (None, 1, '', '0', 'GG', 'AA', '00',
                      g.snapshot_path_key('../escape'), g.snapshot_path_key('/absolute'),
                      g.snapshot_path_key('different')):
            with self.subTest(value=value):
                broken = copy.deepcopy(payload)
                broken['repos'][0]['submodules'][0]['path_key'] = value
                self.assertFalse(g._valid_remote_payload(broken))
        broken = copy.deepcopy(payload)
        broken['repos'][0]['submodules'].append(copy.deepcopy(broken['repos'][0]['submodules'][0]))
        self.assertFalse(g._valid_remote_payload(broken))

    def test_path_labels_distinguish_literal_escapes_without_changing_commands(self):
        for control, literal in (('line\nname', 'line\\nname'), ('line\rname', 'line\\rname'),
                                 ('line\tname', 'line\\tname'), ('line\udcff', 'line\\udcff')):
            self.assertNotEqual(g.display_git_path(control), g.display_git_path(literal))
            self.assertNotEqual(g.file_row('M', control)[0], g.file_row('M', literal)[0])
        command = g.format_git_command(('add', '--', 'line\nname'))
        self.assertIn("$'line\\nname'", command)
        self.assertNotIn("$'line\\\\nname'", command)

    def test_repo_selection_names_distinguish_literal_path_escapes(self):
        states = [g.RepoStatus(self.root / name, name) for name in ('line\nname', 'line\\nname')]
        names = [row[1] for row in g.compact_cells(states)]
        self.assertNotEqual(names[0], names[1])
        self.assertNotEqual(g.selected_repo_context(states[0], 110),
                            g.selected_repo_context(states[1], 110))
        output = io.StringIO()
        with redirect_stdout(output):
            g.print_list(states, color='never')
        self.assertIn('line\\nname', output.getvalue())
        self.assertIn('line\\\\nname', output.getvalue())

    def test_submodule_contents_stay_disabled_when_a_suggestion_is_applied(self):
        g._demo_commit(self.repo, 'other.md', 'base\n', 'other file')
        source, _ = g._demo_repo(self.root, 'source')
        g._dgit(self.repo, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(source), 'deps/lib')
        g._demo_harden(self.repo / 'deps/lib')
        g._dgit(self.repo, 'commit', '-qam', 'module')
        (self.repo / 'deps/lib/draft.txt').write_text('module draft\n')
        (self.repo / 'README.md').write_text('parent draft\n')
        (self.repo / 'other.md').write_text('other draft\n')
        (self.repo / 'notes.txt').write_text('notes\n')
        status = self.status()
        modules = {module.path: module for module in status.submodules}
        items = [{'path': entry.path, 'include': False,
                  'committable': g.committable_against_head(entry, modules.get(entry.path))}
                 for entry in status.files]
        ui = self.ui(status)
        before = g.read_index_entries(self.repo, 10)
        with patch.object(ui, '_choose_commit_group',
                          side_effect=lambda groups: next(group for group in groups if 'README.md' in group.paths)):
            ui._apply_commit_group(status, items)
        selected = {item['path'] for item in items if item['include']}
        self.assertEqual(selected, {'README.md', 'other.md'})
        self.assertFalse(next(item for item in items if item['path'] == 'deps/lib')['committable'])
        self.assertEqual(before, g.read_index_entries(self.repo, 10))

    def test_last_commit_step_rejects_a_disabled_selected_item(self):
        status = self.status()
        ui = self.ui(status)
        items = [{'path': 'module', 'include': True, 'committable': False}]
        with patch.object(g, 'commit_selected') as commit, patch.object(ui, 'prompt_line') as prompt:
            self.assertTrue(ui._commit_step2(status, items))
        commit.assert_not_called()
        prompt.assert_not_called()
        self.assertEqual(ui.message, g.t('nothing_selected'))

    def test_selected_control_path_commit_keeps_neighbor_and_foreign_staging(self):
        lf, literal = 'line\nname.txt', 'line\\nname.txt'
        for path in (lf, literal, 'z-foreign.txt'):
            (self.repo / path).write_text('base\n')
            g._dgit(self.repo, 'add', '--', path)
        g._dgit(self.repo, 'commit', '-qm', 'paths')
        for path in (lf, literal, 'z-foreign.txt'):
            (self.repo / path).write_text('changed\n')
        g._dgit(self.repo, 'add', 'z-foreign.txt')
        before = self.snapshot()
        self.assertIn('+changed', g.file_diff(self.repo, 'M', lf, 10, 'unstaged')[1])
        status = self.status()
        ui = self.ui(status)
        items = [{'path': entry.path, 'include': entry.path == lf, 'committable': True}
                 for entry in status.files]
        with patch.object(ui, 'prompt_line', return_value='record selected path'), \
                patch.object(ui, 'show_busy'), patch('gitmaster_flash.curses.color_pair', return_value=0), \
                patch('gitmaster_flash.curses.flushinp'):
            self.assertTrue(ui._commit_step2(status, items))
        self.assertEqual(g.run_git(self.repo, 'show', 'HEAD:' + lf).stdout, 'changed\n')
        self.assertEqual(g.run_git(self.repo, 'show', 'HEAD:' + literal).stdout, 'base\n')
        staged = g.run_git(self.repo, 'diff', '--cached', '--name-only', '-z').stdout.split('\0')
        self.assertEqual([path for path in staged if path], ['z-foreign.txt'])
        self.assertIn('+changed', g.file_diff(self.repo, 'M', literal, 10, 'unstaged')[1])
        after = self.snapshot()
        self.assertTrue(g._valid_remote_payload(after))
        output = io.StringIO()
        with redirect_stdout(output):
            code = g.print_comparison(before, after, 'before', 'after', as_json=True, offline=True)
        self.assertEqual(code, 1)
        self.assertTrue(any(row['type'] == 'head' for row in json.loads(output.getvalue())['findings']))
