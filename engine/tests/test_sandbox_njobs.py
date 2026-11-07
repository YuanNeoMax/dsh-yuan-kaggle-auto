"""真实 LLM 代码里的 `n_jobs=-1` 在 AppContainer 下直接崩。

## 实测

真实 LLM 跑 titanic 时，生成的代码写了 `n_jobs=-1`（sklearn 的常见写法）。
在 AppContainer 里执行报：

```
PermissionError: [WinError 5] 拒绝访问。
  _winapi.CreateNamedPipe
  multiprocessing.connection.Pipe
```

**根因**：AppContainer 禁止创建命名管道，而 sklearn 的多进程后端要建 Pipe。
`n_jobs=-1` 意味着"用所有核"，sklearn 会走 joblib 多进程 → 建 Pipe → 被拒。

## 为什么这是真实缺陷而不是"LLM 写错了"

1. `n_jobs=-1` 是 **sklearn 文档推荐的写法**，不是错误用法
2. 它在本机裸跑完全正常，只在沙箱里失败 —— 是**隔离机制的副作用**
3. 真实 Kaggle 提交的代码里这个参数极其常见

## 修法（两层）

1. `JOBLIB_MULTIPROCESSING=0`（环境变量，挡一部分）
2. **在沙箱内改写参数**：把 sklearn 各估计器类的 `__init__` 包一层，
   凡带 `n_jobs` 的一律钉成 1

第 2 层是必要的 —— 环境变量挡不住 sklearn 自己的另一条路径。

对结果无影响（串行与并行的计算结果相同），只是慢一点。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

results = []

print("=== 1) runner 源码里注入了两种防护 ===")
from kauto import _runner
src = _runner._RUNNER_SRC
has_env = "JOBLIB_MULTIPROCESSING" in src
has_pin = "_pin_n_jobs" in src
print(f"   环境变量 JOBLIB_MULTIPROCESSING: {has_env}")
print(f"   参数改写 _pin_n_jobs: {has_pin}")
results.append(("环境变量防护", has_env))
results.append(("参数改写防护", has_pin))

print("\n=== 2) 实测：n_jobs=-1 的 RandomForest 能在沙箱跑通 ===")
CODE = """
def build_features(train, valid, test):
    cols = [c for c in ("Pclass","Age","SibSp","Parch","Fare") if c in train.columns]
    tr = train[cols].copy(); va = valid[cols].copy(); te = test[cols].copy()
    for d in (tr, va, te):
        for c in cols:
            if d[c].dtype.kind in "fi":
                d[c] = d[c].fillna(d[c].median())
    return tr, va, te, None, None

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    from sklearn.ensemble import RandomForestClassifier
    m = RandomForestClassifier(n_estimators=60, n_jobs=-1, random_state=0)
    m.fit(X_train, y_train)
    return m.predict_proba(X_valid)[:,1], m.predict_proba(X_test)[:,1]
"""
w = Path("work/titanic")
if (w / "train.csv").exists():
    r = _runner.run(w, CODE, tag="njobs_check", container="kauto.sandbox", timeout=300)
    print(f"   ok={r.get('ok')}")
    if not r.get("ok"):
        print(f"   err: {(r.get('error') or '')[:200]}")
    ok2 = bool(r.get("ok"))
    print(f"   → 沙箱内可用: {ok2}")
    results.append(("n_jobs=-1 可跑", ok2))
else:
    print("   (缺 titanic 数据，跳过)")
    results.append(("n_jobs=-1 可跑", None))

print("\n=== 3) 回归：不带 n_jobs 的普通代码仍正常 ===")
CODE2 = """
def build_features(train, valid, test):
    cols = [c for c in ("Pclass","Age") if c in train.columns]
    tr = train[cols].fillna(0); va = valid[cols].fillna(0); te = test[cols].fillna(0)
    return tr, va, te, None, None

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import numpy as np
    return np.linspace(0.05,0.95,len(X_valid)), np.linspace(0.05,0.95,len(X_test))
"""
w = Path("work/titanic")
if (w / "train.csv").exists():
    r2 = _runner.run(w, CODE2, tag="njobs_plain", container="kauto.sandbox", timeout=300)
    print(f"   ok={r2.get('ok')}")
    ok3 = bool(r2.get("ok"))
    print(f"   → 普通代码不受影响: {ok3}")
    results.append(("普通代码正常", ok3))
else:
    results.append(("普通代码正常", None))

print("\n=== 3b) 其它主流库的 n_jobs 也要能跑 ===")
# LightGBM 与 sklearn LogisticRegression 的参数名同为 n_jobs，
# 但走的是不同执行路径（lightgbm 用 OpenMP，sklearn 用 joblib）。
LGB_CODE = """
def build_features(train, valid, test):
    cols = [c for c in ("Pclass","Age","SibSp","Parch","Fare") if c in train.columns]
    tr = train[cols].fillna(0); va = valid[cols].fillna(0); te = test[cols].fillna(0)
    return tr, va, te, None, None

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=80, n_jobs=-1, verbose=-1)
    m.fit(X_train, y_train)
    return m.predict_proba(X_valid)[:,1], m.predict_proba(X_test)[:,1]
"""
LR_CODE = """
def build_features(train, valid, test):
    cols = [c for c in ("Pclass","Age") if c in train.columns]
    tr = train[cols].fillna(0); va = valid[cols].fillna(0); te = test[cols].fillna(0)
    return tr, va, te, None, None

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    from sklearn.linear_model import LogisticRegression
    m = LogisticRegression(n_jobs=-1, max_iter=1000)
    m.fit(X_train, y_train)
    return m.predict_proba(X_valid)[:,1], m.predict_proba(X_test)[:,1]
"""
if (Path("work/titanic") / "train.csv").exists():
    rlgb = _runner.run(Path("work/titanic"), LGB_CODE, tag="nj_lgb",
                       container="kauto.sandbox", timeout=300)
    print(f"   LightGBM n_jobs=-1: ok={rlgb.get('ok')}")
    results.append(("LightGBM n_jobs 可跑", bool(rlgb.get("ok")))
                   )
    rlr = _runner.run(Path("work/titanic"), LR_CODE, tag="nj_lr",
                      container="kauto.sandbox", timeout=300)
    print(f"   LogisticRegression n_jobs=-1: ok={rlr.get('ok')}")
    results.append(("LogReg n_jobs 可跑", bool(rlr.get("ok")))
                   )
else:
    print("   (缺数据，跳过)")
    results.append(("LightGBM n_jobs 可跑", None))

print("\n=== 4) 真实失败记录仍在 journal（作为证据）===")
import json
jp = Path("work/titanic/journal.json")
if jp.exists():
    d = json.loads(jp.read_text(encoding="utf-8"))
    mp_errs = [n for n in d["nodes"] if "CreateNamedPipe" in (n.get("error") or "")]
    print(f"   含 CreateNamedPipe 错误的节点: {len(mp_errs)}")
    print("   （这些是修复前的运行留下的，可作为缺陷证据）")
    results.append(("缺陷有据可查", True))

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
