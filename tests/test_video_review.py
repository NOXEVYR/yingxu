import hashlib
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server import Application, Server
from yingxu.store import UserError


class VideoReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='yingxu-video-review-')
        self.root = Path(self.tmp.name).resolve()
        with patch('yingxu.skills.Path.home', return_value=self.root/'home'):
            self.app = Application(self.root/'data', self.root/'projects')
        self.video = self.root/'synthetic.mp4'
        if shutil.which('ffmpeg'):
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=s=320x180:r=24:d=3',
                            '-f','lavfi','-i','sine=frequency=880:duration=3','-c:v','libx264','-threads','1','-g','48','-c:a','aac',str(self.video)], check=True)
        else:
            # Metadata/HTTP tests still need enough bytes for bounded Range requests.
            self.video.write_bytes(bytes(range(256)) * 4)
        self.iid = self.app.external.open({'paths':[str(self.video)]})['entries'][0]['id']
        self.service = self.app.video_review
        self.original = hashlib.sha256(self.video.read_bytes()).hexdigest()

    def tearDown(self):
        self.app.close()
        self.assertEqual(hashlib.sha256(self.video.read_bytes()).hexdigest(), self.original)
        self.tmp.cleanup()

    def wait(self, jid):
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            job = self.service.status(jid)
            if job['state'] not in ('queued','running','cancelling'):
                return job
            time.sleep(.05)
        self.fail('export timed out')

    def export(self, **kwargs):
        return self.service.submit(dict(source='external',id=self.iid,start=.75,end=2.25,mode='encode',**kwargs))

    def test_review_roundtrip_conflict_and_invalid_values(self):
        old = self.service.get('external', self.iid)
        value = {'markers':[{'time':1.1,'note':'审片 <tag>'}], 'in':.3, 'out':2.2, 'a':.8, 'b':1.4}
        saved = self.service.save('external',self.iid,dict(revision=old['revision'],review=value))
        self.assertEqual(saved['review'],value)
        self.assertEqual(self.service.get('external',self.iid)['review'],value)
        with self.assertRaises(UserError):
            self.service.save('external',self.iid,dict(revision=old['revision'],review=value))
        for invalid in (float('nan'),float('inf'),True,-1,86401,'1'):
            with self.subTest(invalid=invalid), self.assertRaises(UserError):
                self.service.save('external',self.iid,dict(revision=saved['revision'],review={**value,'in':invalid}))
        with self.assertRaises(UserError):self.service.source('external','../synthetic.mp4')
        with self.assertRaises(UserError):self.service.submit({'path':str(self.video)})

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'local FFmpeg + test-only ffprobe required')
    def test_real_encode_copy_range_and_source_untouched(self):
        for mode in ('encode','copy'):
            job=self.service.submit(dict(source='external',id=self.iid,start=.75,end=2.25,mode=mode))
            result=self.wait(job['id']);self.assertEqual(result['state'],'done',result)
            output=self.service.output(job['id'])
            info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(output)]))
            self.assertEqual({s['codec_type'] for s in info['streams']},{'video','audio'})
            if mode=='encode':self.assertAlmostEqual(float(info['format']['duration']),1.5,delta=.1)
            else:self.assertGreater(float(info['format']['duration']),1.5)  # keyframe preroll is real
            self.assertEqual(set(p.name for p in output.parent.iterdir()),{output.name,'export.json'})

    @unittest.skipUnless(shutil.which('ffmpeg'),'local FFmpeg required')
    def test_cancel_and_failure_cleanup_keep_unrelated_file(self):
        sentinel=self.service._folder()/'keep.txt';sentinel.write_text('unrelated')
        job=self.export();self.service.cancel(job['id']);result=self.wait(job['id'])
        self.assertIn(result['state'],('cancelled','done'))  # completion may win the race
        if result['state']=='cancelled':self.assertFalse((self.service.root/job['id']).exists())
        invalid=self.root/'invalid.mp4';invalid.write_bytes(b'not a video')
        iid=self.app.external.open({'paths':[str(invalid)]})['entries'][0]['id']
        job=self.service.submit(dict(source='external',id=iid,start=0,end=1,mode='encode'))
        result=self.wait(job['id']);self.assertEqual(result['state'],'error');self.assertFalse((self.service.root/job['id']).exists())
        self.assertEqual(sentinel.read_text(),'unrelated');self.assertEqual(invalid.read_bytes(),b'not a video')

    @unittest.skipUnless(shutil.which('ffmpeg'),'local FFmpeg required')
    def test_queued_cancel_and_restart_receipt(self):
        started=threading.Event();release=threading.Event();original=self.service._run
        def held(job,executable):
            started.set();release.wait(10);original(job,executable)
        with patch.object(self.service,'_run',side_effect=held):
            first=self.export();self.assertTrue(started.wait(5))
            second=self.export();self.service.cancel(second['id']);release.set()
            self.assertEqual(self.wait(second['id'])['state'],'cancelled')
            self.assertFalse((self.service.root/second['id']).exists())
            self.assertEqual(self.wait(first['id'])['state'],'done')
        from yingxu.video_review import VideoReview
        restored=VideoReview(self.app)
        try:
            self.assertEqual(restored.status(first['id'])['state'],'done')
            self.assertTrue(restored.output(first['id']).is_file())
            self.assertEqual(restored.recent('external',self.iid)['jobs'][0]['id'],first['id'])
        finally:restored.close()

    def test_http_origin_token_range_and_download_headers(self):
        server=Server(('127.0.0.1',0),self.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(method,path,data=None,headers=None):
            client=http.client.HTTPConnection('127.0.0.1',server.server_port)
            merged={'X-YingXu-Token':self.app.token,'Content-Type':'application/json',**(headers or {})}
            client.request(method,path,json.dumps(data) if data is not None else None,merged)
            response=client.getresponse();result=(response.status,response.read(),dict(response.getheaders()));client.close();return result
        try:
            endpoint='/api/video/review/external/'+self.iid
            self.assertEqual(request('GET',endpoint,headers={'Origin':'https://evil.invalid'})[0],403)
            current=json.loads(request('GET',endpoint)[1])
            payload=dict(revision=current['revision'],review={'markers':[],'in':0,'out':1,'a':None,'b':None})
            self.assertEqual(request('POST',endpoint,payload,{'X-YingXu-Token':''})[0],403)
            self.assertEqual(request('POST',endpoint,payload)[0],200)
            path='/api/external-media/'+self.iid
            result=request('GET',path,headers={'Range':'bytes=20-119'})
            self.assertEqual(result[0],206);self.assertEqual(result[1],self.video.read_bytes()[20:120])
            self.assertTrue(result[2]['Content-Range'].startswith('bytes 20-119/'))
            self.assertEqual(request('GET',path,headers={'Range':'bytes=999999999-'})[0],416)
            self.assertEqual(request('GET',path,headers={'Range':'bytes=-64'})[1],self.video.read_bytes()[-64:])
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
