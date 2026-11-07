"""验证多分类全链路：分层切分 + 指标 + 提交格式。

多分类是真实 Kaggle 上大量存在的赛型，但早期全链路只在二分类上验证过。
实测踩到两个真实缺陷：

1. `detect_split_strategy` 只对 binary 分层，多分类落到 random，
   导致验证集类别比例偏移、CV 与 valid 差距被放大。
2. `subcheck` 假定预测列是数值，多分类提交的是字符串标签（"high"/"mid"），
   write-sub 在自检这一步就崩。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from kauto import prep, subcheck
from kauto.model import score

results = []


def make_fixture(n: int = 1200, seed: int = 3) -> Path:
    d = Path(tempfile.mkdtemp(prefix="mc_fix_"))
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({
        "id": [f"{i:05d}" for i in range(n)],
        "x1": rng.normal(size=n),
        "x2": rng.normal(size=n),
        "seg": rng.choice(list("abcd"), size=n),
    })
    y = np.where(df.x1 + df.x2 > 1, 2, np.where(df.x1 < -0.8, 1, 0))
    df["label"] = [["low", "mid", "high"][v] for v in y]
    df.to_csv(d / "train.csv", index=False)
    te = df.drop(columns=["label"]).copy()
    te["id"] = [f"{10000+i:05d}" for i in range(n)]
    te.to_csv(d / "test.csv", index=False)
    pd.DataFrame({"id": te["id"], "label": ["low"] * n}).to_csv(
        d / "sample_submission.csv", index=False)
    return d


print("=== 1) 多分类切分必须分层 ===")
fx = make_fixture()
meta = prep.prepare("mcx-test", local_dir=str(fx), use_skill=False)
strat = meta["split_strategy"]["strategy"]
print(f"   strategy = {strat}")
print(f"   reason   = {meta['split_strategy'].get('reason')}")
print(f"   → 是 stratified: {strat == 'stratified'}")
results.append(("多分类按标签分层", strat == "stratified"))

# 类别比例是否一致
work = Path(meta["work_dir"])
tr = pd.read_csv(work / "train.csv")
va = pd.read_csv(work / "valid.csv")
dist_tr = tr["label"].value_counts(normalize=True)
dist_va = va["label"].value_counts(normalize=True)
max_gap = max(abs(dist_tr.get(c, 0) - dist_va.get(c, 0)) for c in dist_tr.index)
print(f"   各列占比最大偏差: {max_gap:.3f}")
print(f"   → 偏差 < 0.10: {max_gap < 0.10}")
results.append(("分层后类别比例一致", max_gap < 0.10))

print("\n=== 2) 多分类提交字符串标签应通过自检 ===")
n = 20
out = pd.DataFrame({"id": [f"{i:05d}" for i in range(n)],
                    "label": [["low", "mid", "high"][i % 3] for i in range(n)]})
sample = pd.DataFrame({"id": [f"{i:05d}" for i in range(n)], "label": ["low"] * n})
test = pd.DataFrame({"id": [f"{i:05d}" for i in range(n)]})
try:
    r = subcheck.check(out, sample, "id", test)
    print(f"   PASS：{r['n_unique_values']} 种标签，kind={r.get('prediction_kind')}")
    ok1 = r.get("prediction_kind") == "label"
except Exception as e:
    print("   FAIL：", str(e)[:140])
    ok1 = False
results.append(("字符串标签提交通过自检", ok1))

print("\n=== 3) 但「每行一个不同值」应被拒（误交 id 的典型形态）===")
bad = pd.DataFrame({"id": [f"{i:05d}" for i in range(n)],
                    "label": [f"text_{i}" for i in range(n)]})
try:
    subcheck.check(bad, sample, "id", test)
    print("   FAIL：误交的自由文本被放过了")
    ok2 = False
except Exception as e:
    print("   正确拒收：", str(e)[:120])
    ok2 = True
results.append(("每行唯一值被拒", ok2))

print("\n=== 4) 多分类指标可用 ===")
y3 = np.array([0, 1, 2, 0, 1, 2])
proba = np.array([[.8, .1, .1], [.1, .8, .1], [.1, .1, .8],
                  [.7, .2, .1], [.2, .7, .1], [.1, .2, .7]])
for m in ("accuracy", "logloss", "auc", "f1"):
    v = score(m, y3, proba)
    print(f"   {m:10s} = {v:.4f}")
results.append(("多分类指标可用", True))

print("\n=== 汇总 ===")
bad_n = 0
for name, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad_n += 0 if ok else 1
print()
print("全部通过" if bad_n == 0 else f"{bad_n} 项未通过")
raise SystemExit(1 if bad_n else 0)
