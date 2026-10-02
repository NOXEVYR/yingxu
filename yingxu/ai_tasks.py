"""Local creative task ledger. No model execution, watcher or external copying."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import threading

from .disk_layout import register_directory
from .handoffs import _identity, _private_asset, _clean_text
from .project_layout import category_paths
from .store import UserError, clean_path, now, uid, TEXT_LIMIT

MAX_FILE_BYTES = 8 * 1024 ** 3


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fields(data, allowed):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise UserError('请求字段无效或含有不支持的字段。')
    return data


def text(value, label, limit=4000, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise UserError(f'{label}无效或过长。')
    return _clean_text(value, limit)


def string_list(value, label, maximum=100, limit=2000):
    if not isinstance(value, list) or len(value) > maximum:
        raise UserError(f'{label}数量超出限制。')
    return [text(v, label, limit, True) for v in value]


def page(limit=50, offset=0):
    if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200
            or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
        raise UserError('分页参数无效。')
    return limit, offset


def entity_delta(before, current, key):
    old = {entry[key]: entry for entry in before}
    new = {entry[key]: entry for entry in current}
    return {
        'added': [new[i] for i in new if i not in old],
        'changed': [{'id': i, 'before': old[i], 'current': new[i]} for i in new if i in old and new[i] != old[i]],
        'removed_from_scope': [old[i] for i in old if i not in new],
    }


def stamp(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def hash_file(path):
    """Stream outside Store.lock; reject writes/replacements during the read."""
    path = clean_path(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise UserError('只支持独立普通文件，不能接收链接。', 403)
    before = stamp(path)
    if before[2] > MAX_FILE_BYTES:
        raise UserError('单个成果超过 8 GiB，请拆分后核验。', 413)
    digest = hashlib.sha256()
    consumed = 0
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            consumed += len(block)
            if consumed > before[2]:
                raise UserError('文件仍在增长，请写入完成后重新核验。', 409)
            digest.update(block)
    clean_path(path)
    if stamp(path) != before:
        raise UserError('文件在核验期间发生变化，请写入完成后重试。', 409)
    return digest.hexdigest(), before


class AITaskService:
    def __init__(self, store, skills=None):
        self.store = store
        self.skills = skills
        self.operation_lock = threading.RLock()
        with store.lock, store.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS ai_tasks(
              id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
              title TEXT NOT NULL,kind TEXT NOT NULL,goal TEXT NOT NULL,acceptance TEXT NOT NULL,
              status TEXT NOT NULL,revision INTEGER NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ai_tasks_project ON ai_tasks(project_id,created,id);
            CREATE TABLE IF NOT EXISTS ai_task_create_requests(
              project_id TEXT NOT NULL REFERENCES projects(id),idempotency_key TEXT NOT NULL,
              task_id TEXT NOT NULL REFERENCES ai_tasks(id),payload_json TEXT NOT NULL,
              PRIMARY KEY(project_id,idempotency_key));
            CREATE TABLE IF NOT EXISTS ai_runs(
              id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES ai_tasks(id),run_number INTEGER NOT NULL,
              previous_run_id TEXT,client_id TEXT NOT NULL,conversation_id TEXT NOT NULL,
              input_json TEXT NOT NULL,input_digest TEXT NOT NULL,directories TEXT NOT NULL,
              status TEXT NOT NULL,revision INTEGER NOT NULL,created TEXT NOT NULL,
              UNIQUE(task_id,run_number));
            CREATE TRIGGER IF NOT EXISTS ai_run_input_immutable BEFORE UPDATE OF input_json,input_digest ON ai_runs
              BEGIN SELECT RAISE(ABORT,'run inputs are immutable'); END;
            CREATE TABLE IF NOT EXISTS ai_artifacts(
              id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES ai_tasks(id),run_id TEXT NOT NULL REFERENCES ai_runs(id),
              item_id TEXT NOT NULL REFERENCES items(id),role TEXT NOT NULL,observed_sha256 TEXT NOT NULL,
              verification_status TEXT NOT NULL,review_status TEXT NOT NULL,review_sha256 TEXT NOT NULL DEFAULT '',
              review_notes TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL,created TEXT NOT NULL,
              UNIQUE(run_id,item_id,role));
            CREATE INDEX IF NOT EXISTS ai_artifacts_run ON ai_artifacts(task_id,run_id);
            CREATE TABLE IF NOT EXISTS ai_reviews(
              id TEXT PRIMARY KEY,artifact_id TEXT NOT NULL REFERENCES ai_artifacts(id),decision TEXT NOT NULL,
              sha256 TEXT NOT NULL,notes TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_task_handoffs(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
              task_id TEXT NOT NULL REFERENCES ai_tasks(id),run_id TEXT NOT NULL REFERENCES ai_runs(id),
              client_id TEXT NOT NULL,conversation_id TEXT NOT NULL,mode TEXT NOT NULL,
              base_snapshot_id TEXT,snapshot_json TEXT NOT NULL,result_json TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_task_baselines(
              task_id TEXT NOT NULL,client_id TEXT NOT NULL,conversation_id TEXT NOT NULL,
              snapshot_id TEXT NOT NULL,updated TEXT NOT NULL,PRIMARY KEY(task_id,client_id,conversation_id));
            CREATE TABLE IF NOT EXISTS ai_task_acks(snapshot_id TEXT PRIMARY KEY,created TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS ai_task_handoff_immutable BEFORE UPDATE ON ai_task_handoffs
              BEGIN SELECT RAISE(ABORT,'task handoffs are immutable'); END;
            ''')
        from .ai_receipts import AIReceiptService
        self.receipts = AIReceiptService(self)

    def _task(self, db, task_id, writable=False):
        row = db.execute('SELECT * FROM ai_tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise UserError('AI 任务不存在。', 404)
        result = dict(row)
        self.store._project(db, result['project_id'])
        if writable and result['status'] in ('已完成', '已归档'):
            raise UserError('任务已完成或归档，不能修改。', 409)
        result['acceptance'] = json.loads(result['acceptance'])
        return result

    @staticmethod
    def _expected(entity, data):
        expected = data.get('expected_revision')
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != entity['revision']:
            raise UserError('任务或成果已变化，请刷新后重试。', 409)

    def _run(self, db, task_id, run_id):
        self._task(db, task_id)
        row = db.execute('SELECT * FROM ai_runs WHERE id=? AND task_id=?', (run_id, task_id)).fetchone()
        if row is None:
            raise UserError('轮次不属于此任务。', 404)
        result = dict(row)
        result['input_snapshot'] = json.loads(result.pop('input_json'))
        result['directories'] = json.loads(result['directories'])
        task = self._task(db, task_id)
        project = self.store._project(db, task['project_id'])
        for category, directory in result['directories'].items():
            folder = db.execute('SELECT relative_path,category FROM folders WHERE id=? AND project_id=?', (directory['folder_id'], project['id'])).fetchone()
            if folder and folder['category'] == category:
                directory['relative_path'] = folder['relative_path']
            directory['path'] = str(Path(project['root']) / directory['relative_path'])
        return result

    def create_task(self, data):
        fields(data, ('project_id', 'title', 'kind', 'goal', 'acceptance', 'idempotency_key'))
        pid = _identity(data.get('project_id'), '项目 ID')
        title = text(data.get('title'), '任务标题', 200, True)
        kind = data.get('kind', 'general')
        if kind not in ('general', 'video', 'skill_test'):
            raise UserError('任务类型无效。')
        goal = text(data.get('goal', ''), '任务目标', 100000)
        acceptance = string_list(data.get('acceptance', []), '验收项')
        key = _identity(data['idempotency_key'], '创建幂等标识', 128) if 'idempotency_key' in data else None
        payload = canonical({'project_id': pid, 'title': title, 'kind': kind, 'goal': goal, 'acceptance': acceptance})
        tid = uid()
        with self.store.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.store._project(db, pid)
            existing = db.execute('SELECT task_id,payload_json FROM ai_task_create_requests WHERE project_id=? AND idempotency_key=?', (pid, key)).fetchone() if key is not None else None
            if existing:
                if existing['payload_json'] != payload: raise UserError('同一创建幂等标识不能用于不同任务内容。', 409)
                return self.get_task(existing['task_id'])
            if db.execute('SELECT count(*) FROM ai_tasks WHERE project_id=?', (pid,)).fetchone()[0] >= 500:
                raise UserError('每个项目最多 500 个任务。', 409)
            db.execute('INSERT INTO ai_tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (tid, pid, title, kind, goal, canonical(acceptance), '待开始', 1, now(), now()))
            if key is not None:
                db.execute('INSERT INTO ai_task_create_requests VALUES(?,?,?,?)', (pid, key, tid, payload))
        return self.get_task(tid)

    def list_tasks(self, project_id, limit=50, offset=0):
        page(limit, offset)
        with self.store.connection() as db:
            self.store._project(db, project_id)
            total = db.execute('SELECT count(*) FROM ai_tasks WHERE project_id=?', (project_id,)).fetchone()[0]
            ids = [r[0] for r in db.execute('SELECT id FROM ai_tasks WHERE project_id=? ORDER BY created DESC,id LIMIT ? OFFSET ?', (project_id, limit, offset))]
            tasks = [self._task_summary(db, tid) for tid in ids]
        return {'tasks': tasks, 'total': total}

    def _task_summary(self, db, task_id):
        row = db.execute('SELECT id,project_id,title,kind,status,revision,created,updated FROM ai_tasks WHERE id=?', (task_id,)).fetchone()
        if row is None: raise UserError('AI 任务不存在。', 404)
        self.store._project(db, row['project_id'])
        return dict(row)

    def task_summary(self, task_id):
        with self.store.connection() as db:
            return self._task_summary(db, task_id)

    def update_task(self, task_id, data):
        fields(data, ('title', 'goal', 'acceptance', 'expected_revision', 'archived'))
        if 'archived' in data and data['archived'] is not True:
            raise UserError('归档选项必须明确为 true。')
        with self.operation_lock, self.store.lock, self.store.connection() as db:
            task = self._task(db, task_id, True)
            self._expected(task, data)
            title = text(data.get('title', task['title']), '任务标题', 200, True)
            goal = text(data.get('goal', task['goal']), '任务目标', 100000)
            acceptance = string_list(data.get('acceptance', task['acceptance']), '验收项')
            db.execute('UPDATE ai_tasks SET title=?,goal=?,acceptance=?,status=?,revision=revision+1,updated=? WHERE id=?',
                       (title, goal, canonical(acceptance), '已归档' if data.get('archived') else task['status'], now(), task_id))
        return self.get_task(task_id)

    def get_task(self, task_id):
        with self.store.connection() as db:
            task = self._task(db, task_id)
            task['runs'] = [dict(r) for r in db.execute('SELECT id,task_id,run_number,previous_run_id,client_id,conversation_id,status,revision,created FROM ai_runs WHERE task_id=? ORDER BY run_number DESC', (task_id,))]
            task['artifacts'] = [{**dict(r), 'sha256': r['observed_sha256']} for r in db.execute('SELECT a.*,i.name,i.kind,i.category,i.removed FROM ai_artifacts a JOIN items i ON i.id=a.item_id WHERE a.task_id=? ORDER BY a.rowid DESC LIMIT 200', (task_id,))]
            task['artifacts_total'] = db.execute('SELECT count(*) FROM ai_artifacts WHERE task_id=?', (task_id,)).fetchone()[0]
            task['receipts'] = [dict(r) for r in db.execute('SELECT id,run_id,state,created,updated FROM ai_receipts WHERE task_id=? ORDER BY rowid DESC LIMIT 200', (task_id,))]
            task['receipts_total'] = db.execute('SELECT count(*) FROM ai_receipts WHERE task_id=?', (task_id,)).fetchone()[0]
        return task

    def list_runs(self, task_id, limit=50, offset=0):
        page(limit, offset)
        with self.store.connection() as db:
            self._task(db, task_id)
            total = db.execute('SELECT count(*) FROM ai_runs WHERE task_id=?', (task_id,)).fetchone()[0]
            runs = [dict(r) for r in db.execute('SELECT id,task_id,run_number,previous_run_id,client_id,conversation_id,status,revision,created FROM ai_runs WHERE task_id=? ORDER BY run_number DESC LIMIT ? OFFSET ?', (task_id, limit, offset))]
        return {'runs': runs, 'total': total}

    def get_run(self, task_id, run_id):
        with self.store.connection() as db:
            run = self._run(db, task_id, run_id)
            run['artifacts'] = [{**dict(r), 'sha256': r['observed_sha256']} for r in db.execute('SELECT a.*,i.name,i.kind,i.category,i.removed FROM ai_artifacts a JOIN items i ON i.id=a.item_id WHERE a.task_id=? AND a.run_id=? ORDER BY a.created,a.id LIMIT 200', (task_id, run_id))]
            run['receipts'] = [dict(r) for r in db.execute('SELECT id,run_id,state,created,updated FROM ai_receipts WHERE task_id=? AND run_id=? ORDER BY rowid DESC LIMIT 100', (task_id, run_id))]
            run['handoffs'] = [dict(r) for r in db.execute('SELECT h.id,h.client_id,h.conversation_id,h.mode,h.created,(a.snapshot_id IS NOT NULL) AS acknowledged FROM ai_task_handoffs h LEFT JOIN ai_task_acks a ON a.snapshot_id=h.id WHERE h.task_id=? AND h.run_id=? ORDER BY h.seq DESC LIMIT 200', (task_id, run_id))]
            return run

    def _safe_item(self, project, item_id):
        item = self.store.get_item(item_id)
        if item['project_id'] != project['id'] or _private_asset(item, self.store.data_root, project['root']):
            raise UserError('不能使用跨项目或私密文件。', 403)
        path = self.store.resolve_item_path(item)
        if path.stat().st_nlink != 1:
            raise UserError('不能使用硬链接文件。', 403)
        return item, path

    def _directories(self, project, task_id, run_id):
        root = clean_path(project['root'])
        result = {}
        for category in ('generated', 'references'):
            base = root / category_paths(project)[category]
            current = root
            for part in base.relative_to(root).parts:
                current = current / part
                if not current.exists(): current.mkdir()
                clean_path(current)
            for part in ('AI任务', 'T-' + task_id, 'R-' + run_id):
                current = current / part
                if not current.exists(): current.mkdir()
                clean_path(current)
            register_directory(self.store, project, current)
            with self.store.connection() as db:
                folder = db.execute('SELECT id,removed FROM folders WHERE project_id=? AND relative_path=?', (project['id'], current.relative_to(root).as_posix())).fetchone()
                if folder is None or folder['removed']:
                    raise UserError('轮次目录已移入回收站。', 409)
            result[category] = {'path': str(current), 'folder_id': folder['id'], 'relative_path': current.relative_to(root).as_posix()}
        return result

    def freeze_run(self, task_id, data):
        fields(data, ('goal', 'acceptance', 'input_item_ids', 'skill_pins', 'client_id', 'conversation_id', 'expected_revision'))
        with self.operation_lock:
            with self.store.connection() as db:
                task = self._task(db, task_id, True)
                self._expected(task, data)
                project = self.store._project(db, task['project_id'])
                previous = db.execute('SELECT id,run_number FROM ai_runs WHERE task_id=? ORDER BY run_number DESC LIMIT 1', (task_id,)).fetchone()
            number = previous['run_number'] + 1 if previous else 1
            if number > 200: raise UserError('每个任务最多 200 轮。', 409)
            goal = text(data.get('goal', task['goal']), '本轮目标', 100000, True)
            acceptance = string_list(data.get('acceptance', task['acceptance']), '验收项')
            client = _identity(data.get('client_id'), '客户端身份')
            conversation = _identity(data.get('conversation_id'), '会话身份')
            ids = string_list(data.get('input_item_ids', []), '输入文件 ID', 200, 256)
            if len(set(ids)) != len(ids): raise UserError('输入文件不能重复。')
            inputs = []
            input_checks = []
            for iid in ids:
                item, path = self._safe_item(project, iid)
                info = path.stat()
                entry = {'item_id': iid, 'name': item['name'], 'kind': item['kind'], 'size': info.st_size, 'mtime_ns': info.st_mtime_ns, 'verification': 'metadata_only', 'sha256': ''}
                if item['kind'] in ('text', 'markdown', 'svg', 'html', 'excalidraw') and info.st_size <= TEXT_LIMIT:
                    entry['sha256'], observed = hash_file(path)
                    entry.update(size=observed[2], mtime_ns=observed[3])
                    entry['verification'] = 'sha256'
                else: observed = stamp(path)
                input_checks.append((iid, path, observed))
                inputs.append(entry)
            pins = data.get('skill_pins', [])
            if not isinstance(pins, list) or len(pins) > 32: raise UserError('每轮最多 32 个固定技能。')
            frozen = []
            for pin in pins:
                fields(pin, ('collection_id', 'version'))
                if self.skills is None: raise UserError('技能收藏服务不可用。', 409)
                if not isinstance(pin.get('collection_id'), str) or not re.fullmatch(r'col_[0-9a-f]{32}', pin['collection_id']): raise UserError('技能收藏标识无效。')
                if not isinstance(pin.get('version'), str) or not re.fullmatch('[0-9a-f]{64}', pin['version']): raise UserError('必须选择固定收藏版本。')
                saved = self.skills.collections.get(pin.get('collection_id'), pin['version'])
                if any(p['collection_id'] == saved['id'] for p in frozen): raise UserError('技能收藏不能重复。')
                frozen.append({'collection_id': saved['id'], 'source_skill_id': saved['skill_id'], 'name': saved['name'], 'version': saved['version'], 'manifest_digest': hashlib.sha256(canonical(saved['manifest']).encode()).hexdigest(), 'entry_sha256': saved['etag'], 'package_scope': saved['package_scope'], 'warnings': saved['warnings']})
            snapshot = {'goal': goal, 'acceptance': acceptance, 'inputs': inputs, 'skills': frozen, 'skill_pins': frozen}
            encoded = canonical(snapshot)
            if len(encoded.encode('utf-8')) > 1024 * 1024: raise UserError('本轮输入快照超过 1 MiB。')
            rid = uid()
            directories = self._directories(project, task_id, rid)
            note = Path(directories['references']['path']) / '本轮说明.md'
            with note.open('x', encoding='utf-8') as handle:
                handle.write('# AI 协作本轮说明\n\n' + goal + '\n\n任务 ID：' + task_id + '\n轮次 ID：' + rid + '\n\n生成目录：' + directories['generated']['relative_path'] + '\n\n验收项：\n' + '\n'.join('- ' + v for v in acceptance) + '\n\n数据库固定记录是本轮输入依据；本文件可阅读，编辑不会修改已冻结输入。\n')
            source = self.store.register_source(project['id'], project['root'], 'references')
            self.store.index_files(source, [note])
            with self.store.lock, self.store.connection() as db:
                current = self._task(db, task_id, True)
                self._expected(current, data)
                if self.store._project(db, project['id'])['root'] != project['root']: raise UserError('项目路径已变化，请刷新。', 409)
                for iid, path, observed in input_checks:
                    _item, checked = self._safe_item(project, iid)
                    if checked != path or stamp(clean_path(checked)) != observed:
                        raise UserError('输入文件在冻结期间发生变化，请刷新后重试。', 409)
                db.execute('INSERT INTO ai_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (rid, task_id, number, previous['id'] if previous else None, client, conversation, encoded, hashlib.sha256(encoded.encode()).hexdigest(), canonical(directories), '已冻结', 1, now()))
                db.execute("UPDATE ai_tasks SET status='进行中',revision=revision+1,updated=? WHERE id=?", (now(), task_id))
            return self.get_run(task_id, rid)

    def run_skill_reader(self, task_id, run_id, collection_id):
        from .mcp_skill_files import FixedSkillReader
        run = self.get_run(task_id, run_id)
        pin = next((p for p in run['input_snapshot']['skills'] if p['collection_id'] == collection_id), None)
        if pin is None: raise UserError('该技能不属于本轮固定输入。', 404)
        if self.skills is None: raise UserError('技能收藏服务不可用。', 409)
        return FixedSkillReader(self.skills.collections, collection_id, pin['version'], pin=pin), pin

    def read_run_skill(self, task_id, run_id, collection_id):
        # Preserve the workbench's existing entry-only API; MCP uses the same
        # reader for optional references and listing, without mutable lookups.
        reader, pin = self.run_skill_reader(task_id, run_id, collection_id)
        with self.skills.lock:
            metadata, raw = reader.read()
        try: content = raw.decode('utf-8-sig')
        except UnicodeError as exc: raise UserError('固定技能入口编码无效。', 409) from exc
        return {**pin, 'content': content, 'sha256': metadata['sha256']}

    def _artifact(self, db, task_id, artifact_id):
        self._task(db, task_id)
        row = db.execute('SELECT * FROM ai_artifacts WHERE id=? AND task_id=?', (artifact_id, task_id)).fetchone()
        if row is None: raise UserError('成果不属于此任务。', 404)
        return dict(row)

    def _verify_artifact(self, task, artifact):
        project = self.store.get_project(task['project_id'])
        _item, path = self._safe_item(project, artifact['item_id'])
        digest, identity = hash_file(path)
        if digest != artifact['observed_sha256']:
            with self.store.lock, self.store.connection() as db:
                db.execute("UPDATE ai_artifacts SET verification_status='changed',revision=revision+1 WHERE id=? AND verification_status!='changed'", (artifact['id'],))
            raise UserError('成果内容已变化；原审核仅适用于旧摘要，请在下一轮重新收件。', 409)
        return path, identity, digest

    def review_artifact(self, task_id, artifact_id, data):
        fields(data, ('decision', 'notes', 'expected_revision'))
        decision = data.get('decision')
        if decision not in ('accepted', 'rejected', 'needs_revision'): raise UserError('审核决定无效。')
        notes = text(data.get('notes', ''), '审核备注')
        with self.operation_lock:
            with self.store.connection() as db:
                task = self._task(db, task_id, True)
                artifact = self._artifact(db, task_id, artifact_id)
                self._expected(artifact, data)
            path, identity, digest = self._verify_artifact(task, artifact)
            with self.store.lock, self.store.connection() as db:
                self._task(db, task_id, True)
                self._expected(self._artifact(db, task_id, artifact_id), data)
                if stamp(clean_path(path)) != identity: raise UserError('成果在审核时变化。', 409)
                _item, checked = self._safe_item(self.store._project(db, task['project_id']), artifact['item_id'])
                if checked != path: raise UserError('成果路径在审核期间变化，请重试。', 409)
                db.execute('INSERT INTO ai_reviews VALUES(?,?,?,?,?,?)', (uid(), artifact_id, decision, digest, notes, now()))
                db.execute('UPDATE ai_artifacts SET review_status=?,review_sha256=?,review_notes=?,revision=revision+1 WHERE id=?', (decision, digest, notes, artifact_id))
                db.execute('UPDATE ai_runs SET status=?,revision=revision+1 WHERE id=?', ('需返工' if decision != 'accepted' else '待审核', artifact['run_id']))
                latest = db.execute('SELECT id FROM ai_runs WHERE task_id=? ORDER BY run_number DESC LIMIT 1', (task_id,)).fetchone()
                status = '待审核' if latest['id'] == artifact['run_id'] else task['status']
                db.execute('UPDATE ai_tasks SET status=?,revision=revision+1,updated=? WHERE id=?', (status, now(), task_id))
                result = self._artifact(db, task_id, artifact_id)
            return result

    def complete_task(self, task_id, data):
        fields(data, ('confirmed', 'expected_revision'))
        if data.get('confirmed') is not True: raise UserError('任务完成必须由用户明确确认。')
        with self.operation_lock:
            with self.store.connection() as db:
                task = self._task(db, task_id, True)
                self._expected(task, data)
                latest = db.execute('SELECT id FROM ai_runs WHERE task_id=? ORDER BY run_number DESC LIMIT 1', (task_id,)).fetchone()
                artifacts = [dict(r) for r in db.execute('SELECT * FROM ai_artifacts WHERE task_id=? AND run_id=?', (task_id, latest['id'] if latest else ''))]
                unresolved = self.receipts.unresolved(db, latest['id'] if latest else '')
            accepted = [a for a in artifacts if a['review_status'] == 'accepted']
            if (not accepted or unresolved or any(a['review_status'] not in ('accepted', 'rejected') for a in artifacts)
                    or any(a['review_sha256'] != a['observed_sha256'] for a in accepted)):
                raise UserError('最新轮次仍无已审核成果或有未处理收件错误，不能完成。', 409)
            checked = [(a, *self._verify_artifact(task, a)) for a in accepted]
            with self.store.lock, self.store.connection() as db:
                self._expected(self._task(db, task_id, True), data)
                for a, path, identity, _digest in checked:
                    if stamp(clean_path(path)) != identity: raise UserError('成果在完成核验时变化。', 409)
                    _item, checked_path = self._safe_item(self.store._project(db, task['project_id']), a['item_id'])
                    if checked_path != path: raise UserError('成果路径在完成核验期间变化，请重试。', 409)
                db.execute("UPDATE ai_tasks SET status='已完成',revision=revision+1,updated=? WHERE id=?", (now(), task_id))
            return self.get_task(task_id)

    def create_handoff(self, task_id, run_id, data):
        fields(data, ('client_id', 'conversation_id', 'force_full'))
        client = _identity(data.get('client_id'), '客户端身份')
        conversation = _identity(data.get('conversation_id'), '会话身份')
        if 'force_full' in data and not isinstance(data['force_full'], bool): raise UserError('完整交接选项无效。')
        with self.operation_lock, self.store.lock, self.store.connection() as db:
            task = self._task(db, task_id, True)
            run = self._run(db, task_id, run_id)
            if db.execute('SELECT count(*) FROM ai_task_handoffs WHERE task_id=?', (task_id,)).fetchone()[0] >= 200:
                raise UserError('每个任务最多保留 200 份交接；不会自动清理已确认基线。', 409)
            current = {'task': {'id': task_id, 'title': task['title'], 'kind': task['kind']}, 'run_id': run_id, 'run_number': run['run_number'], 'input': run['input_snapshot'], 'artifacts': [{k: r[k] for k in ('id', 'item_id', 'role', 'observed_sha256', 'verification_status', 'review_status', 'review_sha256', 'review_notes')} for r in db.execute('SELECT * FROM ai_artifacts WHERE task_id=? ORDER BY created,id LIMIT 2000', (task_id,))], 'unresolved_receipts': [list(key) for key in sorted(self.receipts.unresolved(db, run_id))], 'output_directory': run['directories']['generated']['relative_path']}
            baseline = db.execute('SELECT h.* FROM ai_task_baselines b JOIN ai_task_handoffs h ON h.id=b.snapshot_id WHERE b.task_id=? AND b.client_id=? AND b.conversation_id=?', (task_id, client, conversation)).fetchone()
            mode = 'delta' if baseline and not data.get('force_full', False) else 'full'
            old = json.loads(baseline['snapshot_json']) if mode == 'delta' else {}
            old_input = old.get('input', {})
            current_input = current['input']
            changes = {
                'goal': {'current': current_input['goal'], 'changed': old_input.get('goal') != current_input['goal']},
                'acceptance': {'current': current_input['acceptance'], 'changed': old_input.get('acceptance') != current_input['acceptance']},
                'inputs': entity_delta(old_input.get('inputs', []), current_input['inputs'], 'item_id'),
                'skills': entity_delta(old_input.get('skill_pins', []), current_input['skill_pins'], 'collection_id'),
                'artifacts': entity_delta(old.get('artifacts', []), current['artifacts'], 'id'),
                'run_id': run_id, 'run_number': run['run_number'],
                'output_directory': current['output_directory'],
                'unresolved_receipts': current['unresolved_receipts'],
            }
            hid = uid()
            prompt = ('AI 协作完整交接' if mode == 'full' else 'AI 协作变化交接') + '\n\n' + '本轮目标：\n' + run['input_snapshot']['goal'] + '\n\n' + canonical(current if mode == 'full' else changes) + '\n\n成果请写入约定生成目录。metadata_only 只记录索引元数据，没有核验文件内容。removed_from_scope 只表示不在本轮范围，不表示项目文件已删除。生成、收件、审核和任务完成是不同状态；不要声称已经通过用户审核。'
            result = {'id': hid, 'snapshot_id': hid, 'task_id': task_id, 'run_id': run_id, 'client_id': client, 'conversation_id': conversation, 'mode': mode, 'base_snapshot_id': baseline['id'] if mode == 'delta' else None, 'snapshot': current, 'changes': changes, 'prompt': prompt, 'acknowledged': False}
            encoded = canonical(result)
            if len(encoded.encode()) > 2 * 1024 * 1024: raise UserError('交接快照过大，请减少成果或输入。')
            db.execute('INSERT INTO ai_task_handoffs(id,task_id,run_id,client_id,conversation_id,mode,base_snapshot_id,snapshot_json,result_json,created) VALUES(?,?,?,?,?,?,?,?,?,?)', (hid, task_id, run_id, client, conversation, mode, result['base_snapshot_id'], canonical(current), encoded, now()))
            db.execute("UPDATE ai_runs SET status='已交接',revision=revision+1 WHERE id=? AND status='已冻结'", (run_id,))
        return result

    def acknowledge_handoff(self, task_id, snapshot_id):
        with self.operation_lock, self.store.lock, self.store.connection() as db:
            self._task(db, task_id)
            row = db.execute('SELECT * FROM ai_task_handoffs WHERE id=? AND task_id=?', (snapshot_id, task_id)).fetchone()
            if row is None: raise UserError('交接不属于此任务。', 404)
            baseline = db.execute('SELECT h.seq FROM ai_task_baselines b JOIN ai_task_handoffs h ON h.id=b.snapshot_id WHERE b.task_id=? AND b.client_id=? AND b.conversation_id=?', (task_id, row['client_id'], row['conversation_id'])).fetchone()
            if baseline and baseline['seq'] > row['seq']: raise UserError('不能把基线退回更早交接。', 409)
            db.execute('INSERT OR IGNORE INTO ai_task_acks VALUES(?,?)', (snapshot_id, now()))
            db.execute('INSERT INTO ai_task_baselines VALUES(?,?,?,?,?) ON CONFLICT(task_id,client_id,conversation_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,updated=excluded.updated', (task_id, row['client_id'], row['conversation_id'], snapshot_id, now()))
        return {'snapshot_id': snapshot_id, 'acknowledged': True, 'is_current_baseline': True}

    def list_handoffs(self, task_id, client_id, conversation_id, limit=50, offset=0):
        page(limit, offset)
        client = _identity(client_id, '客户端身份')
        conversation = _identity(conversation_id, '会话身份')
        with self.store.connection() as db:
            self._task(db, task_id)
            args = (task_id, client, conversation)
            total = db.execute('SELECT count(*) FROM ai_task_handoffs WHERE task_id=? AND client_id=? AND conversation_id=?', args).fetchone()[0]
            snapshots = []
            for row in db.execute('SELECT h.*,a.created AS acknowledged_at FROM ai_task_handoffs h LEFT JOIN ai_task_acks a ON a.snapshot_id=h.id WHERE task_id=? AND client_id=? AND conversation_id=? ORDER BY seq DESC LIMIT ? OFFSET ?', (*args, limit, offset)):
                result = json.loads(row['result_json'])
                result['acknowledged'] = row['acknowledged_at'] is not None
                snapshots.append(result)
        return {'snapshots': snapshots, 'total': total}
