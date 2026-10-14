"""Real synthetic-media benchmark and independent full RGB decode oracle.

The caller must supply generated media, never private files. 'Cold' refers only
 to application PNG cache; this tool does not flush the operating system cache.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from server import Application
from yingxu import __version__, __build__


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();source=args.media.resolve();original=hashlib.sha256(source.read_bytes()).hexdigest();rows=[]
    with tempfile.TemporaryDirectory(prefix='yingxu-frame-benchmark-') as temporary:
        root=Path(temporary).resolve()
        with patch('yingxu.skills.Path.home',return_value=root/'home'):app=Application(root/'data',root/'projects')
        try:
            item=app.external.open({'paths':[str(source)]})['entries'][0]['id'];service=app.video_frames
            def wait(jid):
                deadline=time.monotonic()+3600
                while time.monotonic()<deadline:
                    result=service.status(jid)
                    if result['state'] not in ('queued','running','cancelling'):
                        assert result['state'] in ('ready','done'),result
                        return result
                    time.sleep(.01)
                raise AssertionError('benchmark timeout')
            start=time.monotonic();index=wait(service.start({'source':'external','id':item})['id']);sid=index['id']
            rows.append({'operation':'index','seconds':time.monotonic()-start,'count':index['count']})
            tail=index['count']-100
            for label,n in [('tail-cold',tail),('tail-cache-hit',tail),('adjacent-cached',tail+1),
                    ('random-uncached',index['count']-700),('tail-revisit',tail)]+[('continuous-step',n) for n in range(tail+2,tail+17)]:
                start=time.monotonic();frame=wait(service.frame(sid,{'ordinal':n})['id'])
                rows.append({'operation':label,'ordinal':n,'seconds':time.monotonic()-start,'pts':frame['pts'],
                    'time_base':frame['time_base'],'decode_mode':frame['decode_mode'],'png_bytes':service.image(frame['id']).stat().st_size})
            # A second independent sequential rawvideo pipeline, not the service's
            # seeking, PNG reader, or framehash parser, checks every indexed hash.
            with sqlite3.connect(service.sessions[sid]['directory']/'index.sqlite') as db:
                expected=db.execute('SELECT rgb_sha256 FROM frames ORDER BY ordinal').fetchall()
            stride=index['stream']['width']*index['stream']['height']*3
            process=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-i',str(source),'-map','0:v:0','-an',
                '-vf','format=rgb24,setpts=N','-fps_mode','passthrough','-f','rawvideo','-threads','1','pipe:1'],stdout=subprocess.PIPE)
            checked=0;start=time.monotonic()
            try:
                while True:
                    frame=process.stdout.read(stride)
                    if not frame:break
                    assert len(frame)==stride,(checked,len(frame))
                    assert checked<len(expected) and hashlib.sha256(frame).hexdigest()==expected[checked][0],checked
                    checked+=1
                assert process.wait(timeout=10)==0
                assert checked==index['count']
            finally:
                process.stdout.close()
                if process.poll() is None:process.kill();process.wait()
            oracle={'frames_checked':checked,'seconds':time.monotonic()-start,'all_rgb_hashes_match':True}
            service.cancel(sid)
        finally:app.close()
    assert hashlib.sha256(source.read_bytes()).hexdigest()==original
    report={'version':__version__,'build':__build__,'source':str(source),'source_bytes':source.stat().st_size,'sha256':original,
        'cache_definition':'application PNG cache; OS page cache not flushed','operations':rows,'independent_whole_decode_oracle':oracle}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
