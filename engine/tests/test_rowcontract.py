"""验证执行器的行数契约：错长度必须当场拒绝，正确形态不能误伤。

实测发现的两个缺陷：
1. `test_pred` 写成 250 行（test 有 400 行），执行器仍返回 ok=True ——
   坏产物会一路流到 write-sub，生成 250 行 submission，直到 Kaggle 才拒。
2. `valid_pred` 长度错时，错误信息是「评分失败：valid_pred 行数 100 与验证集 240 不符」,
   看起来像评分器的问题，不像候选违反契约 —— 会把人往错方向带。

修复：在写盘前做行数校验，且错误以**干净单行 JSON** 报出（不带 traceback）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import _runner

results = []

WRONG_LEN = """
def build_features(train, valid, test):
    return (train[["x1","x2"]], valid[["x1","x2"]], test[["x1","x2"]], None, None)

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import numpy as np
    return (np.linspace(0.05,0.95,250), np.linspace(0.05,0.95,250))
"""

GOOD_BINARY = """
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

print("=== 1) 错长度必须被拒 ===")
r = _runner.run(Path("work/demo-comp"), WRONG_LEN, tag="tc_bad", container=None)
err = r.get("error") or ""
print(f"   ok={r.get('ok')}")
print(f"   err={err[:170]}")
is_blocked = not r.get("ok")
clean = "Traceback" not in err          # 不能带 traceback
actionable = "不符" in err and "期望" in err
row_mentioned = "valid_pred" in err or "test_pred" in err
print(f"   → 被拒: {is_blocked} | 无 traceback: {clean} | 说明行数: {actionable} | 指出哪个: {row_mentioned}")
results.append(("错长度被拒", is_blocked))
results.append(("错误信息干净（无 traceback）", clean))
results.append(("错误信息可行动", actionable and row_mentioned))

print("\n=== 2) 二分类一维预测不能误伤 ===")
r2 = _runner.run(Path("work/demo-comp"), GOOD_BINARY, tag="tc_good", container=None)
print(f"   ok={r2.get('ok')} n_valid={r2.get('n_valid')} n_test={r2.get('n_test')}")
results.append(("二分类一维预测通过", bool(r2.get("ok"))))

print("\n=== 3) 多分类 (n,K) 矩阵不能误伤 ===")
MULTI = """
def build_features(train, valid, test):
    tr = train[["x1","x2"]].copy(); tr["seg"] = train["seg"].astype("category").cat.codes
    va = valid[["x1","x2"]].copy(); va["seg"] = valid["seg"].astype("category").cat.codes
    te = test[["x1","x2"]].copy(); te["seg"] = test["seg"].astype("category").cat.codes
    return tr, va, te, None, None

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=200, random_state=42, verbose=-1)
    m.fit(X_train, y_train.astype("category").cat.codes)
    return m.predict_proba(X_valid), m.predict_proba(X_test)
"""
w_mc = Path("work/mc-comp")
if (w_mc / "test.csv").exists():
    r3 = _runner.run(w_mc, MULTI, tag="tc_multi", container=None)
    print(f"   ok={r3.get('ok')} n_valid={r3.get('n_valid')} n_test={r3.get('n_test')}")
    results.append(("多分类矩阵通过", bool(r3.get("ok"))))
else:
    print("   (缺 work/mc-comp，跳过)")

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
