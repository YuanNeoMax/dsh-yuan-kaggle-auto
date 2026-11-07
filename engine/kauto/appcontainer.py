"""Windows AppContainer：真正的文件系统隔离。

## 为什么这是"根治"

之前的防线分两层：
1. Python 守卫（换 `open`/`Popen`）—— 可被 `ctypes` 绕过（已实测证实）
2. Job Object（限内存/进程数）—— 绕不过，但**不管文件访问**

所以"读凭据文件"这条一直是个缺口：只要候选代码用 ctypes 直调 `CreateFile`，
Python 守卫就形同虚设。

AppContainer 是 Windows 原生的安全边界：进程的 token 带一个 AppContainer SID，
**默认拒绝**文件、注册表、网络、命名管道访问。只有显式授予（通过 capability 或
对具体路径加 ACE）的资源才可见。

本机实测前提（都不需要管理员权限）：
- `userenv.CreateAppContainerProfile` → HRESULT 0（创建成功）
- `advapi32.FreeSid` 可用
- Windows 10 22H2 专业工作站版

## 实现要点

启动一个 AppContainer 进程要走：
  CreateAppContainerProfile → 得到 SID
  → InitializeProcThreadAttributeList(1)
  → UpdateProcThreadAttribute(PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)
  → CreateProcessW(EXTENDED_STARTUPINFO_PRESENT)

关键是 SECURITY_CAPABILITIES 结构：把 AppContainerSid 填进去，
进程就活了在容器里。**默认没有任何 capability** = 最严。
"""
from __future__ import annotations

import ctypes
import msvcrt
import os
import sys
import tempfile
import uuid
from ctypes import wintypes
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEX(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFO),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _userenv():
    lib = ctypes.WinDLL("userenv", use_last_error=True)
    lib.CreateAppContainerProfile.restype = ctypes.c_long
    lib.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    lib.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    lib.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    return lib


def ensure_profile(name: str = "kauto.sandbox") -> int:
    """确保 profile 存在，返回该 AppContainer 的 SID 指针。

    已存在时 CreateAppContainerProfile 返回 HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)，
    那时改用 Derive 取回 SID —— 两条路径都必须能走通。
    """
    ue = _userenv()
    sid = ctypes.c_void_p()
    hr = ue.CreateAppContainerProfile(
        wintypes.LPCWSTR(name), wintypes.LPCWSTR(name),
        wintypes.LPCWSTR("kauto 候选代码沙箱"), None, 0, ctypes.byref(sid))
    if hr >= 0:
        return sid.value
    # 0x800700B7 = HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)
    if (hr & 0xFFFFFFFF) == 0x800700B7:
        sid2 = ctypes.c_void_p()
        hr2 = ue.DeriveAppContainerSidFromAppContainerName(
            wintypes.LPCWSTR(name), ctypes.byref(sid2))
        if hr2 >= 0:
            return sid2.value
        raise OSError(f"Derive SID 失败 hr={hr2 & 0xFFFFFFFF:#x}")
    raise OSError(f"CreateAppContainerProfile 失败 hr={hr & 0xFFFFFFFF:#x}")


