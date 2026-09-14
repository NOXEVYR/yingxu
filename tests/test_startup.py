"""Startup isolation: synthetic homes, a controlled slow scan, no personal data."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import Application
from yingxu.skills import SkillLibrary
from yingxu.store import Store


class StartupTests(unittest.TestCase):
    def test_project_bootstrap_does_not_wait_for_skill_discovery_and_shutdown_drains_it(self):
        with tempfile.TemporaryDirectory(prefix='yingxu-startup-') as temporary:
            root=Path(temporary).resolve();home=root/'home'
            path=home/'.codex/skills/demo/SKILL.md'
            path.parent.mkdir(parents=True);path.write_text('# Synthetic skill',encoding='utf-8')
            entered=threading.Event();release=threading.Event();closed=threading.Event()
            original=SkillLibrary.refresh
            calls=[]
            def slow_scan(library):
                calls.append(library.home);entered.set()
                if not release.wait(10):raise AssertionError('scan was never released')
                return original(library)
            with patch('yingxu.skills.Path.home',return_value=home),patch.object(SkillLibrary,'refresh',slow_scan):
                app=Application(root/'data',root/'projects')
                try:
                    self.assertTrue(entered.is_set())
                    self.assertFalse(app._skills_startup.done())
                    self.assertEqual(app.bootstrap()['app'],'yingxu')
                    self.assertEqual(app.store.list_projects(),[])
                    self.assertEqual(app.project_library.snapshot()['total'],0)
                    self.assertIs(app.skills.start_initial_refresh(app.jobs.pool),app._skills_startup)
                    def close():
                        app.close();closed.set()
                    closer=threading.Thread(target=close);closer.start()
                    self.assertFalse(closed.wait(.05))
                    release.set();closer.join(10)
                    self.assertTrue(closed.is_set())
                    self.assertEqual(app._skills_startup.result()['total'],1)
                    self.assertEqual(calls,[home])
                finally:
                    release.set();app.close()

    def test_background_scan_uses_captured_home_and_queries_do_not_rescan(self):
        with tempfile.TemporaryDirectory(prefix='yingxu-startup-home-') as temporary:
            root=Path(temporary).resolve();home=root/'home'
            path=home/'.codex/skills/demo/SKILL.md'
            path.parent.mkdir(parents=True);path.write_text('# Synthetic skill',encoding='utf-8')
            with patch('yingxu.skills.Path.home',return_value=home):
                app=Application(root/'data',root/'projects')
            try:
                with patch('yingxu.skills.Path.home',side_effect=AssertionError('ambient home must not be read')):
                    self.assertEqual(app._skills_startup.result(timeout=10)['total'],1)
                    with patch.object(app.skills,'refresh',side_effect=AssertionError('queries must not scan')):
                        for unused in range(3):
                            self.assertEqual(app.skills.list()['total'],1)
                            self.assertEqual(app.skills.source_list()['all_total'],1)
            finally:app.close()

    def test_failed_initial_scan_is_visible_preserves_index_and_only_explicit_refresh_retries(self):
        with tempfile.TemporaryDirectory(prefix='yingxu-startup-failure-') as temporary:
            root=Path(temporary).resolve();home=root/'home'
            path=home/'.codex/skills/demo/SKILL.md'
            path.parent.mkdir(parents=True);path.write_text('# Synthetic skill',encoding='utf-8')
            with patch('yingxu.skills.Path.home',return_value=home):
                SkillLibrary(Store(root/'data',root/'projects'))
                with self.assertLogs('yingxu.skills',level='ERROR') as logs,patch.object(SkillLibrary,'refresh',side_effect=RuntimeError('synthetic scan failure')) as refresh:
                    app=Application(root/'data',root/'projects')
                    try:
                        with self.assertRaisesRegex(RuntimeError,'synthetic scan failure'):
                            app._skills_startup.result(timeout=10)
                        for unused in range(2):
                            listing=app.skills.list()
                            self.assertEqual(listing['total'],1)
                            self.assertTrue(listing['errors'])
                            self.assertEqual(next(s for s in listing['sources'] if s['id']=='codex')['status'],'error')
                        self.assertEqual(refresh.call_count,1)
                    except BaseException:app.close();raise
                try:
                    self.assertTrue(logs.output)
                    self.assertFalse(app.skills.refresh()['errors'])
                    self.assertEqual(app.skills.list()['total'],1)
                finally:app.close()

    def test_rejected_startup_submission_raises_without_waiting(self):
        with tempfile.TemporaryDirectory(prefix='yingxu-startup-pool-') as temporary:
            root=Path(temporary).resolve()
            with patch('yingxu.skills.Path.home',return_value=root/'home'):
                library=SkillLibrary(Store(root/'data',root/'projects'),scan=False)
            with ThreadPoolExecutor(max_workers=1) as pool:pass
            with self.assertRaises(RuntimeError):library.start_initial_refresh(pool)
            self.assertIsNone(library._startup_future)

    def test_immediate_catalogue_query_waits_for_whole_initial_scan(self):
        with tempfile.TemporaryDirectory(prefix='yingxu-startup-query-') as temporary:
            root=Path(temporary).resolve();release=threading.Event()
            original=SkillLibrary.refresh
            def controlled(library):
                if not release.wait(10):raise AssertionError('scan was never released')
                return original(library)
            with patch('yingxu.skills.Path.home',return_value=root/'home'),patch.object(SkillLibrary,'refresh',controlled):
                app=Application(root/'data',root/'projects')
                try:
                    with ThreadPoolExecutor(max_workers=1) as reader:
                        query=reader.submit(app.skills.list)
                        try:
                            self.assertFalse(query.done())
                        finally:release.set()
                        self.assertEqual(query.result(timeout=10)['total'],0)
                finally:release.set();app.close()


if __name__=='__main__':unittest.main()
