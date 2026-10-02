"""Fixed Windows 10+ media launcher; stdlib only, no bot/provider imports.

Job-list assignment is atomic with creation, so even a helper crash between
CreateProcessW and ResumeThread cannot strand an uncontained suspended child.
Reference: https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
"""
from contextlib import contextmanager
import ctypes as C
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

DWORD=C.c_uint32
HANDLE=C.c_void_p
SIZE_T=C.c_size_t
CREATE_SUSPENDED=0x00000004
CREATE_NO_WINDOW=0x08000000
EXTENDED_STARTUPINFO_PRESENT=0x00080000
PROC_THREAD_ATTRIBUTE_HANDLE_LIST=0x00020002
PROC_THREAD_ATTRIBUTE_JOB_LIST=0x0002000D
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x00002000
WAIT_OBJECT_0=0
WAIT_TIMEOUT=258


class ParentPipe:
    # Raw reads avoid a daemon holding BufferedReader's lock at interpreter exit.
    def read(self,count): return os.read(sys.stdin.fileno(),count)


class BasicLimits(C.Structure):
    _fields_=[('process_time',C.c_int64),('job_time',C.c_int64),('flags',DWORD),
              ('min_working',SIZE_T),('max_working',SIZE_T),('active_processes',DWORD),
              ('affinity',SIZE_T),('priority',DWORD),('scheduling',DWORD)]


