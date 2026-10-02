"""Owned-only cleanup contracts; real Windows Job integration needs Windows."""
import asyncio
from contextlib import contextmanager
import ctypes as C
from io import BytesIO
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,MagicMock,patch

from utils.process_limits import create_owned_subprocess_exec,communicate_bounded,stop_process,_WindowsOwnedProcess
from utils import windows_owned_process as win


class Backend:
    def __init__(self,fail=None): self.events=[]; self.fail=fail
    def create_job(self): self.events.append('job'); return 10
    def create_suspended(self,command,job):
        self.events.append(('create_suspended',tuple(command),job))
        if self.fail=='create': raise OSError('unsupported_job_assignment')
        return 20,30
    def resume(self,thread):
        self.events.append(('resume',thread))
        if self.fail=='resume': raise OSError('resume_failed')
    def wait(self,process,timeout): self.events.append(('wait',process,timeout)); return self.fail!='parent_eof' or timeout==2000
    def exit_code(self,process): return 7
    def terminate_job(self,job): self.events.append(('terminate_job',job))
    def close(self,handle):
        if handle: self.events.append(('close',handle))


class WindowsJobContracts(unittest.TestCase):
    def test_job_has_kill_on_close_and_no_breakaway_flag(self):
        kernel=self.kernel(); flags=[]
        def limits(job,kind,pointer,size):
            flags.append(C.cast(pointer,C.POINTER(win.ExtendedLimits)).contents.basic.flags)
            return 1
        kernel.SetInformationJobObject.side_effect=limits
        self.assertEqual(10,win.WindowsBackend(kernel).create_job())
        self.assertEqual([win.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE],flags)

    def test_no_grant_never_resumes_payload_and_closes_ownership(self):
        backend=Backend()
        self.assertEqual(125,win.supervise(['payload.exe'],backend,BytesIO()))
        self.assertFalse(any(e[0]=='resume' for e in backend.events if isinstance(e,tuple)))
        self.assertIn(('close',10),backend.events); self.assertIn(('close',20),backend.events)

    def test_success_returns_payload_status_and_closes_job_after_root_exit(self):
        backend=Backend()
        self.assertEqual(7,win.supervise(['payload.exe','argument'],backend,BytesIO(b'G')))
        self.assertLess(backend.events.index(('resume',30)),backend.events.index(('close',10)))
        self.assertEqual(1,backend.events.count(('close',10)))

    def test_native_failures_fail_closed_without_uncontained_fallback(self):
        for failure in ('create','resume'):
            backend=Backend(failure)
            with self.assertRaises(OSError): win.supervise(['payload.exe'],backend,BytesIO(b'G'))
            self.assertEqual(1,backend.events.count(('close',10)))
            self.assertEqual(1,sum(isinstance(e,tuple) and e[0]=='create_suspended' for e in backend.events))

    def test_parent_lease_eof_terminates_job_not_discovered_pids(self):
        backend=Backend('parent_eof')
        self.assertEqual(125,win.supervise(['payload.exe'],backend,BytesIO(b'G')))
        self.assertIn(('terminate_job',10),backend.events)
        self.assertIn(('wait',20,2000),backend.events)

    def kernel(self):
        kernel=MagicMock(); kernel.CreateJobObjectW.return_value=10
        kernel.SetInformationJobObject.return_value=1
        def initialize(buffer,count,flags,size):
            C.cast(size,C.POINTER(win.SIZE_T)).contents.value=128
            return bool(buffer)
        kernel.InitializeProcThreadAttributeList.side_effect=initialize
        kernel.UpdateProcThreadAttribute.return_value=1
        def create(application,command,pa,ta,inherit,flags,env,cwd,startup,info):
            value=C.cast(info,C.POINTER(win.ProcessInfo)).contents
            value.process,value.thread,value.pid,value.tid=20,30,200,300
            return 1
        kernel.CreateProcessW.side_effect=create
        return kernel

    def test_native_creation_atomically_assigns_job_and_only_standard_handles(self):
        kernel=self.kernel(); backend=win.WindowsBackend(kernel)
        @contextmanager
        def handles(): yield 101,102,102
        with patch.object(backend,'standard_handles',handles),patch.object(win.shutil,'which',return_value='/synthetic/python.exe'):
            self.assertEqual((20,30),backend.create_suspended(['python.exe','payload.py'],10))
        attributes=[c.args[2] for c in kernel.UpdateProcThreadAttribute.call_args_list]
        self.assertEqual([win.PROC_THREAD_ATTRIBUTE_HANDLE_LIST,win.PROC_THREAD_ATTRIBUTE_JOB_LIST],attributes)
        handle_args=kernel.UpdateProcThreadAttribute.call_args_list[0].args
        passed=list(C.cast(handle_args[3],C.POINTER(win.HANDLE*2)).contents)
        self.assertEqual([101,102],passed)
        call=kernel.CreateProcessW.call_args.args
        self.assertEqual(win.CREATE_SUSPENDED|win.CREATE_NO_WINDOW|win.EXTENDED_STARTUPINFO_PRESENT,call[5])
        self.assertTrue(call[4]); kernel.DeleteProcThreadAttributeList.assert_called_once()

    def test_unsupported_job_attribute_prevents_createprocess(self):
        kernel=self.kernel(); backend=win.WindowsBackend(kernel)
        kernel.UpdateProcThreadAttribute.side_effect=[1,0]
        @contextmanager
        def handles(): yield 101,102,103
        with patch.object(backend,'standard_handles',handles),patch.object(win.shutil,'which',return_value='/synthetic/python.exe'):
            with self.assertRaises(OSError): backend.create_suspended(['python.exe'],10)
        kernel.CreateProcessW.assert_not_called(); kernel.DeleteProcThreadAttributeList.assert_called_once()


