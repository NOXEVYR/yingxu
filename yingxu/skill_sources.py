"""Known local skill locations; discovery never reads account configuration."""
from pathlib import Path
import json
import os
import time

from .store import UserError, has_link, uid

LABELS = {'yingxu':'映序本地','codex':'Codex','claude':'Claude','dsh':'DSH',
          'workbuddy':'WorkBuddy','zcode':'ZCode','agents':'共享技能','custom':'自定义'}
MAX_CUSTOM_SOURCES = 16
MAX_MANIFEST_BYTES = 512 * 1024
MAX_PLUGIN_ENTRIES = 4096


class DiscoveryBudget(UserError):
    pass


def catalogue(home, local, settings):
    home = Path(home)
    definitions = [
        ('yingxu','yingxu','映序本地',local,'directory'),
        ('codex','codex','Codex 用户技能',home/'.codex/skills','directory'),
        ('claude','claude','Claude 用户技能',home/'.claude/skills','directory'),
        ('dsh','dsh','DSH 用户技能',home/'.dsh/skills','directory'),
        ('workbuddy','workbuddy','WorkBuddy 用户技能',home/'.workbuddy/skills','directory'),
        ('workbuddy_plugins','workbuddy','WorkBuddy 已安装插件',home/'.workbuddy/plugins/cache','installed'),
        ('zcode','zcode','ZCode 插件缓存',home/'.zcode/cli/plugins/cache','plugins'),
        ('agents','agents','共享 Agent 技能',home/'.agents/skills','directory'),
    ]
    result = {}
    for key, group, label, path, mode in definitions:
        saved = settings.get(key, {})
        result[key] = dict(id=key,source=group,group=group,label=saved.get('label') or label,
                           path=str(path),enabled=key=='yingxu' or bool(saved.get('enabled',1)),
                           custom=False,removable=False,readonly=key!='yingxu',mode=mode,
                           status='missing',count=0)
    for key, saved in settings.items():
        if saved['custom']:
            result[key] = dict(id=key,source='custom',group='custom',label=saved['label'],
                               path=saved['path'],enabled=bool(saved['enabled']),custom=True,
                               removable=True,readonly=True,mode='directory',status='missing',count=0)
    return result


def scan_roots(location, check, deadline=None):
    """Return roots or fail closed. Only trusted installed metadata is parsed."""
    deadline = min(deadline if deadline is not None else float('inf'), time.monotonic()+1.5)
    def budget():
        if time.monotonic()>deadline:
            raise DiscoveryBudget('扫描已达到时间预算；保留上次索引。')
    budget()
    root = check(location['path'])
    if not root.exists():
        return [], 'missing'
    if not root.is_dir():
        raise UserError('扫描位置不是文件夹。')
    mode = location['mode']
    if mode == 'directory':
        return [root], 'ready'
    roots = []
    if mode == 'installed':
        manifest = check(root.parent/'installed_plugins.json')
        if not manifest.is_file():
            raise UserError('未找到插件安装清单；没有扫描历史缓存。')
        with manifest.open('rb') as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        budget()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise UserError('插件安装清单过大，已跳过。')
        try:
            value = json.loads(raw)
            plugins = value['plugins']
            if not isinstance(plugins, dict) or len(plugins) > 256:
                raise ValueError()
            entries = 0
            for records in plugins.values():
                if not isinstance(records,list):
                    records = [records]
                entries += len(records)
                budget()
                if entries > 256:
                    raise ValueError()
                for record in records:
                    budget()
                    path = record.get('installPath') if isinstance(record,dict) else None
                    if not isinstance(path,str) or not Path(path).is_absolute():
                        raise ValueError()
                    installed = check(path)
                    if not installed.is_relative_to(root):
                        raise ValueError()
                    folder = check(installed/'skills')
                    if folder.is_dir():
                        roots.append(folder)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise UserError('插件安装清单无效或路径越界；没有扫描历史缓存。') from exc
    else:
        # Only the observed marketplace/plugin/version/skills layout. Never
        # recurse into arbitrary app cache contents or plugin executables.
        folders = [root]
        visits = 0
        for _depth in range(3):
            following = []
            for folder in folders:
                budget()
                check(folder)
                with os.scandir(folder) as entries:
                    for entry in entries:
                        visits += 1
                        budget()
                        if visits > MAX_PLUGIN_ENTRIES:
                            raise DiscoveryBudget('插件目录发现达到预算，请缩小自定义扫描位置。')
                        if not entry.name.startswith('.') and not has_link(Path(entry.path)) and entry.is_dir(follow_symlinks=False):
                            following.append(Path(entry.path))
            folders = following
        for folder in folders:
            budget()
            candidate = check(folder/'skills')
            if candidate.is_dir():
                roots.append(candidate)
    return list(dict.fromkeys(roots)), 'ready'


def custom_location(data, home, check):
    if not isinstance(data,dict) or set(data)-{'path','label'}:
        raise UserError('扫描位置参数无效。')
    value, label = data.get('path'), data.get('label','')
    if not isinstance(value,str) or not value.strip() or len(value)>4096 or '\x00' in value:
        raise UserError('请选择绝对路径的本地技能文件夹。')
    path = Path(value.strip())
    if not path.is_absolute() or str(path).startswith(('\\\\','//')):
        raise UserError('请选择绝对路径的本地技能文件夹。')
    path = check(path)
    if path == Path(path.anchor) or path == Path(home) or path in Path(home).parents:
        raise UserError('请选择具体技能文件夹，不能扫描磁盘或整个用户目录。')
    if not path.is_dir():
        raise UserError('技能文件夹不存在。')
    if not isinstance(label,str) or len(label.strip())>80 or any(ord(c)<32 for c in label):
        raise UserError('位置名称最多 80 字且不能包含控制字符。')
    return dict(id='custom_'+uid(),path=str(path),label=label.strip() or path.name,enabled=1,custom=1)
