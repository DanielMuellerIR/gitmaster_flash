"""Diff-Ebenen und ihre CLI ändern weder Vormerkung noch Dateiinhalt."""
import copy
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

import gitmaster_flash as g


class DiffModeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gmf-diff-modes-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        environment = patch.dict(os.environ, g._demo_env(), clear=True)
        environment.start(); self.addCleanup(environment.stop)
        language = patch.object(g, 'UI_LANG', 'en')
        language.start(); self.addCleanup(language.stop)
        self.repo, _ = g._demo_repo(self.root, 'project')
        self.cfg = copy.deepcopy(g.DEFAULT_CONFIG)
        g.COMMAND_LOG.clear(); self.addCleanup(g.COMMAND_LOG.clear)

    def diff(self, mode, path='README.md', code='M'):
        before = g.read_index_entries(self.repo, 10)
        ok, text = g.file_diff(self.repo, code, path, 10, mode)
        self.assertTrue(ok, text)
        self.assertEqual(before, g.read_index_entries(self.repo, 10))
        self.assertEqual(g.COMMAND_LOG, [])
        return text

    def mixed(self):
        (self.repo/'README.md').write_text('staged version\n')
        g._dgit(self.repo, 'add', 'README.md')
        (self.repo/'README.md').write_text('working version\n')


