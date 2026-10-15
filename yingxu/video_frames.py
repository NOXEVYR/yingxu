"""Bounded, cancellable decoded-frame inspection. No browser seek accuracy claims."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from contextlib import ExitStack, closing
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

from .runtime import frame_tool_paths, frame_tool_version, image_support, windows_change_time
from .store import UserError, clean_path, uid
from .video_review import fingerprint

ACTIVE = ('queued', 'running', 'cancelling')
MAX_FRAMES = 2_000_000
MAX_INDEX = 256 * 1024**2
MAX_CACHE = 64 * 1024**2
MAX_CACHE_ENTRIES = 128
MAX_PIXELS = 16_777_216
PREFETCH_FRAMES = 12


class Cancelled(Exception):
    pass


class VideoFrames:
    def __init__(self, app):
        self.app = app
        self.root = app.store.data_root / 'video-frames'
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='yingxu-frames')
        self.sessions, self.jobs = {}, {}
        self.cache = OrderedDict()
        self.closed = False
        self._recover_owned()

    def capability(self):
        if sys.platform == 'darwin':
            return {'available': False, 'reason': 'Mac 暂未验收受控帧索引工具，暂停帧检查尚未开放。'}
        ffmpeg, ffprobe = frame_tool_paths()
        available = bool(ffmpeg and ffprobe and image_support())
        if sys.platform == 'win32' and not (ffmpeg and ffprobe):
            return {'available': False, 'reason': 'Windows 帧检查需要同一锁定运行库清单中的 FFmpeg 与 ffprobe；不会改用系统 PATH 工具。'}
        return {'available': available, 'reason': '' if available else '缺少经支持的 FFmpeg / ffprobe；Windows 需含已核验工具的运行时包。'}

    def _recover_owned(self):
        if not self.root.exists():
            return
        clean_path(self.root)
        # Recover only names recorded by this cache's own journal, never recurse.
        for directory in list(self.root.iterdir())[:256]:
            if not re.fullmatch('[a-f0-9]{32}', directory.name) or directory.is_symlink():
                continue
            try:
                owner = clean_path(directory / 'owner.json')
                if owner.stat().st_size > 65536:
                    continue
                value = json.loads(owner.read_text())
                if value.get('purpose') != 'yingxu-frame-cache-v1' or value.get('id') != directory.name:
                    continue
                names = value['files']
                if not isinstance(names, list) or len(names) > 256 or any(not self._safe_name(n) for n in names):
                    continue
                for name in names:
                    (directory / name).unlink(missing_ok=True)
                owner.unlink(); directory.rmdir()
            except (OSError, ValueError, TypeError, KeyError, UserError):
                pass

    @staticmethod
    def _safe_name(name):
        return isinstance(name, str) and bool(re.fullmatch(r'(index\.sqlite|[a-f0-9]{32}\.(out|log)|frame-[0-9]+\.png|owner\.tmp)', name))

    def _journal(self, session):
        directory = session['directory']
        value = {'purpose': 'yingxu-frame-cache-v1', 'id': session['id'], 'files': sorted(session['owned'] | {'owner.tmp'})}
        (directory / 'owner.tmp').write_text(json.dumps(value), encoding='utf-8')
        os.replace(directory / 'owner.tmp', directory / 'owner.json')

    def _own(self, session, name):
        assert self._safe_name(name)
        with self.lock:
            session['owned'].add(name); self._journal(session)
        return session['directory'] / name

    def _unlink(self, session, name):
        with self.lock:
            (session['directory'] / name).unlink(missing_ok=True)
            session['owned'].discard(name)
            self._journal(session)

    def _check(self, job, session):
        if self.closed or job['cancel'].is_set() or session['cancel'].is_set() or session['invalid']:
            raise Cancelled()
        now = time.monotonic()
        if now - job.get('source_checked_at', 0) >= .5:
            self._source(session)
            job['source_checked_at'] = now
        if now - job['started'] > (3600 if job['kind'] in ('index', 'export') else 180):
            raise UserError('帧检查达到时间上限，已停止；可取消后重新选择。', 408)

    def _source(self, session):
        try:
            path = self.app.video_review.source(session['source'], session['item'])
            changed = path != session['path'] or fingerprint(path) != session['fingerprint']
            previous_signature = session.get('hash_signature')
            if not changed and previous_signature is not None:
                current_signature, cacheable = self._hash_signature(path)
                # FILE_BASIC_INFO can be temporarily unavailable on Windows.
                # A missing change-time cannot prove mutation: force _hash to
                # read the bytes again and compare against the indexed digest.
                if cacheable and current_signature != previous_signature:
                    changed = True
        except (OSError, UserError):
            session['invalid'] = True
            raise UserError('源视频已移除或不可访问，帧检查已失效。', 409) from None
        if changed:
            session['invalid'] = True
            raise UserError('源视频已改变，旧帧索引已失效，请重新建立索引。', 409)
        return path

    @staticmethod
    def _hash_signature(path):
        stat = path.stat()
        signature = fingerprint(path) + [stat.st_ctime_ns]
        if sys.platform == 'win32':
            change_time = windows_change_time(path)
            signature.append(change_time)
            return signature, change_time is not None
        return signature, True

    def _hash(self, job, session):
        path = self._source(session)
        # Windows st_ctime is creation time, so use FILE_BASIC_INFO.ChangeTime.
        # Linux ctime and the full file identity remain suitable hash-cache keys.
        signature, cacheable = self._hash_signature(path)
        if cacheable and session.get('hash_signature') == signature:
            self._check(job, session)
            return session['sha256']
        digest = hashlib.sha256()
        job['phase'] = '校验源文件'; job['bytes'] = 0
        with path.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                self._check(job, session); digest.update(block); job['bytes'] += len(block)
        self._source(session)
        after, after_cacheable = self._hash_signature(path)
        # A Windows change-time read may fail transiently between the two
        # metadata snapshots. In that case compare the stable file identity,
        # size and mtime, then rely on the full digest below; never cache a
        # signature unless both change-time reads were available.
        metadata_changed = after[:4] != signature[:4]
        if cacheable and after_cacheable:
            metadata_changed = metadata_changed or after != signature
        if metadata_changed:
            session['invalid'] = True
            raise UserError('源视频在校验中改变，请重新建立索引。', 409)
        value = digest.hexdigest()
        if session.get('sha256') and session['sha256'] != value:
            session['invalid'] = True
            raise UserError('源视频内容已改变，旧帧索引已失效。', 409)
        session['hash_signature'] = signature if cacheable and after_cacheable else None
        return value

    def _process(self, job, session, command, *, limit=MAX_INDEX, consume=None):
        # A single worker is not a CPU bound when the codecs/filtergraph auto-
        # spawn one thread per host core. Apply limits before input so decoder
        # threads are bounded separately from the existing encoder option.
        command = list(command)
        executable = str(Path(command[0]).resolve())
        tools = session.get('tools', {})
        if '-version' not in command:
            if executable == tools.get('ffmpeg', {}).get('path'):
                command[1:1] = ['-threads', '1', '-filter_threads', '1', '-filter_complex_threads', '1']
            elif executable == tools.get('ffprobe', {}).get('path'):
                command[1:1] = ['-threads', '1']
        out = self._own(session, job['id'] + '.out')
        log = self._own(session, job['id'] + '.log')
        process = None
        try:
            with out.open('wb') as stdout, log.open('wb') as stderr:
                process_env = None
                if sys.platform == 'win32' and executable in {tool.get('path') for tool in tools.values()}:
                    process_env = os.environ.copy()
                    search = [str(Path(executable).parent)]
                    system_root = process_env.get('SystemRoot')
                    if system_root:
                        search.append(str(Path(system_root) / 'System32'))
                    process_env['PATH'] = ';'.join(search)
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=process_env)
                with out.open('rb') as reader:
                    while True:
                        self._check(job, session)
                        if out.stat().st_size > limit or log.stat().st_size > 1024**2:
                            raise UserError('帧检查输出超过容量限制，已停止。', 413)
                        # Includes in-progress PNGs and indices, not just LRU entries.
                        with self.lock:
                            disk = sum((s['directory']/n).stat().st_size for s in self.sessions.values()
                                for n in s['owned'] if (s['directory']/n).is_file())
                        extra = job.get('partial')
                        if extra and extra.exists() and extra.stat().st_size > 32*1024**3:
                            raise UserError('严格导出超过 32 GiB 限制，已停止。', 413)
                        if disk > 512*1024**2 or shutil.disk_usage(self.root).free < 128*1024**2:
                            raise UserError('帧缓存达到磁盘容量限制，已停止。', 413)
                        if consume:
                            consume(reader, process.poll() is not None)
                        if process.poll() is not None:
                            if consume: consume(reader, True)
                            break
                        job['cancel'].wait(.05)
                if process.returncode or log.stat().st_size:
                    raise UserError('解码或索引失败；此文件暂不能进行可靠帧检查。', 422)
            return out.read_bytes() if not consume else None
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
            self._unlink(session, out.name); self._unlink(session, log.name)

    def start(self, data):
        if not isinstance(data, dict) or set(data) != {'source', 'id'}:
            raise UserError('帧索引只接受已登记的视频 ID。')
        capability = self.capability()
        if not capability['available']:
            raise UserError(capability['reason'], 503)
        path = self.app.video_review.source(data['source'], data['id'])
        with self.lock:
            if self.closed or len(self.sessions) >= 8 or sum(j['state'] in ACTIVE for j in self.jobs.values()) >= 4:
                raise UserError('帧检查队列或会话已满，请先取消已有检查。', 409)
            self.root.mkdir(exist_ok=True); clean_path(self.root)
            # Unknown leftovers are preserved and count against the disk budget.
            total = 0
            for count, entry in enumerate(self.root.rglob('*')):
                if count > 4096: raise UserError('帧缓存目录需整理，暂不新增任务。', 413)
                if entry.is_file(): total += entry.stat().st_size
            if total > 512 * 1024**2: raise UserError('帧缓存达到容量限制，请退出后重试。', 413)
            sid = uid(); directory = self.root / sid; directory.mkdir()
            session = {'id': sid, 'source': data['source'], 'item': data['id'], 'path': path,
                'fingerprint': fingerprint(path), 'directory': directory, 'owned': set(), 'cancel': threading.Event(), 'invalid': False}
            self.sessions[sid] = session; self._journal(session)
            return self._enqueue(session, 'index', jid=sid)

    def _enqueue(self, session, kind, *, jid=None, ordinal=None, fields=None):
        if sum(j['state'] in ACTIVE for j in self.jobs.values()) >= 4:
            raise UserError('帧检查队列已满。', 409)
        if len(self.jobs) >= 64:
            for key in list(self.jobs):
                if self.jobs[key]['state'] not in ACTIVE and (self.jobs[key]['kind'] != 'index' or self.jobs[key]['session'] not in self.sessions):
                    del self.jobs[key]; break
            if len(self.jobs) >= 64: raise UserError('帧任务历史已满，请先关闭检查会话。', 409)
        jid = jid or uid()
        job = {'id': jid, 'session': session['id'], 'kind': kind, 'ordinal': ordinal, 'state': 'queued',
            'phase': '等待处理', 'frames': 0, 'bytes': 0, 'created': time.time(), 'queued_at': time.monotonic(), 'started': time.monotonic(),
            'cancel': threading.Event(), 'error': None}
        job.update(fields or {})
        self.jobs[jid] = job; self.pool.submit(self._run, job, session)
        return self.status(jid)

    def _session(self, sid):
        session = self.sessions.get(sid)
        if not session or session['cancel'].is_set() or session['invalid']:
            raise UserError('帧检查会话已取消或失效，请重新建立索引。', 409)
        try:
            self._source(session)
        except UserError:
            session['cancel'].set()
            if not any(j['session']==sid and j['state'] in ACTIVE for j in self.jobs.values()): self._cleanup(session)
            raise
        return session

    def frame(self, sid, data):
        if not isinstance(data, dict) or set(data) != {'ordinal'} or type(data['ordinal']) is not int:
            raise UserError('静帧请求只接受整数展示序号。')
        with self.lock:
            session = self._session(sid)
            if self.jobs[sid]['state'] != 'ready': raise UserError('索引尚未完成。', 409)
            if not 0 <= data['ordinal'] < session['count']: raise UserError('帧序号超出范围。')
            return self._enqueue(session, 'frame', ordinal=data['ordinal'])

    def export(self, sid, data):
        if not isinstance(data, dict) or set(data) != {'first', 'last', 'audio'} or data['audio'] != 'none':
            raise UserError('严格帧导出需明确指定首尾帧；本阶段仅支持不含音频。')
        with self.lock:
            session = self._session(sid)
            first, last = data['first'], data['last']
            if self.jobs[sid]['state'] != 'ready': raise UserError('索引尚未完成。', 409)
            if type(first) is not int or type(last) is not int or not 0 <= first <= last < session['count']:
                raise UserError('请指定有效首尾帧；两端均包含。')
            if last-first+1 > 10000: raise UserError('单次严格导出最多 10000 帧。', 413)
            return self._enqueue(session, 'export', fields={'first': first, 'last': last})

    def export_manifest(self, jid):
        review = self.app.video_review
        review.output(jid)
        with review.lock:
            if review.jobs[jid]['mode'] != 'frames': raise UserError('此导出没有帧清单。', 404)
            return clean_path(review.jobs[jid]['directory'] / 'frames.json')

    def status(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if not job: raise UserError('未找到帧检查任务。', 404)
            if job['state'] in ('ready', 'done') and job['kind'] != 'export':
                self._session(job['session'])
            result = {k: job[k] for k in ('id', 'session', 'kind', 'state', 'phase', 'frames', 'bytes', 'error')}
            result['verified_frames'] = job.get('verified_frames', 0)
            result['elapsed'] = round(time.monotonic() - job['queued_at'], 1)
            result.update(job.get('result', {}))
            return result

    def cancel(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if not job: raise UserError('未找到帧检查任务。', 404)
            if job['kind'] == 'index':
                session = self.sessions.get(job['session'])
                if session: session['cancel'].set()
                # A ready index has no worker left to finish cancellation. Its
                # child frame jobs do; keep those active until their finally.
                if job['state'] == 'ready': job['state'] = 'cancelled'
                for other in self.jobs.values():
                    if other['session'] == job['session'] and other['state'] in ACTIVE:
                        other['cancel'].set(); other['state'] = 'cancelling'
                if session and not any(j['session'] == session['id'] and j['state'] in ACTIVE for j in self.jobs.values()):
                    self._cleanup(session); job['state'] = 'cancelled'
                elif session and job['state'] != 'cancelled':
                    job['state'] = 'cancelling'
            elif job['state'] in ACTIVE:
                job['cancel'].set(); job['state'] = 'cancelling'
            return self.status(jid)

    def image(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if not job or job['kind'] != 'frame' or job['state'] != 'done': raise UserError('静帧尚未就绪。', 409)
            session = self._session(job['session'])
            path = clean_path(session['directory'] / f"frame-{job['ordinal']}.png")
            if not path.is_file() or (session['id'], job['ordinal']) not in self.cache: raise UserError('邻帧缓存已更新，请重新定位。', 410)
            self.cache.move_to_end((session['id'], job['ordinal']))
            return path

    def _index(self, job, session):
        session['sha256'] = self._hash(job, session)
        ffmpeg, probe = frame_tool_paths()
        if not ffmpeg or not probe:
            raise UserError(self.capability()['reason'] or '缺少受支持的 FFmpeg 与 ffprobe。', 503)
        session['tools'] = {}
        versions = []
        for name, executable in [('ffmpeg', ffmpeg), ('ffprobe', probe)]:
            session['tools'][name] = {'path': str(Path(executable).resolve()), 'fingerprint': fingerprint(Path(executable))}
            version = self._process(job, session, [executable, '-version'], limit=65536).decode('utf-8').splitlines()[0]
            versions.append(version.split('version ', 1)[1].split(' ', 1)[0])
        expected = frame_tool_version() if sys.platform == 'win32' else None
        if (versions[0] != versions[1] or
                (sys.platform == 'win32' and (not expected or not versions[0].startswith(expected)))):
            raise UserError('FFmpeg 与 ffprobe 不符合锁定运行库版本。', 503)
        session['tool_version'] = versions[0]
        options = [probe, '-v', 'error', '-protocol_whitelist', 'file', '-format_whitelist', 'mov,matroska,webm,avi']
        job['phase'] = '读取流信息'
        metadata = json.loads(self._process(job, session, options + ['-select_streams', 'v:0', '-show_streams', '-show_format',
            '-show_entries', 'stream=index,width,height,time_base,start_pts,pix_fmt,color_transfer,sample_aspect_ratio:stream_side_data=rotation:format=start_time,duration',
            '-of', 'json', str(session['path'])], limit=1024**2))
        streams = metadata.get('streams', [])
        if len(streams) != 1: raise UserError('没有可检查的视频流。', 422)
        stream = {k: v for k, v in streams[0].items() if k in ('index','width','height','time_base','start_pts','pix_fmt','color_transfer','sample_aspect_ratio','side_data_list')}
        pixels = int(stream['width']) * int(stream['height'])
        if not 0 < pixels <= MAX_PIXELS: raise UserError('静帧检查暂支持不超过 1677 万像素的视频。', 413)
        if stream.get('color_transfer') in ('smpte2084', 'arib-std-b67'):
            raise UserError('HDR 静帧显示尚未验收，此文件暂不开放帧检查。', 415)
        if stream.get('sample_aspect_ratio', '1:1') not in ('1:1', 'N/A'):
            raise UserError('非方形像素静帧显示尚未验收。', 415)
        tb = Fraction(stream['time_base'])
        if tb <= 0: raise UserError('无效时间基。')
        session['stream'] = stream; session['time_base'] = str(tb)
        db = self._own(session, 'index.sqlite')
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute('PRAGMA journal_mode=OFF')
            connection.execute('CREATE TABLE frames(ordinal INTEGER PRIMARY KEY, pts INTEGER, best_pts INTEGER, duration INTEGER, key_frame INTEGER)')
            def consume(reader, final):
                while True:
                    self._check(job, session)
                    at = reader.tell(); line = reader.readline(65537)
                    if len(line) > 65536: raise UserError('帧索引行超过限制。', 413)
                    if not line: break
                    if not line.endswith(b'\n') and not final: reader.seek(at); break
                    text = line.decode('utf-8', errors='strict').strip()
                    if not text.startswith('frame|'): continue
                    values = dict(piece.split('=', 1) for piece in text.split('|')[1:] if '=' in piece)
                    def integer(name):
                        value = values.get(name, 'N/A')
                        return None if value == 'N/A' else int(value)
                    ordinal = job['frames']
                    if ordinal >= MAX_FRAMES: raise UserError('帧数超过本阶段两百万帧限制。', 413)
                    connection.execute('INSERT INTO frames VALUES(?,?,?,?,?)', (ordinal, integer('pts'), integer('best_effort_timestamp'), integer('duration'), integer('key_frame')))
                    job['frames'] += 1
                    if ordinal % 1000 == 0:
                        connection.commit()
                        if db.stat().st_size > MAX_INDEX: raise UserError('帧索引超过容量限制。', 413)
            job['phase'] = '顺序解码建立索引（不按平均帧率推算）'
            self._process(job, session, options + ['-err_detect', 'explode', '-select_streams', 'v:0', '-show_frames',
                '-show_entries', 'frame=pts,best_effort_timestamp,duration,key_frame', '-of', 'compact=p=1:nk=0', str(session['path'])], consume=consume)
            connection.commit()
            if not job['frames']: raise UserError('没有得到可用解码帧。', 422)
            first = connection.execute('SELECT pts,best_pts FROM frames ORDER BY ordinal LIMIT 1').fetchone()
        self._pixel_index(job, session)
        self._hash(job, session)
        session['count'] = job['frames']; session['first_pts'] = first[0] if first[0] is not None else first[1]
        job['result'] = {'count': session['count'], 'time_base': str(tb), 'source_sha256': session['sha256'],
            'first_pts': str(session['first_pts']) if session['first_pts'] is not None else None,
            'stream': stream, 'tool_version': session['tool_version'], 'mode': 'decoded-still-frames'}

    def _pixel_index(self, job, session):
        """A whole-stream RGB oracle allows seek optimization to fail closed."""
        job['phase'] = '顺序解码校验像素（供后续快速定位核对）'
        with closing(sqlite3.connect(session['directory'] / 'index.sqlite')) as db, db:
            db.execute('ALTER TABLE frames ADD COLUMN rgb_sha256 TEXT')
            number = 0
            def consume(reader, final):
                nonlocal number
                while True:
                    self._check(job, session)
                    at = reader.tell(); line = reader.readline(4097)
                    if not line: break
                    if len(line) > 4096: raise UserError('像素校验行超限。', 413)
                    if not line.endswith(b'\n') and not final: reader.seek(at); break
                    text = line.decode('ascii').strip()
                    if not text or text.startswith('#'): continue
                    parts = [part.strip() for part in text.split(',')]
                    if len(parts) != 6 or not re.fullmatch('[a-f0-9]{64}', parts[-1]):
                        raise UserError('无法验证完整解码像素索引。', 422)
                    if number >= job['frames']: raise UserError('两次解码帧数不一致。', 422)
                    db.execute('UPDATE frames SET rgb_sha256=? WHERE ordinal=?', (parts[-1], number))
                    number += 1
                    job['verified_frames'] = number
                    if number % 1000 == 0: db.commit()
            self._process(job, session, [session['tools']['ffmpeg']['path'], '-nostdin', '-v', 'error', '-xerror',
                '-protocol_whitelist', 'file', '-format_whitelist', 'mov,matroska,webm,avi',
                '-err_detect', 'explode', '-i', str(session['path']), '-map', '0:v:0', '-an',
                '-vf', 'format=rgb24,setpts=N', '-fps_mode', 'passthrough', '-c:v', 'rawvideo',
                '-threads', '1', '-f', 'framehash', '-hash', 'sha256', 'pipe:1'], consume=consume)
            if number != job['frames']: raise UserError('两次解码帧数不一致。', 422)
            # Non-monotone/missing/duplicate PTS retain ordinal prefix decoding.
            session['seek_safe'] = not db.execute('SELECT 1 FROM frames a LEFT JOIN frames b ON b.ordinal=a.ordinal-1 '
                'WHERE a.pts IS NULL OR (a.ordinal>0 AND a.pts<=b.pts) LIMIT 1').fetchone()
            db.execute('CREATE INDEX keyframes ON frames(key_frame,ordinal)')
            db.commit()
            if (session['directory'] / 'index.sqlite').stat().st_size > MAX_INDEX:
                raise UserError('帧索引超过容量限制。', 413)

    def _decode_window(self, job, session, first, last):
        from PIL import Image
        names = [f'frame-{n}.png' for n in range(first, last+1)]
        job['created_frames'] = names
        for name in names: self._own(session, name)
        with closing(sqlite3.connect(session['directory'] / 'index.sqlite')) as db, db:
            hashes = dict(db.execute('SELECT ordinal,rgb_sha256 FROM frames WHERE ordinal BETWEEN ? AND ?', (first,last)))
            anchor = db.execute('SELECT ordinal,pts FROM frames WHERE key_frame=1 AND ordinal<=? ORDER BY ordinal DESC LIMIT 1', (first,)).fetchone()
        accelerated = bool(session['seek_safe'] and anchor and anchor[0] > 0)
        for attempt in range(2 if accelerated else 1):
            seek = accelerated and attempt == 0
            offset = anchor[0] if seek else 0
            command = [session['tools']['ffmpeg']['path'], '-nostdin', '-hide_banner', '-loglevel', 'error', '-xerror', '-y',
                '-protocol_whitelist', 'file', '-format_whitelist', 'mov,matroska,webm,avi', '-err_detect', 'explode']
            if seek:
                seconds = Fraction(anchor[1]) * Fraction(session['time_base'])
                command += ['-copyts', '-seek_timestamp', '1', '-ss', format(float(seconds), '.9f')]
            command += ['-i', str(session['path']), '-map', '0:v:0', '-an']
            filters = (f"trim=start_pts={anchor[1]}," if seek else '') + f'select=between(n\\,{first-offset}\\,{last-offset}),setpts=N/TB,format=rgb24'
            command += ['-vf', filters, '-frames:v', str(last-first+1), '-fps_mode', 'passthrough', '-threads', '1',
                '-start_number', str(first), str(session['directory'] / 'frame-%d.png')]
            job['phase'] = '关键帧定位并逐张核对完整解码像素' if seek else '从起点解码并核对像素'
            job['decode_mode'] = 'verified-keyframe' if seek else 'verified-prefix'
            for name in names: (session['directory'] / name).unlink(missing_ok=True)
            try:
                self._process(job, session, command, limit=1024)
                valid = True
                for n, name in zip(range(first,last+1), names):
                    self._check(job, session)
                    path = session['directory'] / name
                    if not path.is_file() or path.stat().st_size > MAX_CACHE:
                        valid = False; break
                    with Image.open(path) as picture:
                        if picture.width * picture.height > MAX_PIXELS: raise UserError('静帧像素超限。', 413)
                        if hashlib.sha256(picture.convert('RGB').tobytes()).hexdigest() != hashes[n]:
                            valid = False; break
                if valid: return names
            except UserError as error:
                # Only decode/alignment errors permit the slower fallback.
                if not seek or error.status != 422: raise
            if not seek: raise UserError('静帧与完整顺序解码不一致，拒绝展示。', 422)
        raise UserError('无法核对目标静帧。', 422)

    def _extract(self, job, session):
        for tool in session['tools'].values():
            if fingerprint(Path(tool['path'])) != tool['fingerprint']:
                session['invalid'] = True
                raise UserError('解码工具已变化，请重新建立帧索引。', 409)
        self._hash(job, session)
        ordinal = job['ordinal']; count = session['count']
        with closing(sqlite3.connect(session['directory'] / 'index.sqlite')) as connection, connection:
            row = connection.execute('SELECT pts,best_pts,duration,key_frame FROM frames WHERE ordinal=?', (ordinal,)).fetchone()
        key = (session['id'], ordinal)
        if key not in self.cache:
            # Bound prefetch by raw pixel memory as well as compressed LRU size.
            pixels = session['stream']['width'] * session['stream']['height']
            ahead = max(1, min(PREFETCH_FRAMES, MAX_CACHE // max(1, pixels*3) // 2))
            first, last = max(0, ordinal-1), min(count-1, ordinal+ahead)
            names = self._decode_window(job, session, first, last)
            self._hash(job, session)
            with self.lock:
                for n, name in zip(range(first, last+1), names):
                    self.cache[(session['id'], n)] = (session['directory'] / name).stat().st_size
                self.cache.move_to_end(key)
                while sum(self.cache.values()) > MAX_CACHE or len(self.cache) > MAX_CACHE_ENTRIES:
                    (sid, n), _ = self.cache.popitem(last=False)
                    owner = self.sessions.get(sid)
                    if owner: self._unlink(owner, f'frame-{n}.png')
        self._check(job, session)
        pts = row[0] if row[0] is not None else row[1]
        relative = float((pts-session['first_pts'])*Fraction(session['time_base'])) if pts is not None and session['first_pts'] is not None else None
        path = session['directory'] / f'frame-{ordinal}.png'
        job['result'] = {'ordinal': ordinal, 'count': count, 'pts': str(row[0]) if row[0] is not None else None,
            'best_effort_pts': str(row[1]) if row[1] is not None else None, 'time_base': session['time_base'],
            'duration': str(row[2]) if row[2] is not None else None, 'key_frame': bool(row[3]),
            'decode_mode': job.get('decode_mode', 'verified-cache'),
            'source_sha256': session['sha256'], 'image_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'resume_seconds_approximate': relative, 'image_url': '/api/video/frames/jobs/' + job['id'] + '/image'}

    def _export(self, job, session):
        """Inclusive ordinal selection; validate all pixels and rational PTS."""
        self._hash(job, session)
        for tool in session['tools'].values():
            if fingerprint(Path(tool['path'])) != tool['fingerprint']:
                raise UserError('解码工具已变化，请重新建立帧索引。', 409)
        first, last = job['first'], job['last']
        with closing(sqlite3.connect(session['directory'] / 'index.sqlite')) as db, db:
            rows = db.execute('SELECT ordinal,pts,duration,rgb_sha256 FROM frames WHERE ordinal BETWEEN ? AND ? ORDER BY ordinal', (first,last)).fetchall()
        if any(row[1] is None for row in rows): raise UserError('范围含缺失 PTS，不能严格导出。', 422)
        times = [(row[1]-rows[0][1])*Fraction(session['time_base']) for row in rows]
        if any(b<a for a,b in zip(times,times[1:])): raise UserError('范围 PTS 倒退，不能严格导出。', 422)
        # MKV preserves repeated PTS but normally quantizes timestamps to ms.
        # NUT preserves other rational timelines, but rejects repeated PTS.
        container = 'mkv' if all((t*1000).denominator == 1 for t in times) else 'nut'
        if container == 'nut' and len(set(times)) != len(times):
            raise UserError('重复 PTS 不能在此时间基的严格容器中保留，拒绝改写时间戳。', 422)
        review = self.app.video_review
        with review.lock:
            if len(review.jobs) >= 100:
                raise UserError('导出历史已满，请先整理已有导出。', 409)
            directory = review._folder() / job['id']; directory.mkdir()
            job.update({'directory': directory, 'partial': directory / ('partial.'+container),
                'output': directory / ('clip.'+container), 'mode': 'frames', 'format': container,
                'start': float(times[0]), 'end': float(times[-1]), 'source_key': review.source_key(session['path'])})
            review.jobs[job['id']] = job
        published = False
        try:
            job['phase'] = '按源展示帧范围无损编码（不含音频）'
            self._process(job, session, [session['tools']['ffmpeg']['path'], '-nostdin', '-v', 'error', '-xerror', '-n', '-copyts',
                '-protocol_whitelist', 'file', '-format_whitelist', 'mov,matroska,webm,avi', '-err_detect', 'explode',
                '-i', str(session['path']), '-map', '0:v:0', '-an', '-vf',
                f'select=between(n\\,{first}\\,{last}),setpts=PTS-STARTPTS,format=bgr0',
                '-frames:v', str(len(rows)), '-c:v', 'ffv1', '-level', '3', '-threads', '1',
                '-fps_mode', 'passthrough', '-enc_time_base', 'filter', str(job['partial'])], limit=1024)
            job['phase'] = '复验导出每帧像素与有理 PTS'
            metadata = json.loads(self._process(job, session, [session['tools']['ffprobe']['path'], '-v', 'error',
                '-select_streams', 'v:0', '-show_entries', 'stream=time_base:frame=pts', '-of', 'json', str(job['partial'])], limit=8*1024**2))
            output_tb = Fraction(metadata['streams'][0]['time_base'])
            actual_times = [int(frame['pts'])*output_tb for frame in metadata['frames']]
            if actual_times != times: raise UserError('输出时间戳与源帧不一致，已拒绝此导出。', 422)
            hashes = []
            def consume(reader, final):
                while True:
                    self._check(job, session)
                    at=reader.tell(); line=reader.readline(4097)
                    if not line: break
                    if len(line)>4096: raise UserError('导出校验行超限。', 413)
                    if not line.endswith(b'\n') and not final: reader.seek(at); break
                    text=line.decode('ascii').strip()
                    if text and not text.startswith('#'): hashes.append(text.split(',')[-1].strip())
                    if len(hashes)>len(rows): raise UserError('导出帧数不一致。', 422)
            self._process(job, session, [session['tools']['ffmpeg']['path'], '-nostdin', '-v', 'error', '-xerror',
                '-i', str(job['partial']), '-map', '0:v:0', '-an', '-vf', 'format=rgb24,setpts=N',
                '-fps_mode', 'passthrough', '-c:v', 'rawvideo', '-threads', '1', '-f', 'framehash', '-hash', 'sha256', 'pipe:1'], consume=consume)
            if hashes != [row[3] for row in rows]: raise UserError('输出像素或帧数与源范围不一致，已拒绝此导出。', 422)
            self._hash(job, session); self._check(job, session)
            manifest = {'schema': 'yingxu-source-frames-v1', 'first_inclusive': first, 'last_inclusive': last,
                'count': len(rows), 'source_sha256': session['sha256'], 'source_time_base': session['time_base'],
                'output_time_base': str(output_tb), 'container': container, 'codec': 'ffv1', 'pixel_format': 'bgr0',
                'audio': 'none', 'display_rotation_applied': True, 'timeline': 'source PTS minus first selected PTS; exact rational equality verified',
                'last_frame_duration': 'container default; source duration recorded per frame, not an exact end-time promise',
                'frames': [{'ordinal': row[0], 'source_pts': str(row[1]), 'source_duration': None if row[2] is None else str(row[2]),
                    'rgb_sha256': row[3], 'output_pts': str(metadata['frames'][i]['pts'])} for i,row in enumerate(rows)]}
            with self.lock, review.lock:
                self._check(job, session)
                (directory / 'frames.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
                receipt = {key: job[key] for key in ('id', 'source_key', 'created', 'start', 'end', 'mode', 'format')}
                (directory / 'export.json').write_text(json.dumps(receipt), encoding='utf-8')
                os.replace(job['partial'], job['output'])
                job['result'] = {'first': first, 'last': last, 'count': len(rows), 'audio': 'none', 'format': container,
                    'download_url': '/api/video/exports/'+job['id']+'/file',
                    'manifest_url': '/api/video/frames/jobs/'+job['id']+'/manifest'}
                published = True
        finally:
            job['partial'].unlink(missing_ok=True)
            if not published:
                for name in ('frames.json', 'export.json'): (directory / name).unlink(missing_ok=True)
                directory.rmdir()

    def _run(self, job, session):
        try:
            with ExitStack() as stack:
                stack.enter_context(self.app.update_service.mutation('POST', '/api/video/frames'))
                stack.enter_context(self.app.migration_jobs.mutation('POST', '/api/video/frames'))
                self._check(job, session); job['state'] = 'running'; job['started'] = time.monotonic()
                if job['kind'] == 'index': self._index(job, session)
                elif job['kind'] == 'export': self._export(job, session)
                else: self._extract(job, session)
                if job['kind'] != 'export': self._check(job, session)
                terminal = 'ready' if job['kind'] == 'index' else 'done'; job['phase'] = '完成'
        except Cancelled:
            terminal = 'cancelled'; job['phase'] = '已取消'
        except Exception as error:
            terminal = 'error'; job['error'] = str(error) if isinstance(error, UserError) else '帧检查失败，请重新打开；原片不变。'
        finally:
            with self.lock:
                job['state'] = terminal
                if job['kind'] == 'frame' and job['state'] != 'done':
                    for name in job.get('created_frames', []):
                        self.cache.pop((session['id'], int(name[6:-4])), None)
                        try: self._unlink(session, name)
                        except OSError: self._note_cleanup_error(job, '部分本任务帧缓存清理未完成。')
                if job['kind'] == 'index' and job['state'] != 'ready': session['cancel'].set()
                if session['invalid']: session['cancel'].set()
                if session['cancel'].is_set() and not any(j['session'] == session['id'] and j['state'] in ACTIVE for j in self.jobs.values()):
                    self._cleanup(session)

    def _cleanup(self, session):
        for key in list(self.cache):
            if key[0] == session['id']: del self.cache[key]
        try:
            for name in session['owned']: (session['directory'] / name).unlink(missing_ok=True)
            (session['directory'] / 'owner.json').unlink(missing_ok=True)
            session['directory'].rmdir()
        except OSError:
            self._note_cleanup_error(self.jobs[session['id']], '部分帧缓存清理未完成，未删除任何未知文件。')
        self.sessions.pop(session['id'], None)

    @staticmethod
    def _note_cleanup_error(job, message):
        original = job.get('error')
        job['error'] = f'{original}（{message}）' if original else message

    def close(self):
        with self.lock:
            self.closed = True
            for session in self.sessions.values(): session['cancel'].set()
            for job in self.jobs.values(): job['cancel'].set()
        self.pool.shutdown(wait=True)
        with self.lock:
            for session in list(self.sessions.values()): self._cleanup(session)
