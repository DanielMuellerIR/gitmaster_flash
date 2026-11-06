"""Stash-Dateiansicht und bestätigte Aktionen mit echten temporären Git-Repos."""
import copy
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

import gitmaster_flash as g


class StashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gmf-stashes-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, g._demo_env(), clear=True)
        env.start(); self.addCleanup(env.stop)
        lang = patch.object(g, 'UI_LANG', 'en')
        lang.start(); self.addCleanup(lang.stop)
        self.repo, _ = g._demo_repo(self.root, 'project')
        g.COMMAND_LOG.clear(); self.addCleanup(g.COMMAND_LOG.clear)

    def git(self, *args):
        return g._dgit(self.repo, *args)

    def stash(self, name, path='README.md', content=None):
        (self.repo / path).write_text(content or name + '\n')
        self.git('stash', 'push', '-u', '-qm', name)
        return g.read_stashes(self.repo, 10)

    def act(self, action, entries=None, chosen=None):
        entries = entries if entries is not None else g.read_stashes(self.repo, 10)
        target = g.stash_target(self.repo, 10, clean=True) if action != 'drop' else None
        return g.run_stash_action(self.repo, entries, chosen or entries, action, 10, target)

    def ui(self):
        ui = g.TUI(None, self.root, copy.deepcopy(g.DEFAULT_CONFIG), None)
        st = g.collect_status(self.repo, self.root, ui.cfg)
        ui.statuses = ui.all_statuses = [st]
        ui.confirm_in_pager = Mock(return_value=True)
        ui.show_busy = Mock()
        return ui, st

    def test_files_separate_staged_worktree_and_untracked_with_literal_names(self):
        (self.repo/'README.md').write_text('index\n'); self.git('add','README.md')
        (self.repo/'README.md').write_text('worktree\n')
        for name in ('new.txt', 'line\nname', ':(glob)*', 'binary.dat'):
            (self.repo/name).write_bytes(b'new\0binary' if name=='binary.dat' else name.encode()+b'\n')
        self.git('stash','push','-u','-qm','mixed')
        entry = g.read_stashes(self.repo,10)[0]
        files = g.stash_files(self.repo,entry.oid,10)
        self.assertEqual({f.path for f in files},{'README.md','new.txt','line\nname',':(glob)*','binary.dat'})
        for item in files:
            text = '\n'.join(g.stash_file_diff(self.repo,entry.oid,item,10))
            if item.path=='README.md':
                self.assertIn('+index',text); self.assertIn('+worktree',text)
            elif item.path=='binary.dat': self.assertIn('Binary files',text)
            elif item.path==':(glob)*':
                self.assertIn('+:(glob)*',text); self.assertNotIn('+new.txt',text)
            self.assertNotIn('\x1b',text)
        self.assertEqual(g.COMMAND_LOG,[])

    def test_staged_only_changes_are_visible(self):
        (self.repo/'README.md').write_text('index\n'); self.git('add','README.md')
        (self.repo/'README.md').write_text('# project\n'); self.git('stash','push','-qm','index only')
        e=g.read_stashes(self.repo,10)[0]; files=g.stash_files(self.repo,e.oid,10)
        self.assertEqual(len(files),1); self.assertEqual(files[0].staged,'M')
        self.assertIn('+index','\n'.join(g.stash_file_diff(self.repo,e.oid,files[0],10)))

    def test_pop_restores_staged_only_content_and_separate_worktree(self):
        (self.repo/'README.md').write_text('staged content\n');self.git('add','README.md')
        (self.repo/'README.md').write_text('# project\n');self.git('stash','push','-qm','index only')
        self.assertEqual(self.act('pop'),(1,''))
        self.assertEqual(g.run_git(self.repo,'show',':README.md').stdout,'staged content\n')
        self.assertEqual((self.repo/'README.md').read_text(),'# project\n')
        self.assertEqual(g.read_stashes(self.repo,10),[])

    def test_apply_retains_and_pop_removes_only_selected_stash(self):
        self.stash('older','older.txt'); entries=self.stash('newer','newer.txt')
        self.assertEqual(self.act('apply',entries,[entries[1]]),(1,''))
        self.assertEqual(g.read_stashes(self.repo,10),entries)
        self.assertEqual((self.repo/'older.txt').read_text(),'older\n')
        self.git('add','older.txt'); self.git('commit','-qm','applied')
        self.assertEqual(self.act('pop',entries,[entries[0]]),(1,''))
        self.assertEqual([e.oid for e in g.read_stashes(self.repo,10)],[entries[1].oid])
        self.assertEqual((self.repo/'newer.txt').read_text(),'newer\n')
        self.assertTrue(any('stash apply' in line for line in g.COMMAND_LOG))
        self.assertTrue(any('stash drop' in line for line in g.COMMAND_LOG))

    def test_apply_all_and_pop_all_oldest_first(self):
        for action in ('apply','pop'):
            with self.subTest(action=action):
                self.stash('older','older.txt'); entries=self.stash('newer','newer.txt')
                self.assertEqual(self.act(action,entries,list(reversed(entries))),(2,''))
                self.assertTrue((self.repo/'older.txt').exists()); self.assertTrue((self.repo/'newer.txt').exists())
                self.assertEqual(len(g.read_stashes(self.repo,10)),2 if action=='apply' else 0)
                self.git('add','.'); self.git('commit','-qm','applied'); self.git('stash','clear')
                self.git('rm','older.txt','newer.txt'); self.git('commit','-qm','clean')

    def test_pop_all_stops_at_conflict_keeps_failed_and_unattempted_entries(self):
        self.stash('first','first.txt'); self.stash('conflicting',content='stash version\n')
        entries=self.stash('last','last.txt')
        (self.repo/'README.md').write_text('branch version\n');self.git('add','README.md');self.git('commit','-qm','new base')
        count,error=self.act('pop',entries,list(reversed(entries)))
        self.assertEqual(count,1);self.assertIn('stash was kept',error)
        self.assertEqual([e.oid for e in g.read_stashes(self.repo,10)],[entries[0].oid,entries[1].oid])
        self.assertTrue(g.has_unmerged_entries(self.repo,10));self.assertTrue((self.repo/'first.txt').exists())
        self.assertFalse((self.repo/'last.txt').exists())

    def test_pop_all_stops_if_saved_staging_cannot_be_combined(self):
        for name in ('first.txt','second.txt'):
            (self.repo/name).write_text(name+'\n');self.git('add',name)
            self.git('stash','push','-qm',name)
        entries=g.read_stashes(self.repo,10)
        count,error=self.act('pop',entries,list(reversed(entries)))
        self.assertEqual(count,1);self.assertIn('Stopped before applying the next stash',error)
        self.assertEqual([e.oid for e in g.read_stashes(self.repo,10)],[entries[0].oid])
        self.assertEqual(g.run_git(self.repo,'show',':first.txt').stdout,'first.txt\n')
        self.assertFalse((self.repo/'second.txt').exists())

    def test_drop_all_handles_renumbering_and_keeps_worktree(self):
        self.stash('old');entries=self.stash('new')
        (self.repo/'local.txt').write_text('local\n')
        self.assertEqual(self.act('drop',entries,entries),(2,''))
        self.assertEqual(g.read_stashes(self.repo,10),[])
        self.assertEqual((self.repo/'local.txt').read_text(),'local\n')

    def test_changed_stack_or_checkout_after_confirmation_is_rejected(self):
        for change in ('stash','branch','dirty','index'):
            with self.subTest(change=change):
                self.git('reset','--hard','-q');self.git('stash','clear')
                entries=self.stash('approved');target=g.stash_target(self.repo,10,clean=True)
                if change=='stash': self.stash('new')
                elif change=='branch': self.git('switch','-qc','other')
                else:
                    (self.repo/'README.md').write_text('other edit\n')
                    if change=='index':self.git('add','README.md')
                with patch.object(g,'run_git_logged',wraps=g.run_git_logged) as run:
                    count,error=g.run_stash_action(self.repo,entries,entries,'pop',10,target)
                self.assertEqual(count,0);self.assertTrue(error);run.assert_not_called()

    def test_pop_keeps_applied_stash_if_stack_changes_before_drop(self):
        self.stash('other','other.txt');entries=self.stash('approved');original=g.run_git_logged
        def run(repo,*args,**kwargs):
            result=original(repo,*args,**kwargs)
            if 'apply' in args:self.git('stash','store','-m','parallel stash',entries[1].oid)
            return result
        with patch.object(g,'run_git_logged',side_effect=run):count,error=self.act('pop',entries)
        self.assertEqual(count,0);self.assertIn('Applied',error)
        self.assertEqual(len(g.read_stashes(self.repo,10)),3)
        self.assertEqual((self.repo/'README.md').read_text(),'approved\n')
        self.assertFalse(any('stash drop' in line for line in g.COMMAND_LOG))

    def test_timeout_never_drops_stash_or_continues(self):
        self.stash('older','older.txt');entries=self.stash('newer','newer.txt')
        with patch.object(g,'run_git_logged',side_effect=subprocess.TimeoutExpired(['git','stash','apply'],10)) as run:
            count,error=self.act('pop',entries,list(reversed(entries)))
        self.assertEqual(count,0);self.assertIn('uncertain',error);self.assertEqual(run.call_count,1)
        self.assertEqual(g.read_stashes(self.repo,10),entries)

    def test_cancelled_ui_action_changes_nothing(self):
        entries=self.stash('approved');ui,st=self.ui();ui.confirm_in_pager.return_value=False
        with patch.object(g,'run_git_logged') as run:ui.change_stashes(st,entries,entries,'drop')
        run.assert_not_called();self.assertEqual(g.read_stashes(self.repo,10),entries)
        self.assertTrue(g.COMMAND_LOG[-1].startswith('⊘'))

    def test_dirty_ui_target_is_rejected_before_confirmation(self):
        entries=self.stash('approved');(self.repo/'README.md').write_text('local\n');ui,st=self.ui()
        result=ui.change_stashes(st,entries,entries,'apply')
        self.assertIn('clean',result);ui.confirm_in_pager.assert_not_called()
        self.assertEqual((self.repo/'README.md').read_text(),'local\n')

    def test_read_only_cli_groups_files_and_rejects_mutation_options(self):
        entries=self.stash('approved');out=io.StringIO()
        with redirect_stdout(out):self.assertEqual(g.main([str(self.repo),'--stashes','--json']),0)
        payload=json.loads(out.getvalue());self.assertEqual(payload['kind'],'stashes')
        self.assertEqual(payload['stashes'][0]['files'][0]['path'],'README.md')
        self.assertIn('+approved','\n'.join(payload['stashes'][0]['files'][0]['diff']))
        self.assertEqual(g.read_stashes(self.repo,10),entries);self.assertEqual(g.COMMAND_LOG,[])
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):g.main([str(self.repo),'--stashes','--fetch'])

    def test_tree_navigation_expands_files_and_can_pop_all(self):
        self.stash('older','older.txt');entries=self.stash('newer')
        ui,st=self.ui()
        ui.scr=Mock()
        ui.scr.getmaxyx.return_value=(24,80)
        ui.scr.getch.side_effect=[g.curses.KEY_DOWN,g.curses.KEY_RIGHT,g.curses.KEY_DOWN,
                                 g.curses.KEY_LEFT,g.curses.KEY_LEFT,ord('*'),ord('p'),ord('q')]
        ui.change_stashes=Mock(return_value='done');ui.show_pager=Mock()
        with patch.object(g.curses,'color_pair',return_value=0),patch.object(g,'safe_addstr') as draw:
            ui.browse_stashes(st,entries)
        ui.change_stashes.assert_called_once_with(st,entries,list(reversed(entries)),'pop')
        text='\n'.join(call.args[3] for call in draw.call_args_list)
        self.assertIn('+newer',text);self.assertIn('README.md',text);self.assertIn('ALL stashes',text)

    def test_apply_keeps_the_same_stash_selected_for_separate_deletion(self):
        self.stash('older','older.txt');entries=self.stash('newer')
        ui,st=self.ui();ui.scr=Mock();ui.scr.getmaxyx.return_value=(24,80)
        ui.scr.getch.side_effect=[g.curses.KEY_DOWN,g.curses.KEY_DOWN,ord('a'),ord('q')]
        ui.change_stashes=Mock(return_value='done');ui.show_pager=Mock()
        with patch.object(g.curses,'color_pair',return_value=0),patch.object(g,'safe_addstr') as draw:
            ui.browse_stashes(st,entries)
        ui.change_stashes.assert_called_once_with(st,entries,[entries[1]],'apply')
        scope=[call.args[3] for call in draw.call_args_list if call.args[1]==1][-1]
        self.assertIn(entries[1].label,scope)

    def test_git_hooks_and_external_diff_are_disabled(self):
        entries=self.stash('saved')
        marker=self.root/'hook-ran';driver=self.root/'driver'
        driver.write_text('#!/bin/sh\ntouch "'+str(marker)+'"\n');driver.chmod(0o755)
        hooks=self.repo/'.git/hooks'
        for name in ('reference-transaction','post-index-change'):
            hook=hooks/name;hook.write_text(driver.read_text());hook.chmod(0o755)
        self.git('config','core.hooksPath',str(hooks))
        self.git('update-ref','refs/test-hook','HEAD')
        self.assertTrue(marker.exists());marker.unlink()
        with patch.dict(os.environ,{'GIT_EXTERNAL_DIFF':str(driver)}):
            item=g.stash_files(self.repo,entries[0].oid,10)[0]
            g.stash_file_diff(self.repo,entries[0].oid,item,10)
            self.assertEqual(self.act('pop',entries),(1,''))
        self.assertFalse(marker.exists())

    @unittest.skipUnless(shutil.which("zsh"), "zsh unavailable")
    def test_byte_paths_and_wrapper_cli_work_without_changing_files(self):
        name='line\nname.txt';(self.repo/name).write_text('saved\n')
        self.git('stash','push','-u','-qm','byte path')
        command=['zsh','-f','-c','source "$1"; gmf "$2" --stashes --json','test',
                 str(Path(g.__file__).with_name('gmf.zsh')),str(self.repo)]
        result=subprocess.run(command,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        payload=json.loads(result.stdout.decode('utf-8'))
        item=payload['stashes'][0]['files'][0]
        self.assertEqual(item['path_key'],os.fsencode(name).hex())
        self.assertFalse((self.repo/name).exists())



if __name__ == '__main__':
    unittest.main()
