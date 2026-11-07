"""验证超时机制：准时杀进程、不留孤儿、错误信息可诊断。

## 实测的两个事实

1. **同一份代码，耗时差异巨大**：空闲时 39.6s，而 CPU 满载时
   在测试套件里见过 **522s**。所以超时失败不一定是"代码有问题"，
   也可能是"机器被占了"。

2. **超时机制本身是对的**：死循环候选在 `timeout=5` 时**准时 5.0s** 返回，
   进程被清理、没有孤儿。

## 为什么要记这两条

超时给太紧 → 好代码被误杀（尤其在机器忙的时候）；
超时给太松 → 死循环白等十几分钟。
`timebudget` 按 baseline 实测耗时 × 8 估算，但**它算不出"执行时机器会多忙"**。

所以错误信息必须带上"可能是机器负载"这个可能性，
否则用户会去改一份本来没错的代码。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import _runner, timebudget

results = []

print("=== 1) 死循环候选必须准时被超时杀掉 ===")
LOOP = """
def build_features(train, valid, test):
    while True:
        pass

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import numpy as np
    return np.zeros(len(X_valid)), np.zeros(len(X_test))
"""
t0 = time.time()
r = _runner.run(Path("work/demo-comp"), LOOP, tag="to_test", container=None, timeout=5)
dt = time.time() - t0
print(f"   耗时 {dt:.1f}s（timeout=5）ok={r.get('ok')}")
ok1 = (not r.get("ok")) and dt < 15      # 允许一点开销，但不能拖到几十秒
print(f"   → 准时失败: {ok1}")
results.append(("死循环准时超时", ok1))

print("\n=== 2) 超时错误信息必须提到「可能是机器负载」===")
err = r.get("error") or ""
has_load_hint = "机器负载" in err or "重任务" in err
print(f"   错误信息: {err[:130]}")
print(f"   → 含负载提示: {has_load_hint}")
results.append(("错误信息可诊断", has_load_hint))

print("\n=== 3) 超时结果里带 timeout 字段（便于排查）===")
has_field = "timeout" in r
print(f"   {r}")
ok3 = has_field
results.append(("带 timeout 字段", ok3))

print("\n=== 4) timebudget 的估算边界合理 ===")
est = timebudget.estimate(Path("work/demo-comp"))
t = est["timeout"]
print(f"   当前场次超时: {t}s  ← {est['reason']}")
ok4 = timebudget.MIN_TIMEOUT <= t <= timebudget.MAX_TIMEOUT
print(f"   → 在 [{timebudget.MIN_TIMEOUT}, {timebudget.MAX_TIMEOUT}] 内: {ok4}")
results.append(("估算边界合理", ok4))

print("\n=== 5) 实测真实候选耗时远低于当前超时（有余量）===")
code_p = Path("work/demo-comp/archive/3984f15ea28b/candidate.py")
if code_p.exists():
    code = code_p.read_text(encoding="utf-8")
    t0 = time.time()
    rr = _runner.run(Path("work/demo-comp"), code, tag="to_real", container="kauto.sandbox", timeout=300)
    real_dt = time.time() - t0
    print(f"   真实候选执行 {real_dt:.1f}s，超时阈值 {t}s")
    ok5 = rr.get("ok") and real_dt < t
    print(f"   → 有余量: {ok5}")
    results.append(("真实候选有余量", ok5))
else:
    print("   (缺归档代码，跳过)")
    results.append(("真实候选有余量", None))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    mark = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    print(f"  {mark:5s} {n}")
    if ok is False:
        bad += 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