class DiffModeTests(DiffModeFixture):
    def test_mixed_file_has_three_different_comparisons(self):
        self.mixed()
        staged = self.diff('staged'); unstaged = self.diff('unstaged'); combined = self.diff('combined')
        self.assertIn('-# project', staged); self.assertIn('+staged version', staged)
        self.assertNotIn('working version', staged)
        self.assertIn('-staged version', unstaged); self.assertIn('+working version', unstaged)
        self.assertIn('-# project', combined); self.assertIn('+working version', combined)
        self.assertNotIn('staged version', combined)
        self.assertEqual((self.repo/'README.md').read_text(), 'working version\n')

    def test_unborn_staged_and_working_versions_stay_separate(self):
        self.repo = self.root/'unborn'; self.repo.mkdir(); g._dgit(self.repo, 'init', '-q', '-b', 'main')
        g._demo_harden(self.repo); (self.repo/'new.txt').write_text('index\n');g._dgit(self.repo,'add','new.txt')
        (self.repo/'new.txt').write_text('worktree\n')
        self.assertIn('+index', self.diff('staged','new.txt'))
        self.assertIn('-index', self.diff('unstaged','new.txt'))
        self.assertIn('+worktree', self.diff('combined','new.txt'))
        self.assertIsNone(g.current_head(self.repo,10))

    def test_untracked_is_absent_from_staged_but_present_in_other_modes(self):
        (self.repo/'new.txt').write_text('new content\n')
        self.assertEqual(self.diff('staged','new.txt','U'), '')
        for mode in ('combined','unstaged'):
            self.assertIn('+new content', self.diff(mode,'new.txt','U'))

    def test_staged_deletion_and_untracked_replacement_are_distinct(self):
        g._dgit(self.repo,'rm','--cached','README.md')
        status = g.collect_status(self.repo,self.root,self.cfg)
        staged = g.files_for_diff_mode(status.files,'staged');unstaged = g.files_for_diff_mode(status.files,'unstaged')
        self.assertEqual([(e.code,e.path) for e in staged],[('D','README.md')])
        self.assertEqual([(e.code,e.path) for e in unstaged],[('U','README.md')])
        self.assertIn('-# project',self.diff('staged'))
        self.assertIn('+# project',self.diff('unstaged',code='U'))

    def test_staged_rename_and_later_edit_have_separate_layers(self):
        g._dgit(self.repo,'mv','README.md','renamed.md')
        (self.repo/'renamed.md').write_text('# project\nextra worktree line\n')
        staged = self.diff('staged','renamed.md');unstaged = self.diff('unstaged','renamed.md')
        self.assertIn('+# project',staged);self.assertNotIn('extra worktree line',staged)
        self.assertIn('+extra worktree line',unstaged)
        self.assertTrue(g.files_for_diff_mode(g.collect_status(self.repo,self.root,self.cfg).files,'staged'))

    def test_binary_and_mode_changes_are_not_empty(self):
        (self.repo/'binary.dat').write_bytes(b'base\0data');g._dgit(self.repo,'add','binary.dat');g._dgit(self.repo,'commit','-qm','binary')
        (self.repo/'binary.dat').write_bytes(b'index\0data');g._dgit(self.repo,'add','binary.dat')
        (self.repo/'binary.dat').write_bytes(b'worktree\0data')
        for mode in g.DIFF_MODES:self.assertIn('Binary files',self.diff(mode,'binary.dat'))
        g._dgit(self.repo,'config','core.filemode','true');(self.repo/'README.md').chmod(0o755)
        self.assertIn('new mode 100755',self.diff('unstaged'))
        g._dgit(self.repo,'add','README.md');self.assertIn('new mode 100755',self.diff('staged'))

    def test_literal_crlf_paths_do_not_select_a_neighbor(self):
        for path,body in (('line\rname.txt','CR'),('line\nname.txt','LF'),(':(glob)*.txt','magic'),('other.txt','other')):
            (self.repo/path).write_text(body+' base\n');g._dgit(self.repo,'--literal-pathspecs','add','--',path)
        g._dgit(self.repo,'commit','-qm','paths')
        for path,body in (('line\rname.txt','CR'),('line\nname.txt','LF'),(':(glob)*.txt','magic'),('other.txt','other')):
            (self.repo/path).write_text(body+' index\n');g._dgit(self.repo,'--literal-pathspecs','add','--',path);(self.repo/path).write_text(body+' work\n')
        for mode in g.DIFF_MODES:
            text=self.diff(mode,'line\rname.txt');self.assertIn('CR ',text);self.assertNotIn('LF ',text)
            text=self.diff(mode,':(glob)*.txt');self.assertIn('magic ',text);self.assertNotIn('other ',text)

    def test_no_layer_runs_external_diff_or_textconv(self):
        marker=self.root/'driver-ran';driver=self.root/'driver'
        driver.write_text('#!/bin/sh\ntouch '+shlex.quote(str(marker))+'\n');driver.chmod(0o755)
        (self.repo/'.gitattributes').write_text('README.md diff=custom\n');g._dgit(self.repo,'add','.gitattributes');g._dgit(self.repo,'commit','-qm','attributes')
        g._dgit(self.repo,'config','diff.custom.textconv',str(driver));g._dgit(self.repo,'config','color.ui','always');self.mixed()
        with patch.dict(os.environ,{'GIT_EXTERNAL_DIFF':str(driver)}):
            for mode in g.DIFF_MODES:self.assertNotIn('\x1b',self.diff(mode))
        self.assertFalse(marker.exists())

    def test_replaced_head_does_not_change_any_layer(self):
        self.mixed();g._dgit(self.repo,'commit','-qm','replacement');replacement=g.current_head(self.repo,10)
        g._dgit(self.repo,'reset','--mixed','-q','HEAD^');real=g.current_head(self.repo,10)
        g._dgit(self.repo,'replace',real,replacement);self.mixed()
        self.assertIn('-# project',self.diff('combined'));self.assertIn('-# project',self.diff('staged'))
        self.assertIn('-staged version',self.diff('unstaged'))

    def test_submodule_reference_and_dirty_contents_are_read_only(self):
        source,_=g._demo_repo(self.root,'source');g._dgit(self.repo,'-c','protocol.file.allow=always','submodule','add','-q',str(source),'module');module=self.repo/'module';g._demo_harden(module)
        g._dgit(self.repo,'commit','-qam','module');base=g.current_head(module,10)
        g._demo_commit(module,'one.txt','one\n','one');index=g.current_head(module,10);g._dgit(self.repo,'add','module')
        g._demo_commit(module,'two.txt','two\n','two');work=g.current_head(module,10);(module/'draft.txt').write_text('draft\n')
        g._dgit(self.repo,'config','diff.submodule','diff')
        staged=self.diff('staged','module');unstaged=self.diff('unstaged','module');combined=self.diff('combined','module')
        self.assertIn(base,staged);self.assertIn(index,staged);self.assertNotIn(work,staged)
        self.assertIn(index,unstaged);self.assertIn(work,unstaged);self.assertIn('-dirty',unstaged)
        self.assertIn(base,combined);self.assertIn(work,combined)
        self.assertEqual((module/'draft.txt').read_text(),'draft\n')

    def test_unmerged_path_is_available_in_every_mode(self):
        g._dgit(self.repo,'checkout','-qb','side');g._demo_commit(self.repo,'README.md','side\n','side')
        g._dgit(self.repo,'checkout','-q','main');g._demo_commit(self.repo,'README.md','main\n','main')
        result=g.run_git(self.repo,'merge','side');self.assertNotEqual(result.returncode,0)
        status=g.collect_status(self.repo,self.root,self.cfg)
        for mode in g.DIFF_MODES:
            self.assertTrue(g.files_for_diff_mode(status.files,mode));self.assertTrue(self.diff(mode))

    def test_mode_filters_preserve_mixed_entries_and_new_files(self):
        files=[g.ChangedFile('M','index','M '),g.ChangedFile('M','work',' M'),g.ChangedFile('M','both','MM'),g.ChangedFile('U','new','??'),g.ChangedFile('C','conflict','UU')]
        self.assertEqual([e.path for e in g.files_for_diff_mode(files,'staged')],['index','both','conflict'])
        self.assertEqual([e.path for e in g.files_for_diff_mode(files,'unstaged')],['work','both','new','conflict'])
        self.assertEqual(g.files_for_diff_mode(files,'combined'),files)
        with self.assertRaises(ValueError):g.files_for_diff_mode(files,'invalid')

    def test_tui_switches_empty_mode_back_to_working_files(self):
        (self.repo/'new.txt').write_text('new\n');status=g.collect_status(self.repo,self.root,self.cfg)
        screen=Mock();screen.getmaxyx.return_value=(15,65);screen.getch.side_effect=[ord('2'),10,ord('3'),10,ord('q')]
        ui=g.TUI(screen,self.root,self.cfg,None);ui.statuses=[status]
        with patch('gitmaster_flash.curses.color_pair',return_value=0),patch.object(ui,'show_pager') as pager:
            ui.action_file_changes()
        self.assertEqual(pager.call_count,1);self.assertIn('unstaged',pager.call_args.args[0]);self.assertIn('+new','\n'.join(pager.call_args.args[1]))
        self.assertTrue(any(g.t('diff_mode_empty') in str(call) for call in screen.addstr.call_args_list))

    def test_cli_json_is_typed_and_does_not_scan_or_create_config(self):
        self.mixed();output=io.StringIO()
        with patch.object(g,'collect_all',side_effect=AssertionError('scan')),patch.object(g,'load_config',side_effect=AssertionError('config')),redirect_stdout(output):
            result=g.main([str(self.repo),'--file-diff','README.md','--diff-mode','staged','--json'])
        payload=json.loads(output.getvalue());self.assertEqual(result,1);self.assertEqual(payload['kind'],'file_diff');self.assertEqual(payload['mode'],'staged');self.assertTrue(payload['ok']);self.assertIn('+staged version',payload['diff']);self.assertNotIn('working version',payload['diff'])

    def test_cli_empty_and_failed_diffs_have_distinct_exits(self):
        output=io.StringIO()
        with redirect_stdout(output):result=g.main([str(self.repo),'--file-diff','README.md','--diff-mode','staged','--json'])
        self.assertEqual(result,0);self.assertEqual(json.loads(output.getvalue())['diff'],'')
        for path in ('../outside','/absolute','missing.txt','', '.git/config', '.GIT/config'):
            output=io.StringIO()
            with redirect_stdout(output):result=g.main([str(self.repo),'--file-diff',path,'--json'])
            self.assertEqual(result,2,path);self.assertFalse(json.loads(output.getvalue())['ok'])

    def test_cli_rejects_incompatible_actions_before_any_scan(self):
        for extra in (['--fetch'],['--diff','example'],['--compare','a','b'],['--demo'],['--filter','x'],['--view','changes'],['--list']):
            with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as stopped:
                g.main([str(self.repo),'--file-diff','README.md',*extra])
            self.assertEqual(stopped.exception.code,2)
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):g.main(['--diff-mode','staged'])

    def test_cli_requires_repository_root(self):
        sub=self.repo/'nested';sub.mkdir();output=io.StringIO()
        with redirect_stdout(output):result=g.main([str(sub),'--file-diff','README.md','--json'])
        self.assertEqual(result,2);self.assertIn('root',json.loads(output.getvalue())['error'])

    def test_cli_preserves_lines_but_neutralizes_terminal_controls(self):
        (self.repo/'new.txt').write_text('one\n\x1b[31mtwo\rthree\n')
        output=io.StringIO()
        with redirect_stdout(output):result=g.main([str(self.repo),'--file-diff','new.txt'])
        self.assertEqual(result,1);self.assertIn('+one\n',output.getvalue());self.assertNotIn('\x1b',output.getvalue());self.assertIn('\\r',output.getvalue())

    def test_failure_without_stderr_is_not_an_empty_success(self):
        result = subprocess.CompletedProcess([], 128, stdout='', stderr='')
        with patch.object(g, 'run_git', return_value=result):
            ok, text = g.file_diff(self.repo, 'M', 'README.md', 10, 'unstaged')
        self.assertFalse(ok)
        self.assertIn('128', text)
        output = io.StringIO()
        with patch.object(g, 'file_diff', return_value=(False, '')), redirect_stdout(output):
            result = g.main([str(self.repo), '--file-diff', 'README.md', '--json'])
        self.assertEqual(result, 2)
        self.assertFalse(json.loads(output.getvalue())['ok'])

    def test_cli_blackbox_reports_selected_mode(self):
        self.mixed();env=dict(os.environ,HOME=str(self.root),PYTHONDONTWRITEBYTECODE='1')
        result=subprocess.run([sys.executable,str(Path(g.__file__).resolve()),str(self.repo),'--file-diff','README.md','--diff-mode','unstaged','--json'],capture_output=True,text=True,env=env,timeout=20)
        self.assertEqual(result.returncode,1,result.stderr);self.assertIn('-staged version',json.loads(result.stdout)['diff']);self.assertFalse((self.root/'.config/gitmaster_flash').exists())

    def test_linux_app_shortcut_requires_desktop(self):
        screen = Mock()
        cfg = dict(self.cfg, apps={'E': {'name': 'Editor', 'path': str(self.root)}})
        ui = g.TUI(screen, self.root, cfg, None)
        ui.statuses = [g.RepoStatus(self.repo, 'project')]
        with patch.object(g.sys, 'platform', 'linux'), patch.object(g.subprocess, 'run') as run, patch.dict(os.environ, {}, clear=True):
            ui.action_open_app('E')
        run.assert_not_called()
        self.assertEqual(ui.message, g.t('app_no_desktop'))

    def test_linux_app_shortcut_passes_folder_without_shell(self):
        app = self.root / 'editor with spaces'
        app.write_text('#!/bin/sh\nexit 0\n'); app.chmod(0o755)
        cfg = dict(self.cfg, apps={'E': {'name': 'Editor', 'path': str(app)}})
        ui = g.TUI(Mock(), self.root, cfg, None)
        ui.statuses = [g.RepoStatus(self.repo, 'project')]
        child = Mock(); child.wait.side_effect = subprocess.TimeoutExpired('editor', 0.2)
        with patch.object(g.sys, 'platform', 'linux'), patch.dict(os.environ, {'WAYLAND_DISPLAY':'wayland-1'}, clear=True), patch.object(g.subprocess, 'Popen', return_value=child) as spawn, patch.object(g.threading, 'Thread'):
            ui.action_open_app('E')
        self.assertEqual(spawn.call_args.args[0], [str(app), str(self.repo)])
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertEqual(ui.message, g.t('app_starting', name='Editor', rel='project'))
        child.wait.side_effect = None; child.wait.return_value = 2; child.stderr.read.return_value = b"failure"
        with patch.object(g.sys, 'platform', 'linux'), patch.dict(os.environ, {'DISPLAY':':1'}, clear=True), patch.object(g.subprocess, 'Popen', return_value=child):
            ui.action_open_app('E')
        self.assertIn('2', ui.message)

    def test_linux_app_shortcut_rejects_directories_and_spawn_errors(self):
        cfg = dict(self.cfg, apps={'E': {'name': 'Editor', 'path': str(self.root)}})
        ui = g.TUI(Mock(), self.root, cfg, None)
        ui.statuses = [g.RepoStatus(self.repo, 'project')]
        with patch.object(g.sys, 'platform', 'linux'), patch.dict(os.environ, {'DISPLAY':':1'}, clear=True), patch.object(g.subprocess, 'Popen') as spawn:
            ui.action_open_app('E')
        spawn.assert_not_called()
        self.assertEqual(ui.message, g.t('app_not_found', p=str(self.root)))
        app = self.root / 'editor'; app.write_text('invalid'); app.chmod(0o755)
        ui.cfg['apps']['E']['path'] = str(app)
        with patch.object(g.sys, 'platform', 'linux'), patch.dict(os.environ, {'DISPLAY':':1'}, clear=True), patch.object(g.subprocess, 'Popen', side_effect=OSError('launch failed')):
            ui.action_open_app('E')
        self.assertIn('launch failed', ui.message)

    def test_relative_launcher_uses_checked_file_and_reports_delayed_failure(self):
        app = self.root / 'editor'
        wrong = self.root / 'searched'
        wrong.mkdir()
        (wrong / 'editor').write_text('#!/bin/sh\nexit 0\n')
        (wrong / 'editor').chmod(0o755)
        app.write_text('#!/bin/sh\nsleep 0.4\necho fixture_failure >&2\nexit 7\n')
        app.chmod(0o755)
        cfg = dict(self.cfg, apps={'E': {'name': 'Editor', 'path': 'editor'}})
        ui = g.TUI(Mock(), self.root, cfg, None)
        ui.statuses = [g.RepoStatus(self.repo, 'project')]
        before = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(g.sys, 'platform', 'linux'), patch.dict(os.environ, {'DISPLAY': ':1', 'PATH': str(wrong) + os.pathsep + os.environ['PATH']}, clear=True):
                ui.action_open_app('E')
            self.assertEqual(ui.message, g.t('app_starting', name='Editor', rel='project'))
            deadline = time.monotonic() + 5
            while ui.app_results.empty() and time.monotonic() < deadline:
                time.sleep(0.01)
            ui.drain_app_results()
            self.assertIn('Exit 7', ui.message)
            self.assertIn('fixture_failure', ui.message)
        finally:
            os.chdir(before)

    def test_linux_real_launcher_receives_exact_repository_argument(self):
        target = self.root / 'project ; literal $name'
        target.mkdir()
        app = self.root / 'editor launcher'
        received = self.root / 'received'
        app.write_text('#!/bin/sh\nprintf "%s" "$1" > "' + str(received) + '"\n')
        app.chmod(0o755)
        cfg = dict(self.cfg, apps={'E': {'name': 'Editor', 'path': str(app)}})
        ui = g.TUI(Mock(), self.root, cfg, None)
        ui.statuses = [g.RepoStatus(target, 'project')]
        with patch.object(g.sys, 'platform', 'linux'), patch.dict(os.environ, {'DISPLAY':':1'}, clear=True):
            ui.action_open_app('E')
        deadline = time.monotonic() + 5
        while not received.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(received.read_text(), str(target))