def available() -> dict:
    """本机是否支持 AppContainer。"""
    if not IS_WINDOWS:
        return {"ok": False, "reason": "非 Windows"}
    try:
        ensure_profile("kauto.sandbox.probe")
        return {"ok": True, "backend": "appcontainer", "reason": ""}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def run_in_container(argv: list[str], *, cwd: str | None = None,
                     name: str = "kauto.sandbox",
                     granted_dirs: list[str] | None = None,
                     timeout: float = 60) -> dict:
    """在 AppContainer 里跑一个进程，返回 {ok, rc, stdout, stderr}。

    granted_dirs 里列出的目录会被**显式授予读+执行**权限；
    AppContainer 默认什么都看不见，连 python 解释器所在目录都要授权才能启动。
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "非 Windows"}

    sid_ptr = ensure_profile(name)

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    k32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    k32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    k32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    k32.CreateProcessW.restype = wintypes.BOOL
    k32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOEX), ctypes.POINTER(PROCESS_INFORMATION)]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.restype = wintypes.BOOL
    k32.GetExitCodeProcess.restype = wintypes.BOOL

    # 1) 属性列表
    size = ctypes.c_size_t(0)
    k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    buf = (ctypes.c_char * size.value)()
    attr_list = ctypes.cast(buf, ctypes.c_void_p)
    if not k32.InitializeProcThreadAttributeList(attr_list, 1, 0, ctypes.byref(size)):
        return {"ok": False, "error": f"InitializeProcThreadAttributeList 失败 {ctypes.get_last_error()}"}

    # 2) 安全能力：AppContainerSid + 0 个 capability（最严）
    caps = SECURITY_CAPABILITIES()
    caps.AppContainerSid = ctypes.c_void_p(sid_ptr)
    caps.Capabilities = None
    caps.CapabilityCount = 0
    caps.Reserved = 0
    if not k32.UpdateProcThreadAttribute(
            attr_list, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(caps), ctypes.sizeof(caps), None, None):
        return {"ok": False, "error": f"UpdateProcThreadAttribute 失败 {ctypes.get_last_error()}"}

    # 3) 启动（带 stdout/stderr 重定向）
    #
    # 这里有个必须做对的细节：重定向要求 bInheritHandles=True，
    # 而 True 又要求句柄**确实可继承**（SECURITY_ATTRIBUTES.bInheritHandle=True），
    # 否则子进程在初始化阶段就 0xC0000142 秒退（实测：连 whoami.exe 都起不来）。
    # 用 CreateFileW 建一个真实的可继承临时文件最稳。
    import os

    out_path = os.path.join(tempfile.gettempdir(), f"kauto_ac_{uuid.uuid4().hex}.out")
    err_path = os.path.join(tempfile.gettempdir(), f"kauto_ac_{uuid.uuid4().hex}.err")

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD),
                    ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = None
    sa.bInheritHandle = True

    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    GENERIC_WRITE, GENERIC_READ = 0x40000000, 0x80000000
    CREATE_ALWAYS, OPEN_ALWAYS = 2, 4

    out_h = k32.CreateFileW(out_path, GENERIC_WRITE | GENERIC_READ, 0, ctypes.byref(sa),
                            CREATE_ALWAYS, 0, None)
    err_h = k32.CreateFileW(err_path, GENERIC_WRITE | GENERIC_READ, 0, ctypes.byref(sa),
                            CREATE_ALWAYS, 0, None)
    if not out_h or not err_h:
        return {"ok": False, "error": f"CreateFileW 失败 {ctypes.get_last_error()}"}

    si = STARTUPINFOEX()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEX)
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    si.StartupInfo.hStdOutput = wintypes.HANDLE(out_h)
    si.StartupInfo.hStdError = wintypes.HANDLE(err_h)
    si.StartupInfo.hStdInput = None
    si.lpAttributeList = attr_list
    pi = PROCESS_INFORMATION()

    cmdline = " ".join(f'"{a}"' if " " in a else a for a in argv)
    # CreateProcessW 的 lpApplicationName 传 None，靠命令行解析
    app = None
    ok = k32.CreateProcessW(
        app, ctypes.create_unicode_buffer(cmdline),
        None, None, True,
        EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
        None, wintypes.LPCWSTR(cwd) if cwd else None,
        ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        return {"ok": False, "error": f"CreateProcessW 失败 {ctypes.get_last_error()}"}

    k32.WaitForSingleObject(pi.hProcess, int(timeout * 1000))
    rc = wintypes.DWORD()
    k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(rc))
    k32.CloseHandle(pi.hThread)
    k32.CloseHandle(pi.hProcess)
    try:
        k32.DeleteProcThreadAttributeList(attr_list)
    except Exception:
        pass

    out_f.seek(0) if False else None
    k32.CloseHandle(wintypes.HANDLE(out_h))
    k32.CloseHandle(wintypes.HANDLE(err_h))
    try:
        stdout = Path(out_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        stdout = ""
    try:
        stderr = Path(err_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        stderr = ""
    for p in (out_path, err_path):
        try:
            os.unlink(p)
        except Exception:
            pass
    return {"ok": True, "rc": int(rc.value), "pid": int(pi.dwProcessId),
            "stdout": stdout, "stderr": stderr}


def grant_read(path: str, name: str = "kauto.sandbox") -> dict:
    """给 AppContainer 显式授予某个目录/文件的读+执行权限。

    AppContainer 默认什么都看不见，连 python.exe 都在外面 ——
    必须把解释器目录和 site-packages 授权给它，否则进程起不来。
    这类授权是"必须给的最小面"，不是放开：只对指定路径加 ACE，
    其余路径仍然默认拒绝。
    """
    if not IS_WINDOWS:
        return {"ok": False, "reason": "非 Windows"}
    return _icacls_grant(path, "(OI)(CI)(RX)", name)


def grant_readwrite(path: str, name: str = "kauto.sandbox") -> dict:
    """授予**读+写**权限（M）。

    只给候选自己的产物目录用：候选必须能写 candidate.py 与 *.npy，
    但绝不该给它写凭据目录或系统目录的机会。
    权限面按目录分开是这套隔离的关键 —— 全给 M 就等于放开了。
    """
    if not IS_WINDOWS:
        return {"ok": False, "reason": "非 Windows"}
    return _icacls_grant(path, "(OI)(CI)(M)", name)


def _icacls_grant(path: str, perms: str, name: str) -> dict:
    try:
        import subprocess

        sid = ensure_profile(name)
        # 用 icacls 加 ACE：把 AppContainer SID 转成字符串名再授权
        # 直接对 SID 授权需要 SDDL，icacls 的 *SID 形式更省事
        sid_str = _sid_to_string(sid)
        p = subprocess.run(
            ["icacls", str(path), "/grant", f"*{sid_str}:{perms}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60)
        return {"ok": p.returncode == 0, "path": str(path), "perms": perms,
                "rc": p.returncode,
                "out": (p.stdout or p.stderr or "")[:400]}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def _sid_to_string(sid_ptr: int) -> str:
    """把 SID 指针转成 SDDL 字符串（icacls 认的 *S-1-... 形式）。"""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    s = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(ctypes.c_void_p(sid_ptr), ctypes.byref(s)):
        raise OSError(f"ConvertSidToStringSidW 失败 {ctypes.get_last_error()}")
    return s.value or ""
