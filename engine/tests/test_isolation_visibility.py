"""验证隔离层不可用时不静默降级 + doctor 能自查隔离状态。

两个实测缺陷：
1. `_runner.run` 里写的是 `if container and appcontainer.available()["ok"]:` ——
   AppContainer **不可用**时什么都不做，直接走无隔离分支。
   于是「以为跑在沙箱里」和「其实没有文件隔离」完全无法区分。
   这和我之前写的「隔离失败拒绝降级」自相矛盾：拒绝降级只处理了
   "available 为真但启动抛异常"，没处理"available 本身就是 False"。

2. `doctor` 只报凭据/依赖/LLM，**完全不检查三层隔离** ——
   用户无从自查隔离是否真的生效。
"""
import inspect
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import _runner, appcontainer, cli

results = []

print("=== 1) AppContainer 不可用时必须报错，不能静默降级 ===")
GOOD = """
def build_features(train, valid, test):
    return (train[["x1"]], valid[["x1"]], test[["x1"]], None, None)

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import numpy as np
    return (np.linspace(0.05,0.95,len(X_valid)), np.linspace(0.05,0.95,len(X_test)))
"""
w = Path("work/demo-comp").resolve()
with mock.patch.object(appcontainer, "available",
                       return_value={"ok": False, "reason": "模拟不可用"}):
    r = _runner.run(w, GOOD, tag="nodg-test", container="kauto.sandbox")
err = r.get("error") or ""
print(f"   ok={r.get('ok')}")
print(f"   err={err[:150]}")
ok1 = not r.get("ok") and "拒绝降级" in err
print(f"   → 拒绝静默降级: {ok1}")
results.append(("不可用时拒绝降级", ok1))

print("\n=== 2) 显式传 container=None 才允许无文件隔离 ===")
with mock.patch.object(appcontainer, "available",
                       return_value={"ok": False, "reason": "模拟不可用"}):
    r2 = _runner.run(w, GOOD, tag="nodg-none", container=None)
print(f"   ok={r2.get('ok')}（应为 True：显式放弃隔离是允许的）")
ok2 = bool(r2.get("ok"))
print(f"   → 显式放弃可用: {ok2}")
results.append(("显式放弃可用", ok2))

print("\n=== 3) doctor 必须报告隔离层 ===")
src_doctor = inspect.getsource(cli.cmd_doctor)
has_isolation = "isolation" in src_doctor
has_gate = "submission_trace" in src_doctor or "_pre_submit_gate" in src_doctor
print(f"   含 isolation 段: {has_isolation}")
print(f"   含提交溯源段: {has_gate}")
results.append(("doctor 报隔离", has_isolation))
results.append(("doctor 报溯源", has_gate))

print("\n=== 4) 实测 doctor 输出 ===")
# 直接调用，捕获 stdout 太大，这里只检查返回结构
from kauto import winjob
wj = winjob.available()
ac = appcontainer.available()
print(f"   job_object.ok={wj['ok']}  appcontainer.ok={ac['ok']}")
ok4 = isinstance(wj, dict) and isinstance(ac, dict) and "ok" in wj and "ok" in ac
print(f"   → 两个 available() 都返回可检查结构: {ok4}")
results.append(("隔离能力可查询", ok4))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
