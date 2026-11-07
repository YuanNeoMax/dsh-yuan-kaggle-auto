"""对照实验（修正版）：直接观察 run() 内部的 stdout，而不是事后重跑。

之前的写法有个方法论错误：run() 返回后我又单独起了一次 _run.py 拿输出，
那次进程没有被 assign 进 Job —— 于是"有 Job 限制"组测的其实还是无限制情形，
两组结果必然一样。

正确做法：让 run() 把子进程 stdout 也带回来。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import _runner, winjob

if not winjob.available()["ok"]:
    print("跳过：本机无 Job Object")
    raise SystemExit(0)

BYPASS = '''
def build_features(train, valid, test):
    import ctypes
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.WinExec.restype = ctypes.c_uint
        k32.WinExec.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        rc = k32.WinExec(b"cmd /c echo bypass", 0)
        print("CTYPES_EXEC rc=", rc, flush=True)
    except Exception as e:
        print("CTYPES_EXEC_FAIL", type(e).__name__, flush=True)
    return train[["x1"]], valid[["x1"]], test[["x1"]], train["target"], valid["target"]

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import numpy as np
    try:
        big = bytearray(3000 * 1024 * 1024)
        big[::4096] = b"x" * len(big[::4096])
        print("BIG_ALLOC_OK", flush=True)
    except MemoryError:
        print("BIG_ALLOC_BLOCKED", flush=True)
    print("FINAL_MARK", flush=True)
    return (np.linspace(0.05,0.95,len(X_valid)), np.linspace(0.05,0.95,len(X_test)))
'''

w = Path("work/demo-comp").resolve()


def probe(label: str, limits: dict | None) -> dict:
    """直接调用底层，捕获 stdout（不走 _runner.run 的 JSON 提取）。"""
    import subprocess
    from kauto import storage

    tag = f"byp2_{label.replace(' ', '_')}"
    gen = w / f"gen_{tag}"
    gen.mkdir(parents=True, exist_ok=True)
    storage.write_text(gen / "candidate.py", BYPASS)
    script = gen / "_run.py"
    storage.write_text(script, _runner._RUNNER_SRC.format(
        gen=str(gen), work=str(w), folds=0))

    p = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", cwd=str(gen))
    job = None
    if limits:
        job = winjob.create_job(**limits)
        winjob.assign(job, p.pid)
    out, err = p.communicate(timeout=120)
    if job is not None:
        winjob.close(job)
    return {"stdout": out, "stderr": err, "rc": p.returncode}


print("=== 无 Job 限制 ===")
a = probe("nojob", None)
for ln in a["stdout"].splitlines():
    if any(k in ln for k in ("CTYPES", "BIG_ALLOC", "FINAL_MARK")):
        print("   ", ln.strip())

print("\n=== 有 Job 限制（内存 512MB / 进程数 2）===")
b = probe("withjob", {"memory_mb": 512, "active_processes": 2, "cpu_seconds": 600})
for ln in b["stdout"].splitlines():
    if any(k in ln for k in ("CTYPES", "BIG_ALLOC", "FINAL_MARK")):
        print("   ", ln.strip())
print("   rc:", b["rc"])
if b["stderr"]:
    tail = [l for l in b["stderr"].strip().splitlines() if l.strip()][-1:]
    print("   stderr尾:", tail[0][:160] if tail else "")

print("\n=== 判读 ===")
no_big = "BIG_ALLOC_OK" in a["stdout"]
with_big = "BIG_ALLOC_OK" in b["stdout"]
print(f"  无限制时 3GB 分配成功 : {no_big}")
print(f"  有限制时 3GB 分配成功 : {with_big}")
print(f"  → Job 层挡住了内存分配: {no_big and not with_big}")
