from contextlib import contextmanager
import threading
import time
from types import SimpleNamespace
import unittest

from yingxu.migration_jobs import MigrationJobs
from yingxu.store import UserError


class MigrationJobsTests(unittest.TestCase):
    def setUp(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.app = SimpleNamespace(
            store=SimpleNamespace(lock=threading.RLock()),
            context=SimpleNamespace(_export_lock=threading.RLock()),
            jobs=SimpleNamespace(lock=threading.Lock(), jobs={}),
            changed=lambda: None)
        self.service = MigrationJobs(self.app, SimpleNamespace(execute=self.execute))

    def tearDown(self):
        self.release.set()
        self.service.close()

    def execute(self, body, progress=None):
        self.calls.append(body)
        progress({'message': 'copying', 'completed': 1, 'total': 2})
        self.entered.set()
        self.release.wait(5)
        return {'projects': [{'id': 'p'}]}

    def submit(self, token='preview-token'):
        with self.service.mutation('POST', '/api/project-storage/migration'):
            return self.service.submit({'token': token})['job_id']

    def finish(self, jid):
        self.release.set()
        deadline = time.monotonic() + 5
        while self.service.get(jid)['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        return self.service.get(jid)

    def test_retry_accepted_token_never_runs_twice_and_reads_remain_available(self):
        jid = self.submit()
        self.assertTrue(self.entered.wait(2))
        self.assertEqual(self.submit(), jid)
        self.assertEqual(len(self.calls), 1)
        with self.service.mutation('GET'):
            self.assertEqual(self.service.get(jid)['completed'], 1)
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            with self.assertRaises(UserError):
                with self.service.mutation(method, '/api/upload'):
                    self.fail('write was allowed during migration')
        with self.assertRaises(UserError):
            self.submit('different-token')
        self.assertEqual(self.finish(jid)['state'], 'done')
        self.assertEqual(self.submit(), jid)
        with self.service.mutation('POST', '/api/upload'):
            pass

    def test_inflight_writer_and_import_queue_prevent_start(self):
        with self.service.mutation('POST', '/api/upload'):
            with self.assertRaises(UserError):
                self.submit()
        self.app.jobs.jobs['import'] = {'state': 'queued'}
        with self.assertRaises(UserError):
            self.submit()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service.writers, 0)

    def test_exception_releases_gate_and_retry_reports_same_failure(self):
        def fail(*args, **kwargs):
            raise UserError('source changed')
        self.service.migration.execute = fail
        jid = self.submit()
        result = self.finish(jid)
        self.assertEqual(result['state'], 'error')
        self.assertEqual(result['error'], 'source changed')
        self.assertEqual(self.submit(), jid)
        with self.service.mutation('PATCH'):
            pass

    def test_context_refresh_failure_does_not_report_committed_move_as_failed(self):
        def fail():
            raise UserError('export unavailable')
        self.app.changed = fail
        jid = self.submit()
        result = self.finish(jid)
        self.assertEqual(result['state'], 'done')
        self.assertTrue(result['result']['warnings'])

    def test_invalid_body_and_unknown_job(self):
        for body in (None, {}, {'token': []}, {'token': ''}, {'token': 'a', 'extra': 1}):
            with self.assertRaises(UserError):
                self.service.submit(body)
        with self.assertRaises(UserError):
            self.service.get('missing')


if __name__ == '__main__':
    unittest.main()
