"""Owned POSIX session watchdog with a parent-death pipe lease.

Unlike Windows Job Objects, POSIX process groups cannot survive an arbitrary
SIGKILL of this watchdog as a kernel-enforced ownership boundary. Normal bot
death closes the lease; cancellation signals are handled before bounded group
cleanup. No cgroup or system security configuration is changed.
"""
import os
import signal
import subprocess
import sys
import threading
import time


class ParentPipe:
    def read(self,count): return os.read(sys.stdin.fileno(),count)


def supervise(command,control):
    if not all(hasattr(os,k) for k in ('waitid','WNOWAIT','P_PID')):
        raise OSError('owned_posix_nonreaping_wait_unavailable')
    stopped=threading.Event()
    previous={sig:signal.signal(sig,lambda *_:stopped.set()) for sig in (signal.SIGTERM,signal.SIGINT)}
    child=None; exited=False
    try:
        if control.read(1)!=b'G': return 125
        child=subprocess.Popen(command,stdin=subprocess.DEVNULL,close_fds=True,start_new_session=True)
        def watch_parent():
            try: control.read(1)
            finally: stopped.set()
        threading.Thread(target=watch_parent,daemon=True).start()
        while True:
            # Do not reap the session leader before group cleanup: retaining
            # its PID prevents a later process/session from reusing that PGID.
            result=os.waitid(os.P_PID,child.pid,os.WEXITED|os.WNOHANG|os.WNOWAIT)
            if result is not None:
                exited=True
                return result.si_status if result.si_code==os.CLD_EXITED else 128+result.si_status
            if stopped.is_set(): return 125
            time.sleep(.025)
    finally:
        if child is not None:
            try:
                if not exited:
                    try: os.killpg(child.pid,signal.SIGTERM)
                    except ProcessLookupError: pass
                    time.sleep(.5)
                try: os.killpg(child.pid,signal.SIGKILL)
                except ProcessLookupError: pass
            finally:
                try: child.wait(timeout=1.)
                except subprocess.TimeoutExpired: pass
        for sig,handler in previous.items(): signal.signal(sig,handler)


def main():
    if os.name!='posix' or len(sys.argv)<3 or sys.argv[1]!='--': return 125
    try: return supervise(sys.argv[2:],ParentPipe())
    except Exception:
        print('owned_posix_process_containment_failed',file=sys.stderr,flush=True)
        return 125


if __name__=='__main__': raise SystemExit(main())
