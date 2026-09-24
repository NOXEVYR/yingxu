import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yingxu import incremental_install as install


def sha(raw): return hashlib.sha256(raw).hexdigest()


class IncrementalInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingxu-incremental-install-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'program'; self.data = self.base / 'data'
        self.root.mkdir(); self.data.mkdir()
        self.plan_id = 'a' * 32
        self.folder = self.data / 'updates/incremental' / self.plan_id
        self.stage = self.folder / 'stage'; self.stage.mkdir(parents=True)
        self.job = self.data / 'updates/incremental-install' / ('b' * 32); self.job.mkdir(parents=True)
        self.old = {'YingXu.exe':b'old-exe','server.py':b'same', 'launcher.pyw':b'launch',
                    'frontend/index.html':b'html','frontend/obsolete.js':b'obsolete'}
        self.new = dict(self.old, **{'YingXu.exe':b'new-exe','yingxu/new.py':b'new'})
        del self.new['frontend/obsolete.js']
        def manifest(files, version):
            return json.dumps(dict(application='YingXu',root='YingXu/',architecture='Windows x64',version=version,
                files=[dict(path=n,bytes=len(v),sha256=sha(v)) for n,v in files.items()])).encode()
        old_manifest = manifest(self.old,'0.4.18'); new_manifest=manifest(self.new,'0.4.19')
        for name,raw in dict(self.old, **{'RELEASE_MANIFEST.json':old_manifest}).items():
            target=self.root/name; target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        changes=[];reused=[]
        for name,raw in dict(self.new,**{'RELEASE_MANIFEST.json':new_manifest}).items():
            prior=old_manifest if name=='RELEASE_MANIFEST.json' else self.old.get(name)
            entry=dict(path=name,bytes=len(raw),sha256=sha(raw))
            if prior!=raw:
                entry['expected_old_sha256']=sha(prior) if prior is not None else None
                changes.append(entry);target=self.stage/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
            else:reused.append(entry)
        self.plan=dict(schema=1,id=self.plan_id,install_root=str(self.root),current_version='0.4.18',version='0.4.19',
            state='ready',old_manifest_sha256=sha(old_manifest),manifest_sha256=sha(new_manifest),changes=changes,reused=reused,
            removed=[dict(path='frontend/obsolete.js',expected_old_sha256=sha(b'obsolete'))])
        self.write_plan()

    def write_plan(self): (self.folder/'plan.json').write_text(json.dumps(self.plan),encoding='utf-8')
    def snapshot(self): return install.validate_plan(self.data,self.root,self.plan_id)
    def test_only_changed_files_and_obsolete_are_processed(self):
        before=(self.root/'server.py').stat().st_mtime_ns
        (self.root/'personal-note.md').write_bytes(b'private')
        count=install.apply_incremental(self.snapshot(),self.job)
        self.assertEqual(count,4)
        self.assertEqual((self.root/'server.py').stat().st_mtime_ns,before)
        self.assertEqual((self.root/'personal-note.md').read_bytes(),b'private')
        self.assertFalse((self.root/'frontend/obsolete.js').exists())
        self.assertEqual((self.root/'YingXu.exe').read_bytes(),b'new-exe')
        self.assertEqual(install.read_json(self.job/'journal.json')['state'],'installed')

    def test_failed_replace_rolls_back_originals(self):
        real=os.replace;calls=[]
        def fail(source,target):
            calls.append(str(target))
            if len(calls)==2:raise OSError('synthetic failure')
            real(source,target)
        with self.assertRaises(OSError):install.apply_incremental(self.snapshot(),self.job,replace=fail)
        for name,raw in self.old.items():self.assertEqual((self.root/name).read_bytes(),raw)
        self.assertFalse((self.root/'yingxu/new.py').exists())
        self.assertEqual(install.read_json(self.job/'journal.json')['state'],'rolled_back')

    def test_final_validation_preserves_external_reused_edit_and_rolls_back(self):
        real=os.replace
        def concurrent(source,target):
            real(source,target);(self.root/'server.py').write_bytes(b'external edit')
        with self.assertRaises(ValueError):install.apply_incremental(self.snapshot(),self.job,replace=concurrent)
        self.assertEqual((self.root/'YingXu.exe').read_bytes(),b'old-exe')
        self.assertEqual((self.root/'server.py').read_bytes(),b'external edit')
        self.assertEqual(install.read_json(self.job/'journal.json')['state'],'rolled_back')

    def test_locked_replace_cleans_exact_owned_pending(self):
        def locked(source,target):raise PermissionError('synthetic lock')
        with self.assertRaises(PermissionError):install.apply_incremental(self.snapshot(),self.job,replace=locked)
        self.assertEqual(list(self.root.rglob('*.yx-update-*')),[])
        self.assertEqual((self.root/'YingXu.exe').read_bytes(),b'old-exe')

    def test_conflicting_pending_is_retained(self):
        def locked(source,target):
            Path(source).write_bytes(b'concurrent new data');raise PermissionError('synthetic lock')
        with self.assertRaises(PermissionError):install.apply_incremental(self.snapshot(),self.job,replace=locked)
        remains=list(self.root.rglob('*.yx-update-*'))
        self.assertEqual(len(remains),1)
        self.assertEqual(remains[0].read_bytes(),b'concurrent new data')
        self.assertEqual(install.read_json(self.job/'journal.json')['state'],'recovery_required')

    def test_partial_copy_failure_cleans_only_source_prefix(self):
        source=self.base/'source';source.write_bytes(b'known program bytes')
        target=self.base/'partial'
        def partial(src,dst,length):dst.write(src.read(4));raise OSError('synthetic disk full')
        with patch.object(install.shutil,'copyfileobj',side_effect=partial):
            with self.assertRaises(OSError):install._copy(source,target,sha(source.read_bytes()))
        self.assertFalse(target.exists())

    def test_partial_copy_conflict_is_preserved(self):
        source=self.base/'source';source.write_bytes(b'known program bytes')
        target=self.base/'partial'
        def partial(src,dst,length):dst.write(b'user');raise OSError('synthetic conflict')
        with patch.object(install.shutil,'copyfileobj',side_effect=partial):
            with self.assertRaises(OSError):install._copy(source,target,sha(source.read_bytes()))
        self.assertEqual(target.read_bytes(),b'user')

    def test_interrupted_write_has_durable_recovery(self):
        real=os.replace
        def interrupt(source,target):real(source,target);raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):install.apply_incremental(self.snapshot(),self.job,replace=interrupt)
        self.assertTrue(install.rollback(self.job))
        self.assertEqual((self.root/'YingXu.exe').read_bytes(),b'old-exe')

    def test_recovery_preserves_conflicting_user_change(self):
        real=os.replace
        def interrupt(source,target):real(source,target);raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):install.apply_incremental(self.snapshot(),self.job,replace=interrupt)
        (self.root/'YingXu.exe').write_bytes(b'user replacement')
        self.assertFalse(install.rollback(self.job))
        self.assertEqual((self.root/'YingXu.exe').read_bytes(),b'user replacement')

    def test_local_modification_refused(self):
        (self.root/'server.py').write_bytes(b'changed')
        with self.assertRaises(ValueError):self.snapshot()

    def test_same_hash_new_timestamp_refused_after_prepare(self):
        snap=self.snapshot();path=self.root/'server.py';st=path.stat()
        os.utime(path,ns=(st.st_atime_ns,st.st_mtime_ns+1000000000))
        with self.assertRaises(ValueError):install.apply_incremental(snap,self.job)
        self.assertEqual((self.root/'YingXu.exe').read_bytes(),b'old-exe')

    def test_stage_change_after_prepare_refused(self):
        snap=self.snapshot();(self.stage/'YingXu.exe').write_bytes(b'evil')
        with self.assertRaises(ValueError):install.apply_incremental(snap,self.job)

    def test_plan_mutation_refused(self):
        snap=self.snapshot();self.plan['version']='0.4.20';self.write_plan()
        with self.assertRaises(ValueError):install.apply_incremental(snap,self.job)

    def test_extra_stage_file_refused(self):
        (self.stage/'unknown.md').write_bytes(b'extra')
        with self.assertRaises(ValueError):self.snapshot()

    def test_missing_obsolete_can_be_explicitly_absent(self):
        (self.root/'frontend/obsolete.js').unlink()
        self.plan['removed'][0]['expected_old_sha256']=None;self.write_plan()
        install.apply_incremental(self.snapshot(),self.job)
        self.assertEqual((self.root/'YingXu.exe').read_bytes(),b'new-exe')

    def test_absent_obsolete_recreated_after_prepare_is_preserved(self):
        path=self.root/'frontend/obsolete.js';path.unlink()
        self.plan['removed'][0]['expected_old_sha256']=None;self.write_plan();snap=self.snapshot()
        path.write_bytes(b'new user file')
        with self.assertRaises(ValueError):install.apply_incremental(snap,self.job)
        self.assertEqual(path.read_bytes(),b'new user file')

    def test_unmanaged_target_not_overwritten(self):
        path=self.root/'yingxu/new.py';path.parent.mkdir();path.write_bytes(b'private')
        with self.assertRaises(ValueError):self.snapshot()

    def test_userdata_path_refused(self):
        self.plan['changes'].append(dict(path='data/yingxu.sqlite3',bytes=0,sha256=sha(b''),expected_old_sha256=None));self.write_plan()
        with self.assertRaises(ValueError):self.snapshot()

    def test_hardlink_refused(self):
        os.link(self.root/'server.py',self.base/'alias.py')
        with self.assertRaises(ValueError):self.snapshot()

    def test_low_disk_space_refused_before_writes(self):
        snap=self.snapshot()
        class Usage:free=1
        with patch.object(install.shutil,'disk_usage',return_value=Usage()):
            with self.assertRaises(ValueError):install.apply_incremental(snap,self.job)
        self.assertEqual((self.root/'YingXu.exe').read_bytes(),b'old-exe')

    def test_both_processes_must_exit_after_commit(self):
        class Guard:
            def __init__(self):self.done=False
            def exited(self):return self.done
        a,b=Guard(),Guard();turn=[0]
        def step(_):
            turn[0]+=1
            if turn[0]==1:a.done=True
            if turn[0]==2:b.done=True
            if turn[0]==3:(self.job/'commit.json').write_text('{}')
        with patch.object(install.time,'sleep',side_effect=step):
            self.assertTrue(install.wait_for_commit_and_exit(self.job,[a,b],timeout=3))
        self.assertEqual(turn[0],3)

    def test_cancel_keeps_running_process_and_files(self):
        class Guard:
            def exited(self):return False
        (self.job/'cancel.json').write_text('{}')
        self.assertFalse(install.wait_for_commit_and_exit(self.job,[Guard()],timeout=1))
        self.assertEqual((self.root/'YingXu.exe').read_bytes(),b'old-exe')

    def test_no_commit_timeout_never_writes(self):
        class Guard:
            def exited(self):return True
        self.assertFalse(install.wait_for_commit_and_exit(self.job,[Guard()],timeout=0))

    def test_downloader_and_installer_allowlists_match(self):
        from yingxu import range_zip
        self.assertEqual(install.ROOT_FILES,range_zip.ROOT_FILES)
        self.assertEqual(install.CODE_SUFFIXES,range_zip.CODE_SUFFIXES)
        for name in ('MIGRATION.md','runtime/Lib/a.pyd','frontend/icons.svg','docs/change.md'):
            self.assertEqual(install.program_path(name),range_zip.program_path(name))
        for name in ('data/settings.json','../server.py','runtime/a?.dll','runtime/COM0.py','frontend//a.js'):
            for module in (install,range_zip):
                try:self.assertFalse(module.program_path(name))
                except ValueError:pass

    def test_rollback_restores_removed_file_after_interrupt(self):
        snap=self.snapshot();real=install.write_json
        def interrupted(path,value):
            real(path,value)
            if Path(path).name=='journal.json' and any(e.get('done') and e['path']=='frontend/obsolete.js' for e in value.get('operations',[])):
                raise KeyboardInterrupt()
        with patch.object(install,'write_json',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):install.apply_incremental(snap,self.job)
        self.assertTrue(install.rollback(self.job))
        self.assertEqual((self.root/'frontend/obsolete.js').read_bytes(),b'obsolete')

    def test_independent_minimal_python_runtime(self):
        runtime=os.environ.get('YINGXU_INSTALLER_TEST_RUNTIME')
        if not runtime:self.skipTest('Set YINGXU_INSTALLER_TEST_RUNTIME to an official read-only runtime')
        import subprocess
        target=self.base/'helper'
        interpreter=install.copy_helper_runtime(Path(runtime).parent,target)
        script=target/'incremental_install.py'
        script.write_bytes(Path(install.__file__).read_bytes())
        result=subprocess.run([str(interpreter),'-I','-B','-c',
            'import ctypes,hashlib,sqlite3,ssl,incremental_install;print(hashlib.sha256(b"test").hexdigest())'],
            cwd=self.base,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),sha(b'test'))
        self.assertFalse((target/'Lib').exists())
        self.assertFalse((target/'webview2').exists())
        self.assertLess(sum(p.stat().st_size for p in target.iterdir()),48*1024*1024)

    @unittest.skipUnless(os.name=='nt','Windows installer boundary')
    def test_source_python_cannot_prepare_installation(self):
        before=set(self.data.rglob('*'))
        with self.assertRaisesRegex(ValueError,'自带 Python'):
            install.prepare_install(self.data,self.root,self.plan_id,os.getpid())
        self.assertEqual(set(self.data.rglob('*')),before)


if __name__=='__main__':unittest.main()