class OwnedProcessTests(unittest.IsolatedAsyncioTestCase):
    def raw_windows_process(self):
        return SimpleNamespace(stdin=SimpleNamespace(write=MagicMock(),drain=AsyncMock(),close=MagicMock()),
            stdout=SimpleNamespace(read=AsyncMock(return_value=b'output')),
            stderr=SimpleNamespace(read=AsyncMock(return_value=b'')),returncode=None,pid=123,
            wait=AsyncMock(return_value=0),terminate=MagicMock(),kill=MagicMock())

    async def test_windows_launch_grants_only_fixed_helper_and_retains_control_lease(self):
        raw=self.raw_windows_process()
        with patch('utils.process_limits._is_windows',return_value=True),patch('asyncio.create_subprocess_exec',new=AsyncMock(return_value=raw)) as launch:
            process=await create_owned_subprocess_exec('payload.exe','argument',stdout=asyncio.subprocess.PIPE)
        command=launch.await_args.args; options=launch.await_args.kwargs
        self.assertEqual([sys.executable,'-I'],list(command[:2]))
        self.assertTrue(command[2].endswith('windows_owned_process.py'))
        self.assertEqual(('--','payload.exe','argument'),command[3:])
        self.assertEqual(asyncio.subprocess.PIPE,options['stdin'])
        raw.stdin.write.assert_called_once_with(b'G'); raw.stdin.close.assert_not_called()
        self.assertIsNone(process.stdin)
        with patch('utils.process_limits._is_windows',return_value=True):
            with self.assertRaises(ValueError): await create_owned_subprocess_exec('payload.exe',stdin=asyncio.subprocess.PIPE)

    async def test_windows_cancellation_before_launch_completion_never_grants_payload(self):
        raw=self.raw_windows_process(); started=asyncio.Event(); release=asyncio.Event()
        async def launch(*args,**kwargs): started.set(); await release.wait(); return raw
        with patch('utils.process_limits._is_windows',return_value=True),patch('asyncio.create_subprocess_exec',side_effect=launch):
            task=asyncio.create_task(create_owned_subprocess_exec('payload.exe'))
            await started.wait(); task.cancel(); release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        raw.stdin.write.assert_not_called(); raw.stdin.close.assert_called(); raw.terminate.assert_called_once()

    async def test_windows_adapter_never_feeds_or_closes_lease_during_collection(self):
        stdin=SimpleNamespace(close=MagicMock())
        stdout=SimpleNamespace(read=AsyncMock(return_value=b'output'))
        stderr=SimpleNamespace(read=AsyncMock(return_value=b''))
        raw=SimpleNamespace(stdin=stdin,stdout=stdout,stderr=stderr,wait=AsyncMock(return_value=0),communicate=AsyncMock())
        process=_WindowsOwnedProcess(raw)
        self.assertEqual((b'output',b''),await process.communicate())
        raw.communicate.assert_not_awaited(); stdin.close.assert_called_once()
        with self.assertRaises(ValueError): await process.communicate(b'not-a-control-message')


    async def test_cancel_during_launch_cleans_process_that_finishes_launching_late(self):
        spawned=asyncio.Event(); release=asyncio.Event()
        raw=self.raw_windows_process()
        async def launch(*args,**kwargs): spawned.set(); await release.wait(); return raw
        with patch('asyncio.create_subprocess_exec',side_effect=launch),patch('utils.process_limits.os.killpg'):
            task=asyncio.create_task(create_owned_subprocess_exec('fixture'))
            await spawned.wait(); task.cancel(); release.set()
            with self.assertRaises(asyncio.CancelledError): await task
            raw.stdin.write.assert_not_called(); raw.terminate.assert_called_once()


    async def test_dubbing_uses_explicit_existing_output_root(self):
        import ai.dubbing as dubbing
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); tool=root/'tool'; tool.mkdir(); outputs=root/'attempt'; outputs.mkdir()
            (tool/'main.py').write_text('import pathlib,sys\np=pathlib.Path(sys.argv[sys.argv.index("--output")+1]);p.write_bytes(b"fixture")\n')
            with patch.object(dubbing,'VIDEOTRANS_DIR',tool),patch.object(dubbing,'VIDEOTRANS_RUNS',root/'unused'),patch.object(dubbing,'_resolve_python',return_value=Path(sys.executable)):
                ok,path,error=await dubbing.run_dubbing('', 'generation',input_file=root/'input.wav',output_root=outputs)
                self.assertTrue(ok,error); self.assertEqual(outputs/'generation'/'dubbed.mp4',path)
                self.assertFalse((root/'unused').exists())
                for invalid in ('../outside','a/b',''):
                    with self.assertRaises(ValueError): await dubbing.run_dubbing('',invalid,input_file=root/'input.wav',output_root=outputs)
                with self.assertRaises(ValueError): await dubbing.run_dubbing('','valid',input_file=root/'input.wav',output_root=root/'missing')


