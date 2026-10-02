"""Synthetic temporary files only; no real media, providers or user data."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch
from bot.media_spool import MediaSpool,SpoolError,default_root,_unsafe


class MediaSpoolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.source=self.base/'input.wav'; self.source.write_bytes(b'synthetic-media'*20)
        self.spool=MediaSpool(self.base/'owned',max_file_bytes=1024,max_total_bytes=16384)
    def tearDown(self): self.temp.cleanup()

    def test_platform_default_roots_are_durable_and_override_absolute(self):
        self.assertEqual(Path('/appdata/Arti/media'),default_root(platform='win32',environ={'LOCALAPPDATA':'/appdata'},home='/home/user'))
        self.assertEqual(Path('/xdg/arti/media'),default_root(platform='linux',environ={'XDG_DATA_HOME':'/xdg'},home='/home/user'))
        self.assertEqual(Path('/home/user/.local/share/arti/media'),default_root(platform='linux',environ={},home='/home/user'))
        with self.assertRaises(SpoolError): default_root(environ={'ARTI_MEDIA_SPOOL_DIR':'relative'})

    def test_stage_restart_verify_safe_suffix_and_source_preserved(self):
        ns=self.spool.create_namespace(); descriptor=self.spool.stage(self.source,namespace=ns)
        self.assertEqual({'version','namespace','leaf','size','sha256'},set(descriptor))
        self.assertTrue(descriptor['leaf'].endswith('.wav'))
        self.assertNotIn(str(self.source),str(descriptor))
        recreated=MediaSpool(self.spool.root,max_file_bytes=1024,max_total_bytes=16384)
        with recreated.open_verified(descriptor) as stream: self.assertEqual(self.source.read_bytes(),stream.read())
        self.assertTrue(self.source.exists()); self.assertEqual(ns,descriptor['namespace'])

    def test_cleanup_is_idempotent_and_cannot_resurrect_namespace(self):
        descriptor=self.spool.stage(self.source); ns=descriptor['namespace']
        self.assertTrue(self.spool.cleanup(ns)); self.assertTrue(self.spool.cleanup(ns))
        self.assertTrue(self.source.exists())
        with self.assertRaises(SpoolError): self.spool.workdir(ns)
        with self.assertRaises(SpoolError): self.spool.stage(self.source,namespace=ns)
        with self.assertRaises(SpoolError):
            with self.spool.open_verified(descriptor): pass

    def test_descriptor_rejects_paths_versions_sizes_and_digest_changes(self):
        original=self.spool.stage(self.source)
        changes=({'version':2},{'version':True},{'namespace':'../escape'},{'leaf':'../input.wav'},
                 {'leaf':'a'*32+'.exe'},{'size':True},{'size':-1},{'sha256':'x'*64},{'extra':'path'})
        for change in changes:
            with self.subTest(change=change),self.assertRaises(SpoolError):
                with self.spool.open_verified({**original,**change}): pass
        path=self.spool.workdir(original['namespace'])/original['leaf']; path.write_bytes(b'z'*original['size'])
        with self.assertRaises(SpoolError):
            with self.spool.open_verified(original): pass

    def test_input_size_quota_and_explicit_suffix_bounds(self):
        with self.assertRaises(SpoolError): self.spool.stage(self.source,max_bytes=10)
        with self.assertRaises(SpoolError): self.spool.stage(self.source,suffix='.exe')
        quota=MediaSpool(self.base/'quota',max_file_bytes=1024,max_total_bytes=1024)
        quota.stage(self.source); quota.stage(self.source)
        with self.assertRaisesRegex(SpoolError,'quota'): quota.stage(self.source)
        self.assertTrue(self.source.exists())

    def test_atomic_failed_copy_leaves_no_final_or_part(self):
        ns=self.spool.create_namespace(); calls=[]
        def revoke(): calls.append(1); return len(calls)==1
        with self.assertRaisesRegex(SpoolError,'revoked'): self.spool.stage(self.source,namespace=ns,validate=revoke)
        self.assertEqual({'.owner.json','.use.lock'},{p.name for p in self.spool.workdir(ns).iterdir()})

    def test_copy_only_to_validated_app_owned_namespace(self):
        descriptor=self.spool.stage(self.source); ns=self.spool.create_namespace()
        copied=self.spool.copy_to(descriptor,ns)
        self.assertEqual(descriptor['sha256'],copied['sha256'])
        self.assertNotEqual(descriptor['namespace'],copied['namespace'])
        with self.spool.open_verified(copied) as stream: self.assertEqual(self.source.read_bytes(),stream.read())
        with self.assertRaises(SpoolError): self.spool.copy_to(descriptor,'/tmp/arbitrary')

    @unittest.skipUnless(hasattr(os,'symlink'),'symlinks unavailable')
    def test_symlink_source_root_namespace_and_nested_cleanup_are_denied(self):
        link=self.base/'input-link.wav'; link.symlink_to(self.source)
        with self.assertRaises(SpoolError): self.spool.stage(link)
        root_link=self.base/'root-link'; root_link.symlink_to(self.spool.root,target_is_directory=True)
        with self.assertRaises(SpoolError): MediaSpool(root_link).create_namespace()
        ns=self.spool.create_namespace(); nested=self.spool.workdir(ns)/'escape'; nested.symlink_to(self.base,target_is_directory=True)
        with self.assertRaises(SpoolError): self.spool.cleanup(ns)
        self.assertTrue(self.source.exists()); self.assertTrue(nested.is_symlink())

    def test_reparse_attributes_are_denied_without_following(self):
        class WindowsInfo:
            st_mode=stat.S_IFDIR; st_file_attributes=0x400
        self.assertTrue(_unsafe(WindowsInfo()))

    @unittest.skipUnless(hasattr(os,'mkfifo'),'FIFO unavailable')
    def test_fifo_source_rejected_without_blocking(self):
        fifo=self.base/'fifo'; os.mkfifo(fifo)
        with self.assertRaises(SpoolError): self.spool.stage(fifo)

    def test_collect_respects_live_namespaces_age_and_budget(self):
        namespaces=[self.spool.stage(self.source)['namespace'] for _ in range(3)]
        old=time.time()-90000
        for ns in namespaces:
            for p in self.spool.workdir(ns).iterdir(): os.utime(p,(old,old))
            os.utime(self.spool.workdir(ns),(old,old))
        fresh=self.spool.stage(self.source)['namespace']
        removed=self.spool.collect({namespaces[0]},budget=1)
        self.assertEqual(1,len(removed)); self.assertNotIn(namespaces[0],removed)
        self.assertTrue(self.spool.workdir(fresh).exists())
        self.assertTrue(self.spool.workdir(namespaces[0]).exists())
        with self.assertRaises(SpoolError): self.spool.collect(set(),min_age_seconds=0)

    def test_unmarked_uuid_directory_is_never_deleted(self):
        ns=self.spool.create_namespace(); (self.spool.workdir(ns)/'.owner.json').unlink()
        with self.assertRaises(SpoolError): self.spool.cleanup(ns)
        self.assertTrue((self.spool.root/ns).exists())

    def test_parallel_staging_has_unique_atomic_files(self):
        ns=self.spool.create_namespace()
        with ThreadPoolExecutor(max_workers=4) as executor:
            descriptors=list(executor.map(lambda _:self.spool.stage(self.source,namespace=ns),range(4)))
        self.assertEqual(4,len({d['leaf'] for d in descriptors}))
        for descriptor in descriptors:
            with self.spool.open_verified(descriptor) as stream: self.assertEqual(descriptor['size'],len(stream.read()))

    def test_cleanup_scan_budget_is_bounded(self):
        spool=MediaSpool(self.base/'bounded',max_entries=16)
        ns=spool.create_namespace()
        for i in range(20): (spool.workdir(ns)/str(i)).write_bytes(b'x')
        with self.assertRaisesRegex(SpoolError,'scan_budget'): spool.cleanup(ns)
        self.assertTrue(spool.workdir(ns).exists())

    def test_files_over_codec_32mib_are_supported_without_base64(self):
        big=self.base/'synthetic.mp4'
        with big.open('wb') as stream: stream.truncate(33*1024**2)
        spool=MediaSpool(self.base/'large',max_file_bytes=50*1024**2,max_total_bytes=100*1024**2)
        descriptor=spool.stage(big)
        self.assertEqual(33*1024**2,descriptor['size'])
        self.assertLess(len(str(descriptor)),300)
        with spool.open_verified(descriptor) as stream: self.assertEqual(b'\0'*16,stream.read(16))

    def test_execution_hold_blocks_cleanup_and_supports_nested_verification(self):
        descriptor=self.spool.stage(self.source); ns=descriptor['namespace']
        with self.spool.hold(ns):
            with self.assertRaisesRegex(SpoolError,'namespace_busy'): self.spool.cleanup(ns)
            with self.spool.open_verified(descriptor) as stream: self.assertEqual(self.source.read_bytes(),stream.read())
            self.assertEqual(self.spool.workdir(ns)/descriptor['leaf'],self.spool.resolve(descriptor))
            self.assertGreaterEqual(self.spool.size_bytes(ns),descriptor['size'])
        self.assertTrue(self.spool.cleanup(ns))

    def test_transport_read_hold_blocks_cleanup_until_closed(self):
        descriptor=self.spool.stage(self.source)
        with self.spool.open_verified(descriptor):
            with self.assertRaisesRegex(SpoolError,'namespace_busy'): self.spool.cleanup(descriptor['namespace'])
        self.assertTrue(self.spool.cleanup(descriptor['namespace']))

    def test_independent_context_cannot_take_active_execution_lease(self):
        import contextvars
        descriptor=self.spool.stage(self.source); ns=descriptor['namespace']
        def attempt():
            with self.spool.hold(ns): pass
        with self.spool.hold(ns):
            with self.assertRaisesRegex(SpoolError,'namespace_busy'): contextvars.Context().run(attempt)

    def test_collect_skips_held_old_namespace(self):
        descriptor=self.spool.stage(self.source); ns=descriptor['namespace']; old=time.time()-90000
        for path in self.spool.workdir(ns).iterdir(): os.utime(path,(old,old))
        os.utime(self.spool.workdir(ns),(old,old))
        with self.spool.hold(ns): self.assertEqual([],self.spool.collect(set()))
        self.assertEqual([ns],self.spool.collect(set()))

    def test_execution_lease_is_cross_process(self):
        import subprocess,sys
        descriptor=self.spool.stage(self.source); ns=descriptor['namespace']
        script='''import sys
from bot.media_spool import MediaSpool,SpoolError
try:
    with MediaSpool(sys.argv[1]).hold(sys.argv[2]): pass
except SpoolError as e:
    raise SystemExit(0 if str(e)=='spool_namespace_busy' else 2)
raise SystemExit(3)
'''
        with self.spool.hold(ns):
            result=subprocess.run([sys.executable,'-c',script,str(self.spool.root),ns],capture_output=True,timeout=5)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_execution_context_propagates_to_async_thread_staging(self):
        import asyncio
        ns=self.spool.create_namespace()
        async def run():
            with self.spool.hold(ns):
                descriptor=await asyncio.to_thread(self.spool.stage,self.source,namespace=ns)
                size=await asyncio.to_thread(self.spool.size_bytes,ns)
                path=await asyncio.to_thread(self.spool.resolve,descriptor)
                self.assertGreaterEqual(size,descriptor['size'])
                self.assertEqual(ns,path.parent.name)
        asyncio.run(run())
        self.assertTrue(self.spool.cleanup(ns))

    def vanishing_scan(self,path):
        from contextlib import contextmanager
        from types import SimpleNamespace
        real_scandir=os.scandir
        @contextmanager
        def scan(directory):
            with real_scandir(directory) as listing:
                entries=list(listing)
            wrapped=[]
            for entry in entries:
                if Path(entry.path)==path:
                    def disappearing_stat(*,follow_symlinks=False):
                        path.unlink()
                        raise FileNotFoundError('synthetic vanished intermediate')
                    wrapped.append(SimpleNamespace(path=entry.path,stat=disappearing_stat))
                else: wrapped.append(entry)
            yield iter(wrapped)
        return patch('bot.media_spool.os.scandir',side_effect=scan)

    def test_accounting_tolerates_disappearing_entry_but_cleanup_is_strict(self):
        ns=self.spool.create_namespace(); transient=self.spool.workdir(ns)/'decoder.tmp'
        for action in (lambda:self.spool.size_bytes(ns),self.spool.create_namespace,lambda:self.spool.stage(self.source,namespace=ns)):
            transient.write_bytes(b'temporary')
            with self.vanishing_scan(transient): action()
            self.assertFalse(transient.exists())
        transient.write_bytes(b'temporary')
        with self.vanishing_scan(transient),self.assertRaises(FileNotFoundError): self.spool.cleanup(ns)
        self.assertTrue((self.spool.workdir(ns)/'.owner.json').exists())

    def test_accounting_tolerates_disappearing_directory_before_validation(self):
        from bot.media_spool import _check_path
        ns=self.spool.create_namespace(); transient=self.spool.workdir(ns)/'decoder'; transient.mkdir()
        def vanish(path,**kwargs):
            if Path(path)==transient and transient.exists(): transient.rmdir()
            return _check_path(path,**kwargs)
        with patch('bot.media_spool._check_path',side_effect=vanish): self.spool.size_bytes(ns)
        transient.mkdir()
        with patch('bot.media_spool._check_path',side_effect=vanish),self.assertRaises(SpoolError): self.spool.cleanup(ns)
        self.assertTrue((self.spool.workdir(ns)/'.owner.json').exists())

    def test_accounting_tolerates_directory_vanishing_before_scandir_only(self):
        ns=self.spool.create_namespace(); transient=self.spool.workdir(ns)/'decoder'; transient.mkdir()
        real_scandir=os.scandir
        def vanish(path):
            if Path(path)==transient: transient.rmdir()
            return real_scandir(path)
        with patch('bot.media_spool.os.scandir',side_effect=vanish): self.spool.size_bytes(ns)
        transient.mkdir()
        with patch('bot.media_spool.os.scandir',side_effect=vanish),self.assertRaises(FileNotFoundError): self.spool.cleanup(ns)

    def test_accounting_does_not_hide_permission_errors(self):
        ns=self.spool.create_namespace(); transient=self.spool.workdir(ns)/'decoder'; transient.mkdir()
        real_scandir=os.scandir
        def denied(path):
            if Path(path)==transient: raise PermissionError('synthetic denied directory')
            return real_scandir(path)
        with patch('bot.media_spool.os.scandir',side_effect=denied),self.assertRaises(PermissionError): self.spool.size_bytes(ns)
