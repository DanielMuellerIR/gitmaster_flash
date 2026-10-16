"""Arbeitsansichten verwenden denselben Zustandsbeleg in TUI, Liste und Vergleich."""
import copy
import curses
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

import gitmaster_flash as g


class WorkViewTests(unittest.TestCase):
    def setUp(self):
        self.cfg=copy.deepcopy(g.DEFAULT_CONFIG)
        self.lang=patch.object(g,'UI_LANG','en');self.lang.start();self.addCleanup(self.lang.stop)

    def status(self,name='project',**kwargs):
        return g.RepoStatus(Path('/tmp')/name,name,branch='main',head_oid='a'*40,**kwargs)

    def remote(self,name='origin',**kwargs):
        kwargs.setdefault('branch_exists',True)
        kwargs.setdefault('fetch_refspecs_safe',True)
        kwargs.setdefault('fetch_url_safe',True)
        kwargs.setdefault('branch_mapping_safe',True)
        return g.RemoteStatus(name,**kwargs)

    def ui(self,statuses,view='all',keys=()):
        scr=Mock();scr.getmaxyx.return_value=(24,110);scr.getch.side_effect=keys
        ui=g.TUI(scr,Path('/tmp'),self.cfg,None,status_view=view)
        ui.all_statuses=list(statuses);ui.apply_filter()
        return ui

    def test_views_match_model_and_payload_with_independent_path_search(self):
        cases=[('changes',self.status(modified=1)),('changes',self.status(stashes=['wip'])),
               ('changes',self.status(operation='rebase')),('incoming',self.status(remotes=[self.remote(behind=3)])),
               ('outgoing',self.status(remotes=[self.remote(ahead=2)])),
               ('outgoing',self.status(remotes=[self.remote(branch_exists=False)])),
               ('access',self.status(remotes=[self.remote(fetch_failed=True)])),
               ('access',self.status(branches_error='cannot read')),
               ('publish',self.status(remotes=[self.remote('github',public=True,ahead=2)]))]
        clean=self.status('clean')
        for view,st in cases:
            with self.subTest(view=view,st=st):
                self.assertTrue(g.repo_matches_view(st,view))
                self.assertTrue(g.repo_matches_view(g.status_dict(st),view))
                self.assertFalse(g.repo_matches_view(clean,view))
                self.assertEqual(g.filter_statuses([st,clean],'PROJECT',view),[st])
                self.assertEqual(g.filter_repo_dicts([g.status_dict(st)],'PROJECT',view),[g.status_dict(st)])
                self.assertEqual(g.filter_statuses([st,clean],'absent',view),[])

    def test_publication_is_separate_from_sync_and_changes_overall_attention(self):
        st=self.status(remotes=[self.remote(is_sync=True),self.remote('github',public=True,ahead=3)])
        self.assertTrue(st.sync_clean)
        self.assertTrue(st.publication_pending)
        self.assertFalse(st.clean_and_synced)
        self.assertEqual(g.compact_mark(st)[0],'G')
        self.assertEqual(st.severity(),2)
        self.assertIn('Sync ✔',g.selected_repo_context(st,110))
        self.assertIn('G pending',g.selected_repo_context(st,110))
        output=io.StringIO()
        with redirect_stdout(output):g.print_list([st])
        self.assertIn('GitHub publication pending',output.getvalue())

    def test_public_fetch_failure_does_not_falsify_private_sync_state(self):
        st = self.status(error='github: auth', fetch_error=True, remote_state='error',
                         remotes=[self.remote(is_sync=True),
                                  self.remote('github', public=True, fetch_failed=True)])
        self.assertTrue(st.sync_clean)
        self.assertFalse(st.clean_and_synced)
        st.remotes[0].fetch_failed = True
        self.assertFalse(st.sync_clean)

    def test_unborn_detached_and_unknown_heads_do_not_invent_publication(self):
        for oid,branch in ((None,'main'),('','main'),('a'*40,'(detached)')):
            st=self.status(remotes=[self.remote('github',public=True,branch_exists=False)])
            st.head_oid,st.branch=oid,branch
            self.assertFalse(st.publication_pending)
        st.head_oid,st.branch='a'*40,'main'
        self.assertTrue(st.publication_pending)

    def test_noncurrent_uncovered_branch_is_outgoing_even_when_current_is_synced(self):
        st=self.status()
        st.branches=[g.BranchInfo('topic', oid='b'*40, coverage='uncovered', uncovered_commits=1)]
        st.branches_scanned=True
        self.assertTrue(g.repo_matches_view(st,'outgoing'))
        self.assertTrue(g.repo_matches_view(g.status_dict(st),'outgoing'))

    def test_comparison_keeps_counterpart_when_only_one_side_matches(self):
        clean=g.status_dict(self.status())
        changed=g.status_dict(self.status(modified=1))
        left,right=g.filter_comparison_repos([changed],[clean],'','changes')
        self.assertEqual(left,[changed]);self.assertEqual(right,[clean])
        lines=g.diff_status({'repos':left},{'repos':right},'here','there')
        self.assertFalse(any('only on' in line for line in lines))
        self.assertTrue(lines)
        self.assertTrue(any('here' in line for line in lines))
        self.assertEqual(g.filter_comparison_repos([clean],[clean],'','changes'),([],[]))

    def test_selection_stays_by_path_and_refresh_updates_membership(self):
        a,b=self.status('a',modified=1),self.status('b',modified=1)
        ui=self.ui([a,b],'changes');ui.selected=1
        ui.apply_filter(keep_selection=True);self.assertEqual(ui.current().path,b.path)
        fresh=self.status('b')
        with patch.object(g,'collect_status',return_value=fresh):ui.refresh_one(b)
        self.assertEqual([st.rel for st in ui.statuses],['a'])
        self.assertEqual([st.rel for st in ui.all_statuses],['a','b'])
        self.assertEqual(ui.hidden_dirty(),0)

    def test_background_results_enter_and_leave_view_without_losing_other_selection(self):
        a,b=self.status('a'),self.status('b',modified=1)
        ui=self.ui([a,b],'changes')
        ui._merge_scanned(self.status('a',modified=1))
        self.assertEqual([s.rel for s in ui.statuses],['a','b'])
        self.assertEqual(ui.current().path,b.path)
        ui._merge_scanned(self.status('b'))
        self.assertEqual([s.rel for s in ui.statuses],['a'])

    def test_work_view_menu_preserves_path_search_and_cancel_keeps_choice(self):
        st=self.status(modified=1);ui=self.ui([st],keys=[curses.KEY_DOWN,10])
        ui.filter_query='project'
        with patch.object(g.curses,'color_pair',return_value=0):ui.dispatch_action('B')
        self.assertEqual(ui.status_view,'changes');self.assertEqual(ui.filter_query,'project')
        ui.scr.getch.side_effect=[curses.KEY_DOWN,27]
        with patch.object(g.curses,'color_pair',return_value=0):ui.dispatch_action('B')
        self.assertEqual(ui.status_view,'changes')

    def test_escape_clears_search_then_view_before_quitting(self):
        ui=self.ui([self.status(modified=1)],'changes',keys=[27,27,ord('q')]);ui.filter_query='project'
        with patch.object(ui,'draw'):ui._loop()
        self.assertEqual(ui.filter_query,'');self.assertEqual(ui.status_view,'all')

    def test_view_header_and_hidden_attention_are_visible_at_65_columns(self):
        st=self.status(modified=1);hidden=self.status('hidden',error='broken')
        ui=self.ui([st,hidden],'changes');ui.scr.getmaxyx.return_value=(12,65)
        drawn=[];ui.scr.addstr.side_effect=lambda y,x,text,*args:drawn.append((y,text))
        with patch.object(g.curses,'color_pair',return_value=0):ui.draw()
        header=next(text for y,text in drawn if y==0)
        self.assertIn('view: changes',header);self.assertIn('1 hidden',header)
        for lang in ('en','de'):
            with patch.object(g,'UI_LANG',lang):
                for key in ('f1_short','f2_short','f3_short'):
                    self.assertLessEqual(g.cell_width(g.t(key,apps='F/V Apps')),64)

    def test_no_matching_view_is_explicit_and_cli_exposes_filter_scope(self):
        st=self.status(modified=1)
        for view,expected in (('changes',1),('incoming',0)):
            output,error=io.StringIO(),io.StringIO()
            with patch.object(g,'load_config',return_value=self.cfg),patch.object(g,'collect_all',return_value=[st]),redirect_stdout(output),redirect_stderr(error):
                code=g.main(['/tmp','--json','--view',view])
            data=json.loads(output.getvalue())
            self.assertEqual(data['view']['id'],view)
            self.assertEqual(data['view']['matches'],expected)
            self.assertEqual(data['view']['total'],1)
            self.assertEqual(data['view']['hidden_attention'],1-expected)
            self.assertEqual(code,1 if expected else 0)
            if not expected:self.assertIn('nothing was checked',error.getvalue())

    def test_ssh_comparison_view_is_serialized_and_preserves_both_sides(self):
        st=self.status(modified=1);other={'version':g.__version__,'repos':[g.status_dict(self.status())]}
        output=io.StringIO()
        with patch.object(g,'fetch_remote_status',return_value=other),patch.object(g,'collect_all',return_value=[st]),redirect_stdout(output):
            code=g.run_diff('example',Path('/tmp'),self.cfg,fetch=False,as_json=True,view='changes')
        data=json.loads(output.getvalue());self.assertEqual(code,1)
        self.assertEqual(data['filter']['view'],'changes')
        self.assertEqual(data['filter']['selection'], 'either-side')
        self.assertEqual(data['filter']['here_view_matches'], 1)
        self.assertEqual(data['filter']['host_view_matches'], 0)
        self.assertFalse(any('only on' in line for line in data['differences']))

    def test_json_boolean_extensions_are_checked(self):
        data={'version':g.__version__,'repos':[g.status_dict(self.status())]}
        self.assertTrue(g._valid_remote_payload(data))
        for key in ('sync_clean','publication_pending'):
            broken=copy.deepcopy(data);broken['repos'][0][key]='yes'
            self.assertFalse(g._valid_remote_payload(broken))
    def test_proven_unsafe_remote_configuration_needs_attention(self):
        remote = self.remote(fetch_refspec_fingerprint='known', fetch_refspecs_safe=False)
        st = self.status(remotes=[remote])
        self.assertTrue(g.repo_matches_view(st, 'access'))
        self.assertFalse(st.clean_and_synced)
        self.assertEqual(g.compact_mark(st)[0], '✘')
        self.assertEqual(st.severity(), 0)



class PublicationIntegrationTests(unittest.TestCase):
    def test_current_commit_synced_privately_but_not_on_github_is_actionable(self):
        with tempfile.TemporaryDirectory(prefix='gmf-publish-') as tmp,patch.dict(os.environ,g._demo_env(),clear=True):
            root=Path(tmp);repo,_=g._demo_repo(root,'project')
            g._demo_extra_remote(root,repo,'github',url='https://github.com/example/project.git')
            g._demo_ahead(repo,1);g._dgit(repo,'push','-q','origin','main')
            st=g.collect_status(repo,root,copy.deepcopy(g.DEFAULT_CONFIG))
            self.assertTrue(st.sync_clean);self.assertFalse(st.clean_and_synced)
            self.assertTrue(st.publication_pending)
            self.assertEqual(g.compact_mark(st)[0],'G')
            self.assertIn(st,g.filter_statuses([st],'','publish'))
            self.assertFalse(g.filter_statuses([st],'','changes'))
