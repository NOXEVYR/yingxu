"""Actual decoded video, integer PTS and pixel-oracle tests; synthetic files only."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import closing
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server import Application, Server
from yingxu.store import UserError
from yingxu.video_frames import VideoFrames


class WindowsFrameSignatureTests(unittest.TestCase):
    def _assert_change_time_readability_transition_during_hash(self, before, after):
        with tempfile.TemporaryDirectory(prefix='yingxu-win-change-time-transition-') as temporary:
            path=Path(temporary)/'源片 中文.mp4';path.write_bytes(b'unchanged bytes')
            digest=hashlib.sha256(b'unchanged bytes').hexdigest()
            service=VideoFrames.__new__(VideoFrames)
            service.closed=False
            service._source=lambda session:path
            service._check=lambda job,session: None
            # Force a digest read so both metadata snapshots bracket the hash.
            session={'path':path,'sha256':digest,'hash_signature':None,'invalid':False}
            job={'started':time.monotonic(),'kind':'index','bytes':0}
            with patch('yingxu.video_frames.sys.platform','win32'), \
                    patch.object(service,'_hash_signature',side_effect=[(before, before[-1] is not None),
                                                                        (after, after[-1] is not None)]):
                self.assertEqual(service._hash(job,session),digest)
            self.assertFalse(session['invalid'])
            self.assertIsNone(session['hash_signature'])

    def test_readable_change_time_may_become_unavailable_during_hash(self):
        self._assert_change_time_readability_transition_during_hash(
            [1,2,5,3,77,123],[1,2,5,3,77,None])

    def test_unavailable_change_time_may_become_readable_during_hash(self):
        self._assert_change_time_readability_transition_during_hash(
            [1,2,5,3,77,None],[1,2,5,3,77,123])

    def test_unavailable_change_time_rehashes_and_restores_cache_safely(self):
        with tempfile.TemporaryDirectory(prefix='yingxu-win-change-time-fallback-') as temporary:
            path=Path(temporary)/'源片 中文.mp4';path.write_bytes(b'first')
            token={'value':123}
            class Review:
                def source(self, source, item): return path
            service=VideoFrames.__new__(VideoFrames)
            service.app=SimpleNamespace(video_review=Review());service.closed=False
            service._check=lambda job,session: None
            session={'path':path,'source':'external','item':'fixture','fingerprint':[1,2,5,3],
                     'cancel':threading.Event(),'invalid':False}
            job={'started':time.monotonic(),'kind':'index','bytes':0}
            stable_stat=SimpleNamespace(st_ctime_ns=77)
            with patch('yingxu.video_frames.sys.platform','win32'), \
                    patch('yingxu.video_frames.fingerprint',return_value=[1,2,5,3]), \
                    patch('yingxu.video_frames.windows_change_time',side_effect=lambda _:token['value']), \
                    patch.object(Path,'stat',return_value=stable_stat):
                digest=service._hash(job,session)
                session['sha256']=digest
                self.assertEqual(session['hash_signature'][-1],123)

                token['value']=None
                self.assertIs(service._source(session),path)
                self.assertFalse(session['invalid'])
                self.assertEqual(service._hash(job,session),digest)
                self.assertIsNone(session['hash_signature'])

                token['value']=123
                self.assertEqual(service._hash(job,session),digest)
                self.assertEqual(session['hash_signature'][-1],123)

                token['value']=None
                path.write_bytes(b'other')
                with self.assertRaisesRegex(UserError,'内容已改变'):
                    service._hash(job,session)
            self.assertTrue(session['invalid'])

    def test_change_time_invalidates_hash_when_creation_time_and_mtime_are_stable(self):
        with tempfile.TemporaryDirectory(prefix='yingxu-win-change-time-') as temporary:
            path=Path(temporary)/'源片 中文.mp4';path.write_bytes(b'first')
            token={'value':101}
            class Review:
                def source(self, source, item): return path
            service=VideoFrames.__new__(VideoFrames)
            service.app=SimpleNamespace(video_review=Review());service.closed=False
            service._check=lambda job,session: None
            session={'path':path,'source':'external','item':'fixture','fingerprint':[1,2,5,3],
                     'cancel':threading.Event(),'invalid':False}
            job={'started':time.monotonic(),'kind':'index','bytes':0}
            stable_stat=SimpleNamespace(st_ctime_ns=77)
            with patch('yingxu.video_frames.sys.platform','win32'), \
                    patch('yingxu.video_frames.fingerprint',return_value=[1,2,5,3]), \
                    patch('yingxu.video_frames.windows_change_time',side_effect=lambda _:token['value']), \
                    patch.object(Path,'stat',return_value=stable_stat):
                first=service._hash(job,session)
                self.assertEqual(first,hashlib.sha256(b'first').hexdigest())
                path.write_bytes(b'other')
                token['value']=102
                with self.assertRaisesRegex(UserError,'已改变'):
                    service._hash(job,session)
            self.assertTrue(session['invalid'])

    def test_windows_process_uses_unicode_bundle_path_and_releases_owned_files(self):
        with tempfile.TemporaryDirectory(prefix='yingxu-win-process-') as temporary:
            root=Path(temporary).resolve();directory=root/'缓存 中文';directory.mkdir()
            executable=directory/'运行库 bin'/'ffmpeg.exe';executable.parent.mkdir()
            service=VideoFrames.__new__(VideoFrames)
            service.lock=threading.RLock();service.root=root;service.sessions={}
            service._check=lambda job,session: None
            session={'id':'a'*32,'directory':directory,'owned':set(),
                     'tools':{'ffmpeg':{'path':str(executable.resolve())}}}
            job={'id':'b'*32,'cancel':threading.Event()}
            process=SimpleNamespace(returncode=0,poll=lambda:0)
            with patch('yingxu.video_frames.sys.platform','win32'), \
                    patch('yingxu.video_frames.subprocess.Popen',return_value=process) as popen:
                service._process(job,session,[str(executable),'-version'])
            environment=popen.call_args.kwargs['env']
            search=environment['PATH'].split(';')
            self.assertEqual(search[0],str(executable.parent))
            self.assertNotIn(os.environ.get('PATH',''),search)
            self.assertEqual({path.name for path in directory.iterdir() if path.is_file()},{'owner.json'})
            self.assertFalse(session['owned'])
from tools.make_video_fixtures import generate


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg and ffprobe required')
class VideoFrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_temp = tempfile.TemporaryDirectory(prefix='yingxu-frame-fixtures-')
        cls.media = generate(Path(cls.media_temp.name).resolve())

    @classmethod
    def tearDownClass(cls):
        cls.media_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingxu-frame-test-')
        self.root = Path(self.temp.name).resolve()
        with patch('yingxu.skills.Path.home', return_value=self.root/'home'):
            self.app = Application(self.root/'data', self.root/'projects')
        self.service = self.app.video_frames
        self.originals = {}

    def tearDown(self):
        self.app.close()
        for path, digest in self.originals.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.temp.cleanup()

    def open(self, name):
        path = self.media/name
        self.originals[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        return self.app.external.open({'paths':[str(path)]})['entries'][0]['id']

    def wait(self, jid, state=None):
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            value = self.service.status(jid)
            if value['state'] not in ('queued','running','cancelling'):
                if state: self.assertEqual(value['state'], state, value)
                return value
            time.sleep(.02)
        self.fail('frame operation timed out')

    def index(self, name='cfr.mp4'):
        value = self.service.start({'source':'external','id':self.open(name)})
        ready = self.wait(value['id'], 'ready')
        self.assertEqual(ready['verified_frames'], ready['count'])
        return ready

    def frame(self, sid, ordinal):
        value = self.service.frame(sid, {'ordinal':ordinal})
        return self.wait(value['id'], 'done')

    def test_cfr_vfr_offset_duplicate_pts_rotation_against_decode_oracle(self):
        from PIL import Image
        for name, count in [('cfr.mp4',192),('vfr.mp4',128),('offset.mp4',192),('duplicate-pts.mkv',16),('rotated.mp4',192)]:
            with self.subTest(name=name):
                index = self.index(name); sid = index['id']
                self.assertEqual(index['count'], count)
                probe = json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_frames','-show_entries','frame=pts,best_effort_timestamp','-of','json',str(self.media/name)]))['frames']
                with closing(sqlite3.connect(self.service.sessions[sid]['directory']/'index.sqlite')) as db, db:
                    rows = db.execute('SELECT ordinal,pts FROM frames ORDER BY ordinal').fetchall()
                self.assertEqual(rows, [(n, row.get('pts')) for n,row in enumerate(probe)])
                if name=='vfr.mp4': self.assertGreater(len({rows[n+1][1]-rows[n][1] for n in range(count-1)}),1)
                if name=='offset.mp4': self.assertGreater(int(index['first_pts']),0)
                if name=='rotated.mp4': self.assertEqual(index['stream']['side_data_list'][0]['rotation'],90)
                if name=='duplicate-pts.mkv': self.assertEqual(len({row[1] for row in rows}),8)
                # A separate whole-stream raw RGB decode is the oracle, including
                # default display-matrix rotation. No seek or average FPS involved.
                raw = subprocess.check_output(['ffmpeg','-v','error','-i',str(self.media/name),'-map','0:v:0','-an','-vf','format=rgb24','-fps_mode','passthrough','-f','rawvideo','-threads','1','pipe:1'])
                width,height = (360,640) if name=='rotated.mp4' else (640,360)
                stride = width*height*3
                self.assertEqual(len(raw), stride*count)
                samples = sorted({0,1,count//2,count-2,count-1,96 if count>96 else 2})
                for n in samples:
                    result = self.frame(sid,n)
                    self.assertEqual(result['pts'],str(probe[n]['pts']))
                    with Image.open(self.service.image(result['id'])) as picture:
                        self.assertEqual(picture.size,(width,height))
                        self.assertEqual(picture.convert('RGB').tobytes(),raw[n*stride:(n+1)*stride])
                self.service.cancel(sid)
                self.assertNotIn(sid,self.service.sessions)

    def test_adjacent_cache_hit_lru_and_strict_input(self):
        index=self.index();sid=index['id'];first=self.frame(sid,20)
        with patch.object(self.service,'_process',side_effect=AssertionError('neighbor should be cached')):
            self.frame(sid,21)
        for bad in (True,1.5,-1,192,'12'):
            with self.subTest(bad=bad),self.assertRaises(UserError): self.service.frame(sid,{'ordinal':bad})
        with self.assertRaises(UserError): self.service.start({'path':str(self.media/'cfr.mp4')})
        with patch('yingxu.video_frames.MAX_CACHE',150000):
            result=self.frame(sid,80)
            self.assertLessEqual(sum(self.service.cache.values()),150000)
            self.assertTrue(self.service.image(result['id']).is_file())
            with self.assertRaises(UserError): self.service.image(first['id'])
        with patch('yingxu.video_frames.MAX_CACHE_ENTRIES',4):
            self.frame(sid,100);self.frame(sid,130)
            self.assertLessEqual(len(self.service.cache),4)
        # Terminal child jobs cannot make status history grow without bound.
        with patch.object(self.service,'_extract',return_value=None):
            for _ in range(66):self.frame(sid,1)
        self.assertLessEqual(len(self.service.jobs),64)

    def test_source_mutation_rejects_index_even_when_stat_restored(self):
        path=self.root/'mutable.mp4';shutil.copyfile(self.media/'cfr.mp4',path)
        iid=self.app.external.open({'paths':[str(path)]})['entries'][0]['id']
        sid=self.wait(self.service.start({'source':'external','id':iid})['id'],'ready')['id']
        cached=self.frame(sid,1)
        stat=path.stat();data=bytearray(path.read_bytes());data[-1]^=1;path.write_bytes(data)
        os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        with self.assertRaises(UserError): self.service.image(cached['id'])
        with self.assertRaises(UserError): self.service.frame(sid,{'ordinal':1})
        self.assertNotIn(sid,self.service.sessions)
        self.assertEqual(path.read_bytes(),data)

    def test_cancel_running_child_cleans_session_and_keeps_unknown_file(self):
        sid=self.index()['id'];directory=self.service.sessions[sid]['directory']
        keep=directory/'unrelated.txt';keep.write_text('keep')
        entered=threading.Event();release=threading.Event();original=self.service._extract
        def held(job,session):
            entered.set();release.wait(5);original(job,session)
        with patch.object(self.service,'_extract',side_effect=held):
            frame=self.service.frame(sid,{'ordinal':100});self.assertTrue(entered.wait(5))
            self.service.cancel(sid);release.set()
            self.wait(frame['id'],'cancelled')
        self.assertNotIn(sid,self.service.sessions)
        self.assertEqual(list(directory.iterdir()),[keep]);self.assertEqual(keep.read_text(),'keep')

    def test_queue_bound_and_queued_cancel(self):
        entered=threading.Event();release=threading.Event();original=self.service._index
        def held(job,session):
            entered.set();release.wait(5);original(job,session)
        iid=self.open('cfr.mp4')
        with patch.object(self.service,'_index',side_effect=held):
            jobs=[self.service.start({'source':'external','id':iid}) for _ in range(4)]
            self.assertTrue(entered.wait(5))
            with self.assertRaises(UserError): self.service.start({'source':'external','id':iid})
            for job in jobs:self.service.cancel(job['id'])
            release.set()
            for job in jobs:self.wait(job['id'],'cancelled')
        self.assertFalse(self.service.sessions)

    def test_process_cancel_terminates_and_cleans(self):
        sid=self.index()['id'];original=self.service._extract;entered=threading.Event()
        def held(job,session):
            entered.set()
            import sys
            self.service._process(job,session,[sys.executable,'-c','import time; time.sleep(60)'])
            original(job,session)
        with patch.object(self.service,'_extract',side_effect=held):
            task=self.service.frame(sid,{'ordinal':100});self.assertTrue(entered.wait(5));time.sleep(.15)
            self.service.cancel(task['id']);self.wait(task['id'],'cancelled')
        self.assertEqual({p.name for p in self.service.sessions[sid]['directory'].iterdir()},{'owner.json','index.sqlite'})
        self.frame(sid,1)

    def test_failed_decoder_and_oversized_index_clean_owned_files(self):
        broken=self.root/'broken.mp4';broken.write_bytes(b'not a video')
        iid=self.app.external.open({'paths':[str(broken)]})['entries'][0]['id']
        result=self.wait(self.service.start({'source':'external','id':iid})['id'],'error')
        self.assertFalse((self.service.root/result['id']).exists());self.assertEqual(broken.read_bytes(),b'not a video')
        with patch('yingxu.video_frames.MAX_FRAMES',10):
            task=self.service.start({'source':'external','id':self.open('cfr.mp4')})
            result=self.wait(task['id'],'error');self.assertIn('帧数超过',result['error'])
            self.assertFalse((self.service.root/result['id']).exists())

    def test_recovery_only_removes_owned_files_and_capability_gates(self):
        self.service.root.mkdir(exist_ok=True);sid='a'*32;folder=self.service.root/sid;folder.mkdir()
        (folder/'index.sqlite').write_bytes(b'cache');(folder/'unknown.txt').write_text('keep')
        (folder/'owner.json').write_text(json.dumps({'purpose':'yingxu-frame-cache-v1','id':sid,'files':['index.sqlite']}))
        restored=VideoFrames(self.app)
        try:self.assertEqual([p.name for p in folder.iterdir()],['unknown.txt'])
        finally:restored.close()
        with patch('yingxu.video_frames.sys.platform','darwin'):self.assertFalse(self.service.capability()['available'])
        with patch('yingxu.video_frames.sys.platform','win32'), patch('yingxu.video_frames.frame_tool_paths',return_value=(None,None)):
            self.assertFalse(self.service.capability()['available'])
        with patch('yingxu.video_frames.frame_tool_paths',return_value=('locked-ffmpeg',None)):
            self.assertFalse(self.service.capability()['available'])

    def test_unicode_source_path_is_decoded_and_exported(self):
        source_dir=self.root/'素材 中文';source_dir.mkdir()
        source=source_dir/'镜头 测试.mp4';shutil.copyfile(self.media/'cfr.mp4',source)
        iid=self.app.external.open({'paths':[str(source)]})['entries'][0]['id']
        index=self.wait(self.service.start({'source':'external','id':iid})['id'],'ready')
        frame=self.frame(index['id'],7)
        self.assertEqual(frame['ordinal'],7)
        task=self.service.export(index['id'],{'first':4,'last':6,'audio':'none'})
        result=self.wait(task['id'],'done')
        self.assertEqual(result['count'],3)
        self.assertTrue(self.app.video_review.output(task['id']).is_file())

    @unittest.skipUnless(os.name=='nt','Windows source-handle behavior')
    def test_windows_cancel_releases_handle_for_unicode_source(self):
        source_dir=self.root/'源片 中文';source_dir.mkdir()
        source=source_dir/'取消时仍在解码.mp4';shutil.copyfile(self.media/'cfr.mp4',source)
        self.originals[source]=hashlib.sha256(source.read_bytes()).hexdigest()
        iid=self.app.external.open({'paths':[str(source)]})['entries'][0]['id']
        sid=self.wait(self.service.start({'source':'external','id':iid})['id'],'ready')['id']
        entered=threading.Event();original_popen=subprocess.Popen
        def tracked_popen(*args,**kwargs):
            process=original_popen(*args,**kwargs);entered.set();return process
        def hold_source(job,session):
            self.service._process(job,session,[session['tools']['ffmpeg']['path'],'-re','-nostdin','-hide_banner',
                '-loglevel','error','-i',str(session['path']),'-map','0:v:0','-an','-f','null','-'])
        with patch.object(self.service,'_extract',side_effect=hold_source), \
                patch('yingxu.video_frames.subprocess.Popen',side_effect=tracked_popen):
            task=self.service.frame(sid,{'ordinal':10})
            self.assertTrue(entered.wait(5))
            self.service.cancel(task['id'])
            self.wait(task['id'],'cancelled')
        moved=source.with_name('取消后可重命名.mp4')
        os.replace(source,moved);os.replace(moved,source)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),self.originals[source])

    def test_keyframe_seek_verified_and_mismatch_falls_back(self):
        sid=self.index()['id']
        result=self.frame(sid,145)
        self.assertEqual(result['decode_mode'],'verified-keyframe')
        original=self.service._process
        def wrong_seek(job,session,command,**kwargs):
            if '-ss' in command:
                command=list(command);at=command.index('-vf')+1
                command[at]=command[at].replace('select=between(n\\,1\\,14)', 'select=between(n\\,2\\,15)')
                # Force a mismatched sequence regardless of prefetch bounds.
                command[at]=command[at].replace('trim=start_pts=', 'trim=start_pts=1+')
            return original(job,session,command,**kwargs)
        with patch.object(self.service,'_process',side_effect=wrong_seek):
            result=self.frame(sid,97)
        self.assertEqual(result['decode_mode'],'verified-prefix')
        self.assertEqual(self.service.jobs[result['id']]['state'],'done')

    def test_strict_video_export_all_frames_pts_pixels_and_persistent_history(self):
        from fractions import Fraction
        for name,first,last,container in [('cfr.mp4',5,31,'nut'),('vfr.mp4',93,102,'nut'),
                ('offset.mp4',5,31,'nut'),('rotated.mp4',5,31,'nut'),('duplicate-pts.mkv',2,9,'mkv'),('cfr.mp4',0,0,'mkv')]:
            with self.subTest(name=name,first=first,last=last):
                sid=self.index(name)['id']
                task=self.service.export(sid,{'first':first,'last':last,'audio':'none'})
                result=self.wait(task['id'],'done')
                self.assertEqual(result['format'],container)
                output=self.app.video_review.output(task['id'])
                manifest=json.loads(self.service.export_manifest(task['id']).read_text())
                self.assertEqual((manifest['first_inclusive'],manifest['last_inclusive'],manifest['count']),(first,last,last-first+1))
                raw_source=subprocess.check_output(['ffmpeg','-v','error','-i',str(self.media/name),'-map','0:v:0','-an',
                    '-vf',f'select=between(n\\,{first}\\,{last}),setpts=N,format=rgb24','-fps_mode','passthrough','-f','rawvideo','-threads','1','pipe:1'])
                raw_output=subprocess.check_output(['ffmpeg','-v','error','-i',str(output),'-map','0:v:0','-an','-vf','setpts=N,format=rgb24',
                    '-fps_mode','passthrough','-f','rawvideo','-threads','1','pipe:1'])
                self.assertEqual(raw_output,raw_source)
                self.assertEqual(manifest['audio'],'none')
                origin=int(manifest['frames'][0]['source_pts'])
                for frame in manifest['frames']:
                    self.assertEqual((int(frame['source_pts'])-origin)*Fraction(manifest['source_time_base']),int(frame['output_pts'])*Fraction(manifest['output_time_base']))
                self.service.cancel(sid)
                self.assertTrue(output.is_file())
                self.assertTrue(self.service.export_manifest(task['id']).is_file())
                self.assertEqual(self.service.status(task['id'])['state'],'done')
        from yingxu.video_review import VideoReview
        restored=VideoReview(self.app)
        try:
            self.assertEqual(restored.status(task['id'])['mode'],'frames')
            self.assertEqual(restored.output(task['id']),output)
        finally:restored.close()

    def test_strict_export_validation_and_cancel_cleanup(self):
        sid=self.index()['id']
        for data in ({'first':0,'last':1,'audio':'copy'},{'first':True,'last':1,'audio':'none'},
                {'first':2,'last':1,'audio':'none'},{'first':0,'last':192,'audio':'none'}):
            with self.assertRaises(UserError):self.service.export(sid,data)
        entered=threading.Event();release=threading.Event();original=self.service._process
        def held(job,session,command,**kwargs):
            if job['kind']=='export' and '-c:v' in command and 'ffv1' in command:
                entered.set();release.wait(5)
            return original(job,session,command,**kwargs)
        with patch.object(self.service,'_process',side_effect=held):
            task=self.service.export(sid,{'first':1,'last':80,'audio':'none'})
            self.assertTrue(entered.wait(5));self.service.cancel(task['id']);release.set()
            self.wait(task['id'],'cancelled')
        self.assertFalse((self.app.video_review.root/task['id']).exists())
        self.frame(sid,1)

    def test_export_rejects_changed_output_pts_and_pixel_hashes(self):
        sid=self.index()['id'];original=self.service._process
        for mismatch in ('pts','pixels'):
            if mismatch=='pixels':
                with closing(sqlite3.connect(self.service.sessions[sid]['directory']/'index.sqlite')) as db, db:
                    db.execute('UPDATE frames SET rgb_sha256=? WHERE ordinal=5',('0'*64,))
            def corrupted(job,session,command,**kwargs):
                value=original(job,session,command,**kwargs)
                if job['kind']=='export' and '-show_entries' in command and mismatch=='pts':
                    data=json.loads(value);data['frames'][-1]['pts']+=1
                    return json.dumps(data).encode()
                return value
            with patch.object(self.service,'_process',side_effect=corrupted):
                task=self.service.export(sid,{'first':5,'last':8,'audio':'none'})
                self.wait(task['id'],'error')
            self.assertFalse((self.app.video_review.root/task['id']).exists())
            with self.assertRaises(UserError):self.app.video_review.output(task['id'])

    def test_source_change_stops_inflight_process_and_deleted_source_invalidates(self):
        import sys
        path=self.root/'mutable.mp4';shutil.copyfile(self.media/'cfr.mp4',path)
        iid=self.app.external.open({'paths':[str(path)]})['entries'][0]['id']
        sid=self.wait(self.service.start({'source':'external','id':iid})['id'],'ready')['id']
        entered=threading.Event()
        def running(job,session):
            entered.set()
            self.service._process(job,session,[sys.executable,'-c','import time; time.sleep(30)'])
        with patch.object(self.service,'_extract',side_effect=running):
            task=self.service.frame(sid,{'ordinal':30});self.assertTrue(entered.wait(5))
            with path.open('ab') as handle:handle.write(b'changed synthetic copy')
            started=time.monotonic();result=self.wait(task['id'],'error')
            self.assertLess(time.monotonic()-started,3)
            self.assertIn('已改变',result['error'])
        self.assertNotIn(sid,self.service.sessions)
        path2=self.root/'removed.mp4';shutil.copyfile(self.media/'cfr.mp4',path2)
        iid=self.app.external.open({'paths':[str(path2)]})['entries'][0]['id']
        sid=self.wait(self.service.start({'source':'external','id':iid})['id'],'ready')['id']
        cached=self.frame(sid,1);path2.unlink()
        with self.assertRaises(UserError):self.service.image(cached['id'])
        self.assertNotIn(sid,self.service.sessions)

    def test_http_auth_id_only_and_png(self):
        server=Server(('127.0.0.1',0),self.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(method,path,data=None,headers=None):
            client=http.client.HTTPConnection('127.0.0.1',server.server_port)
            client.request(method,path,json.dumps(data) if data is not None else None,{'X-YingXu-Token':self.app.token,'Content-Type':'application/json',**(headers or {})})
            response=client.getresponse();result=(response.status,response.read(),dict(response.getheaders()));client.close();return result
        try:
            route='/api/video/frames/index';payload={'source':'external','id':self.open('cfr.mp4')}
            self.assertEqual(request('POST',route,payload,{'X-YingXu-Token':''})[0],403)
            self.assertEqual(request('POST',route,payload,{'Origin':'https://evil.invalid'})[0],403)
            self.assertEqual(request('POST',route,{'path':'/private.mp4'})[0],400)
            status,body,_=request('POST',route,payload);self.assertEqual(status,202);sid=json.loads(body)['id'];self.wait(sid,'ready')
            status,body,_=request('POST',route+'/'+sid+'/frame',{'ordinal':24});self.assertEqual(status,202);jid=json.loads(body)['id'];self.wait(jid,'done')
            status,body,headers=request('GET','/api/video/frames/jobs/'+jid+'/image')
            self.assertEqual(status,200);self.assertTrue(body.startswith(b'\x89PNG'));self.assertIn('image/png',headers['Content-Type'])
            self.assertEqual(request('GET','/api/video/frames/jobs/'+jid+'/image',headers={'Origin':'https://evil.invalid'})[0],403)
            export_route=route+'/'+sid+'/export';selection={'first':3,'last':7,'audio':'none'}
            self.assertEqual(request('POST',export_route,selection,{'Origin':'https://evil.invalid'})[0],403)
            status,body,_=request('POST',export_route,selection);self.assertEqual(status,202)
            eid=json.loads(body)['id'];self.wait(eid,'done')
            manifest_route='/api/video/frames/jobs/'+eid+'/manifest'
            self.assertEqual(request('GET',manifest_route,headers={'Origin':'https://evil.invalid'})[0],403)
            status,body,headers=request('GET',manifest_route)
            self.assertEqual(status,200);self.assertEqual(json.loads(body)['count'],5)
            self.assertIn('attachment',headers['Content-Disposition'])
            self.assertEqual(request('POST',route+'/'+sid+'/cancel',{})[0],200)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__': unittest.main()
