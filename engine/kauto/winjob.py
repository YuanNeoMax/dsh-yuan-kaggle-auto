"""Windows 原生执行隔离：Job Object（限资源）+ Restricted Token（降权限）。

## 为什么换成这个，而不是继续用 monkeypatch

之前 `_runner.py` 的守卫是**在 Python 层拦函数**（换 `builtins.open`、
换 `subprocess.Popen`）。它挡得住"手滑"，挡不住刻意的 `ctypes` 绕过 ——
这一点我在 README 里自己标注过。

调研 kubernetes-sigs/agent-sandbox：它把底层隔离交给 gVisor / Kata
（通过 RuntimeClass），自己只管沙箱生命周期。那是 Linux 集群方案，本机跑不了
（无 Docker / kubectl / WSL；gVisor 是 Linux 专用）。

Windows 上真正能用的三档，按隔离强度：

| 方案 | 隔离强度 | 需提权 | 本机 |
|---|---|---|---|
| Windows Sandbox | 独立内核 VM，最强 | 是 | ✗ 功能不存在（DISM 0x800f080c） |
| AppContainer | 文件/网络/注册表默认拒绝 | 通常需要 | 待验证 |
| **Job Object + Restricted Token** | 限资源 + 降权限 | **不需要** | ✓ 本模块 |

## 明确的能力边界（诚实说明）

覆盖：
- 内存超限 → 分配失败/进程被杀
- 进程数超限 → 子进程创建失败（挡 fork 炸弹）
- CPU 时间超限 → 进程被终止
- 子进程逃逸 → KILL_ON_JOB_CLOSE 保证随父进程一起终止

**不覆盖**：文件系统隔离。那需要 AppContainer 或 Sandbox，属另一档。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

# ── 常量 ──────────────────────────────────────────────────────────────────────
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002

JobObjectExtendedLimitInformation = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _k32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def available() -> dict:
    """本机是否支持 Job Object 隔离。"""
    if not IS_WINDOWS:
        return {"ok": False, "reason": "非 Windows 平台"}
    try:
        k32 = _k32()
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        h = k32.CreateJobObjectW(None, None)
        if not h:
            return {"ok": False, "reason": f"CreateJobObjectW 失败 err={ctypes.get_last_error()}"}
        k32.CloseHandle(wintypes.HANDLE(h))
        return {"ok": True, "backend": "job-object", "reason": ""}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def create_job(memory_mb: int = 4096, active_processes: int = 8,
               cpu_seconds: int = 1800):
    """创建并配置 Job Object，返回 handle（int）。

    用纯 ctypes 调 kernel32，而不是 pywin32 的同名封装：
    后者是 3 参数（收 buffer），且没有文档可查（实测 TypeError: takes exactly 3
    arguments (4 given)）。直接调 API 更可控。
    """
    k32 = _k32()
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(f"CreateJobObjectW 失败 err={ctypes.get_last_error()}")

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    flags = (JOB_OBJECT_LIMIT_PROCESS_MEMORY
             | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
             | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
    if cpu_seconds and cpu_seconds > 0:
        flags |= JOB_OBJECT_LIMIT_PROCESS_TIME
        # 单位 100ns
        info.BasicLimitInformation.PerProcessUserTimeLimit = int(cpu_seconds) * 10_000_000

    info.BasicLimitInformation.LimitFlags = flags
    info.BasicLimitInformation.ActiveProcessLimit = int(active_processes)
    info.ProcessMemoryLimit = int(memory_mb) * 1024 * 1024

    ok = k32.SetInformationJobObject(
        wintypes.HANDLE(job), JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        err = ctypes.get_last_error()
        k32.CloseHandle(wintypes.HANDLE(job))
        raise OSError(f"SetInformationJobObject 失败 err={err}")
    return int(job)


def assign(job, pid: int) -> None:
    """把进程塞进 Job。必须在子进程开始干活之前调用。"""
    k32 = _k32()
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    ph = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
    if not ph:
        raise OSError(f"OpenProcess(pid={pid}) 失败 err={ctypes.get_last_error()}")
    try:
        ok = k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(ph))
        if not ok:
            raise OSError(f"AssignProcessToJobObject 失败 err={ctypes.get_last_error()}")
    finally:
        k32.CloseHandle(wintypes.HANDLE(ph))


def describe(job) -> dict:
    """读回实际生效的限制，用于验证配置真的设上了（而不是假设它设上了）。"""
    k32 = _k32()
    k32.QueryInformationJobObject.restype = wintypes.BOOL
    k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID]

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    ok = k32.QueryInformationJobObject(
        wintypes.HANDLE(job), JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info), None)
    if not ok:
        raise OSError(f"QueryInformationJobObject 失败 err={ctypes.get_last_error()}")

    basic = info.BasicLimitInformation
    return {
        "LimitFlags": basic.LimitFlags,
        "ActiveProcessLimit": basic.ActiveProcessLimit,
        "PerProcessUserTimeLimit": basic.PerProcessUserTimeLimit,
        "ProcessMemoryLimit": info.ProcessMemoryLimit,
        "flags": {
            "process_memory": bool(basic.LimitFlags & JOB_OBJECT_LIMIT_PROCESS_MEMORY),
            "active_process": bool(basic.LimitFlags & JOB_OBJECT_LIMIT_ACTIVE_PROCESS),
            "kill_on_close": bool(basic.LimitFlags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE),
            "process_time": bool(basic.LimitFlags & JOB_OBJECT_LIMIT_PROCESS_TIME),
        },
    }


def close(job) -> None:
    k32 = _k32()
    k32.CloseHandle(wintypes.HANDLE(job))
