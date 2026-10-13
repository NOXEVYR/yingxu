"""Non-destructive review sidecars and bounded, cancellable local FFmpeg exports.

Only registered item/external IDs enter here; no HTTP-supplied disk paths or
FFmpeg options. All output and temporary bytes belong to one generated job.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time

from .runtime import ffmpeg_path
from .store import UserError, clean_path, uid


def number(value, optional=False):
    if value is None and optional:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 86400:
        raise UserError('时间必须为 0 至 86400 秒之间的有限数字。')
    return float(value)


def fingerprint(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


class VideoReview:
    def __init__(self, app):
        self.app = app
        self.root = app.store.data_root / 'video-review'
        self.lock = threading.RLock()
        self.jobs = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='yingxu-video')
        self.closed = False
        self._restore_exports()

    def _restore_exports(self):
        if not self.root.exists():return
        try:
            clean_path(self.root)
            with os.scandir(self.root) as entries:
                for index, entry in enumerate(entries):
                    if index >= 2000 or len(self.jobs) >= 100:break
                    if not re.fullmatch('[a-f0-9]{32}',entry.name) or not entry.is_dir(follow_symlinks=False):continue
                    try:
                        directory=clean_path(Path(entry.path));receipt=clean_path(directory/'export.json')
                        if receipt.stat().st_size>4096:continue
                        saved=json.loads(receipt.read_text())
                        if saved['id']!=entry.name or saved['mode'] not in ('copy','encode') or not re.fullmatch('[a-f0-9]{64}',saved['source_key']):continue
                        number(saved['start']);number(saved['end'])
                        if type(saved['created']) not in (int,float) or not math.isfinite(saved['created']):continue
                        output=clean_path(directory/('clip.mkv' if saved['mode']=='copy' else 'clip.mp4'))
                        if not output.is_file():continue
                        self.jobs[entry.name]={**saved,'state':'done','error':None,'directory':directory,'output':output,'cancel':threading.Event()}
                    except (OSError,ValueError,KeyError,TypeError,UserError):continue
        except (OSError,UserError):return

    @staticmethod
    def source_key(path):
        return hashlib.sha256(os.path.normcase(str(path)).encode('utf-8')).hexdigest()

    def recent(self, kind, iid):
        key=self.source_key(self.source(kind,iid))
        with self.lock:
            values=sorted((j for j in self.jobs.values() if j['source_key']==key),key=lambda j:j['created'],reverse=True)[:20]
            return {'jobs':[self.status(j['id']) for j in values]}

    def reveal(self, jid):
        output=self.output(jid)
        if os.name=='nt':
            subprocess.Popen([str(Path(os.environ.get('WINDIR','C:/Windows'))/'explorer.exe'),'/select,',str(output)])
        else:
            import sys
            if sys.platform!='darwin':raise UserError('请在浏览器下载片段；当前环境没有原生文件夹窗口。',400)
            from .macos import open_path
            open_path(output,reveal=True)
        return {'ok':True}

    def source(self, kind, iid):
        if kind not in ('file', 'external') or not isinstance(iid, str) or not re.fullmatch('[a-f0-9]{32}', iid):
            raise UserError('请选择已打开或登记的视频。')
        if kind == 'external':
            path = self.app.external.resolve(iid)
            from .store import KINDS
            is_video = KINDS.get(path.suffix.lower()) == 'video'
        else:
            item = self.app.store.get_item(iid)
            is_video = item['kind'] == 'video'
            path = self.app.store.resolve_item_path(item)
        path = clean_path(path)
        if not is_video or not path.is_file():
            raise UserError('仅支持已登记的视频文件。', 415)
        return path

    def _folder(self):
        self.root.mkdir(exist_ok=True)
        return clean_path(self.root)

    def _review_path(self, path, kind, iid):
        # External session IDs change after restart; canonical path is stable.
        key = hashlib.sha256(('file:'+iid).encode()).hexdigest() if kind=='file' else self.source_key(path)
        return self._folder() / (key + '.json')

    def get(self, kind, iid):
        with self.lock:
            source = self.source(kind, iid)
            target = self._review_path(source,kind,iid)
            if target.exists() and clean_path(target).stat().st_size>2*1024**2:raise UserError('审片记录超过大小限制，请检查应用数据。',413)
            raw = clean_path(target).read_bytes() if target.exists() else b''
            value = json.loads(raw) if raw else None
            if value:self.validate(value['review'])
            changed = bool(value and value['fingerprint'] != fingerprint(source))
            return {'revision': hashlib.sha256(raw).hexdigest(), 'review': value['review'] if value else None,
                    'source_changed': changed}

    @staticmethod
    def validate(data):
        if not isinstance(data, dict) or set(data) != {'markers', 'in', 'out', 'a', 'b'}:
            raise UserError('审片数据格式无效。')
        marks = data['markers']
        if not isinstance(marks, list) or len(marks) > 500:
            raise UserError('每个视频最多保存 500 个标记。')
        result = {key: number(data[key], True) for key in ('in', 'out', 'a', 'b')}
        result['markers'] = []
        for mark in marks:
            if not isinstance(mark, dict) or set(mark) != {'time', 'note'} or not isinstance(mark['note'], str) or len(mark['note']) > 500:
                raise UserError('标记必须包含时间和最多 500 字的备注。')
            result['markers'].append({'time': number(mark['time']), 'note': mark['note']})
        for start, end in (('in', 'out'), ('a', 'b')):
            if result[start] is not None and result[end] is not None and result[end] <= result[start]:
                raise UserError('终点必须晚于起点。')
        return result

    def save(self, kind, iid, data):
        if not isinstance(data, dict) or set(data) != {'revision', 'review'}:
            raise UserError('保存审片数据需要版本和审片内容。')
        review = self.validate(data['review'])
        with self.lock:
            current = self.get(kind, iid)
            if data['revision'] != current['revision']:
                raise UserError('审片记录已更改，请重新载入后保存。', 409)
            source = self.source(kind, iid)
            target = self._review_path(source,kind,iid)
            temporary = target.with_name(target.name + '.' + uid() + '.tmp')
            raw = json.dumps({'fingerprint': fingerprint(source), 'review': review}, ensure_ascii=False).encode('utf-8')
            try:
                with temporary.open('xb') as handle:
                    handle.write(raw); handle.flush(); os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return {'revision': hashlib.sha256(raw).hexdigest(), 'review': review, 'source_changed': False}

    def submit(self, data):
        if not isinstance(data, dict) or set(data) != {'source', 'id', 'start', 'end', 'mode'}:
            raise UserError('导出只接受视频 ID、入出点和裁切方式。')
        start, end = number(data['start']), number(data['end'])
        if end <= start:
            raise UserError('出点必须晚于入点。')
        if data['mode'] not in ('copy', 'encode'):
            raise UserError('请选择快速复制或精确时间裁切。')
        executable = ffmpeg_path()
        if not executable:
            raise UserError('未找到 FFmpeg，无法导出；原视频不变。', 503)
        source = self.source(data['source'], data['id'])
        with self.lock:
            if self.closed or sum(j['state'] in ('queued', 'running', 'cancelling') for j in self.jobs.values()) >= 4:
                raise UserError('导出队列已满，请等待或取消现有任务。', 409)
            # Keep at most 100 recent in-memory job records. Never remove outputs.
            if len(self.jobs) >= 100:
                for key in list(self.jobs):
                    if self.jobs[key]['state'] in ('done', 'error', 'cancelled'):
                        del self.jobs[key]; break
            jid = uid()
            directory = self._folder() / jid
            directory.mkdir()  # exclusive, task-owned; no user supplied output path
            job = {'id': jid, 'state': 'queued', 'cancel': threading.Event(), 'source': data['source'],
                   'item': data['id'], 'path': source, 'fingerprint': fingerprint(source), 'source_key':self.source_key(source),'created':time.time(),
                   'start': start, 'end': end, 'mode': data['mode'], 'directory': directory,
                   'output': directory / ('clip.mkv' if data['mode'] == 'copy' else 'clip.mp4'),
                   'error': None}
            self.jobs[jid] = job
            self.pool.submit(self._run, job, executable)
            return self.status(jid)

    def status(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if not job:
                raise UserError('未找到此导出任务。', 404)
            return {'id': jid, 'state': job['state'], 'error': job['error'], 'mode': job['mode'],
                    'start': job['start'], 'end': job['end'],
                    'download_url': '/api/video/exports/' + jid + '/file' if job['state'] == 'done' else None}

    def cancel(self, jid):
        with self.lock:
            self.status(jid)
            job = self.jobs[jid]
            if job['state'] in ('queued', 'running', 'cancelling'):
                job['cancel'].set(); job['state'] = 'cancelling'
            return self.status(jid)

    def output(self, jid):
        with self.lock:
            if self.status(jid)['state'] != 'done':
                raise UserError('导出尚未完成。', 409)
            return clean_path(self.jobs[jid]['output'])

    def _run(self, job, executable):
        partial = job['directory'] / ('partial' + job['output'].suffix)
        error_log = job['directory'] / 'ffmpeg.log'
        process = None
        final_state, error = 'error', None
        try:
            with ExitStack() as stack:
                stack.enter_context(self.app.update_service.mutation('POST', '/api/video/exports'))
                stack.enter_context(self.app.migration_jobs.mutation('POST', '/api/video/exports'))
                if job['cancel'].is_set():
                    final_state = 'cancelled'; return
                source = self.source(job['source'], job['item'])
                if source != job['path'] or fingerprint(source) != job['fingerprint']:
                    raise UserError('源视频已改变，请重新选择入出点。')
                with self.lock:
                    job['state'] = 'running'
                command = [executable, '-nostdin', '-hide_banner', '-loglevel', 'error', '-n']
                if job['mode'] == 'copy':
                    # Input seeking + stream copy retains keyframe preroll; UI warns.
                    command += ['-ss', str(job['start']), '-protocol_whitelist', 'file', '-format_whitelist', 'mov,matroska,webm,avi', '-i', str(source), '-t', str(job['end']-job['start']),
                                '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy', '-avoid_negative_ts', 'make_zero']
                else:
                    # Accurate input seek decodes/discards preroll. Preserve VFR timing;
                    # this is time-domain trim, not an unverified exact-frame claim.
                    command += ['-ss', str(job['start']), '-accurate_seek', '-protocol_whitelist', 'file', '-format_whitelist', 'mov,matroska,webm,avi', '-i', str(source), '-t', str(job['end']-job['start']),
                                '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
                                '-pix_fmt', 'yuv420p', '-fps_mode', 'vfr', '-threads', '2', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart']
                command += [str(partial)]
                with error_log.open('xb') as log:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    deadline = time.monotonic() + 3600
                    while process.poll() is None:
                        if (partial.exists() and partial.stat().st_size > 32*1024**3) or error_log.stat().st_size > 8*1024**2 or shutil.disk_usage(job['directory']).free < 128*1024**2:
                            raise UserError('导出达到容量限制，已停止；原视频不变。')
                        if job['cancel'].wait(.1) or time.monotonic() > deadline:
                            process.terminate()
                            try: process.wait(timeout=3)
                            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
                            if job['cancel'].is_set():
                                final_state = 'cancelled'; return
                            raise UserError('导出超过一小时，已停止并清理本任务临时文件。')
                    if process.returncode:
                        raise UserError('导出失败：当前编码、尺寸或容器无法按此方式导出，请尝试另一种方式。')
                if not partial.is_file() or partial.stat().st_size < 1024:
                    raise UserError('没有可导出的视频，请检查入出点。')
                if self.source(job['source'], job['item']) != source or fingerprint(source) != job['fingerprint']:
                    raise UserError('导出期间源视频发生改变，结果已丢弃；请重新打开。')
                with self.lock:
                    if job['cancel'].is_set():
                        final_state = 'cancelled'; return
                    # Destination directory is exclusive and the output name is ours.
                    receipt={key:job[key] for key in ('id','source_key','created','start','end','mode')}
                    with (job['directory']/'export.json').open('x',encoding='utf-8') as handle:
                        json.dump(receipt,handle);handle.flush();os.fsync(handle.fileno())
                    os.replace(partial, job['output'])
                    final_state = 'done'
        except Exception as exc:
            error = str(exc) if isinstance(exc, UserError) else '导出失败；原文件保持不变。'
        finally:
            if process is not None and process.poll() is None:
                process.kill(); process.wait()
            try:
                # No recursive deletion; only exact files created by this job.
                partial.unlink(missing_ok=True); error_log.unlink(missing_ok=True)
                if final_state != 'done':
                    (job['directory']/'export.json').unlink(missing_ok=True)
                    job['directory'].rmdir()
            except OSError:
                error = (error or '') + ' 本任务临时文件清理未完成，请检查应用数据中的 video-review/' + job['id'] + '。'
                final_state = 'error'
            with self.lock:
                job['state'], job['error'] = final_state, error

    def close(self):
        with self.lock:
            self.closed = True
            for job in self.jobs.values():
                if job['state'] in ('queued', 'running', 'cancelling'):
                    job['cancel'].set()
        self.pool.shutdown(wait=True, cancel_futures=False)
