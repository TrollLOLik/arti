"""Explicit native containment smoke: python -m tools.media_process_smoke --platform posix|windows.

Windows Job Object execution is not validated by Linux contract tests.
"""
import argparse
import asyncio
import os
import sys
import unittest
from utils.process_limits import create_owned_subprocess_exec,communicate_bounded

class PosixSmoke(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_kills_owned_descendant_but_not_unrelated_process(self):
        import psutil
        unrelated=await asyncio.create_subprocess_exec(sys.executable,'-c','import time; time.sleep(60)')
        try:
            code='import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);print(p.pid,flush=True);time.sleep(60)'
            process=await create_owned_subprocess_exec(sys.executable,'-c',code,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            child_pid=int(await process.stdout.readline())
            with self.assertRaises(TimeoutError): await communicate_bounded(process,.05)
            self.assertIsNotNone(process.returncode)
            self.assertTrue(not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status()==psutil.STATUS_ZOMBIE)
            self.assertIsNone(unrelated.returncode)
        finally:
            unrelated.kill(); await unrelated.wait()
    async def test_abrupt_parent_death_closes_lease_and_stops_descendants(self):
        import psutil
        controller='''import asyncio,os,sys
from utils.process_limits import create_owned_subprocess_exec
async def run():
 code='import subprocess,sys,os,time; p=subprocess.Popen([sys.executable,"-c","import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"]);print(os.getpid(),p.pid,flush=True);time.sleep(60)'
 p=await create_owned_subprocess_exec(sys.executable,'-c',code,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
 line=await p.stdout.readline()
 print(p.pid,line.decode().strip(),flush=True)
 os._exit(0)
asyncio.run(run())
'''
        process=await asyncio.create_subprocess_exec(sys.executable,'-c',controller,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        pids=[int(v) for v in (await asyncio.wait_for(process.stdout.readline(),5)).split()]
        self.assertEqual(3,len(pids)); await asyncio.wait_for(process.wait(),5)
        for _ in range(80):
            alive=[pid for pid in pids if psutil.pid_exists(pid) and psutil.Process(pid).status()!=psutil.STATUS_ZOMBIE]
            if not alive: break
            await asyncio.sleep(.025)
        self.assertEqual([],alive)


class WindowsSmoke(unittest.IsolatedAsyncioTestCase):
    async def test_real_windows_timeout_kills_parent_and_grandchild(self):
        import psutil
        code='import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);print(p.pid,flush=True);time.sleep(60)'
        process=await create_owned_subprocess_exec(sys.executable,'-c',code,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        child=int(await asyncio.wait_for(process.stdout.readline(),10))
        with self.assertRaises(TimeoutError): await communicate_bounded(process,.05)
        for _ in range(40):
            if not psutil.pid_exists(child): break
            await asyncio.sleep(.05)
        self.assertFalse(psutil.pid_exists(child))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--platform',required=True,choices=('posix','windows'))
    args=parser.parse_args()
    expected='nt' if args.platform=='windows' else 'posix'
    if os.name!=expected: raise SystemExit('This smoke test requires the selected native platform.')
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(WindowsSmoke if args.platform=='windows' else PosixSmoke)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
