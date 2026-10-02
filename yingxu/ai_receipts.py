"""Explicit bounded receipt and SHA-256 verification for AI task outputs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time

from .ai_tasks import canonical, fields, hash_file, stamp, text, MAX_FILE_BYTES
from .handoffs import _private_asset, _identity
from .store import UserError, SAFE_EXTENSIONS, clean_path, now, uid


class AIReceiptService:
    def __init__(self, tasks):
        self.tasks = tasks
        self.store = tasks.store
        with self.store.lock, self.store.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS ai_receipts(
              id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES ai_tasks(id),run_id TEXT NOT NULL REFERENCES ai_runs(id),
              idempotency_key TEXT NOT NULL,plan_json TEXT NOT NULL,state TEXT NOT NULL,
              result_json TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL,
              UNIQUE(run_id,idempotency_key));''')
            for row in db.execute("SELECT id,result_json FROM ai_receipts WHERE state='receiving'").fetchall():
                result = json.loads(row['result_json'])
                result.update(state='interrupted', error='收件被中断；已有逐项结果保留，请用新幂等标识明确重试未完成项目。')
                db.execute("UPDATE ai_receipts SET state='interrupted',result_json=?,updated=? WHERE id=?", (canonical(result), now(), row['id']))

    def _boundary(self, task_id, run_id):
        with self.store.connection() as db:
            task = self.tasks._task(db, task_id, True)
            run = self.tasks._run(db, task_id, run_id)
            project = self.store._project(db, task['project_id'])
            latest = db.execute('SELECT id FROM ai_runs WHERE task_id=? ORDER BY run_number DESC LIMIT 1', (task_id,)).fetchone()
            if latest['id'] != run_id: raise UserError('只允许向当前最新轮次收件。', 409)
        folder = run['directories']['generated']
        root = clean_path(project['root'])
        directory = clean_path(root / folder['relative_path'])
        if not directory.is_relative_to(root): raise UserError('本轮目录超出项目。', 403)
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM folders WHERE id=? AND project_id=? AND removed=0', (folder['folder_id'], project['id'])).fetchone()
            if row is None or row['relative_path'] != folder['relative_path'] or row['category'] != 'generated':
                raise UserError('本轮目录已回收或移出项目。', 409)
        return task, run, project, directory

    def _relative(self, directory, relative):
        if not isinstance(relative, str) or len(relative) > 1000 or '\\' in relative or ':' in relative:
            raise UserError('成果相对路径无效。')
        parts = relative.split('/')
        if not parts or len(parts) > 8 or any(not p or p in ('.', '..') or p.endswith((' ', '.')) or any(ord(c) < 32 for c in p) for p in parts):
            raise UserError('成果路径越界或过深。')
        path = clean_path(directory.joinpath(*parts))
        if not path.is_relative_to(directory) or not path.is_file() or path.suffix.lower() not in SAFE_EXTENSIONS:
            raise UserError('仅支持本轮生成目录里的可管理普通文件。', 403)
        return path

    def preview(self, task_id, run_id):
        _task, _run, project, directory = self._boundary(task_id, run_id)
        files, errors = [], []
        deadline = time.monotonic() + 3
        pending = [(directory, 0)]
        entries = 0
        total_bytes = 0
        while pending:
            folder, depth = pending.pop()
            try:
                folder = clean_path(folder)
                with os.scandir(folder) as iterator:
                    for entry in iterator:
                        entries += 1
                        if entries > 2000 or time.monotonic() > deadline:
                            return {'files': files, 'errors': errors + ['候选检查达到数量或时间限制。'], 'truncated': True}
                        try:
                            path = clean_path(entry.path)
                            if _private_asset({'path': str(path)}, self.store.data_root, project['root']):
                                errors.append('跳过私密文件或目录。'); continue
                            if path.is_dir():
                                if depth < 7: pending.append((path, depth + 1))
                                else: errors.append('目录过深，已跳过。')
                            elif path.is_file() and path.suffix.lower() in SAFE_EXTENSIONS:
                                if len(files) >= 200: return {'files': files, 'errors': errors + ['一次最多列出 200 个候选。'], 'truncated': True}
                                size = path.stat().st_size
                                if size > MAX_FILE_BYTES:
                                    errors.append('跳过超过 8 GiB 的文件。'); continue
                                total_bytes += size
                                if total_bytes > 32 * 1024 ** 3: return {'files': files, 'errors': errors + ['候选总大小超过 32 GiB。'], 'truncated': True}
                                files.append({'relative_path': path.relative_to(directory).as_posix(), 'size': size})
                        except (OSError, UserError):
                            errors.append('某个文件不可读取或包含链接，已跳过。')
            except (OSError, UserError):
                errors.append('目录或文件不可读取，或包含链接。')
        return {'files': sorted(files, key=lambda f: f['relative_path']), 'errors': errors, 'truncated': False}

    def receive(self, task_id, run_id, data):
        fields(data, ('idempotency_key', 'files'))
        key = _identity(data.get('idempotency_key'), '收件幂等标识', 128)
        files = data.get('files')
        if not isinstance(files, list) or not 1 <= len(files) <= 200: raise UserError('每次收件请选择 1 至 200 个文件。')
        for entry in files:
            fields(entry, ('relative_path', 'item_id', 'role', 'expected_sha256'))
            if bool(entry.get('relative_path')) == bool(entry.get('item_id')): raise UserError('请只提供本轮相对路径或已有文件 ID。')
            if entry.get('relative_path') and (not isinstance(entry['relative_path'], str) or len(entry['relative_path']) > 1000): raise UserError('成果路径无效或过长。')
            if entry.get('item_id'): _identity(entry['item_id'], '成果文件 ID')
            text(entry.get('role', 'result'), '成果用途', 80, True)
            if 'expected_sha256' in entry and (not isinstance(entry['expected_sha256'], str) or not re.fullmatch('[0-9a-fA-F]{64}', entry['expected_sha256'])): raise UserError('声明 SHA-256 格式无效。')
        plan = canonical(files)
        with self.tasks.operation_lock:
            # Retrieve an already completed result before checking writable state.
            with self.store.connection() as db:
                self.tasks._task(db, task_id)
                existing = db.execute('SELECT * FROM ai_receipts WHERE run_id=? AND idempotency_key=? AND task_id=?', (run_id, key, task_id)).fetchone()
            if existing:
                if existing['plan_json'] != plan: raise UserError('同一幂等标识不能用于不同收件内容。', 409)
                if existing['state'] != 'receiving': return json.loads(existing['result_json'])
                # Interrupted attempts never silently replay filesystem writes.
                result = json.loads(existing['result_json'])
                result['state'] = 'interrupted'
                result['error'] = '收件被中断；已有逐项结果保留，请用新幂等标识明确重试未完成项目。'
                with self.store.lock, self.store.connection() as db:
                    db.execute('UPDATE ai_receipts SET state=?,result_json=?,updated=? WHERE id=?', ('interrupted', canonical(result), now(), existing['id']))
                return result
            task, run, project, directory = self._boundary(task_id, run_id)
            receipt_id = uid()
            result = {'id': receipt_id, 'receipt_id': receipt_id, 'task_id': task_id, 'run_id': run_id, 'state': 'receiving', 'results': []}
            with self.store.lock, self.store.connection() as db:
                self.tasks._task(db, task_id, True)
                if db.execute('SELECT count(*) FROM ai_receipts WHERE run_id=?', (run_id,)).fetchone()[0] >= 100:
                    raise UserError('每轮最多保留 100 次收件记录，请创建下一轮。', 409)
                db.execute('INSERT INTO ai_receipts VALUES(?,?,?,?,?,?,?,?,?)', (receipt_id, task_id, run_id, key, plan, 'receiving', canonical(result), now(), now()))
            total_bytes = 0
            for entry in files:
                outcome = {'relative_path': entry.get('relative_path'), 'item_id': entry.get('item_id'), 'role': text(entry.get('role', 'result'), '成果用途', 80, True)}
                try:
                    self._boundary(task_id, run_id)
                    if entry.get('item_id'):
                        item, path = self.tasks._safe_item(project, entry['item_id'])
                    else:
                        path = self._relative(directory, entry['relative_path'])
                        if _private_asset({'path': str(path)}, self.store.data_root, project['root']): raise UserError('不能接收私密文件。', 403)
                        with self.store.connection() as db:
                            found = db.execute('SELECT id,removed FROM items WHERE project_id=? AND path=?', (project['id'], str(path))).fetchone()
                        if found and found['removed']: raise UserError('成果已在回收站，收件不能恢复。', 409)
                        source = self.store.register_source(project['id'], project['root'], 'generated')
                        self.store.index_files(source, [path])
                        with self.store.connection() as db:
                            found = db.execute('SELECT id FROM items WHERE project_id=? AND path=? AND removed=0', (project['id'], str(path))).fetchone()
                        if found is None: raise UserError('文件未成功登记或目录已回收。', 409)
                        item, path = self.tasks._safe_item(project, found['id'])
                    total_bytes += path.stat().st_size
                    if total_bytes > 32 * 1024 ** 3: raise UserError('本次收件超过 32 GiB，请拆分后重试。', 413)
                    digest, identity = hash_file(path)
                    if entry.get('expected_sha256') and digest != entry['expected_sha256'].lower(): raise UserError('实际文件 SHA-256 与声明不一致。', 409)
                    with self.store.lock, self.store.connection() as db:
                        self.tasks._task(db, task_id, True)
                        current, checked = self.tasks._safe_item(self.store._project(db, project['id']), item['id'])
                        if checked != path or stamp(clean_path(path)) != identity: raise UserError('文件在提交收件时变化。', 409)
                        old = db.execute('SELECT * FROM ai_artifacts WHERE run_id=? AND item_id=? AND role=?', (run_id, item['id'], outcome['role'])).fetchone()
                        if old and old['observed_sha256'] != digest: raise UserError('本轮已接收另一版本，请在下一轮重新收件。', 409)
                        if not old:
                            count = db.execute('SELECT count(*) FROM ai_artifacts WHERE task_id=?', (task_id,)).fetchone()[0]
                            if count >= 2000: raise UserError('每个任务最多 2000 条成果关联。', 409)
                            if db.execute('SELECT count(*) FROM ai_artifacts WHERE run_id=?', (run_id,)).fetchone()[0] >= 200:
                                raise UserError('每轮最多 200 个成果，请创建下一轮。', 409)
                            aid = uid()
                            db.execute('INSERT INTO ai_artifacts(id,task_id,run_id,item_id,role,observed_sha256,verification_status,review_status,revision,created) VALUES(?,?,?,?,?,?,?,?,?,?)', (aid, task_id, run_id, item['id'], outcome['role'], digest, 'verified', 'pending', 1, now()))
                        else: aid = old['id']
                        outcome.update(status='verified', artifact_id=aid, item_id=item['id'], sha256=digest)
                except (UserError, OSError) as exc:
                    outcome.update(status='error', error=str(exc), error_status=getattr(exc, 'status', 400))
                result['results'].append(outcome)
                with self.store.lock, self.store.connection() as db:
                    db.execute('UPDATE ai_receipts SET result_json=?,updated=? WHERE id=?', (canonical(result), now(), receipt_id))
            result['state'] = 'partial' if any(r['status'] == 'error' for r in result['results']) else 'completed'
            with self.store.lock, self.store.connection() as db:
                db.execute('UPDATE ai_receipts SET state=?,result_json=?,updated=? WHERE id=?', (result['state'], canonical(result), now(), receipt_id))
                db.execute('UPDATE ai_runs SET status=?,revision=revision+1 WHERE id=?', ('待审核' if result['state'] == 'completed' else '收件有错误', run_id))
                db.execute("UPDATE ai_tasks SET status='待审核',revision=revision+1,updated=? WHERE id=?", (now(), task_id))
            return result

    def list(self, task_id, run_id, limit=50, offset=0):
        from .ai_tasks import page
        page(limit, offset)
        self.tasks.get_run(task_id, run_id)
        with self.store.connection() as db:
            total = db.execute('SELECT count(*) FROM ai_receipts WHERE task_id=? AND run_id=?', (task_id, run_id)).fetchone()[0]
            receipts = [json.loads(r[0]) for r in db.execute('SELECT result_json FROM ai_receipts WHERE task_id=? AND run_id=? ORDER BY created DESC,id LIMIT ? OFFSET ?', (task_id, run_id, limit, offset))]
        return {'receipts': receipts, 'total': total}

    def get(self, task_id, receipt_id):
        with self.store.connection() as db:
            self.tasks._task(db, task_id)
            row = db.execute('SELECT result_json FROM ai_receipts WHERE id=? AND task_id=?', (receipt_id, task_id)).fetchone()
        if row is None: raise UserError('收件记录不属于此任务。', 404)
        return json.loads(row[0])

    def unresolved(self, db, run_id):
        """Only a later verified receipt for the same target+role resolves an error."""
        unresolved = set()
        for row in db.execute('SELECT plan_json,state,result_json FROM ai_receipts WHERE run_id=? ORDER BY rowid', (run_id,)):
            plan = json.loads(row['plan_json'])
            result = json.loads(row['result_json'])
            outcomes = result.get('results', [])
            for index, entry in enumerate(plan):
                role = text(entry.get('role', 'result'), '成果用途', 80, True)
                key = ('relative_path', entry['relative_path'], role) if entry.get('relative_path') else ('item_id', entry.get('item_id'), role)
                outcome = outcomes[index] if index < len(outcomes) else {}
                if outcome.get('status') == 'verified':
                    unresolved.discard(key)
                    # A retry can select the file by indexed ID instead of its original relative path.
                    iid = outcome.get('item_id')
                    unresolved.discard(('item_id', iid, role))
                    item = db.execute('SELECT path FROM items WHERE id=?', (iid,)).fetchone()
                    run = db.execute('SELECT directories,task_id FROM ai_runs WHERE id=?', (run_id,)).fetchone()
                    if item and run:
                        task = self.tasks._task(db, run['task_id'])
                        project = self.store._project(db, task['project_id'])
                        directory = json.loads(run['directories'])['generated']
                        folder = db.execute('SELECT relative_path FROM folders WHERE id=?', (directory['folder_id'],)).fetchone()
                        base = Path(project['root']) / (folder['relative_path'] if folder else directory['relative_path'])
                        path = Path(item['path'])
                        if path.is_relative_to(base): unresolved.discard(('relative_path', path.relative_to(base).as_posix(), role))
                else:
                    unresolved.add(key)
        return unresolved
