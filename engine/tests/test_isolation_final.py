"""最终验证：AppContainer 下的候选代码既跑得通、又偷不到凭据。

这是"根治"的验收标准，两条必须同时成立：
1. 正常候选代码能完整跑出预测（否则隔离太严、等于废掉工具）
2. 恶意候选代码读不到 ~/.dsh 凭据（否则隔离形同虚设）

只测其中一条都是假验证：只测 1 会漏掉安全问题，只测 2 会漏掉可用性问题。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import _runner

GOOD = """
def build_features(train, valid, test):
    def enc(df, ref):
        out = df[["x1","x2","x3","x4"]].copy()
        out["segment"] = df["segment"].map(ref["segment"].value_counts(normalize=True)).fillna(0.0)
        return out
    return (enc(train, train), enc(valid, train), enc(test, train), train["target"], valid["target"])

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=200, random_state=42, verbose=-1)
    m.fit(X_train, y_train)
    return m.predict_proba(X_valid)[:, 1], m.predict_proba(X_test)[:, 1]
"""

# 恶意候选：尝试读凭据、尝试列 .dsh 目录。它必须跑不完（或读不到）。
EVIL = """
def build_features(train, valid, test):
    import os, json
    home = os.path.expanduser("~")
    found = []
    for p in (os.path.join(home, ".dsh", ".credentials.yaml"),
              os.path.join(home, ".dsh")):
        try:
            if os.path.isdir(p):
                os.listdir(p); found.append("LISTED " + p)
            else:
                open(p, "rb").read(16); found.append("READ " + p)
        except PermissionError:
            found.append("DENIED " + p)
        except Exception as e:
            found.append("BLOCKED " + p + " " + type(e).__name__)
    open(os.path.join(GEN_MARK, "leak_probe.txt"), "w").write("\\n".join(found))
    return train[["x1"]], valid[["x1"]], test[["x1"]], train["target"], valid["target"]

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import numpy as np
    return (np.linspace(0.05,0.95,len(X_valid)), np.linspace(0.05,0.95,len(X_test)))
"""

w = Path("work/demo-comp").resolve()
results = []

print("=== 测试 1：正常候选在容器内跑通 ===")
r1 = _runner.run(w, GOOD, tag="final_good", container="kauto.sandbox")
print("   ok:", r1.get("ok"), "| n_valid:", r1.get("n_valid"), "| n_test:", r1.get("n_test"))
results.append(("正常候选可运行", bool(r1.get("ok"))))

print("\n=== 测试 2：恶意候选读凭据 ===")
# 让恶意代码把结果写到自己的 gen 目录（那里有写权限）
tag = "final_evil"
gen = w / f"gen_{tag}"
gen.mkdir(parents=True, exist_ok=True)
evil_code = EVIL.replace("GEN_MARK", repr(str(gen)))
r2 = _runner.run(w, evil_code, tag=tag, container="kauto.sandbox")
print("   ok:", r2.get("ok"))
probe_file = gen / "leak_probe.txt"
if probe_file.exists():
    content = probe_file.read_text(encoding="utf-8", errors="replace")
    for line in content.splitlines():
        print("   ", line.strip())
    denied = all(("DENIED" in l) or ("BLOCKED" in l) for l in content.splitlines() if l.strip())
else:
    print("   (probe 文件未生成 — 可能写权限或路径问题)")
    denied = False
results.append(("凭据被隔离", denied))

print("\n=== 汇总 ===")
bad = 0
for name, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad += 0 if ok else 1
print()
if bad == 0:
    print("隔离既安全又可用")
else:
    print(f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
