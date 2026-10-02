"""映序 local HTTP application. Offline by default; manual update lookup only. No telemetry."""
from __future__ import annotations
import argparse
from contextlib import nullcontext
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlsplit

from yingxu import __version__, __build__
from yingxu.runtime import image_support
from yingxu.paths import default_data_root, default_project_root, instance_id
from yingxu.store import Store,UserError,CATEGORIES,STATUSES,SAFE_EXTENSIONS
from yingxu.store import safe_name,uid,clean_path
from yingxu.jobs import Jobs,Thumbnails

ROOT=Path(__file__).resolve().parent
MAX_REJECT_DRAIN=128*1024
REJECT_DRAIN_SECONDS=.05


class Application:
    def __init__(self,data_root,project_root):
        from yingxu.settings import Settings
        from yingxu.project_storage import ProjectStorage
        data_root=Path(data_root).resolve()
        data_root.mkdir(parents=True,exist_ok=True)
        self.settings=Settings(data_root)
        configured_root=self.settings.get()['project_storage_root']
        # Never recreate an offline configured location or touch the former
        # default directory before Settings has a chance to repair the choice.
        self.store=Store(data_root,configured_root or project_root,create_project_root=not configured_root)
        from yingxu.project_migration import ProjectMigration, recover_pending
        recover_pending(self.store,self.settings)
        self.project_storage=ProjectStorage(self.store,self.settings)
        self.store.project_storage=self.project_storage
        resume_token=os.environ.pop('YINGXU_RESUME_SESSION_TOKEN','')
        self.token=resume_token if re.fullmatch(r'[A-Za-z0-9_-]{40,128}',resume_token) else secrets.token_urlsafe(32)
        self.picker_lock=threading.Lock()
        self.native_picker=None
        self.desktop_message=None
        self._close_lock=threading.Lock()
        self._closed=False
        self.demo_lock=threading.Lock()
        from yingxu.skills import SkillLibrary
        from yingxu.context import ContextExporter
        from yingxu.organize import Organize
        self.organize=Organize(self.store)
        self.skills=SkillLibrary(self.store,scan=False)
        self.context=ContextExporter(self.store,self.skills)
        from yingxu.handoffs import HandoffService
        self.handoffs=HandoffService(self.store,self.skills,self.context)
        from yingxu.mcp import ProjectMCP
        from yingxu.ai_tasks import AITaskService
        self.ai_tasks=AITaskService(self.store,self.skills)
        self.mcp=ProjectMCP(self.store,self.skills,self.ai_tasks)
        from yingxu.mcp_listener import MCPListener
        self.mcp_listener=MCPListener(self.mcp,self.store.data_root)
        self.jobs=Jobs(self.store,self.context.request)
        self.thumbnails=Thumbnails(self.store)
        from yingxu.trash import TrashDeletion
        self.trash_deletion=TrashDeletion(self.store,self.skills)
        from yingxu.external import ExternalPreviews
        from yingxu.project_library import ProjectLibrary
        self.external=ExternalPreviews(self.store.data_root)
        self.project_library=ProjectLibrary(self.store)
        from yingxu.search import GlobalSearch
        self.search=GlobalSearch(self.store,self.skills)
        from yingxu.resource_groups import ResourceGroups
        self.resource_groups=ResourceGroups(self.store)
        from yingxu.markdown_assets import MarkdownAssets
        self.markdown_assets=MarkdownAssets(self.store)
        from yingxu.maintenance import Maintenance
        self.maintenance=Maintenance(self.store)
        from yingxu.migration_jobs import MigrationJobs
        self.project_migration=ProjectMigration(self.store,self.project_storage)
        self.migration_jobs=MigrationJobs(self,self.project_migration)
        from yingxu.ai_connections import AIConnectionStore
        from yingxu.ai_call_jobs import AICallService
        self.ai_connections=AIConnectionStore(self.store)
        self.ai_calls=AICallService(self.store,self.ai_tasks,self.migration_jobs,self.ai_connections)
        from yingxu.ai_capability_selections import AICapabilitySelections
        self.ai_selections=AICapabilitySelections(self.store,self.ai_tasks,self.ai_connections)
        from yingxu.update_service import UpdateService
        self.update_service=UpdateService(self,ROOT,__version__)
        from yingxu.ai_hub_sources import HubSourceStore
        from yingxu.ai_hub_calls import AIHubCallService
        self.ai_hub_sources=HubSourceStore(self.store)
        self.ai_hub_calls=AIHubCallService(self.store,self.ai_tasks,self.ai_selections,self.ai_hub_sources,self.migration_jobs,self.update_service)
        from yingxu.ai_hub_receipts import AIHubReceipts
        self.ai_hub_receipts=AIHubReceipts(self.store,self.ai_tasks,self.ai_hub_calls)
        self._skills_startup=self.skills.start_initial_refresh(self.jobs.pool)

    def close(self):
        """Drain accepted writes before stopping the context export worker."""
        with self._close_lock:
            if self._closed:return
            self._closed=True
            try:
                try:
                    try:self.ai_hub_calls.close(timeout=5)
                    finally:self.ai_calls.close(timeout=5)
                finally:
                    try:self.mcp_listener.close()
                    finally:self.mcp.close()
            finally:
                try:
                    try:self.update_service.close()
                    finally:self.migration_jobs.close()
                finally:
                    try:
                        self.jobs.pool.shutdown(wait=True,cancel_futures=False)
                    finally:
                        try:self.thumbnails.pool.shutdown(wait=True,cancel_futures=False)
                        finally:
                            if not self.context.close():
                                raise RuntimeError('项目交接写入尚未结束，请检查本地日志。')

    def bootstrap(self):
        return {'app':'yingxu','version':__version__,'build_revision':__build__,'token':self.token,'settings':self.settings.get(),
          'project_root':str(self.store.project_root),'data_root':str(self.store.data_root),
          'categories':[{'key':k,'label':v[0]} for k,v in CATEGORIES.items()], 'statuses':STATUSES,
          'capabilities':{'project_file_sync':True,'lazy_markdown':True,'document_search':True,'maintenance':True,'thumbnails':image_support(), 'image_thumbnails':image_support(),'ffmpeg':bool(self.thumbnails.ffmpeg),'docx_edit':True,'platform':sys.platform,'native_picker':os.name=='nt' or self.native_picker is not None,'skills':True,'project_context':True,'folders':True,'trash':True,'move_files':True,'trash_delete':True,'settings':True,'external_open':True,'project_library':True,'project_storage':True,'global_search':True,'resource_groups':True,'manual_update_check':True,'automatic_updates':True,'skill_collections':True,'skill_organization':True,'incremental_handoff':True,'mcp_project_read':True,'ai_collaboration_tasks':True,'ai_tool_calls':True,'ai_hub_source_execution':True}}

    def changed(self,project_id=None):
        with self.store.connection() as db:
            rows=db.execute('SELECT id FROM projects WHERE removed=0'+(' AND id=?' if project_id else ''),
                            (project_id,) if project_id else ()).fetchall()
        for row in rows:self.context.request(row['id'])

    def trash(self,project_id='',limit=48,offset=0,q=''):
        limit=max(1,min(200,int(limit)));offset=max(0,min(10_000_000,int(offset)))
        where='b.restored=0 AND b.purged=0';args=[]
        if project_id:where+=' AND b.project_id=?';args.append(project_id)
        union='''SELECT b.id,b.id AS batch_id,b.kind,b.target_id,b.project_id,b.name,b.created,
            (SELECT count(*) FROM trash_members m WHERE m.batch_id=b.id) AS count,
            NULL AS source,NULL AS editable,NULL AS path FROM trash_batches b WHERE '''+where+'''
            UNION ALL SELECT id,id,'skill',id,NULL,name,removed_at,1,source,editable,path
            FROM yx_skills WHERE removed=1 AND purged=0'''
        q=str(q).strip()
        if len(q)>1000:raise UserError('搜索内容过长。')
        filtered=' FROM ('+union+')'
        if q:filtered+=' WHERE instr(lower(name),lower(?))>0';args.append(q)
        with self.store.connection() as db:
            total=db.execute('SELECT count(*)'+filtered,args).fetchone()[0]
            entries=[dict(row) for row in db.execute('SELECT *'+filtered+' ORDER BY created DESC,id LIMIT ? OFFSET ?',[*args,limit,offset])]
        return {'entries':entries,'total':total,'limit':limit,'offset':offset,'truncated':offset+len(entries)<total}

    def pick(self,kind):
        if sys.platform=='darwin' and self.native_picker is not None:
            if kind not in ('folder','files'):raise UserError('选择器类型不正确。')
            if not self.picker_lock.acquire(False):raise UserError('已有一个文件选择窗口打开。',409)
            try:return {'paths':list(self.native_picker(kind) or [])}
            finally:self.picker_lock.release()
        if os.name!='nt':raise UserError('当前环境不支持原生选择器，请粘贴本机绝对路径。')
        if not self.picker_lock.acquire(False):raise UserError('已有一个文件选择窗口打开，请先完成选择。',409)
        try:
            if kind=='folder':
                script="Add-Type -AssemblyName System.Windows.Forms; $d=New-Object Windows.Forms.FolderBrowserDialog; $d.Description='选择要引用的素材文件夹'; $d.ShowNewFolderButton=$false; if($d.ShowDialog() -eq 'OK'){@($d.SelectedPath)|ConvertTo-Json -Compress} else {'[]'}"
            elif kind=='files':
                script="Add-Type -AssemblyName System.Windows.Forms; $d=New-Object Windows.Forms.OpenFileDialog; $d.Title='选择素材、剧本或分镜文件'; $d.Multiselect=$true; $d.Filter='创作文件|*.zip;*.md;*.txt;*.docx;*.doc;*.pdf;*.svg;*.html;*.htm;*.png;*.jpg;*.jpeg;*.webp;*.gif;*.mp4;*.mov;*.webm;*.mkv;*.wav;*.mp3;*.blend;*.glb;*.fbx;*.obj;*.srt;*.json;*.csv|所有文件|*.*'; if($d.ShowDialog() -eq 'OK'){@($d.FileNames)|ConvertTo-Json -Compress} else {'[]'}"
            else:raise UserError('选择器类型不正确。')
            import base64
            prefix='[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding; '
            script=script.replace('$d.ShowDialog()', '$d.ShowDialog($owner)').replace('Add-Type -AssemblyName System.Windows.Forms;', 'Add-Type -AssemblyName System.Windows.Forms; $owner=New-Object Windows.Forms.Form; $owner.TopMost=$true; $owner.ShowInTaskbar=$false; $owner.Opacity=0; $owner.Show();')+'; $owner.Dispose()'
            encoded=base64.b64encode((prefix+script).encode('utf-16-le')).decode('ascii')
            ps=Path(os.environ.get('WINDIR','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
            try:
                result=subprocess.run([str(ps),'-NoProfile','-STA','-EncodedCommand',encoded],capture_output=True,timeout=300,creationflags=subprocess.CREATE_NO_WINDOW)
            except subprocess.TimeoutExpired:raise UserError('选择窗口等待超时，请重新打开。')
            if result.returncode:raise UserError('选择器未能打开，请使用粘贴路径导入。')
            try:paths=json.loads(result.stdout.decode('utf-8-sig').strip() or '[]')
            except (ValueError,UnicodeError):raise UserError('未能读取选择结果，请使用粘贴路径导入。')
            return {'paths':[paths] if isinstance(paths,str) else paths}
        finally:self.picker_lock.release()

    def open_file(self,data):
        item=self.store.get_item(data.get('id'));path=self.store.resolve_item_path(item)
        action=data.get('action','open')
        if sys.platform=='darwin':
            if action not in ('reveal','open'):raise UserError('不支持的打开方式。')
            from yingxu.macos import open_path
            open_path(path,reveal=action=='reveal')
            return {'ok':True,'focus_folder':str(path.parent) if action=='reveal' else None}
        if os.name!='nt':raise UserError('此操作需要 Windows 桌面环境。')
        if action=='reveal':
            if data.get('native_open') is True:return {'ok':True,'focus_folder':str(path.parent),'native_open':True,'reveal_file':str(path)}
            subprocess.Popen(['explorer.exe','/select,',str(path)],creationflags=subprocess.CREATE_NO_WINDOW)
        elif action=='open':os.startfile(str(path))
        else:raise UserError('不支持的打开方式。')
        return {'ok':True,'focus_folder':str(path.parent) if action=='reveal' else None}

    def open_folder(self,data):
        if not isinstance(data,dict) or set(data)-{'project_id','category','folder_id','skill_id','native_open'}:
            raise UserError('请选择已登记的项目文件夹或 SKILL。')
        skill_target='skill_id' in data
        if 'native_open' in data and not isinstance(data['native_open'],bool):raise UserError('打开方式参数无效。')
        if skill_target and (set(data)-{'native_open'}!={'skill_id'} or not isinstance(data['skill_id'],str) or not re.fullmatch('[a-f0-9]{32}',data['skill_id'])):
            raise UserError('SKILL 位置请求仅接受一个有效的 skill_id。')
        if os.name!='nt' and sys.platform!='darwin':raise UserError('此操作需要桌面环境。')
        with self.skills.lock,self.store.lock:
            if skill_target:
                path=self.skills.directory(data['skill_id'])
            else:
                project=self.store.get_project(data.get('project_id'))
                root=clean_path(project['root'])
                folder_id=data.get('folder_id')
                if folder_id not in (None,'','root'):
                    folder=self.organize.get_folder(folder_id)
                    if folder['project_id']!=project['id']:raise UserError('文件夹不属于这个项目。',403)
                    path=self.organize.folder_path(project['id'],folder['category'],folder_id)
                elif data.get('category') not in (None,'','all'):
                    path=self.organize.folder_path(project['id'],data['category'])
                else:path=root
                if not path.is_relative_to(root):raise UserError('文件夹不存在或路径已改变。',404)
            path=clean_path(path)
            if not path.is_dir():raise UserError('文件夹不存在或路径已改变。',404)
            if sys.platform=='darwin':
                from yingxu.macos import open_path
                open_path(path)
            else:
                if data.get('native_open') is True:return {'ok':True,'focus_folder':str(path),'native_open':True,'reveal_file':None}
                explorer=Path(os.environ.get('WINDIR','C:/Windows'))/'explorer.exe'
                subprocess.Popen([str(explorer),str(path)],creationflags=subprocess.CREATE_NO_WINDOW)
        return {'ok':True,'focus_folder':str(path)}

    def paste_clipboard(self,data):
        import io
        from types import SimpleNamespace
        from yingxu.clipboard_files import read_files,read_image_png
        pid=data.get('project_id');category=data.get('category','unclassified')
        self.organize.folder_path(pid,category,data.get('folder_id'))
        paths=read_files()
        if not paths:
            image=read_image_png()
            if image is None:raise UserError('剪贴板里没有图片或文件。请在浏览器中选择“复制图片”（不是复制图片地址），或在系统文件夹中复制文件。')
            handler=SimpleNamespace(rfile=io.BytesIO(image),headers={'Content-Length':str(len(image))})
            item=self.receive_upload(handler,{'project':pid,'category':category,'folder_id':data.get('folder_id'),'name':'粘贴图片.png'})
            return {'items':[item],'job_ids':[],'error':None}
        checked=[]
        for value in paths:
            path=clean_path(value)
            if not path.is_file():raise UserError('暂不支持粘贴整个文件夹，请选择其中的文件，或使用导入文件夹。')
            if path.suffix.lower() not in SAFE_EXTENSIONS and path.suffix.lower()!='.zip':raise UserError('剪贴板包含不支持的文件类型，请重新选择。')
            checked.append(path)
        items=[];job_ids=[];error_message=None
        for path in checked:
            try:
                clean_path(path)
                if path.suffix.lower()=='.zip':
                    job_ids.append(self.jobs.submit_archive(pid,category,path,data.get('folder_id'))['job_id'])
                    continue
                with path.open('rb') as source:
                    handler=SimpleNamespace(rfile=source,headers={'Content-Length':str(os.fstat(source.fileno()).st_size)})
                    items.append(self.receive_upload(handler,{'project':pid,'category':category,'folder_id':data.get('folder_id'),'name':path.name}))
            except (OSError,UserError) as error:
                if not items and not job_ids:raise
                error_message=f'已粘贴 {len(items)} 个文件，已提交 {len(job_ids)} 个 ZIP；后续文件未完成：{error}。原文件不变。'
                break
        return {'items':items,'job_ids':job_ids,'error':error_message}

    def receive_archive(self,handler,query,length):
        from yingxu.archive_import import MAX_ARCHIVE
        if length>MAX_ARCHIVE:raise UserError('ZIP 最大支持 8 GiB。',413)
        pid=query.get('project');category=query.get('category','unclassified');folder_id=query.get('folder_id')
        self.organize.folder_path(pid,category,folder_id)
        cache=self.store.data_root/'archive-uploads';cache.mkdir(exist_ok=True);clean_path(cache)
        if shutil.disk_usage(cache).free<length+512*1024**2:raise UserError('暂存 ZIP 的磁盘空间不足。',507)
        temporary=cache/(uid()+'.zip');queued=False
        try:
            with temporary.open('xb') as output:
                remaining=length
                while remaining:
                    chunk=handler.rfile.read(min(1024**2,remaining))
                    if not chunk:raise UserError('ZIP 上传中断，未解压。')
                    output.write(chunk);remaining-=len(chunk)
            result=self.jobs.submit_archive(pid,category,temporary,folder_id,safe_name(query.get('name','素材.zip')),cleanup=True)
            queued=True
            return result
        finally:
            if not queued:temporary.unlink(missing_ok=True)

    def receive_upload(self,handler,query):
        pid=query.get('project','');category=query.get('category','references')
        if category not in CATEGORIES:raise UserError('请选择有效分类。')
        project=self.store.get_project(pid)
        if handler.headers.get('Transfer-Encoding'):raise UserError('请使用工作台拖放上传，当前传输格式不支持。')
        try:length=int(handler.headers.get('Content-Length','-1'))
        except ValueError:raise UserError('无法读取文件大小。')
        if not 0<=length<=128*1024**3:raise UserError('单文件须小于128 GiB。',413)
        filename=safe_name(query.get('name',''))
        if Path(filename).suffix.lower()=='.zip':return self.receive_archive(handler,query,length)
        if Path(filename).suffix.lower() not in SAFE_EXTENSIONS:raise UserError('此文件类型暂不支持导入。')
        root=clean_path(project['root'])
        folder_id=query.get('folder_id')
        folder=self.organize.folder_path(pid,category,None if folder_id in ('','root',None) else folder_id)
        folder.mkdir(parents=True,exist_ok=True);clean_path(folder)
        if shutil.disk_usage(folder).free<length+512*1024*1024:raise UserError('项目磁盘剩余空间不足。',507)
        target=folder/filename
        if target.exists():target=target.with_name(target.stem+'_'+uid()[:6]+target.suffix)
        temporary=folder/('.yingxu-upload-'+uid()+'.tmp')
        try:
            with self.store.lock:
                self.organize.folder_path(pid,category,None if folder_id in ('','root',None) else folder_id)
                output=temporary.open('xb')
            with output as out:
                remaining=length
                while remaining:
                    chunk=handler.rfile.read(min(1024*1024,remaining))
                    if not chunk:raise UserError('上传中断，未完成的文件不会进入项目。')
                    out.write(chunk);remaining-=len(chunk)
                out.flush();os.fsync(out.fileno())
            with self.store.lock:
                # The destination may have been recycled during a long stream.
                # Revalidate before publication; failed uploads leave only removable temp data.
                current_folder=self.organize.folder_path(pid,category,None if folder_id in ('','root',None) else folder_id)
                if current_folder!=folder:raise UserError('上传期间目标文件夹已改变，请重新导入。',409)
                # Exclusive destination publication, so racing uploads cannot replace a file.
                if os.name=='nt':os.rename(temporary,target)
                else:
                    os.link(temporary,target);temporary.unlink()
                source=next(s for s in self.store.sources(pid) if s['path']==str(root))
                self.store.index_files(source,[target])
                self.organize.assign_imported(source,[target],category,None if folder_id in ('','root',None) else folder_id)
                with self.store.connection() as db:iid=db.execute('SELECT id FROM items WHERE project_id=? AND path=?',(pid,str(target))).fetchone()[0]
            self.context.request(pid)
            return self.store.get_item(iid,True)
        finally:
            if temporary.exists():temporary.unlink(missing_ok=True)


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=False
    def __init__(self,address,app,*,mcp_only=False):
        self.app=app
        self.mcp_only=mcp_only
        self.slots=threading.BoundedSemaphore(4 if mcp_only else 24)
        super().__init__(address,Handler)
        if not mcp_only and hasattr(self.app,'mcp_listener'):
            self.app.mcp_listener.attach(self.server_port,
                lambda port:Server(('127.0.0.1',port),app,mcp_only=True))
    def process_request(self,request,client_address):
        if not self.slots.acquire(timeout=1):
            self.shutdown_request(request);return
        try:super().process_request(request,client_address)
        except Exception:self.slots.release();raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.slots.release()


def parse_range(header,length):
    if not header:return 0,length-1,False
    match=re.fullmatch(r'bytes=(\d*)-(\d*)',header.strip())
    if not match or (not match[1] and not match[2]):raise UserError('不支持的媒体范围。',416)
    if not match[1]:
        size=int(match[2]);start=max(0,length-size);end=length-1
        if size<=0:raise UserError('媒体范围无效。',416)
    else:
        start=int(match[1]);end=min(int(match[2]) if match[2] else length-1,length-1)
    if start>=length or start>end:raise UserError('媒体范围超出文件。',416)
    return start,end,True


class Handler(BaseHTTPRequestHandler):
    server_version=f'YingXu/{__version__}'
    protocol_version='HTTP/1.1'

    def setup(self):
        super().setup();self.connection.settimeout(5 if self.server.mcp_only else 30)

    @property
    def app(self):return self.server.app

    def log_message(self,fmt,*args):
        # No query strings, local file paths, tokens, or document text in access logs.
        if args and isinstance(args[0],str) and 'api/health' in args[0]:return
        print(f'[{self.log_date_time_string()}] {self.command} {urlsplit(self.path).path} {args[1] if len(args)>1 else ""}',flush=True)

    def headers_common(self,content_type,csp=None):
        self.send_header('Content-Type',content_type)
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Frame-Options','SAMEORIGIN')
        self.send_header('Content-Security-Policy',csp or "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self'; object-src 'self'; frame-src 'self' blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")

    def check_origin(self,write=False):
        expected=f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host')!=expected:raise UserError('仅允许本机工作台访问。',403)
        origin=self.headers.get('Origin')
        if origin and origin!='http://'+expected:raise UserError('请求来源不受信任。',403)
        if self.headers.get('Sec-Fetch-Site')=='cross-site':raise UserError('禁止跨站访问本地文件。',403)
        if write and not secrets.compare_digest(self.headers.get('X-YingXu-Token',''),self.app.token):raise UserError('会话已更新，请刷新工作台后重试。',403)

    def body(self):
        if self.headers.get('Transfer-Encoding'):raise UserError('不支持此请求传输格式。')
        try:size=int(self.headers.get('Content-Length','0'))
        except ValueError:raise UserError('请求长度不正确。')
        if not 0<=size<=4*1024*1024:raise UserError('请求过大。',413)
        if size and not self.headers.get('Content-Type','').startswith('application/json'):raise UserError('仅接受 JSON 请求。',415)
        try:
            data=json.loads(self.rfile.read(size).decode('utf-8')) if size else {}
            if not isinstance(data,dict):raise ValueError()
            return data
        except (ValueError,UnicodeError):raise UserError('请求格式不正确。')

    def json(self,data,status=200,extra=None):
        raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        self.send_response(status);self.headers_common('application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)))
        for k,v in (extra or {}).items():self.send_header(k,v)
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(raw)

    def reject_json(self,data,status,extra=None):
        """Deliver a denial before bounded disposal of unread socket bytes.

        Closing a Windows socket with a POST body still arriving can reset TCP
        and discard the error response. Never parse this untrusted data or run
        a business operation; half-close writes first, then cap bytes and the
        total time independently of Content-Length and Transfer-Encoding.
        """
        self.close_connection=True
        self.json(data,status,{**(extra or {}),'Connection':'close'})
        self.wfile.flush()
        previous_timeout=self.connection.gettimeout()
        try:
            self.connection.shutdown(socket.SHUT_WR)
            deadline=time.monotonic()+REJECT_DRAIN_SECONDS
            remaining=MAX_REJECT_DRAIN
            while remaining>0:
                duration=deadline-time.monotonic()
                if duration<=0:break
                self.connection.settimeout(duration)
                block=self.connection.recv(min(16*1024,remaining))
                if not block:break
                remaining-=len(block)
        except (OSError,ValueError):pass
        finally:
            try:self.connection.settimeout(previous_timeout)
            except (OSError,ValueError):pass

    def file(self,path,media=False,immutable=False,opened=None):
        path=Path(path)
        if media and path.suffix.lower()=='.svg':
            from yingxu.svg_content import read_svg_bytes
            with (nullcontext(opened) if opened is not None else path.open('rb')) as handle:
                safe=read_svg_bytes(handle)
            self.send_response(200)
            self.headers_common('image/svg+xml; charset=utf-8',"default-src 'none'; style-src 'unsafe-inline'; sandbox; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.send_header('Content-Length',str(len(safe)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            if self.command!='HEAD':self.wfile.write(safe)
            return
        length=os.fstat(opened.fileno()).st_size if opened is not None else path.stat().st_size
        try:start,end,partial=parse_range(self.headers.get('Range') if media else None,length)
        except UserError as e:
            self.json({'error':str(e)},416,{'Content-Range':f'bytes */{length}'});return
        mime=mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        if media and path.suffix.lower() in ('.html','.htm'):mime='text/plain; charset=utf-8'
        if path.suffix=='.js':mime='application/javascript'
        if path.suffix in ('.md','.txt','.srt','.vtt','.json','.yaml','.yml','.csv'):mime='text/plain; charset=utf-8'
        self.send_response(206 if partial else 200);self.headers_common(mime)
        self.send_header('Content-Length',str(max(0,end-start+1)))
        self.send_header('Cache-Control','private, max-age=86400' if immutable else 'no-cache')
        if media:
            self.send_header('Accept-Ranges','bytes')
            if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{length}')
        if mime=='application/octet-stream' or (media and path.suffix.lower() in ('.html','.htm')):self.send_header('Content-Disposition','attachment')
        self.end_headers()
        if self.command=='HEAD':return
        with (nullcontext(opened) if opened is not None else path.open('rb')) as f:
            f.seek(start);remaining=end-start+1
            while remaining>0:
                chunk=f.read(min(128*1024,remaining))
                if not chunk:break
                self.wfile.write(chunk);remaining-=len(chunk)

    def handle_request(self):
        path=urlsplit(self.path).path
        listener=getattr(self.app,'mcp_listener',None)
        if listener and listener.matches(self.server.server_port,path):return self.handle_mcp_request()
        if self.server.mcp_only:
            return self.reject_json({'error':'MCP 接口不存在。'},404)
        if self.command in ('GET','HEAD'):return self.handle_application_request()
        try:
            self.check_origin(self.command not in ('GET','HEAD'))
            path=urlsplit(self.path).path
            with self.app.update_service.mutation(self.command,path):
                if path.startswith('/api/updates/'):
                    return self.handle_application_request()
                with self.app.migration_jobs.mutation(self.command,path):
                    return self.handle_application_request()
        except UserError as error:
            self.reject_json({'error':str(error)},error.status)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,socket.timeout):pass

    def mcp_endpoint(self):
        return self.app.mcp_listener.status()['endpoint']

    def handle_mcp_request(self):
        from yingxu.mcp import MAX_REQUEST, ProjectMCP
        try:
            # This is a separate read-only authorization surface. Never accept
            # the workbench session token or bypass its write guards elsewhere.
            self.app.mcp_listener.check_origin(self.server.server_port,self.headers)
            if urlsplit(self.path).query:raise UserError('MCP 不接受 URL 参数。',400)
            with self.app.mcp.request_slot():
                generation=self.app.mcp_listener.request_generation(self.server.server_port,
                    urlsplit(self.path).path,self.headers.get('Authorization',''))
                if self.command!='POST':
                    return self.reject_json(ProjectMCP.error(None,-32600,'Use POST for MCP'),405,{'Allow':'POST'})
                if self.headers.get('Transfer-Encoding'):raise UserError('MCP 不支持此传输格式。',400)
                try:size=int(self.headers.get('Content-Length','0'))
                except ValueError:raise UserError('MCP 请求长度无效。',400)
                if not 0<size<=MAX_REQUEST:raise UserError('MCP 请求大小无效。',413)
                if self.headers.get('Content-Type','').split(';',1)[0].strip().lower()!='application/json':
                    raise UserError('MCP 仅接受 JSON。',415)
                try:message=json.loads(self.rfile.read(size).decode('utf-8'))
                except (ValueError,UnicodeError,RecursionError):
                    return self.reject_json(ProjectMCP.error(None,-32700,'Invalid JSON'),400)
                result,status=self.app.mcp_listener.dispatch(self.server.server_port,urlsplit(self.path).path,
                    lambda:self.app.mcp.handle(message,self.headers,reserved=True),generation=generation)
                if result is None:
                    self.send_response(status);self.headers_common('application/json')
                    self.send_header('Cache-Control','no-store');self.send_header('Content-Length','0');self.end_headers()
                    return
                return self.json(result,status)
        except UserError as error:
            return self.reject_json(ProjectMCP.error(None,-32000,str(error)),error.status)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,socket.timeout):pass
        except Exception:
            # Documents, paths and credential fields never enter an MCP error.
            return self.reject_json(ProjectMCP.error(None,-32603,'MCP read failed'),500)

    def handle_ai_request(self,path,query,data=None):
        """Local workbench task ledger; MCP credentials never authorize writes."""
        service=self.app.ai_tasks
        if '/calls' in path or '/capability-selections' in path or '/hub-calls' in path:
            return self.handle_tool_call_request(path,query,data)
        if path=='/api/ai-tasks':
            if self.command in ('GET','HEAD'):
                if set(query)-{'project_id','limit','offset'}:raise UserError('协作列表参数无效。')
                try:limit,offset=int(query.get('limit',48)),int(query.get('offset',0))
                except (ValueError,TypeError):raise UserError('协作分页参数无效。') from None
                return self.json(service.list_tasks(query.get('project_id',''),limit=limit,offset=offset))
            if self.command=='POST' and not query:return self.json(service.create_task(data),201)
        match=re.fullmatch(r'/api/ai-tasks/([a-f0-9]{32})(?:/(.*))?',path)
        if not match or query:raise UserError('协作接口不存在或参数无效。',404)
        task,tail=match[1],match[2] or ''
        if not tail:
            if self.command in ('GET','HEAD'):return self.json(service.get_task(task))
            if self.command=='PATCH':return self.json(service.update_task(task,data))
        if tail=='runs' and self.command=='POST':return self.json(service.freeze_run(task,data),201)
        if tail=='complete' and self.command=='POST':return self.json(service.complete_task(task,data))
        run=re.fullmatch(r'runs/([a-f0-9]{32})(?:/(handoff|candidates|receive))?',tail)
        if run:
            if self.command in ('GET','HEAD'):
                if not run[2]:return self.json(service.get_run(task,run[1]))
                if run[2]=='candidates':return self.json(service.receipts.preview(task,run[1]))
            if self.command=='POST':
                if run[2]=='handoff':return self.json(service.create_handoff(task,run[1],data),201)
                if run[2]=='receive':return self.json(service.receipts.receive(task,run[1],data),201)
        review=re.fullmatch(r'artifacts/([a-f0-9]{32})/review',tail)
        if review and self.command=='PATCH':return self.json(service.review_artifact(task,review[1],data))
        ack=re.fullmatch(r'handoffs/([a-f0-9]{32})/ack',tail)
        if ack and self.command=='POST':
            if data:raise UserError('确认交接不接受额外字段。')
            return self.json(service.acknowledge_handoff(task,ack[1]))
        receipt=re.fullmatch(r'receipts/([a-f0-9]{32})',tail)
        if receipt and self.command in ('GET','HEAD'):return self.json(service.receipts.get(task,receipt[1]))
        raise UserError('协作接口不存在。',404)

    def handle_tool_call_request(self,path,query,data=None):
        hub_receipt=re.fullmatch(r'/api/ai-tasks/([a-f0-9]{32})/runs/([a-f0-9]{32})/hub-calls/([a-f0-9]{32})/receipts(?:/([a-f0-9]{32})(?:/(resume))?)?',path)
        if hub_receipt:
            task,run,binding,selected,action=hub_receipt.groups()
            if query:raise UserError('曜核成果关联不接受额外参数。')
            receipts=self.app.ai_hub_receipts
            if self.command in ('GET','HEAD') and not action:
                return self.json(receipts.get(task,run,binding,selected) if selected else receipts.list(task,run,binding))
            if self.command=='POST' and not selected:return self.json(receipts.receive(task,run,binding,data),201)
            if self.command=='POST' and action=='resume':
                if not isinstance(data,dict) or data:raise UserError('恢复原关联只接受空对象。')
                return self.json(receipts.resume(task,run,binding,selected))
            raise UserError('曜核成果关联接口不存在。',404)
        hub_match=re.fullmatch(r'/api/ai-tasks/([a-f0-9]{32})/runs/([a-f0-9]{32})/hub-calls(?:/([a-f0-9]{32})(?:/(accept|query|cancel|restore))?)?',path)
        if hub_match:
            task,run,binding,action=hub_match.groups()
            if self.command in ('GET','HEAD') and not binding:
                if set(query)-{'limit','offset'}:raise UserError('曜核执行分页无效。')
                try:limit,offset=int(query.get('limit',48)),int(query.get('offset',0))
                except ValueError:raise UserError('曜核执行分页无效。') from None
                return self.json(self.app.ai_hub_calls.list(task,run,limit,offset))
            if query:raise UserError('曜核执行接口不接受额外参数。')
            if self.command in ('GET','HEAD') and binding and not action:return self.json(self.app.ai_hub_calls.get(task,run,binding))
            if self.command=='POST' and not binding:return self.json(self.app.ai_hub_calls.prepare(task,run,data),201)
            if self.command=='POST' and action:
                if not isinstance(data,dict) or data:raise UserError('曜核动作只接受空对象。')
                if action=='restore':return self.json(self.app.ai_hub_calls.restore_prepare(task,run,binding),201)
                return self.json(self.app.ai_hub_calls.operate(task,run,binding,action))
            raise UserError('曜核执行接口不存在。',404)
        if path=='/api/ai-hub-sources':
            if query:raise UserError('来源配置不接受额外参数。')
            if self.command in ('GET','HEAD'):return self.json(self.app.ai_hub_sources.list())
            if self.command=='POST':
                with self.app.ai_hub_calls.local_operation():return self.json(self.app.ai_hub_sources.put(data),201)
        hub_source=re.fullmatch(r'/api/ai-hub-sources/([a-f0-9]{32})(?:/(check))?',path)
        if hub_source:
            if query:raise UserError('来源配置不接受额外参数。')
            if self.command in ('GET','HEAD') and not hub_source[2]:return self.json(self.app.ai_hub_sources.get(hub_source[1]))
            if self.command=='POST' and hub_source[2]:
                if not isinstance(data,dict) or data:raise UserError('来源检查只接受空对象。')
                with self.app.ai_hub_calls.local_operation():return self.json(self.app.ai_hub_sources.check(hub_source[1]))
        """Explicit workbench actions only; no MCP write or arbitrary endpoint proxy."""
        reading=self.command in ('GET','HEAD')
        receipt=re.fullmatch(r'/api/ai-tasks/([a-f0-9]{32})/runs/([a-f0-9]{32})/calls/([a-f0-9]{32})/receipts(?:/([a-f0-9]{32})(?:/(resume))?)?',path)
        if receipt:
            if query:raise UserError('调用成果关联不接受额外参数。')
            task,run,attempt,selected,action=receipt.groups()
            service=self.app.ai_calls.receipts
            if reading and not selected:return self.json(service.list(task,run,attempt))
            if reading and selected and not action:return self.json(service.get(task,run,attempt,selected))
            if self.command=='POST' and not selected:return self.json(service.receive(task,run,attempt,data),201)
            if self.command=='POST' and action=='resume':
                if not isinstance(data,dict) or data:raise UserError('恢复关联只接受空对象。')
                return self.json(service.resume(task,run,attempt,selected),201)
            raise UserError('调用成果关联接口不存在。',404)
        selection=re.fullmatch(r'/api/ai-tasks/([a-f0-9]{32})/runs/([a-f0-9]{32})/capability-selections(?:/([a-f0-9]{32})(?:/(verify))?)?',path)
        if selection:
            task,run,selected,action=selection.groups()
            if reading and not selected:
                if set(query)-{'limit','offset'}:raise UserError('选型分页参数无效。')
                try:limit,offset=int(query.get('limit',24)),int(query.get('offset',0))
                except ValueError:raise UserError('选型分页参数无效。') from None
                return self.json(self.app.ai_selections.list(task,run,limit,offset))
            if query:raise UserError('选型接口不接受额外参数。')
            if reading and selected and not action:return self.json(self.app.ai_selections.get(task,run,selected))
            if self.command=='POST' and not selected:return self.json(self.app.ai_selections.create(task,run,data),201)
            if self.command=='POST' and action:
                if not isinstance(data,dict) or data:raise UserError('核对选型仅接受空对象。')
                return self.json(self.app.ai_selections.verify(task,run,selected))
            raise UserError('选型接口不存在。',404)
        call_list=re.fullmatch(r'/api/ai-tasks/([a-f0-9]{32})/runs/([a-f0-9]{32})/calls',path)
        if reading and call_list:
            if set(query)-{'limit','offset'}:raise UserError('请求记录分页参数无效。')
            try:limit,offset=int(query.get('limit',48)),int(query.get('offset',0))
            except ValueError:raise UserError('请求记录分页参数无效。') from None
            return self.json(self.app.ai_calls.list(*call_list.groups(),limit=limit,offset=offset))
        if query:raise UserError('工具调用接口不接受额外参数。',400)
        if path=='/api/ai-connections':
            if reading:return self.json(self.app.ai_connections.list())
            if self.command=='POST':return self.json(self.app.ai_connections.put(data),201)
        connection=re.fullmatch(r'/api/ai-connections/([a-f0-9]{32})(?:/(check))?',path)
        if connection:
            if reading and not connection[2]:return self.json(self.app.ai_connections.get_public(connection[1]))
            if self.command=='POST' and connection[2]=='check':
                if not isinstance(data,dict) or data:raise UserError('检查连接只接受空对象。')
                return self.json(self.app.ai_connections.check(connection[1]))
        if path=='/api/ai-calls/status' and reading:
            direct=self.app.ai_calls.status();hub=self.app.ai_hub_calls.status()
            direct.update(local_busy=bool(direct['local_busy'] or hub['local_busy']),busy=bool(direct['busy'] or hub['busy']),
                local_queued=direct['local_queued']+hub['local_queued'],remote_pending=direct['remote_pending']+hub['remote_pending'],hub=hub)
            return self.json(direct)
        match=re.fullmatch(r'/api/ai-tasks/([a-f0-9]{32})/runs/([a-f0-9]{32})/calls(?:/([a-f0-9]{32})(?:/(query|cancel))?)?',path)
        if match:
            task,run,attempt,action=match.groups()
            if not attempt:
                if reading:return self.json(self.app.ai_calls.list(task,run))
                if self.command=='POST':return self.json(self.app.ai_calls.create(task,run,data),202)
            elif reading and not action:return self.json(self.app.ai_calls.get(task,run,attempt))
            elif self.command=='POST' and action:
                if not isinstance(data,dict) or data:raise UserError('核对或取消请求只接受空对象。')
                operation=self.app.ai_calls.query if action=='query' else self.app.ai_calls.cancel
                return self.json(operation(task,run,attempt),202)
        raise UserError('工具调用接口不存在。',404)

    def handle_application_request(self):
        try:
            self.check_origin(self.command not in ('GET','HEAD'))
            parsed=urlsplit(self.path);path=parsed.path
            query={k:v[-1] for k,v in parse_qs(parsed.query).items()}
            if self.command in ('GET','HEAD'):
                if path.startswith(('/api/ai-connections','/api/ai-calls','/api/ai-hub-sources')):
                    self.check_origin(True)
                    return self.handle_tool_call_request(path,query)
                if path.startswith('/api/ai-tasks'):
                    self.check_origin(True)
                    return self.handle_ai_request(path,query)
                if path=='/api/mcp/status':
                    self.check_origin(True)
                    if query:raise UserError('MCP 状态不接受额外参数。')
                    return self.json(self.app.mcp_listener.status())
                if path=='/api/updates/automatic/status':
                    self.check_origin(True)
                    if query:raise UserError('自动更新状态不接受额外参数。')
                    return self.json(self.app.update_service.automatic_status())
                if path=='/api/handoffs':
                    if set(query)!={'project_id','client_id','conversation_id'}:raise UserError('交接查询参数无效。')
                    return self.json(self.app.handoffs.status(**query))
                handoff=re.fullmatch(r'/api/handoffs/([a-f0-9]{32})',path)
                if handoff:
                    if query:raise UserError('交接记录不接受额外参数。')
                    return self.json(self.app.handoffs.get(handoff[1]))
                if path=='/api/skill-organization':
                    if query:raise UserError('技能整理列表不接受额外参数。')
                    return self.json(self.app.skills.organization.list())
                if path=='/api/skill-collections':
                    if set(query)-{'project'}:raise UserError('收藏查询参数无效。')
                    return self.json(self.app.skills.collections.list(query.get('project','')))
                if path.startswith('/api/skill-collections/'):
                    if set(query)-{'version'}:raise UserError('收藏读取参数无效。')
                    return self.json(self.app.skills.collections.get(path.rsplit('/',1)[-1],query.get('version')))
                if path=='/api/updates/status':
                    self.check_origin(True)
                    if query:raise UserError('更新状态不接受额外参数。')
                    return self.json(self.app.update_service.status())
                if path=='/api/updates/cache':
                    self.check_origin(True)
                    if query:raise UserError('缓存整理不接受额外参数。')
                    return self.json(self.app.update_service.update_cache())
                if path=='/api/updates/install/status':
                    self.check_origin(True)
                    if set(query)-{'ticket'}:raise UserError('更新安装参数无效。')
                    return self.json(self.app.update_service.install_status(query.get('ticket')))
                if path=='/api/health':return self.json({'app':'yingxu','ok':True,'version':__version__,'build_revision':__build__,'program_id':instance_id(ROOT),'instance_id':instance_id(self.app.store.data_root)})
                if path=='/api/bootstrap':return self.json(self.app.bootstrap())
                if path=='/api/settings':return self.json(self.app.settings.get())
                if path=='/api/project-storage':return self.json(self.app.project_storage.snapshot())
                if path=='/api/project-storage/migration/status':return self.json(self.app.migration_jobs.status())
                migration_job=re.fullmatch(r'/api/project-storage/migration/jobs/([a-f0-9]{32})',path)
                if migration_job:return self.json(self.app.migration_jobs.get(migration_job[1]))
                if path=='/api/project-library':return self.json(self.app.project_library.snapshot())
                if path=='/api/markdown-assets/file-link':
                    if set(query)-{'note','item'}:raise UserError('文件链接仅接受笔记与素材 ID。')
                    return self.json(self.app.markdown_assets.file_link(query.get('note',''),query.get('item','')))
                if path=='/api/markdown-assets/resolve-file':
                    if set(query)-{'note','path'}:raise UserError('文件链接仅接受笔记 ID 与相对路径。')
                    return self.json(self.app.markdown_assets.resolve_file(query.get('note',''),query.get('path','')))
                if path=='/api/markdown-assets/link':
                    if set(query)-{'note','image'}:raise UserError('图片引用仅接受笔记与素材 ID。')
                    return self.json(self.app.markdown_assets.link(query.get('note',''),query.get('image','')))
                if path=='/api/markdown-assets/image':
                    if set(query)-{'note','path','wiki'} or query.get('wiki','0') not in ('0','1'):raise UserError('笔记图片参数无效。')
                    with self.app.markdown_assets.open_image(query.get('note',''),query.get('path',''),wiki=query.get('wiki')=='1') as opened:
                        return self.file(opened.name,media=True,opened=opened)
                if path=='/api/resource-groups':
                    if set(query)-{'project'}:raise UserError('素材组列表仅接受项目参数。')
                    return self.json(self.app.resource_groups.list(query.get('project','')))
                group=re.fullmatch(r'/api/resource-groups/([a-f0-9]{32})',path)
                if group:
                    if query:raise UserError('素材组详情仅接受素材组 ID。')
                    return self.json(self.app.resource_groups.get(group[1]))
                if path=='/api/projects':return self.json({'projects':self.app.store.list_projects()})
                if path=='/api/search':
                    if set(query)-{'q','limit','offset'}:raise UserError('全局搜索仅接受关键词与分页参数。')
                    return self.json(self.app.search.search(query.get('q',''),query.get('limit',30),query.get('offset',0)))
                if path=='/api/items':return self.json(self.app.store.list_items(query.get('project',''),**{k:query[k] for k in ('category','q','status','kind','limit','offset','sort','folder') if k in query}))
                if path=='/api/folders':return self.json(self.app.organize.folders(query.get('project',''),query.get('category','')))
                if path=='/api/trash':return self.json(self.app.trash(query.get('project',''),query.get('limit',48),query.get('offset',0),query.get('q','')))
                if path=='/api/skill-sources':
                    if query:raise UserError('扫描位置列表不接受额外参数。')
                    return self.json(self.app.skills.source_list())
                if path=='/api/skills':
                    if set(query)-{'q','project','source','source_id'}:raise UserError('技能列表参数无效。')
                    return self.json(self.app.skills.list(query.get('q',''),query.get('project',''),query.get('source',''),query.get('source_id','')))
                if path=='/api/context':return self.json(self.app.context.get(query.get('project','')))
                external=re.fullmatch(r'/api/(external|external-media)/([a-f0-9]{32})',path)
                if external:
                    if query:raise UserError('外部预览仅接受已登记的文件 ID。')
                    if external[1]=='external':return self.json(self.app.external.detail(external[2]))
                    with self.app.external.open_media(external[2]) as opened:
                        return self.file(opened.name,media=True,opened=opened)
                native=re.fullmatch(r'/api/native-file/([a-f0-9]{32})',path)
                if native:return self.json({'path':str(self.app.store.resolve_item_path(self.app.store.get_item(native[1])))})
                match=re.fullmatch(r'/api/(items|content|media|thumbnail|jobs|skills)/([a-f0-9]{32})',path)
                if match:
                    resource,iid=match.groups()
                    if resource=='items':return self.json(self.app.store.get_item(iid,True))
                    if resource=='content':return self.json(self.app.store.read_content(iid))
                    if resource=='jobs':return self.json(self.app.jobs.get(iid))
                    if resource=='skills':return self.json(self.app.skills.get(iid))
                    if resource=='media':return self.file(self.app.store.resolve_item_path(self.app.store.get_item(iid)),media=True)
                    if resource=='thumbnail':
                        target=self.app.thumbnails.request(iid)
                        return self.file(target,immutable=False) if target else self.json({'pending':True},202,{'Retry-After':'1'})
                if path.startswith('/api/'):raise UserError('接口不存在。',404)
                relative=path.lstrip('/') or 'index.html'
                frontend_root=(ROOT/'frontend').resolve()
                static=(frontend_root/relative).resolve()
                if not static.is_relative_to(frontend_root) or not static.is_file() or static.suffix not in ('.html','.js','.css','.svg','.ico','.png','.woff2'):
                    raise UserError('页面文件不存在。',404)
                return self.file(static)
            if self.command=='POST' and path=='/api/upload':return self.json(self.app.receive_upload(self,query),201)
            data=self.body()
            if path.startswith(('/api/ai-connections','/api/ai-calls','/api/ai-hub-sources')):
                return self.handle_tool_call_request(path,query,data)
            if path.startswith('/api/ai-tasks'):
                return self.handle_ai_request(path,query,data)
            if self.command=='POST' and path=='/api/mcp/configure':
                if query:raise UserError('MCP 设置不接受额外参数。')
                return self.json(self.app.mcp_listener.configure(data))
            if self.command=='POST' and path=='/api/mcp/connection':
                if data or query:raise UserError('MCP 连接参数无效。')
                return self.json(self.app.mcp_listener.connection())
            if self.command=='POST' and path.startswith('/api/updates/'):
                service=self.app.update_service
                if path=='/api/updates/automatic/start':
                    if data or query:raise UserError('自动更新参数无效。')
                    service.start_automatic_updates()
                    return self.json(service.automatic_status())
                if path=='/api/updates/automatic/check':
                    if data or query:raise UserError('自动更新参数无效。')
                    return self.json(service.automatic_check(force=True))
                if path=='/api/updates/plan':
                    if data or query:raise UserError('检查差异不接受额外参数。')
                    return self.json(service.plan())
                if path=='/api/updates/download':
                    if set(data)!={'plan_id'} or query:raise UserError('下载更新参数无效。')
                    return self.json(service.download(data['plan_id']))
                if path=='/api/updates/cache/clean':
                    if set(data)!={'preview_id'} or query:raise UserError('缓存整理参数无效。')
                    return self.json(service.update_cache(data['preview_id']))
                if path=='/api/updates/install/prepare':
                    if set(data)!={'plan_id','native_pid'} or query:raise UserError('准备安装参数无效。')
                    return self.json(service.prepare(data['plan_id'],data['native_pid']))
                if path=='/api/updates/install/commit':
                    if set(data)!={'ticket'} or query:raise UserError('确认安装参数无效。')
                    result=service.commit(data['ticket'])
                    try:
                        self.json(result)
                        self.wfile.flush()
                    finally:
                        # The helper waits for this process to drain accepted
                        # work and exit, even if the HTTP response is lost.
                        threading.Thread(target=self.server.shutdown,daemon=True).start()
                    return
                if path=='/api/updates/install/cancel':
                    if set(data)!={'ticket'} or query:raise UserError('取消安装参数无效。')
                    return self.json(service.cancel(data['ticket']))
            if path=='/api/skill-folders' and self.command=='POST':
                if query or 'name' not in data or set(data)-{'name','parent_id'}:raise UserError('技能文件夹参数无效。')
                return self.json(self.app.skills.organization.create_folder(**data),201)
            skill_folder=re.fullmatch(r'/api/skill-folders/(fld_[a-f0-9]{32})',path)
            if skill_folder:
                if query:raise UserError('技能文件夹操作不接受额外参数。')
                if self.command=='PATCH' and data and not set(data)-{'name','parent_id'}:
                    return self.json(self.app.skills.organization.update_folder(skill_folder[1],**data))
                if self.command=='DELETE' and not data:
                    return self.json(self.app.skills.organization.delete_folder(skill_folder[1]))
                raise UserError('技能文件夹操作或参数无效。')
            if path=='/api/skill-metadata' and self.command=='POST':
                if query or 'skill_ids' not in data or set(data)-{'skill_ids','folder_id','tags','notes','tags_mode'}:raise UserError('技能整理参数无效。')
                return self.json(self.app.skills.organization.update_metadata(**data))
            if self.command=='POST' and path.startswith('/api/skill-collections/'):
                if query:raise UserError('收藏操作参数无效。')
                library=self.app.skills.collections
                if path=='/api/skill-collections/preview' and set(data)=={'skill_id'}:
                    return self.json(library.preview(data['skill_id']))
                if path=='/api/skill-collections/collect' and set(data)=={'token'}:
                    return self.json(library.collect(data['token']),201)
                if path=='/api/skill-collections/classify' and set(data)=={'id','category','tags'}:
                    return self.json(library.classify(data['id'],data['category'],data['tags']))
                if path=='/api/skill-collections/bind' and {'id','project_id','bound'}<=set(data) and not set(data)-{'id','project_id','bound','version'}:
                    result=library.bind(data['project_id'],data['id'],data['bound'],data.get('version'))
                    self.app.context.request(data['project_id'])
                    return self.json(result)
                if path=='/api/skill-collections/export' and set(data)=={'project_id'}:
                    return self.json(library.export(data['project_id']))
                raise UserError('收藏操作或参数无效。')
            if self.command=='POST' and path=='/api/handoffs':
                if query or not {'project_id','client_id','conversation_id','task','asset_ids'}<=set(data) or set(data)-{'project_id','client_id','conversation_id','task','asset_ids','force_full'}:raise UserError('交接参数无效。')
                return self.json(self.app.handoffs.create(**data),201)
            if self.command=='POST' and path=='/api/handoffs/acknowledge':
                if query or set(data)!={'snapshot_id'}:raise UserError('交接确认参数无效。')
                return self.json(self.app.handoffs.acknowledge(data['snapshot_id']))
            if self.command == 'POST' and path == '/api/updates/check':
                if data or query:raise UserError('检查更新不接受额外参数。')
                from yingxu.updates import check_update
                return self.json(check_update())
            if self.command == 'POST' and path == '/api/updates/open':
                if set(data) != {'tag'} or query:raise UserError('发布页参数无效。')
                from yingxu.updates import open_release
                return self.json(open_release(data['tag']))
            group=re.fullmatch(r'/api/resource-groups/([a-f0-9]{32})(/members|/transfer)?',path)
            if group:
                if group[2]=='/transfer':
                    if self.command=='POST':return self.json(self.app.resource_groups.transfer(group[1],data))
                    raise UserError('接口或请求方式不存在。',404)
                if group[2]:
                    if self.command=='POST':return self.json(self.app.resource_groups.add(group[1],data))
                    if self.command=='DELETE':return self.json(self.app.resource_groups.remove(group[1],data))
                else:
                    if self.command=='PATCH':return self.json(self.app.resource_groups.rename(group[1],data))
                    if self.command=='DELETE':return self.json(self.app.resource_groups.dissolve(group[1],data))
                raise UserError('接口或请求方式不存在。',404)
            if self.command=='POST' and path=='/api/maintenance/preview':return self.json(self.app.maintenance.preview(data))
            if self.command=='POST' and path=='/api/maintenance/cleanup':return self.json(self.app.maintenance.execute(data))
            if self.command=='PATCH' and path=='/api/settings':
                if 'project_storage_root' in data:raise UserError('请通过项目存放位置单独保存目录。')
                return self.json(self.app.settings.update(data))
            if self.command=='POST' and path=='/api/project-storage/migration/preview':
                return self.json(self.app.migration_jobs.preview(data))
            if self.command=='POST' and path=='/api/project-storage/migration':
                return self.json(self.app.migration_jobs.submit(data),202)
            if self.command=='POST' and path=='/api/project-storage':return self.json(self.app.project_storage.configure(data))
            library_contents=re.fullmatch(r'/api/project-folders/([a-f0-9]{32})/delete-contents(/preview)?',path)
            if library_contents and self.command=='POST':
                if query:raise UserError('删除分类不接受查询参数。')
                if library_contents[2]:
                    if data:raise UserError('删除预览不接受额外参数。')
                    return self.json(self.app.project_library.preview_delete_contents(library_contents[1]))
                result=self.app.project_library.delete_contents(library_contents[1],data,self.app.organize)
                # Catalogue deletion is committed; export/refresh failures must not invite replay.
                try:
                    for pid in result['project_ids']:
                        with self.app.store.connection() as db:
                            project=dict(db.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone())
                        self.app.context.archive(project)
                    self.app.changed()
                except Exception:
                    traceback.print_exc()
                    result['warnings']=['内容已移入回收站，项目交接信息尚未刷新。']
                return self.json(result)
            library_folder=re.fullmatch(r'/api/project-folders/([a-f0-9]{32})',path)
            if library_folder:
                if self.command=='PATCH':return self.json(self.app.project_library.update_folder(library_folder[1],data))
                if self.command=='DELETE':return self.json(self.app.project_library.delete_folder(library_folder[1]))
            library_project=re.fullmatch(r'/api/project-library/([a-f0-9]{32})(/visit)?',path)
            if library_project:
                if self.command=='POST' and library_project[2]:return self.json(self.app.project_library.visit(library_project[1]))
                if self.command=='PATCH' and not library_project[2]:return self.json(self.app.project_library.assign_project(library_project[1],data))
            if path=='/api/skill-source-labels' and self.command=='POST':
                if query:raise UserError('标签操作不接受查询参数。')
                return self.json(self.app.skills.add_filter_label(data),201)
            skill_label=re.fullmatch(r'/api/skill-source-labels/([a-z0-9_]{1,64})',path)
            if skill_label:
                if query:raise UserError('标签操作不接受查询参数。')
                if self.command=='DELETE' and not data:return self.json(self.app.skills.remove_filter_label(skill_label[1]))
                if self.command=='PATCH' and set(data)=={'hidden'} and data['hidden'] is False:return self.json(self.app.skills.remove_filter_label(skill_label[1],restore=True))
                raise UserError('标签操作无效。')
            skill_source=re.fullmatch(r'/api/skill-sources/([a-z0-9_]{1,64})',path)
            if skill_source:
                if query:raise UserError('扫描位置操作不接受查询参数。')
                if self.command=='PATCH':return self.json(self.app.skills.update_source(skill_source[1],data))
                if self.command=='DELETE':
                    if data:raise UserError('移除扫描位置不接受额外参数。')
                    return self.json(self.app.skills.update_source(skill_source[1],{},remove=True))
            if self.command=='POST':
                if path=='/api/skill-sources':
                    if query:raise UserError('扫描位置操作不接受查询参数。')
                    return self.json(self.app.skills.add_source(data),201)
                if path=='/api/resource-groups':return self.json(self.app.resource_groups.create(data),201)
                if path=='/api/project-folders':return self.json(self.app.project_library.create_folder(data),201)
                if path=='/api/project-library/open-folder':
                    if set(data)-{'path','folder_id'} or query:raise UserError('打开项目文件夹参数无效。')
                    return self.json(self.app.jobs.open_project_folder(data.get('path'),data.get('folder_id')),202)
                if path=='/api/external-open':return self.json(self.app.external.open(data))
                external_save=re.fullmatch(r'/api/external/([a-f0-9]{32})/content',path)
                if external_save:return self.json(self.app.external.save(external_save[1],data))
                if path=='/api/projects':
                    project=self.app.store.create_project(data.get('name',''),data.get('description',''),folder_id=data.get('folder_id'));self.app.context.request(project['id']);return self.json(project,201)
                if path=='/api/items/batch-properties':
                    result=self.app.store.batch_properties(data)
                    self.app.context.request(data['project_id']);return self.json(result)
                if path=='/api/items':
                    item=self.app.store.create_item(data);self.app.context.request(item['project_id']);return self.json(item,201)
                if path=='/api/clipboard/paste':return self.json(self.app.paste_clipboard(data),201)
                if path=='/api/import':return self.json(self.app.jobs.submit(data.get('project_id'),data.get('category','references'),data.get('paths',[]),data.get('folder_id',''),mode=data.get('mode','reference'),move_owned=data.get('move_owned',False)),202)
                if path=='/api/project-files/sync':
                    if query or set(data)!={'project_id'}:raise UserError('项目文件同步参数无效。')
                    return self.json(self.app.jobs.submit(data['project_id'],owned_only=True),202)
                if path=='/api/rescan':return self.json(self.app.jobs.submit(data.get('project_id')),202)
                if path=='/api/macos/desktop':
                    if self.app.desktop_message is None:raise UserError('当前环境没有 macOS 桌面窗口。',404)
                    return self.json({'ok':bool(self.app.desktop_message(data,self.headers.get('X-YingXu-Token','')))})
                if path=='/api/pick':return self.json(self.app.pick(data.get('kind')))
                if path=='/api/open':return self.json(self.app.open_file(data))
                if path=='/api/open-folder':return self.json(self.app.open_folder(data))
                if path=='/api/rename':
                    result=self.app.store.rename_file(data.get('id'),data.get('name',''))
                    for project in self.app.store.list_projects():self.app.context.request(project['id'])
                    return self.json(result)
                if path=='/api/relations':
                    result=self.app.store.add_relation(data.get('source_id'),data.get('target_id'),data.get('relation','关联'));self.app.context.request(self.app.store.get_item(data.get('source_id'))['project_id']);return self.json(result,201)
                if path=='/api/demo':
                    from yingxu.demo import create_demo
                    with self.app.demo_lock:project=create_demo(self.app.store)
                    self.app.context.request(project['id']);return self.json(project,201)
                if path=='/api/skills':return self.json(self.app.skills.create(data),201)
                if path=='/api/skills/refresh':return self.json(self.app.skills.refresh())
                if path=='/api/skills/bind':
                    result=self.app.skills.bind(data.get('project_id'),data.get('skill_id'),data.get('bound'));self.app.context.request(data.get('project_id'));return self.json(result)
                if path=='/api/context/refresh':return self.json(self.app.context.export(data.get('project_id')))
                if path=='/api/backup':return self.json({'path':str(self.app.store.backup_database())})
                if path=='/api/trash/delete-preview':return self.json(self.app.trash_deletion.preview(data))
                if path=='/api/trash/delete':
                    result=self.app.trash_deletion.delete(data);self.app.changed();return self.json(result)
                if path=='/api/folders':
                    result=self.app.organize.create_folder(data.get('project_id'),data.get('category'),data.get('name',''),data.get('parent_id'))
                    self.app.changed(result['project_id']);return self.json(result,201)
                if path=='/api/move':
                    conflict=data.get('conflict','error')
                    if not isinstance(conflict,str) or conflict not in ('error','rename','skip'):
                        raise UserError('同名文件处理方式无效。')
                    target=data.get('target_project_id')
                    if target is not None and (not isinstance(target,str) or not re.fullmatch(r'[a-f0-9]{32}',target)):
                        raise UserError('目标项目无效。')
                    source=self.app.store.get_item(data['ids'][0])['project_id'] if target and isinstance(data.get('ids'),list) and data['ids'] else None
                    if target and target!=source:
                        if conflict!='error':raise UserError('跨项目同名文件请先重命名后再移动。')
                        from yingxu.cross_project import move_items
                        with self.app.migration_jobs.cross_project_move():
                            result=move_items(self.app.organize,data.get('ids'),target,data.get('category'),data.get('folder_id'),physical_only=True)
                        try:self.app.changed()
                        except Exception:
                            traceback.print_exc()
                            result.setdefault('warnings',[]).append('文件已移动，项目索引需要重新刷新。')
                        return self.json(result)
                    from yingxu.organize import MoveConflict
                    try:result=self.app.organize.move_items(data.get('ids'),data.get('category'),data.get('folder_id'),conflict=conflict)
                    except MoveConflict as error:
                        return self.json({'error':str(error),'code':'move_name_conflict','conflicts':error.conflicts},409)
                    try:self.app.changed()
                    except Exception:
                        traceback.print_exc()
                        result.setdefault('warnings',[]).append('文件已移动，项目索引需要重新刷新。')
                    return self.json(result)
                if path=='/api/trash/items':
                    result=self.app.organize.delete_items(data.get('ids'));self.app.changed(result['project_id']);return self.json(result)
                restoring=re.fullmatch(r'/api/trash/([a-f0-9]{32})/restore',path)
                if restoring:
                    result=self.app.skills.restore(restoring[1]) if data.get('kind')=='skill' else self.app.organize.restore(restoring[1])
                    self.app.changed();return self.json(result)
            organization=re.fullmatch(r'/api/(folders|projects)/([a-f0-9]{32})',path)
            if organization:
                resource,oid=organization.groups()
                if resource=='folders':
                    if self.command=='PATCH':result=self.app.organize.rename_folder(oid,data.get('name',''))
                    elif self.command=='DELETE':result=self.app.organize.delete_folder(oid)
                    else:raise UserError('接口或请求方式不存在。',404)
                else:
                    if self.command=='PATCH':result=self.app.organize.update_project(oid,data)
                    elif self.command=='DELETE':
                        project=self.app.store.get_project(oid)
                        result=self.app.organize.delete_project(oid)
                        self.app.context.archive(project)
                    else:raise UserError('接口或请求方式不存在。',404)
                self.app.changed();return self.json(result)
            match=re.fullmatch(r'/api/(items|content|relations|skills)/([a-f0-9]{32})',path)
            if match:
                resource,iid=match.groups()
                if resource=='skills' and self.command=='PUT':
                    result=self.app.skills.save(iid,data)
                    for project in self.app.store.list_projects():self.app.context.request(project['id'])
                    return self.json(result)
                if resource=='skills' and self.command=='DELETE':
                    result=self.app.skills.remove(iid);self.app.changed();return self.json(result)
                if resource=='relations' and self.command=='DELETE':
                    with self.app.store.connection() as db:row=db.execute('SELECT i.project_id FROM relations r JOIN items i ON i.id=r.source_id WHERE r.id=?',(iid,)).fetchone()
                    result=self.app.store.remove_relation(iid)
                    if row:self.app.context.request(row[0])
                    return self.json(result)
                item=self.app.store.get_item(iid)
                if resource=='items' and self.command=='PATCH':result=self.app.store.update_item(iid,data)
                elif resource=='items' and self.command=='DELETE':result=self.app.organize.delete_items([iid])
                elif resource=='content' and self.command=='PUT':result=self.app.store.save_content(iid,data)
                else:raise UserError('接口或请求方式不存在。',404)
                if resource=='content':
                    with self.app.store.connection() as db:
                        project_ids=[r[0] for r in db.execute('SELECT DISTINCT project_id FROM items WHERE path=? AND removed=0',(item['path'],))]
                    for pid in project_ids:self.app.context.request(pid)
                else:self.app.context.request(item['project_id'])
                return self.json(result)
            raise UserError('接口或请求方式不存在。',404)
        except UserError as e:
            if self.command not in ('GET','HEAD'):self.reject_json({'error':str(e)},e.status)
            else:self.json({'error':str(e)},e.status)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,socket.timeout):pass
        except (ValueError,TypeError,KeyError) as e:
            self.reject_json({'error':'请求字段不正确，请检查输入。'},400)
        except OSError:
            traceback.print_exc();self.reject_json({'error':'文件操作失败，可能被占用或已移动；你的原文件和草稿会保留。'},409)
        except Exception:
            traceback.print_exc();self.reject_json({'error':'工作台遇到错误，请查看本地服务日志。'},500)

    do_GET=handle_request
    do_HEAD=handle_request
    do_POST=handle_request
    do_PUT=handle_request
    do_PATCH=handle_request
    do_DELETE=handle_request


def main():
    parser=argparse.ArgumentParser(description='映序 · 本地视频创作工作台')
    parser.add_argument('--port',type=int,default=8791)
    parser.add_argument('--data',type=Path,default=default_data_root())
    parser.add_argument('--projects-root',type=Path,default=default_project_root())
    args=parser.parse_args()
    if not 1024<=args.port<=65535:raise SystemExit('端口范围应为1024至65535。')
    app=Application(args.data,args.projects_root)
    try:server=Server(('127.0.0.1',args.port),app)
    except BaseException:
        app.close();raise
    print(f'映序 {__version__} · http://127.0.0.1:{args.port}',flush=True)
    try:server.serve_forever(poll_interval=.3)
    finally:
        server.server_close();app.close()


if __name__=='__main__':main()
