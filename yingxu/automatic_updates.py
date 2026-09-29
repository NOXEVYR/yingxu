"""Low-frequency automatic checks and bounded Windows pre-downloads."""
import json
import re
import sys
import threading
import time

from .incremental_update import checked_path, write_file

DAILY_INTERVAL = 24 * 60 * 60
BACKOFF_BASE = 60 * 60
BACKOFF_MAX = 7 * DAILY_INTERVAL
MAX_AUTOMATIC_DOWNLOAD = 50 * 1024 * 1024
DEFAULT_DELAY = 8
WORKER_JOIN_TIMEOUT = 17
STATE_FILE = 'updates/automatic/state.json'
MAX_STATE_BYTES = 32 * 1024

_DEFAULT_STATE = {
    'schema': 1,
    'state': 'idle',
    'last_attempt': 0,
    'next_attempt': 0,
    'consecutive_failures': 0,
    'latest_version': '',
    'release_url': '',
    'update_available': None,
    'plan_id': '',
    'download_bytes': 0,
    'message': '尚未自动检查更新。',
}
_STATES = {'idle', 'disabled', 'checking', 'planning', 'available', 'downloading',
           'ready', 'current', 'manual_required', 'error'}


class AutomaticUpdates:
    """Schedule one delayed check per UI-ready trigger and persist its cadence."""

    def __init__(self, update_service, *, clock=time.time):
        self.update_service = update_service
        self.data_root = update_service.app.store.data_root
        self._clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._force_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker = None
        self._active = False
        self._persistence_error = ''
        self._state = self._load_state()

    def _load_state(self):
        try:
            path = checked_path(self.data_root, STATE_FILE)
            if not path.exists():
                return dict(_DEFAULT_STATE)
            if not path.is_file() or path.stat().st_size > MAX_STATE_BYTES or path.stat().st_nlink != 1:
                raise ValueError('state file is not an independent bounded file')
            raw = path.read_bytes()
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError('state file is too large')
            value = json.loads(raw.decode('utf-8'))
            if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('state') not in _STATES or
                    type(value.get('last_attempt')) is not int or value['last_attempt'] < 0 or
                    type(value.get('next_attempt')) is not int or value['next_attempt'] < 0 or
                    type(value.get('consecutive_failures')) is not int or not 0 <= value['consecutive_failures'] <= 16 or
                    not isinstance(value.get('latest_version'), str) or len(value['latest_version']) > 64 or
                    not isinstance(value.get('release_url'), str) or len(value['release_url']) > 512 or
                    value.get('update_available') not in (None, True, False) or
                    not isinstance(value.get('plan_id'), str) or
                    (value['plan_id'] and not re.fullmatch(r'[a-f0-9]{32}', value['plan_id'])) or
                    type(value.get('download_bytes')) is not int or value['download_bytes'] < 0 or
                    not isinstance(value.get('message'), str) or len(value['message']) > 1000):
                raise ValueError('state fields are invalid')
            return {**_DEFAULT_STATE, **value}
        except FileNotFoundError:
            return dict(_DEFAULT_STATE)
        except Exception:
            self._persistence_error = '自动更新状态记录无法安全读取；原文件已保留，自动检查已停用。'
            state = dict(_DEFAULT_STATE)
            state.update(state='error', message=self._persistence_error)
            return state

    def _save_locked(self):
        encoded = (json.dumps(self._state, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')) + '\n').encode('utf-8')
        if len(encoded) > MAX_STATE_BYTES:
            raise ValueError('automatic update state exceeds its size bound')
        write_file(self.data_root, STATE_FILE, encoded)

    def _set(self, **fields):
        with self._lock:
            if self._stopping():
                return False
            self._state.update(fields)
            try:
                self._save_locked()
            except Exception:
                self._persistence_error = '自动更新状态无法安全保存；已停止自动检查并保留现有记录。'
                self._state.update(state='error', message=self._persistence_error)
                return False
            return True

    def status(self):
        with self._lock:
            value = dict(self._state)
            worker = self._worker
            scheduled = bool(worker and worker.is_alive())
            running = self._active
            persistence_error = self._persistence_error
        try:
            prefs = self.update_service.app.settings.get()
            check_enabled = prefs['automatic_update_check']
            download_enabled = prefs['automatic_update_download']
        except Exception:
            check_enabled = download_enabled = False
        if persistence_error:
            value.update(state='error', message=persistence_error)
        elif value.get('state') == 'ready':
            current = getattr(self.update_service, 'current_version', '')
            if not self._version_is_newer(value.get('latest_version', ''), current):
                value.update(state='current', update_available=False, plan_id='', download_bytes=0,
                             message='当前已是最新正式版。')
            else:
                try:
                    manager_status = self.update_service._manager().status()
                except Exception:
                    manager_status = {}
                if (manager_status.get('state') != 'ready' or
                        manager_status.get('plan_id') != value.get('plan_id') or
                        manager_status.get('latest_version') != value.get('latest_version')):
                    value.update(state='available', plan_id='', download_bytes=0,
                                 message='发现新版本；正在重新核对此前下载的更新文件。')
        return {**value,
                'automatic_check_enabled': check_enabled,
                'automatic_download_enabled': download_enabled,
                'platform': sys.platform,
                'running': running,
                'scheduled': scheduled,
                'download_limit_bytes': MAX_AUTOMATIC_DOWNLOAD,
                'install_automatically': False}

    @staticmethod
    def _version_key(version):
        if not isinstance(version, str):
            return None
        match = re.fullmatch(r'(\d{1,4})\.(\d{1,4})\.(\d{1,4})', version)
        return tuple(map(int, match.groups())) if match else None

    @classmethod
    def _version_is_newer(cls, candidate, current):
        candidate_key = cls._version_key(candidate)
        current_key = cls._version_key(current)
        return candidate_key is not None and current_key is not None and candidate_key > current_key

    def _preferences(self):
        return self.update_service.app.settings.get()

    def _stopping(self):
        return self._stop.is_set() or bool(getattr(self.update_service, 'closed', False))

    def _can_run(self, force):
        if self._persistence_error:
            return False
        if force:
            return True
        now = int(self._clock())
        with self._lock:
            last_attempt = self._state['last_attempt']
            next_attempt = self._state['next_attempt']
        return now >= next_attempt and (last_attempt == 0 or now - last_attempt >= DAILY_INTERVAL)

    def _needs_resume(self):
        with self._lock:
            state = dict(self._state)
        if state.get('state') != 'ready' or not self._version_is_newer(
                state.get('latest_version', ''), getattr(self.update_service, 'current_version', '')):
            return False
        manager_status = self.update_service._manager().status()
        return (manager_status.get('state') != 'ready' or
                manager_status.get('plan_id') != state.get('plan_id') or
                manager_status.get('latest_version') != state.get('latest_version'))

    def start(self, delay=DEFAULT_DELAY, *, force=False):
        """Start a single background job; callers invoke this after UI readiness."""
        if type(delay) not in (int, float) or not 0 <= delay <= 300:
            raise ValueError('automatic update delay must be between 0 and 300 seconds')
        with self._lock:
            if self._stop.is_set():
                return self.status()
            if self._worker and self._worker.is_alive():
                if force:
                    self._force_event.set()
                    self._wake_event.set()
                else:
                    self._wake_event.set()
                return self.status()
            if force:
                self._force_event.set()
            self._worker = threading.Thread(target=self._run, args=(float(delay),),
                                            name='yingxu-automatic-updates', daemon=True)
            self._worker.start()
        return self.status()

    def check_now(self, force=True):
        """Queue an explicit user-requested automatic update check immediately."""
        return self.start(0, force=bool(force))

    def _failure(self, error):
        with self._lock:
            failures = min(16, self._state['consecutive_failures'] + 1)
            delay = min(BACKOFF_BASE * (2 ** min(failures - 1, 8)), BACKOFF_MAX)
            attempted = self._state['last_attempt'] or int(self._clock())
        message = str(error).strip()[:800] or '自动检查更新失败，请稍后重试。'
        self._set(state='error', consecutive_failures=failures,
                  next_attempt=attempted + delay, message=message)

    def _run(self, delay):
        if delay and self._stop.wait(delay):
            return
        while not self._stop.is_set():
            force = self._force_event.is_set()
            if force:
                self._force_event.clear()
            try:
                prefs = self._preferences()
                if not force and not prefs['automatic_update_check']:
                    self._set(state='disabled', message='自动更新检查已关闭。')
                    self._wait_for_wake(60)
                    continue
                resume = not force and self._needs_resume()
                if not self._can_run(force) and not resume:
                    self._wait_until_due()
                    continue
                with self._lock:
                    self._active = True
                try:
                    self._check_once(force or resume)
                except Exception as error:
                    self._failure(error)
                finally:
                    with self._lock:
                        self._active = False
            except Exception as error:
                self._failure(error)

    def _wait_for_wake(self, seconds):
        self._wake_event.wait(seconds)
        self._wake_event.clear()

    def _wait_until_due(self):
        now = int(self._clock())
        with self._lock:
            last_attempt = self._state['last_attempt']
            next_attempt = self._state['next_attempt']
        due = max(next_attempt, last_attempt + DAILY_INTERVAL if last_attempt else now)
        self._wait_for_wake(max(1, min(60, due - now)))

    def _check_once(self, force):
        try:
            prefs = self._preferences()
            if not force and not prefs['automatic_update_check']:
                self._set(state='disabled', message='自动更新检查已关闭。')
                return
            if not self._can_run(force):
                return
            attempted = int(self._clock())
            if not self._set(state='checking', last_attempt=attempted,
                             message='正在后台检查官方更新。', plan_id='', download_bytes=0):
                return
            from .updates import check_update
            result = check_update()
            # check_update can block for several bounded HTTP requests. Shutdown
            # may time out its join while that call is in flight; after it
            # returns, do not write status or continue into incremental planning.
            if self._stopping():
                return
            latest = str(result.get('latest_version', ''))[:64]
            update_available = bool(result.get('update_available'))
            release_url = str(result.get('url', ''))[:512]
            common = dict(last_attempt=attempted, next_attempt=0, consecutive_failures=0,
                          latest_version=latest, release_url=release_url,
                          update_available=update_available)
            if not update_available:
                self._set(**common, state='current', message='当前已是最新正式版。')
                return
            if sys.platform != 'win32':
                self._set(**common, state='available',
                          message='发现新版本；此平台需下载完整包并手动安装。')
                return
            prefs = self._preferences()
            if not prefs['automatic_update_download']:
                self._set(**common, state='available', message='发现新版本；自动下载已关闭。')
                return
            self._prepare_windows_download(common)
        except Exception as error:
            self._failure(error)

    def _wait_manager(self, timeout):
        manager = self.update_service._manager()
        deadline = time.monotonic() + timeout
        while not self._stop.is_set() and time.monotonic() < deadline:
            status = manager.status()
            if status.get('state') not in ('planning', 'downloading'):
                return status
            self._stop.wait(0.2)
        if self._stop.is_set():
            return manager.status()
        raise TimeoutError('增量更新检查超时。')

    def _prepare_windows_download(self, common):
        if self._stopping():
            return
        service = self.update_service
        manager = service._manager()
        before = manager.status()
        if self._stopping():
            return
        target_version = common.get('latest_version')
        refreshed = False
        if (before.get('state') == 'ready' and
                before.get('latest_version') == target_version and
                self._version_is_newer(target_version, getattr(service, 'current_version', ''))):
            self._set(**common, state='ready', plan_id=before.get('plan_id', ''),
                      download_bytes=before.get('total_download_bytes', 0),
                      message='增量更新文件已准备好；可在设置中查看并选择安装。')
            return
        if before.get('state') in ('planning', 'downloading'):
            status = self._wait_manager(1810)
        elif before.get('state') == 'planned' and before.get('latest_version') == target_version:
            status = before
        else:
            service.plan()
            refreshed = True
            self._set(**common, state='planning', message='正在核对安全的增量更新计划。')
            status = self._wait_manager(660)
        if self._stop.is_set():
            return

        # A manual or background plan can finish after the official metadata
        # check. Match its target release before trusting its size or plan id.
        if (status.get('state') in ('planned', 'ready') and
                status.get('latest_version') != target_version and not refreshed):
            service.plan()
            refreshed = True
            self._set(**common, state='planning', plan_id='', download_bytes=0,
                      message='已有增量计划目标版本已变化，正在按最新正式版重新核对。')
            status = self._wait_manager(660)
            if self._stop.is_set():
                return
        if status.get('state') == 'ready':
            if status.get('latest_version') != target_version:
                self._set(**common, state='manual_required', plan_id='',
                          message='增量计划仍指向其他版本，已阻止自动下载；请重新检查更新。')
                return
            self._set(**common, state='ready', plan_id=status.get('plan_id', ''),
                      download_bytes=status.get('total_download_bytes', 0),
                      message='增量更新文件已准备好；可在设置中查看并选择安装。')
            return
        if status.get('state') != 'planned':
            self._set(**common, state='manual_required', plan_id='',
                      message='发现新版本，但无法生成安全的增量计划；请从设置查看更新详情。')
            return
        if status.get('latest_version') != target_version:
            self._set(**common, state='manual_required', plan_id='',
                      message='增量计划仍指向其他版本，已阻止自动下载；请重新检查更新。')
            return
        plan_id = status.get('plan_id', '')
        download_bytes = status.get('total_download_bytes')
        if (not isinstance(plan_id, str) or not re.fullmatch(r'[a-f0-9]{32}', plan_id) or
                type(download_bytes) is not int or download_bytes < 0):
            self._set(**common, state='manual_required',
                      message='增量更新计划信息不完整；请从设置查看更新详情。')
            return
        if download_bytes > MAX_AUTOMATIC_DOWNLOAD:
            self._set(**common, state='manual_required', plan_id=plan_id,
                      download_bytes=download_bytes,
                      message='更新超过 50 MiB 自动下载上限，需要你在设置中确认下载。')
            return
        prefs = service.app.settings.get()
        if self._stopping():
            return
        if not prefs['automatic_update_check']:
            self._set(**common, state='disabled', plan_id=plan_id,
                      download_bytes=download_bytes,
                      message='自动更新检查已关闭；增量计划已就绪，没有开始下载。')
            return
        if not prefs['automatic_update_download']:
            self._set(**common, state='available', plan_id=plan_id,
                      download_bytes=download_bytes, message='自动下载已关闭；增量计划已就绪。')
            return
        if self._stopping():
            return
        self._set(**common, state='downloading', plan_id=plan_id,
                  download_bytes=download_bytes, message='正在后台下载并校验小于等于 50 MiB 的变化文件。')
        service.download(plan_id)
        status = self._wait_manager(1810)
        if self._stop.is_set():
            return
        if status.get('state') == 'ready':
            self._set(**common, state='ready', plan_id=plan_id,
                      download_bytes=status.get('total_download_bytes', download_bytes),
                      message='增量文件已下载并校验；安装仍需你保存文稿并确认退出。')
        else:
            self._failure(status.get('message') or '自动增量下载未能完成。')

    def close(self):
        self._stop.set()
        self._force_event.set()
        self._wake_event.set()
        with self._lock:
            worker = self._worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=WORKER_JOIN_TIMEOUT)