class CoverageCountTests(DiffModeFixture):
    def test_same_oid_is_counted_once_per_ref_measurement(self):
        for i in range(24):g._dgit(self.repo,'branch',f'alias-{i}')
        calls=[];original=g.run_git
        def record(*args,**kwargs):
            if args[1:3]==('rev-list','--count'):calls.append(args[3])
            return original(*args,**kwargs)
        with patch.object(g,'run_git',side_effect=record):branches=g.read_branch_inventory(self.repo,self.cfg)
        self.assertEqual(len(branches),25);self.assertEqual(len(calls),1);self.assertTrue(all(b.coverage=='covered' for b in branches))

    def test_distinct_tips_and_later_scan_get_fresh_counts(self):
        g._dgit(self.repo,'checkout','-qb','topic');g._demo_commit(self.repo,'new.txt','new\n','topic');g._dgit(self.repo,'branch','alias');g._dgit(self.repo,'checkout','-q','main')
        before={b.name:b for b in g.read_branch_inventory(self.repo,self.cfg)};self.assertEqual(before['topic'].uncovered_commits,1)
        g._dgit(self.repo,'push','-q','origin','topic')
        after={b.name:b for b in g.read_branch_inventory(self.repo,self.cfg)};self.assertEqual(after['topic'].uncovered_commits,0);self.assertEqual(after['alias'].uncovered_commits,0)

    def test_ref_race_is_still_rejected_with_shared_oid_counts(self):
        g._dgit(self.repo,'branch','alias');original=g.run_git;changed=False
        def move(*args,**kwargs):
            nonlocal changed
            result=original(*args,**kwargs)
            if args[1:3]==('rev-list','--count') and not changed:
                changed=True;g._dgit(self.repo,'branch','new-alias')
            return result
        with patch.object(g,'run_git',side_effect=move),self.assertRaises(g.GitReadError):g.read_branch_inventory(self.repo,self.cfg)
