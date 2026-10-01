"""Automatic update scheduling stays delayed, bounded, persistent, and install-free."""
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from yingxu.automatic_updates import (BACKOFF_BASE, MAX_AUTOMATIC_DOWNLOAD,
                                      DAILY_INTERVAL, AutomaticUpdates)
from yingxu.settings import DEFAULTS, Settings
from yingxu.store import UserError
from yingxu.update_service import UpdateService


def wait_idle(service, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service.status()
        if not state['running']:
            return state
        time.sleep(.01)
    raise AssertionError('automatic update worker did not finish')


def wait_state(service, expected, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service.status()
        if state['state'] == expected:
            return state
        time.sleep(.01)
    raise AssertionError(f'automatic update state did not become {expected!r}')


class StubManager:
    def __init__(self, download_bytes=0):
        self.value = {'state': 'idle', 'plan_id': '', 'total_download_bytes': download_bytes,
                      'message': ''}
        self.download_calls = []

    def status(self):
        return dict(self.value)

    def close(self):
        pass


class StubUpdateService:
    def __init__(self, root, download_bytes=0):
        data = root / 'data'
        data.mkdir()
        self.app = SimpleNamespace(store=SimpleNamespace(data_root=data), settings=Settings(data))
        self.manager = StubManager(download_bytes)
        self.planned = False
        self.plan_calls = 0
        self.plan_version = '0.4.22'
        self.plan_build = 'workflow.3'
        self.plan_id = 'a' * 32
        self.current_version = '0.4.21'
        self.current_build = ''

    def _manager(self):
        return self.manager

    def plan(self):
        self.planned = True
        self.plan_calls += 1
        self.manager.value.update(state='planned', plan_id=self.plan_id,
                                  latest_version=self.plan_version, latest_build=self.plan_build)
        return self.manager.status()

    def download(self, plan_id):
        self.manager.download_calls.append(plan_id)
        self.manager.value.update(state='ready')
        return self.manager.status()


class AutomaticUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='yingxu-automatic-updates-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.service = StubUpdateService(self.root)

    def finished(self, automatic, timeout=2):
        result = wait_idle(automatic, timeout)
        automatic.close()
        return result

    def test_preferences_default_on_persist_and_reject_non_boolean_values(self):
        self.assertTrue(DEFAULTS['automatic_update_check'])
        self.assertTrue(DEFAULTS['automatic_update_download'])
        result = self.service.app.settings.update({'automatic_update_check': False,
                                                   'automatic_update_download': False})
        self.assertFalse(result['automatic_update_check'])
        self.assertFalse(Settings(self.service.app.store.data_root).get()['automatic_update_download'])
        original = self.service.app.settings.path.read_bytes()
        for patch_value in ({'automatic_update_check': 1}, {'automatic_update_download': None},
                            {'automatic_update_check': 'true'}):
            with self.subTest(patch=patch_value), self.assertRaises(UserError):
                self.service.app.settings.update(patch_value)
        self.assertEqual(self.service.app.settings.path.read_bytes(), original)

    def test_check_runs_in_background_and_is_throttled_across_service_restarts(self):
        completed = threading.Event()
        result = {'update_available': False, 'latest_version': '0.4.21', 'url': ''}
        with patch('yingxu.updates.check_update', side_effect=lambda: (completed.set(), result)[1]) as check:
            first = AutomaticUpdates(self.service)
            self.addCleanup(first.close)
            first.start(0)
            self.assertTrue(completed.wait(1))
            self.assertEqual(self.finished(first)['state'], 'current')
            reopened = AutomaticUpdates(self.service)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.status()['state'], 'current')
            reopened.start(0)
            self.assertFalse(reopened.status()['running'])
            self.assertEqual(check.call_count, 1)
            reopened.close()

    def test_error_uses_persisted_exponential_backoff(self):
        now = 2_000_000
        done = threading.Event()

        def fail():
            done.set()
            raise OSError('synthetic offline')

        with patch('yingxu.updates.check_update', side_effect=fail) as check:
            automatic = AutomaticUpdates(self.service, clock=lambda: now)
            self.addCleanup(automatic.close)
            automatic.start(0)
            self.assertTrue(done.wait(1))
            state = self.finished(automatic)
            self.assertEqual(state['state'], 'error')
            self.assertEqual(state['consecutive_failures'], 1)
            self.assertEqual(state['next_attempt'], now + BACKOFF_BASE)
            reopened = AutomaticUpdates(self.service, clock=lambda: now)
            self.addCleanup(reopened.close)
            reopened.start(0)
            self.assertFalse(reopened.status()['running'])
            self.assertEqual(check.call_count, 1)
            reopened.close()

    def test_daily_check_repeats_while_the_application_stays_open(self):
        class MutableClock:
            value = 3_000_000

            def __call__(self):
                return self.value

        clock = MutableClock()
        calls = 0
        first = threading.Event()
        second = threading.Event()

        def current():
            nonlocal calls
            calls += 1
            (first if calls == 1 else second).set()
            return {'update_available': False, 'latest_version': '0.4.21', 'url': ''}

        with patch('yingxu.updates.check_update', side_effect=current) as check:
            automatic = AutomaticUpdates(self.service, clock=clock)
            self.addCleanup(automatic.close)
            automatic.start(0)
            self.assertTrue(first.wait(1))
            wait_idle(automatic)
            clock.value += DAILY_INTERVAL + 1
            automatic.start(0)
            self.assertTrue(second.wait(1))
            wait_idle(automatic)
            self.assertEqual(check.call_count, 2)
            automatic.close()

    def test_persisted_ready_requires_a_live_matching_plan_and_newer_version(self):
        automatic = AutomaticUpdates(self.service)
        self.addCleanup(automatic.close)
        automatic._set(state='ready', last_attempt=2_000_000, latest_version='0.4.22',
                       latest_build='workflow.3', update_available=True, plan_id='a' * 32, download_bytes=123)
        self.service.manager.value.update(state='ready', plan_id='a' * 32, latest_version='0.4.22',
                                         latest_build='workflow.3')
        self.assertEqual(automatic.status()['state'], 'ready')

        self.service.manager.value.update(state='idle', plan_id='', latest_version='')
        status = automatic.status()
        self.assertEqual(status['state'], 'available')
        self.assertTrue(status['update_available'])
        self.assertEqual(status['plan_id'], '')

        automatic.close()
        self.service.current_version = '0.4.22'
        self.service.current_build = 'workflow.3'
        reopened = AutomaticUpdates(self.service)
        self.addCleanup(reopened.close)
        status = reopened.status()
        self.assertEqual(status['state'], 'current')
        self.assertFalse(status['update_available'])
        self.assertEqual(status['plan_id'], '')

    def test_windows_auto_downloads_only_a_plan_at_or_below_50_mib(self):
        done = threading.Event()
        found = {'update_available': True, 'latest_version': '0.4.22',
                 'url': 'https://github.com/NOXEVYR/yingxu/releases/tag/yingxu-v0.4.22'}
        with patch('yingxu.automatic_updates.sys.platform', 'win32'), \
                patch('yingxu.updates.check_update', side_effect=lambda: (done.set(), found)[1]):
            self.service.manager.value['total_download_bytes'] = MAX_AUTOMATIC_DOWNLOAD
            automatic = AutomaticUpdates(self.service)
            self.addCleanup(automatic.close)
            automatic.start(0)
            self.assertTrue(done.wait(1))
            state = self.finished(automatic)
            self.assertEqual(state['state'], 'ready', state)
            self.assertEqual(self.service.manager.download_calls, ['a' * 32])
            self.assertFalse(state['install_automatically'])

        larger_root = self.root / 'larger'
        larger_root.mkdir()
        larger = StubUpdateService(larger_root, MAX_AUTOMATIC_DOWNLOAD + 1)
        done.clear()
        with patch('yingxu.automatic_updates.sys.platform', 'win32'), \
                patch('yingxu.updates.check_update', side_effect=lambda: (done.set(), found)[1]):
            automatic = AutomaticUpdates(larger)
            self.addCleanup(automatic.close)
            automatic.start(0)
            self.assertTrue(done.wait(1))
            state = self.finished(automatic)
            self.assertEqual(state['state'], 'manual_required')
            self.assertIn('50 MiB', state['message'])
            self.assertEqual(larger.manager.download_calls, [])

    def test_outdated_planned_or_ready_plan_is_refreshed_before_auto_download(self):
        found = {'update_available': True, 'latest_version': '0.4.23',
                 'url': 'https://github.com/NOXEVYR/yingxu/releases/tag/yingxu-v0.4.23'}
        for initial_state in ('planned', 'ready'):
            with self.subTest(initial_state=initial_state):
                case_root = self.root / initial_state
                case_root.mkdir()
                service = StubUpdateService(case_root, 1024)
                service.plan_version = '0.4.23'
                service.plan_id = 'c' * 32
                service.manager.value.update(state=initial_state, plan_id='b' * 32,
                                             latest_version='0.4.22')
                done = threading.Event()
                with patch('yingxu.automatic_updates.sys.platform', 'win32'), \
                        patch('yingxu.updates.check_update',
                              side_effect=lambda: (done.set(), found)[1]):
                    automatic = AutomaticUpdates(service)
                    self.addCleanup(automatic.close)
                    automatic.start(0)
                    self.assertTrue(done.wait(1))
                    state = self.finished(automatic)
                self.assertEqual(state['latest_version'], '0.4.23')
                self.assertEqual(state['state'], 'ready')
                self.assertEqual(service.plan_calls, 1)
                self.assertEqual(service.manager.download_calls, ['c' * 32])

    def test_still_mismatched_plan_is_never_downloaded(self):
        stale_root = self.root / 'stale'
        stale_root.mkdir()
        service = StubUpdateService(stale_root, 1024)
        service.plan_version = '0.4.22'
        service.manager.value.update(state='planned', plan_id='b' * 32,
                                     latest_version='0.4.22')
        found = {'update_available': True, 'latest_version': '0.4.23', 'url': ''}
        done = threading.Event()
        with patch('yingxu.automatic_updates.sys.platform', 'win32'), \
                patch('yingxu.updates.check_update', side_effect=lambda: (done.set(), found)[1]):
            automatic = AutomaticUpdates(service)
            self.addCleanup(automatic.close)
            automatic.start(0)
            self.assertTrue(done.wait(1))
            state = self.finished(automatic)
        self.assertEqual(state['state'], 'manual_required')
        self.assertIn('已阻止自动下载', state['message'])
        self.assertEqual(service.manager.download_calls, [])

    def test_same_version_newer_build_downloads_and_survives_restart(self):
        self.service.current_version = self.service.plan_version = '0.4.22'
        self.service.current_build = 'workflow.2'
        self.service.plan_build = 'workflow.3'
        found = {'update_available': True, 'latest_version': '0.4.22',
                 'latest_build': 'workflow.3', 'update_kind': 'build', 'url': ''}
        with patch('yingxu.automatic_updates.sys.platform', 'win32'), \
                patch('yingxu.updates.check_update', return_value=found):
            automatic = AutomaticUpdates(self.service)
            self.addCleanup(automatic.close)
            automatic.start(0)
            state = self.finished(automatic)
        self.assertEqual(state['state'], 'ready', state)
        self.assertEqual(state['latest_build'], 'workflow.3')
        self.assertEqual(state['current_build'], 'workflow.2')
        self.assertEqual(state['update_kind'], 'build')
        self.assertEqual(self.service.manager.download_calls, ['a' * 32])
        reopened = AutomaticUpdates(self.service)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.status()['state'], 'ready')
        self.service.current_build = 'workflow.3'
        self.assertEqual(reopened.status()['state'], 'current')

    def test_same_version_cached_plan_must_match_the_exact_target_build(self):
        found = {'update_available': True, 'latest_version': '0.4.22',
                 'latest_build': 'workflow.4', 'update_kind': 'build', 'url': ''}
        for initial_state, refreshed_build in (('planned', 'workflow.4'), ('ready', 'workflow.4'),
                                                ('ready', 'workflow.3')):
            with self.subTest(initial_state=initial_state, refreshed_build=refreshed_build):
                case_root = self.root / (initial_state + refreshed_build)
                case_root.mkdir()
                service = StubUpdateService(case_root, 1024)
                service.current_version = service.plan_version = '0.4.22'
                service.current_build = 'workflow.2'
                service.plan_build = refreshed_build
                service.plan_id = 'c' * 32
                service.manager.value.update(state=initial_state, plan_id='b' * 32,
                                             latest_version='0.4.22', latest_build='workflow.3')
                with patch('yingxu.automatic_updates.sys.platform', 'win32'), \
                        patch('yingxu.updates.check_update', return_value=found):
                    automatic = AutomaticUpdates(service)
                    self.addCleanup(automatic.close)
                    automatic.start(0)
                    state = self.finished(automatic)
                self.assertEqual(service.plan_calls, 1)
                if refreshed_build == 'workflow.4':
                    self.assertEqual(state['state'], 'ready', state)
                    self.assertEqual(service.manager.download_calls, ['c' * 32])
                else:
                    self.assertEqual(state['state'], 'manual_required', state)
                    self.assertEqual(service.manager.download_calls, [])

    def test_uncertain_or_older_build_cannot_trigger_auto_download(self):
        self.service.current_version = '0.4.22'
        self.service.current_build = 'workflow.3'
        for build, kind, available in (('other.4', 'manual', False), ('', 'manual', False),
                                       ('workflow.2', 'build', True)):
            with self.subTest(build=build):
                found = {'update_available': available, 'latest_version': '0.4.22',
                         'latest_build': build, 'update_kind': kind, 'url': ''}
                with patch('yingxu.automatic_updates.sys.platform', 'win32'), \
                        patch('yingxu.updates.check_update', return_value=found):
                    automatic = AutomaticUpdates(self.service)
                    self.addCleanup(automatic.close)
                    automatic.check_now()
                    state = self.finished(automatic)
                self.assertEqual(state['state'], 'manual_required', state)
        self.assertEqual(self.service.plan_calls, 0)
        self.assertEqual(self.service.manager.download_calls, [])

    def test_cross_version_build_replacement_refreshes_a_ready_plan(self):
        self.service.current_version = '0.4.21'
        self.service.current_build = 'workflow.1'
        self.service.plan_build = 'workflow.3'
        self.service.manager.value.update(state='ready', plan_id='b' * 32,
                                         latest_version='0.4.22', latest_build='workflow.2')
        for advertised_build in ('workflow.3', ''):
            with self.subTest(advertised_build=advertised_build):
                self.service.manager.value.update(state='ready', plan_id='b' * 32,
                                                 latest_build='workflow.2')
                found = {'update_available': True, 'latest_version': '0.4.22',
                         'latest_build': advertised_build, 'update_kind': 'version', 'url': ''}
                with patch('yingxu.automatic_updates.sys.platform', 'win32'), \
                        patch('yingxu.updates.check_update', return_value=found):
                    automatic = AutomaticUpdates(self.service)
                    self.addCleanup(automatic.close)
                    automatic.check_now()
                    state = self.finished(automatic)
                self.assertEqual(state['state'], 'ready', state)
                self.assertEqual(state['latest_build'], 'workflow.3')
                self.assertEqual(state['plan_id'], 'a' * 32)
        self.assertEqual(self.service.plan_calls, 2)
        self.assertEqual(self.service.manager.download_calls, ['a' * 32, 'a' * 32])

    def test_unidentified_ready_cache_is_hidden_and_replanned_even_when_plan_is_in_flight(self):
        automatic = AutomaticUpdates(self.service)
        self.addCleanup(automatic.close)
        automatic._set(state='ready', latest_version='0.4.22', update_available=True, plan_id='b' * 32)
        self.service.manager.value.update(state='ready', latest_version='0.4.22', plan_id='b' * 32)
        self.assertEqual(automatic.status()['state'], 'available')
        self.assertTrue(automatic._needs_resume())
        automatic.close()
        self.service.manager.value['state'] = 'planning'
        self.service.plan_build = 'workflow.3'
        found = {'update_available': True, 'latest_version': '0.4.22', 'update_kind': 'version', 'url': ''}
        with patch('yingxu.automatic_updates.sys.platform', 'win32'), \
                patch('yingxu.updates.check_update', return_value=found):
            automatic = AutomaticUpdates(self.service)
            self.addCleanup(automatic.close)
            # The previous in-flight plan finishes without a known build.
            with patch.object(automatic, '_wait_manager', side_effect=[
                    {'state':'ready', 'latest_version':'0.4.22', 'plan_id':'b' * 32},
                    {'state':'planned', 'latest_version':'0.4.22', 'latest_build':'workflow.3',
                     'plan_id':'a' * 32, 'total_download_bytes':0},
                    {'state':'ready', 'latest_version':'0.4.22', 'latest_build':'workflow.3',
                     'plan_id':'a' * 32, 'total_download_bytes':0}]):
                automatic.check_now()
                state = self.finished(automatic)
        self.assertEqual(state['state'], 'ready', state)
        self.assertEqual(state['latest_build'], 'workflow.3')
        self.assertEqual(self.service.plan_calls, 1)
        self.assertEqual(self.service.manager.download_calls, ['a' * 32])

    def test_ready_cache_unknown_identity_is_manual_and_live_build_mismatch_is_not_ready(self):
        self.service.current_version = '0.4.22'
        self.service.current_build = 'workflow.2'
        automatic = AutomaticUpdates(self.service)
        self.addCleanup(automatic.close)
        automatic._set(state='ready', latest_version='0.4.22', latest_build='workflow.3',
                       update_kind='build', update_available=True, plan_id='a' * 32)
        self.service.manager.value.update(state='ready', plan_id='a' * 32,
                                         latest_version='0.4.22', latest_build='workflow.4')
        self.assertEqual(automatic.status()['state'], 'available')
        self.service.current_build = 'other.2'
        state = automatic.status()
        self.assertEqual(state['state'], 'manual_required')
        self.assertEqual(state['plan_id'], '')
        self.assertEqual(state['update_kind'], 'manual')

    def test_fresh_plan_without_build_requires_manual_review_without_repeated_download(self):
        self.service.plan_build = ''
        found = {'update_available': True, 'latest_version': '0.4.22', 'update_kind': 'version', 'url': ''}
        with patch('yingxu.automatic_updates.sys.platform', 'win32'), \
                patch('yingxu.updates.check_update', return_value=found):
            automatic = AutomaticUpdates(self.service)
            self.addCleanup(automatic.close)
            automatic.start(0)
            state = self.finished(automatic)
        self.assertEqual(state['state'], 'manual_required', state)
        self.assertEqual(state['update_kind'], 'manual')
        self.assertFalse(automatic._needs_resume())
        self.assertEqual(self.service.plan_calls, 1)
        self.assertEqual(self.service.manager.download_calls, [])

    def test_target_build_changed_after_download_cannot_report_ready(self):
        self.service.current_version = self.service.plan_version = '0.4.22'
        self.service.current_build = 'workflow.2'
        self.service.plan_build = 'workflow.3'
        original_download = self.service.download

        def changed_download(plan_id):
            original_download(plan_id)
            self.service.manager.value['latest_build'] = 'workflow.4'

        found = {'update_available': True, 'latest_version': '0.4.22',
                 'latest_build': 'workflow.3', 'update_kind': 'build', 'url': ''}
        with patch('yingxu.automatic_updates.sys.platform', 'win32'), \
                patch('yingxu.updates.check_update', return_value=found), \
                patch.object(self.service, 'download', side_effect=changed_download):
            automatic = AutomaticUpdates(self.service)
            self.addCleanup(automatic.close)
            automatic.start(0)
            state = self.finished(automatic)
        self.assertNotEqual(state['state'], 'ready', state)
        self.assertEqual(state['plan_id'], '')

    def test_non_windows_reports_full_package_manual_install(self):
        done = threading.Event()
        found = {'update_available': True, 'latest_version': '0.4.22',
                 'url': 'https://github.com/NOXEVYR/yingxu/releases/tag/yingxu-v0.4.22'}
        with patch('yingxu.automatic_updates.sys.platform', 'darwin'), \
                patch('yingxu.updates.check_update', side_effect=lambda: (done.set(), found)[1]):
            automatic = AutomaticUpdates(self.service)
            self.addCleanup(automatic.close)
            automatic.start(0)
            self.assertTrue(done.wait(1))
            state = self.finished(automatic)
        self.assertEqual(state['state'], 'available')
        self.assertIn('完整包', state['message'])
        self.assertFalse(self.service.planned)

    def test_disabled_setting_does_not_check_and_close_cancels_delayed_worker(self):
        self.service.app.settings.update({'automatic_update_check': False})
        with patch('yingxu.updates.check_update') as check:
            automatic = AutomaticUpdates(self.service)
            self.addCleanup(automatic.close)
            state = automatic.start(0)
            state = wait_state(automatic, 'disabled')
            self.assertEqual(state['state'], 'disabled')
            self.assertFalse(state['running'])
            check.assert_not_called()

        self.service.app.settings.update({'automatic_update_check': True})
        with patch('yingxu.updates.check_update') as check:
            automatic = AutomaticUpdates(self.service)
            self.addCleanup(automatic.close)
            automatic.start(1)
            worker = automatic._worker
            automatic.close()
            self.assertFalse(worker.is_alive())
            check.assert_not_called()

    def test_late_metadata_result_after_close_cannot_create_manager_or_write_state(self):
        root = self.root / 'closing'
        root.mkdir()
        data = root / 'data'
        data.mkdir()
        app = SimpleNamespace(store=SimpleNamespace(data_root=data), settings=Settings(data))
        service = UpdateService(app, root / 'install', '0.4.21')
        automatic = service._automatic()
        entered = threading.Event()
        release = threading.Event()

        def delayed_check():
            entered.set()
            release.wait(5)
            return {'update_available': True, 'latest_version': '0.4.22', 'url': ''}

        with patch('yingxu.updates.check_update', side_effect=delayed_check), \
                patch('yingxu.automatic_updates.WORKER_JOIN_TIMEOUT', 0), \
                patch('yingxu.incremental_update.UpdateManager') as manager_factory:
            service.start_automatic_updates(0)
            self.assertTrue(entered.wait(1))
            state_path = data / 'updates' / 'automatic' / 'state.json'
            before_close = state_path.read_bytes()
            service.close()
            self.assertTrue(service.closed)
            self.assertEqual(state_path.read_bytes(), before_close)
            self.assertEqual(service.status()['automatic']['state'], 'checking')
            for operation in (service.plan, lambda: service.download('a' * 32),
                              service.start_automatic_updates, service.automatic_check):
                with self.assertRaises(UserError):
                    operation()
            release.set()
            worker = automatic._worker
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertFalse(service.status()['automatic']['running'])
            manager_factory.assert_not_called()
            self.assertEqual(state_path.read_bytes(), before_close)

    def test_concurrent_status_and_state_write_do_not_reverse_service_lock_order(self):
        data = self.service.app.store.data_root
        app = SimpleNamespace(store=SimpleNamespace(data_root=data), settings=self.service.app.settings)
        service = UpdateService(app, self.root / 'install', '0.4.21')
        service.manager = StubManager()
        self.addCleanup(service.close)
        automatic = service._automatic()

        status_entered = threading.Event()
        continue_status = threading.Event()
        stopping_entered = threading.Event()
        service_lock_waiting = threading.Event()
        set_finished = threading.Event()
        service_lock_timeout = threading.Event()
        errors = []
        original_service_auto_status = service.automatic_status
        original_auto_status = automatic.status
        original_stopping = automatic._stopping

        def gated_service_auto_status():
            status_entered.set()
            if not continue_status.wait(2):
                raise AssertionError('test did not release status gate')
            return original_service_auto_status()

        def bounded_auto_status():
            # Make a regression fail without leaving either test thread wedged:
            # on the old lock ordering status waits here while holding the
            # service lock and _set waits for that same lock while holding this.
            if not automatic._lock.acquire(timeout=.35):
                service_lock_timeout.set()
                return {'state': 'busy'}
            automatic._lock.release()
            return original_auto_status()

        def observed_stopping():
            stopping_entered.set()
            return original_stopping()

        service.automatic_status = gated_service_auto_status
        automatic.status = bounded_auto_status
        automatic._stopping = observed_stopping

        def run_status():
            try:
                service.status()
            except Exception as error:
                errors.append(error)

        def run_set():
            try:
                automatic._set(state='checking', message='并发锁检查')
            except Exception as error:
                errors.append(error)
            finally:
                set_finished.set()

        class ObservedRLock:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def acquire(self, blocking=True, timeout=-1):
                if threading.current_thread().name == 'test-update-set':
                    acquired = self.wrapped.acquire(False)
                    if acquired:
                        self.wrapped.release()
                    else:
                        service_lock_waiting.set()
                return self.wrapped.acquire(blocking, timeout)

            def release(self):
                return self.wrapped.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *_exc):
                self.release()

        service.lock = ObservedRLock(service.lock)

        status_thread = threading.Thread(target=run_status, name='test-update-status')
        set_thread = threading.Thread(target=run_set, name='test-update-set')
        status_thread.start()
        self.assertTrue(status_entered.wait(1))
        set_thread.start()
        self.assertTrue(stopping_entered.wait(1))
        deadline = time.monotonic() + 1
        while not set_finished.is_set() and not service_lock_waiting.is_set() and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertTrue(set_finished.is_set() or service_lock_waiting.is_set(),
                        'state write neither completed nor reached the service lock')
        continue_status.set()
        status_thread.join(timeout=2)
        set_thread.join(timeout=2)

        self.assertFalse(status_thread.is_alive(), 'status thread leaked')
        self.assertFalse(set_thread.is_alive(), 'state-write thread leaked')
        self.assertFalse(errors)
        self.assertFalse(service_lock_waiting.is_set(), 'state write acquired service lock under automatic lock')
        self.assertFalse(service_lock_timeout.is_set(), 'status blocked behind a reverse lock dependency')


if __name__ == '__main__':
    unittest.main()
