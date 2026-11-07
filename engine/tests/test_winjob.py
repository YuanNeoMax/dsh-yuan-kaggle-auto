"""验证 Job Object 的资源限制**真的会被触发**，而不只是"设置成功"。

三个必须实测的行为：
1. 内存超限 —— 申请超过上限的内存，进程必须失败
2. 进程数超限 —— 超过 ActiveProcessLimit 后，创建子进程必须失败（挡 fork 炸弹）
3. KILL_ON_JOB_CLOSE —— 关闭 job 时，跑在里面的进程必须一起死

只验证"配置读出来正确"是不够的：参数设上但没生效，和没设有一样糟。
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import winjob

if not winjob.available()["ok"]:
    print("本机不支持 Job Object：", winjob.available())
    raise SystemExit(1)

PY = sys.executable
results = []


def run_in_job(job, script: str, timeout: float = 40):
    """启动子进程并立刻塞进 job，返回 (returncode, stdout, stderr)。"""
    p = subprocess.Popen([PY, "-c", script], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace")
    try:
        winjob.assign(job, p.pid)
    except Exception as e:
        p.kill()
        return (None, "", f"assign failed: {e}")
    try:
        out, err = p.communicate(timeout=timeout)
        return (p.returncode, out, err)
    except subprocess.TimeoutExpired:
        p.kill()
        return (None, "", "TIMEOUT")


# ── 测试 1：内存超限 ─────────────────────────────────────────────────────────
print("=== 测试1：内存超限（上限 512MB，尝试申请 900MB）===")
job = winjob.create_job(memory_mb=512, active_processes=8, cpu_seconds=600)
script = """
try:
    b = bytearray(900 * 1024 * 1024)
    b[::4096] = b'x' * len(b[::4096])
    print("ALLOCATED")
except MemoryError:
    print("MEMORY_ERROR")
except Exception as e:
    print("OTHER:", type(e).__name__)
"""
rc, out, err = run_in_job(job, script)
print(f"  rc={rc} out={out.strip()[:80]} err={err.strip()[:120]}")
# 判定：出现 MEMORY_ERROR 或进程非零退出，都说明限制生效。
# 只有打印出 ALLOCATED 才说明"申请 900MB 在 512MB 上限下成功了"，即限制没生效。
blocked = ("MEMORY_ERROR" in out) or (rc not in (0, None))
results.append(("内存超限生效", blocked))
winjob.close(job)

# ── 测试 2：进程数超限 ───────────────────────────────────────────────────────
print("\n=== 测试2：进程数超限（上限 2，尝试再起 3 个子进程）===")
job = winjob.create_job(memory_mb=2048, active_processes=2, cpu_seconds=600)
script = """
import subprocess, sys
ok = 0
fail = 0
for _ in range(3):
    try:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
        ok += 1
    except Exception as e:
        fail += 1
print(f"spawn_ok={ok} spawn_fail={fail}")
"""
rc, out, err = run_in_job(job, script, timeout=60)
print(f"  rc={rc} out={out.strip()[:120]} err={err.strip()[:200]}")
limited = "spawn_fail=" in out and not out.strip().endswith("spawn_fail=0")
results.append(("进程数限生效", limited))
winjob.close(job)

# ── 测试 3：KILL_ON_JOB_CLOSE ───────────────────────────────────────────────
print("\n=== 测试3：关闭 job 时子进程一起终止 ===")
job = winjob.create_job(memory_mb=2048, active_processes=4, cpu_seconds=600)
p = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
try:
    winjob.assign(job, p.pid)
    print("  已 assign，等待 1 秒…")
    time.sleep(1)
    alive_before = p.poll() is None
    winjob.close(job)          # 关闭 job → KILL_ON_JOB_CLOSE 触发
    time.sleep(1.5)
    rc = p.poll()
    killed = rc is not None
    print(f"  close 前存活={alive_before}  close 后 rc={rc}  被终止={killed}")
    results.append(("KILL_ON_JOB_CLOSE 生效", killed))
except Exception as e:
    print("  FAIL:", e)
    results.append(("KILL_ON_JOB_CLOSE 生效", False))
finally:
    try:
        p.kill()
    except Exception:
        pass

print("\n=== 汇总 ===")
bad = 0
for name, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad += 0 if ok else 1
print()
print("全部生效" if bad == 0 else f"{bad} 项未生效")
raise SystemExit(1 if bad else 0)
