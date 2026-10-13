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
import unittest
from unittest.mock import patch

from server import Application, Server
from yingxu.store import UserError
from yingxu.video_frames import VideoFrames
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
        return self.wait(value['id'], 'ready')

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
                with sqlite3.connect(self.service.sessions[sid]['directory']/'index.sqlite') as db:
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
        self.frame(sid,1)
        stat=path.stat();data=bytearray(path.read_bytes());data[-1]^=1;path.write_bytes(data)
        os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        result=self.wait(self.service.frame(sid,{'ordinal':1})['id'],'error')
        self.assertIn('内容已改变',result['error']);self.assertNotIn(sid,self.service.sessions)
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
        with patch('yingxu.video_frames.sys.platform','win32'):self.assertFalse(self.service.capability()['available'])
        with patch('yingxu.video_frames.ffprobe_path',return_value=None):self.assertFalse(self.service.capability()['available'])

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
            self.assertEqual(request('POST',route+'/'+sid+'/cancel',{})[0],200)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__': unittest.main()
