import os
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import Mock, patch

from macos_app import CloseGuard, Desktop, PREVIEW, stop_server
from server import Application, Server
from yingxu.paths import default_data_root
from yingxu import macos


class MacAdaptersTests(unittest.TestCase):
    def test_preview_and_close_drains_all_workers_in_order_once(self):
        self.assertEqual(PREVIEW, '0.4.20-mac.1')
        app=Application.__new__(Application)
        app._close_lock=threading.Lock();app._closed=False
        events=[]
        app.update_service=Mock();app.migration_jobs=Mock();app.jobs=Mock();app.thumbnails=Mock();app.context=Mock()
        app.update_service.close.side_effect=lambda:events.append(('updates',{}))
        app.migration_jobs.close.side_effect=lambda:events.append(('migration',{}))
        app.jobs.pool.shutdown.side_effect=lambda **kw:events.append(('jobs',kw))
        app.thumbnails.pool.shutdown.side_effect=lambda **kw:events.append(('thumbnails',kw))
        app.context.close.side_effect=lambda:events.append(('context',{})) or True
        app.close();app.close()
        self.assertEqual(events,[('updates',{}),('migration',{}),('jobs',{'wait':True,'cancel_futures':False}),('thumbnails',{'wait':True,'cancel_futures':False}),('context',{})])

    def test_close_attempts_remaining_cleanup_when_one_worker_fails(self):
        app=Application.__new__(Application)
        app._close_lock=threading.Lock();app._closed=False
        app.update_service=Mock();app.migration_jobs=Mock();app.jobs=Mock();app.thumbnails=Mock();app.context=Mock()
        app.jobs.pool.shutdown.side_effect=RuntimeError('synthetic worker failure')
        with self.assertRaises(RuntimeError):app.close()
        app.thumbnails.pool.shutdown.assert_called_once()
        app.context.close.assert_called_once()
        server=Mock();server.shutdown.side_effect=RuntimeError('synthetic shutdown failure')
        host=Mock()
        with self.assertRaises(RuntimeError):stop_server(server,host)
        server.server_close.assert_called_once();host.close.assert_called_once()

    def test_real_isolated_backend_stops_threads_and_keeps_044_capabilities(self):
        before={thread.ident for thread in threading.enumerate()}
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();app=Application(root/'data',root/'projects')
            server=Server(('127.0.0.1',0),app)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                caps=app.bootstrap()['capabilities']
                for key in ('maintenance','document_search','lazy_markdown'):
                    self.assertTrue(caps[key])
                # Exercise an accepted job, not just an empty executor shutdown.
                app.jobs.pool.submit(lambda:None).result(timeout=3)
                origin='http://127.0.0.1:'+str(server.server_port)
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                def post(token,request_origin=origin):
                    request=urllib.request.Request(origin+'/api/macos/desktop',data=b'{"action":"desktop-ready"}',
                        headers={'Content-Type':'application/json','Origin':request_origin,'X-YingXu-Token':token})
                    with opener.open(request,timeout=3) as response:return json.load(response)
                with self.assertRaises(urllib.error.HTTPError) as unavailable:post(app.token)
                self.assertEqual(unavailable.exception.code,404)
                app.desktop_message=Mock(return_value=True)
                for token,request_origin in [('wrong',origin),(app.token,'https://example.com')]:
                    with self.assertRaises(urllib.error.HTTPError) as rejected:post(token,request_origin)
                    self.assertEqual(rejected.exception.code,403)
                app.desktop_message.assert_not_called()
                self.assertEqual(post(app.token),{'ok':True})
                app.desktop_message.assert_called_once_with({'action':'desktop-ready'},app.token)
            finally:stop_server(server,app);thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertFalse([t.name for t in threading.enumerate() if t.ident not in before and t.name.startswith('yingxu-')])

    def test_native_picker_validates_kind_and_releases_lock_on_cancel_or_failure(self):
        app=Application.__new__(Application);app.picker_lock=threading.Lock();app.native_picker=Mock(return_value=None)
        with patch('server.sys.platform','darwin'):
            self.assertEqual(app.pick('files'),{'paths':[]})
            with self.assertRaisesRegex(Exception,'类型'):app.pick('arbitrary')
            app.native_picker.side_effect=OSError('cancelled')
            with self.assertRaises(OSError):app.pick('folder')
        self.assertFalse(app.picker_lock.locked())

    def test_default_directory_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()
            with patch('yingxu.paths.sys.platform','darwin'), patch('yingxu.paths.Path.home',return_value=root), patch.dict(os.environ,{'YINGXU_DATA_DIR':''}):
                self.assertEqual(default_data_root(),root/'Library/Application Support/YingXu')
                self.assertFalse((root/'Library').exists())
                with patch.dict(os.environ,{'YINGXU_DATA_DIR':str(root/'override')}):
                    self.assertEqual(default_data_root(),root/'override')

    def test_close_cancel_stale_response_and_busy_request(self):
        guard=CloseGuard();first=guard.begin()
        self.assertIsNone(guard.begin())
        self.assertFalse(guard.respond('wrong-id',True))
        self.assertFalse(guard.respond(first,False))
        second=guard.begin()
        self.assertNotEqual(first,second)
        self.assertFalse(guard.respond(first,True))
        self.assertFalse(guard.respond(second,'true'))
        third=guard.begin();self.assertTrue(guard.respond(third,True))
        self.assertTrue(guard.allowed)

    def test_expired_close_never_destroys_window(self):
        guard=CloseGuard();request=guard.begin();guard.deadline=0
        self.assertFalse(guard.respond(request,True));self.assertFalse(guard.allowed)

    def test_bridge_validates_token_origin_and_request(self):
        app=Mock(token='local-token');host=Desktop(app,'http://127.0.0.1:8791')
        host.window=Mock();host.window.get_current_url.return_value=host.origin+'/?desktop=macos'
        bridge=host
        request=host.guard.begin();data={'action':'exit-response','requestId':request,'allow':True}
        self.assertFalse(bridge.post_message(data,'wrong'));host.window.destroy.assert_not_called()
        host.window.get_current_url.return_value='https://example.com/'
        self.assertFalse(bridge.post_message(data,'local-token'));host.window.destroy.assert_not_called()
        host.window.get_current_url.return_value=host.origin+'/?desktop=macos'
        self.assertTrue(bridge.post_message(data,'local-token'));host.window.destroy.assert_called_once()

    def test_finder_uses_argument_list_and_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();file=root/'literal $(name).txt';file.write_text('synthetic')
            with patch('yingxu.macos.subprocess.run') as run:
                macos.open_path(file,reveal=True)
                self.assertEqual(run.call_args.args[0],['/usr/bin/open','-R',str(file)])

    @unittest.skipUnless(sys.platform=='darwin','Requires Darwin renamex_np')
    def test_native_rename_refuses_existing_files_and_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()
            for is_directory in (False,True):
                a=root/('directory-a' if is_directory else 'a.txt')
                b=root/('directory-b' if is_directory else 'b.txt')
                if is_directory:a.mkdir();b.mkdir()
                else:a.write_bytes(b'original');b.write_bytes(b'keep')
                with self.assertRaises(FileExistsError):macos.rename_exclusive(a,b)
                self.assertTrue(a.exists());self.assertTrue(b.exists())
                c=root/('directory-c' if is_directory else 'c.txt')
                macos.rename_exclusive(a,c)
                self.assertFalse(a.exists());self.assertTrue(c.exists())
