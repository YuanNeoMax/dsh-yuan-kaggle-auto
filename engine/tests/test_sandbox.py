"""检查候选代码的执行沙箱是否真的有限制。

prompt 里写了"不许联网、不许读写文件、不许 import 上面之外的库"，
但如果这只是口头约束，一个 buggy 或恶意的候选代码就能：
- 删掉 work 目录里的数据
- 读凭据文件
- 联网

本脚本验证这些动作在现有执行器里是否真的被拦住。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import _runner
from kauto.iter import extract_code

EVIL = """```python
def build_features(train, valid, test):
    import os, subprocess, urllib.request

    # 1) 能不能写文件
    try:
        open('/tmp/kauto_escape_probe.txt', 'w').write('escaped')
        print('WRITE_OK')
    except Exception as e:
        print('WRITE_BLOCKED', type(e).__name__)

    # 2) 能不能起子进程
    try:
        r = subprocess.run(['cmd', '/c', 'echo hi'], capture_output=True, timeout=5)
        print('SUBPROC_OK', r.returncode)
    except Exception as e:
        print('SUBPROC_BLOCKED', type(e).__name__)

    # 3) 能不能联网
    try:
        urllib.request.urlopen('http://example.com', timeout=3)
        print('NET_OK')
    except Exception as e:
        print('NET_BLOCKED', type(e).__name__)

    return train[['x1']], valid[['x1']], test[['x1']], train['target'], valid['target']

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    # 返回有变化的预测，而不是常数 —— 常数会被新的退化守卫判为失败，
    # 那样这个脚本就测不到"沙箱拦越权"这件事本身了。
    import numpy as np
    return (np.linspace(0.05, 0.95, len(X_valid)),
            np.linspace(0.05, 0.95, len(X_test)))
```"""

work = Path("work/demo-comp")
if not work.exists():
    print("先跑 prepare demo-comp")
    raise SystemExit(1)

res = _runner.run(work, extract_code(EVIL), tag="sandbox_probe")
print("run ok:", res.get("ok"))
print("error:", (res.get("error") or "")[:400])

# 子进程 stdout 里会带出 probe 结果
gen = work / "gen_sandbox_probe"
stdout_txt = ""
run_py = gen / "_run.py"
if run_py.exists():
    import subprocess
    p = subprocess.run([sys.executable, str(run_py)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    stdout_txt = p.stdout or ""
for line in stdout_txt.splitlines():
    if any(k in line for k in ("WRITE", "SUBPROC", "NET")):
        print("  probe:", line)

probe = Path("/tmp/kauto_escape_probe.txt")
print("逃逸文件是否被创建:", probe.exists())
if probe.exists():
    probe.unlink()
