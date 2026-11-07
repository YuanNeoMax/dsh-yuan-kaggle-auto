"""验证结构性禁写：验证集标签在 build_features 阶段物理不可见。

与"正则识别泄漏"的根本区别：
- 旧方案：靠规则猜哪些写法是泄漏，实测漏掉 4/5
- 新方案：valid/test 传进 build_features 时 target 列已被剥离，
  读 fva["target"] 直接 KeyError —— 泄漏从"需要识别"变成"做不到"

本脚本测两件事：
1. 正常候选（不碰标签）仍然跑通 —— 否则等于把工具搞废了
2. 各种泄漏手法全部失败 —— 且失败原因是 KeyError，不是"被规则抓到"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import _runner

w = Path("work/demo-comp").resolve()

NORMAL = """
def build_features(train, valid, test):
    def enc(df, ref):
        out = df[["x1","x2","x3","x4"]].copy()
        out["segment"] = df["segment"].map(ref["segment"].value_counts(normalize=True)).fillna(0.0)
        return out
    return (enc(train, train), enc(valid, train), enc(test, train), None, None)

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=200, random_state=42, verbose=-1)
    m.fit(X_train, y_train)
    return m.predict_proba(X_valid)[:, 1], m.predict_proba(X_test)[:, 1]
"""

# 五种真实的泄漏写法，全部试图在 build_features 里碰到 valid 的标签
LEAKS = {
    "把 valid 标签当特征": """
def build_features(train, valid, test):
    tr = train[["x1","x2"]].copy(); tr["leak"] = train["target"].astype(float)
    va = valid[["x1","x2"]].copy(); va["leak"] = valid["target"].astype(float)
    te = test[["x1","x2"]].copy();  te["leak"] = 0.0
    return tr, va, te, None, None

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import numpy as np
    return (X_valid["leak"].to_numpy(), np.zeros(len(X_test)))
""",
    "在 valid 上做 target encoding": """
def build_features(train, valid, test):
    enc = train.groupby("x1")["target"].mean()
    tr = train[["x1","x2"]].copy(); tr["te"] = tr["x1"].map(enc).fillna(0)
    va = valid[["x1","x2"]].copy(); va["te"] = va["x1"].map(enc).fillna(0)
    te = test[["x1","x2"]].copy();  te["te"] = 0.0
    return tr, va, te, None, None

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import numpy as np
    return (np.linspace(0.05,0.95,len(X_valid)), np.linspace(0.05,0.95,len(X_test)))
""",
    "用 valid 标签做统计填充": """
def build_features(train, valid, test):
    m = valid["target"].mean()
    tr = train[["x1","x2"]].copy(); tr["fill"] = tr["x2"].fillna(m)
    va = valid[["x1","x2"]].copy(); va["fill"] = va["x2"].fillna(m)
    te = test[["x1","x2"]].copy();  te["fill"] = 0.0
    return tr, va, te, None, None

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import numpy as np
    return (np.linspace(0.05,0.95,len(X_valid)), np.linspace(0.05,0.95,len(X_test)))
""",
}

results = []

print("=== 1) 正常候选（不碰标签）===")
r = _runner.run(w, NORMAL, tag="s_normal", container=None)
print("   ok:", r.get("ok"), "| n_valid:", r.get("n_valid"))
results.append(("正常候选可运行", bool(r.get("ok"))))

print("\n=== 2) 泄漏手法是否全部失效 ===")
for name, code in LEAKS.items():
    res = _runner.run(w, code, tag="s_leak_" + str(len(results)), container=None)
    err = (res.get("error") or "")
    keyerr = "KeyError" in err or "缺少标签列" in err or "\"target\"" in err
    print(f"   {'阻断' if not res.get('ok') else '通过 ← 危险'}  {name}")
    if not res.get("ok"):
        tail = [l for l in err.splitlines() if l.strip()][-1:]
        print(f"         原因: {tail[0][:100] if tail else ''}")
    results.append((f"阻断泄漏：{name}", not res.get("ok") and keyerr))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("结构性禁写生效" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