class IOCounters(C.Structure):
    _fields_=[(name,C.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]


class ExtendedLimits(C.Structure):
    _fields_=[('basic',BasicLimits),('io',IOCounters),('process_memory',SIZE_T),
              ('job_memory',SIZE_T),('peak_process',SIZE_T),('peak_job',SIZE_T)]


class StartupInfo(C.Structure):
    _fields_=[('cb',DWORD),('reserved',C.c_wchar_p),('desktop',C.c_wchar_p),('title',C.c_wchar_p),
              *[(name,DWORD) for name in ('x','y','xsize','ysize','xchars','ychars','fill','flags')],
              ('show',C.c_uint16),('reserved_size',C.c_uint16),('reserved_bytes',C.c_void_p),
              ('stdin',HANDLE),('stdout',HANDLE),('stderr',HANDLE)]


class StartupInfoEx(C.Structure):
    _fields_=[('startup',StartupInfo),('attributes',C.c_void_p)]


class ProcessInfo(C.Structure):
    _fields_=[('process',HANDLE),('thread',HANDLE),('pid',DWORD),('tid',DWORD)]


class WindowsBackend:
    def __init__(self,kernel=None):
        if kernel is None:
            if os.name!='nt': raise OSError('windows_job_requires_windows')
            kernel=C.WinDLL('kernel32',use_last_error=True)
        self.kernel=kernel
        declarations={
            'CreateJobObjectW':([C.c_void_p,C.c_wchar_p],HANDLE),
            'SetInformationJobObject':([HANDLE,C.c_int,C.c_void_p,DWORD],C.c_int),
            'InitializeProcThreadAttributeList':([C.c_void_p,DWORD,DWORD,C.POINTER(SIZE_T)],C.c_int),
            'UpdateProcThreadAttribute':([C.c_void_p,DWORD,SIZE_T,C.c_void_p,SIZE_T,C.c_void_p,C.c_void_p],C.c_int),
            'DeleteProcThreadAttributeList':([C.c_void_p],None),
            'CreateProcessW':([C.c_wchar_p,C.c_wchar_p,C.c_void_p,C.c_void_p,C.c_int,DWORD,C.c_void_p,C.c_wchar_p,C.c_void_p,C.POINTER(ProcessInfo)],C.c_int),
            'ResumeThread':([HANDLE],DWORD),
            'WaitForSingleObject':([HANDLE,DWORD],DWORD),
            'GetExitCodeProcess':([HANDLE,C.POINTER(DWORD)],C.c_int),
            'TerminateJobObject':([HANDLE,C.c_uint32],C.c_int),
            'CloseHandle':([HANDLE],C.c_int),
            'GetStdHandle':([DWORD],HANDLE),
            'SetHandleInformation':([HANDLE,DWORD,DWORD],C.c_int),
        }
        for name,(args,result) in declarations.items():
            function=getattr(kernel,name); function.argtypes=args; function.restype=result

    @staticmethod
    def check(ok,operation):
        if not ok:
            error=C.get_last_error() if hasattr(C,'get_last_error') else 0
            raise OSError(error,'windows_job_'+operation+'_failed')

    def create_job(self):
        job=self.kernel.CreateJobObjectW(None,None)
        self.check(job,'create')
        limits=ExtendedLimits(); limits.basic.flags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            self.check(self.kernel.SetInformationJobObject(job,9,C.byref(limits),C.sizeof(limits)),'limits')
        except BaseException:
            self.close(job)
            raise
        return job

    @contextmanager
    def standard_handles(self):
        import msvcrt
        with open(os.devnull,'rb',buffering=0) as null:
            handles=(msvcrt.get_osfhandle(null.fileno()),self.kernel.GetStdHandle((-11)&0xffffffff),
                     self.kernel.GetStdHandle((-12)&0xffffffff))
            for handle in set(handles):
                if handle in (None,0,-1,C.c_void_p(-1).value): raise OSError('windows_job_standard_handle_unavailable')
                self.check(self.kernel.SetHandleInformation(handle,1,1),'handle_inheritance')
            yield handles

    def create_suspended(self,command,job):
        executable=shutil.which(command[0])
        if not executable or Path(executable).suffix.lower()!='.exe':
            raise OSError('windows_job_executable_unavailable')
        executable=str(Path(executable).resolve())
        commandline=subprocess.list2cmdline([executable,*command[1:]])
        if '\0' in commandline or len(commandline)>=32767: raise OSError('windows_job_command_invalid')
        size=SIZE_T()
        self.kernel.InitializeProcThreadAttributeList(None,2,0,C.byref(size))
        if not size.value: raise OSError('windows_job_attributes_unavailable')
        buffer=C.create_string_buffer(size.value)
        self.check(self.kernel.InitializeProcThreadAttributeList(buffer,2,0,C.byref(size)),'attributes')
        try:
            with self.standard_handles() as (stdin,stdout,stderr):
                handles=(HANDLE*len(set((stdin,stdout,stderr))))(*sorted(set((stdin,stdout,stderr))))
                jobs=(HANDLE*1)(job)
                for attribute,values in ((PROC_THREAD_ATTRIBUTE_HANDLE_LIST,handles),(PROC_THREAD_ATTRIBUTE_JOB_LIST,jobs)):
                    self.check(self.kernel.UpdateProcThreadAttribute(buffer,0,attribute,C.byref(values),C.sizeof(values),None,None),'assignment_attribute')
                startup=StartupInfoEx(); startup.startup.cb=C.sizeof(startup)
                startup.startup.flags=0x100
                startup.startup.stdin, startup.startup.stdout, startup.startup.stderr=stdin,stdout,stderr
                startup.attributes=C.addressof(buffer)
                info=ProcessInfo()
                flags=CREATE_SUSPENDED|CREATE_NO_WINDOW|EXTENDED_STARTUPINFO_PRESENT
                self.check(self.kernel.CreateProcessW(executable,C.create_unicode_buffer(commandline),None,None,True,flags,
                                                      None,None,C.byref(startup),C.byref(info)),'launch')
                return info.process,info.thread
        finally:
            self.kernel.DeleteProcThreadAttributeList(buffer)

    def resume(self,thread):
        # Exactly one suspend count was requested; no fallback resumes.
        self.check(self.kernel.ResumeThread(thread)==1,'resume')

    def wait(self,process,milliseconds):
        result=self.kernel.WaitForSingleObject(process,milliseconds)
        if result not in (WAIT_OBJECT_0,WAIT_TIMEOUT): raise OSError('windows_job_wait_failed')
        return result==WAIT_OBJECT_0

    def exit_code(self,process):
        code=DWORD(); self.check(self.kernel.GetExitCodeProcess(process,C.byref(code)),'exit_code')
        return code.value

    def terminate_job(self,job): self.check(self.kernel.TerminateJobObject(job,125),'terminate')
    def close(self,handle):
        if handle: self.kernel.CloseHandle(handle)


def supervise(command,backend,control):
    """Only the job is terminated; no process lookup or system-wide PID kills."""
    job=backend.create_job(); process=thread=None
    try:
        process,thread=backend.create_suspended(command,job)
        if control.read(1)!=b'G': return 125
        stopped=threading.Event()
        def watch_parent():
            try: control.read(1)
            finally: stopped.set()
        threading.Thread(target=watch_parent,daemon=True).start()
        backend.resume(thread); backend.close(thread); thread=None
        while not backend.wait(process,100):
            if stopped.is_set():
                backend.terminate_job(job)
                backend.wait(process,2000)
                return 125
        return backend.exit_code(process)
    finally:
        # Closing the last, non-inherited job handle also removes descendants
        # after successful root exit and after exceptions/parent death.
        try: backend.close(job)
        finally:
            backend.close(thread); backend.close(process)


def main():
    if os.name!='nt' or len(sys.argv)<3 or sys.argv[1]!='--': return 125
    try: return supervise(sys.argv[2:],WindowsBackend(),ParentPipe())
    except Exception:
        # Do not echo payload argv, source paths, credentials, or provider text.
        print('owned_windows_process_containment_failed',file=sys.stderr,flush=True)
        return 125


if __name__=='__main__': raise SystemExit(main())
