"""OS resource bounds for the trusted temporary-document parser subprocess."""
from __future__ import annotations

import os


MEMORY_BYTES = 512 * 1024 * 1024
CPU_SECONDS = 20
_job_handle = None


def restrict_process() -> None:
    global _job_handle
    if os.name != "nt":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        return
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("basic", BasicLimit), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    handle = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimit()
    limits.basic.flags = 0x00000100 | 0x00000002 | 0x00000008
    limits.basic.process_time = CPU_SECONDS * 10_000_000
    limits.basic.active_processes = 1
    limits.process_memory = MEMORY_BYTES
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise RuntimeError("Document parser isolation could not be established")
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise RuntimeError("Document parser process limit could not be established")
    _job_handle = handle

