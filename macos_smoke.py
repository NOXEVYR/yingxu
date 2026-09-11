"""Isolated smoke checks runnable from the frozen macOS executable."""
from pathlib import Path
import hashlib
import http.client
import json
import os
import tempfile
import threading
import time
from urllib.parse import urlencode


def run():
    from server import Application, Server, ROOT
    from macos_app import stop_server, PREVIEW
    from yingxu.macos import rename_exclusive, recycle
    from yingxu.runtime import ffmpeg_path
    from PIL import Image
    from yingxu import __version__
    import subprocess
    checks=[]
    with tempfile.TemporaryDirectory(prefix='yingxu-macos-smoke-') as temporary:
        root=Path(temporary).resolve()
        os.environ.update(USERPROFILE=str(root/'user'),HOME=str(root/'user'),
                          YINGXU_DATA_DIR=str(root/'data'),YINGXU_PROJECTS_DIR=str(root/'projects'))
        app=Application(root/'data',root/'projects')
        service=Server(('127.0.0.1',0),app)
        thread=threading.Thread(target=service.serve_forever,daemon=True);thread.start()
        def request(method,path,data=None,raw_response=False):
            c=http.client.HTTPConnection('127.0.0.1',service.server_port,timeout=10)
            body=json.dumps(data).encode() if data is not None else None
            headers={'Content-Type':'application/json','X-YingXu-Token':app.token}
            try:
                c.request(method,path,body,headers);r=c.getresponse();raw=r.read()
                assert r.status==200 or r.status==201,(r.status,path,raw[:300])
                return raw if raw_response else json.loads(raw)
            finally:c.close()
        try:
            assert request('GET','/api/health')['version']==__version__=='0.4.4'
            assert b'<html' in request('GET','/?desktop=macos',raw_response=True)
            assert b'yingxuMac' in request('GET','/macos.js',raw_response=True)
            checks.append('Frozen bundle serves its resolved frontend root through HTTP')
            project=request('POST','/api/projects',{'name':'Mac 合成项目'})
            item=request('POST','/api/items',{'project_id':project['id'],'category':'scripts','name':'测试文稿','content':'# 中文\n正文'})
            original=Path(item['path']).read_bytes()
            changed=app.store.rename_file(item['id'],'改名成功')
            assert Path(changed['path']).read_bytes()==original
            folder=app.organize.create_folder(project['id'],'scripts','初始目录')
            app.organize.rename_folder(folder['id'],'已改名目录')
            checks.append('HTTP project/document creation, original file and folder rename')
            source=root/'source.txt';target=root/'target.txt'
            source.write_text('source');target.write_text('destination')
            try:rename_exclusive(source,target)
            except FileExistsError:pass
            else:raise AssertionError('Rename replaced destination')
            assert source.read_text()=='source' and target.read_text()=='destination'
            checks.append('Darwin exclusive rename refuses existing destination')
            target.unlink()
            result=recycle(source)
            recovered=Path(result['recycle_path'])
            assert not source.exists() and recovered.read_text()=='source'
            # Recover only the unique synthetic fixture created by this check.
            recovered.rename(source)
            checks.append('Native macOS Trash round trip preserves synthetic file bytes')
            picture=root/'fixture.png';Image.new('RGB',(320,180),'green').save(picture)
            ffmpeg=ffmpeg_path()
            import sys
            if getattr(sys,'frozen',False):
                assert ffmpeg and Path(ffmpeg).resolve().is_relative_to(ROOT.resolve()), 'Frozen app must use its bundled FFmpeg'
            if not ffmpeg:
                assert not getattr(sys,'frozen',False), 'Frozen app is missing FFmpeg'
                import imageio_ffmpeg
                ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
            video=root/'fixture.mp4'
            subprocess.run([ffmpeg,'-nostdin','-loglevel','error','-loop','1','-i',str(picture),'-t','0.2',
                '-c:v','libx264','-pix_fmt','yuv420p','-threads','1',str(video)],check=True,timeout=30,capture_output=True)
            assert video.stat().st_size>100
            checks.append('Bundled Pillow and FFmpeg create PNG and H.264 without downloads')
            manifest=json.loads((ROOT/'frontend/live-markdown.manifest.json').read_text())
            assert hashlib.sha256((ROOT/'frontend/live-markdown.js').read_bytes()).hexdigest()==manifest['sha256']
            assert (ROOT/'frontend/macos.js').is_file()
            checks.append('Offline frontend and Markdown bundle integrity')
            canvas_root=ROOT/'frontend/canvas'
            canvas_manifest=json.loads((canvas_root/'manifest.json').read_text())
            for entry in canvas_manifest['files']:
                file=canvas_root/entry['path']
                assert file.is_file() and file.stat().st_size==entry['bytes']
                assert hashlib.sha256(file.read_bytes()).hexdigest()==entry['sha256']
            assert {p.relative_to(canvas_root).as_posix() for p in canvas_root.rglob('*') if p.is_file()}=={x['path'] for x in canvas_manifest['files']}|{'manifest.json'}
            checks.append('Every offline canvas asset and font matches the 0.4.4 manifest')
            companion=request('POST','/api/items',{'project_id':project['id'],'category':'scripts','name':'附件文稿','content':'文件链接验收'})
            link=request('GET','/api/markdown-assets/file-link?'+urlencode({'note':item['id'],'item':companion['id']}))
            resolved=request('GET','/api/markdown-assets/resolve-file?'+urlencode({'note':item['id'],'path':link['relative_path']+'#yx-item='+companion['id']}))
            assert resolved['id']==companion['id']
            assert Path(changed['path']).read_bytes()==original
            checks.append('0.4.4 Markdown file links resolve without rewriting the source note')
            preview=request('POST','/api/maintenance/preview',{'include_cache':False,'include_versions':False})
            assert not preview['truncated'] and preview['reclaimable_files']==0
            cleaned=request('POST','/api/maintenance/cleanup',{'token':preview['token']})
            assert cleaned['removed_files']==0 and Path(changed['path']).read_bytes()==original
            checks.append('0.4.4 maintenance preview and empty cleanup retain original documents')
        finally:stop_server(service,app)
    print(json.dumps({'ok':True,'version':PREVIEW,'checks':checks,'real_user_data_used':False},ensure_ascii=False))


if __name__=='__main__':run()
