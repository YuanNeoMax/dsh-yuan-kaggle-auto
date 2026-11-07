"""验证极端值清洗：既要拦住真异常，又不能误伤正常值。

## 实测出来的两轮失败

**第一版**：阈值 `|x| > 1e15`。脏数据夹具里放 1e9（金额型异常）——
1e9 远小于 1e15，**完全通过，进了模型**。那个阈值只够拦住溢出级的值。

**第二版**：改成 `max(|中位数|, std) × 1e6`。仍然没拦住！
因为该列里 2% 是 1e9，**std 被拉大到 3e7**，阈值变成 3e13 —— 1e9 又过去了。
这就是「用被异常值污染的统计量去判异常值」的循环。

**第三版（当前）**：用 **IQR（四分位距）**。它只看中间 50% 的跨度，对极端值稳健。

实测结果：
- `num_outlier` 1e9 → 2.98（被清洗，记录进 nonfinite_cleaned）
- `num_signal` / `text_number` / `pct_str` 完全没被碰
- 干净数据 `demo-comp` 的 cv_mean 保持 0.46569 不变
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from kauto.model import build_matrix

results = []

print("=== 1) 含 2% 极端值的列必须被清洗 ===")
rng = np.random.default_rng(77)
n = 500
df = pd.DataFrame({
    "id": [f"{i:05d}" for i in range(n)],
    "clean": rng.normal(size=n),
    "outlier": np.where(rng.random(n) < 0.02, 1e9, rng.normal(size=n)),
    "target": (rng.random(n) > 0.5).astype(int),
})
meta = {"id_col": "id", "target": "target", "target_cols": ["target"]}
(m,), feats, cat, rep = build_matrix([df], df, meta)
print(f"   outlier 原始 max: {df['outlier'].max():.4g}")
print(f"   outlier 清洗后 max: {m['outlier'].max():.4g}")
print(f"   nonfinite_cleaned: {rep['nonfinite_cleaned']}")
cleaned = m["outlier"].max() < 1e3
recorded = "outlier" in rep["nonfinite_cleaned"]
print(f"   → 被清洗: {cleaned}  被记录: {recorded}")
results.append(("极端值被清洗", cleaned))
results.append(("极端值被记录", recorded))

print("\n=== 2) 正常列不能被误伤 ===")
print(f"   clean 范围: {m['clean'].min():.3f} ~ {m['clean'].max():.3f}  NaN={int(m['clean'].isna().sum())}")
n_clean_nan = int(m["clean"].isna().sum())
ok2 = n_clean_nan == 0 and abs(float(m["clean"].max())) < 10
print(f"   → 正常列零误伤: {ok2}")
results.append(("正常列零误伤", ok2))

print("\n=== 3) 大数值但均匀分布的列不能被当成异常 ===")
# 例如金额列全是 1e5~1e7 量级，没有离群点 —— 不应被清
df2 = pd.DataFrame({
    "id": [f"{i:05d}" for i in range(n)],
    "amount": rng.uniform(1e5, 1e7, n),
    "target": (rng.random(n) > 0.5).astype(int),
})
(m2,), feats2, cat2, rep2 = build_matrix([df2], df2, meta)
n_nan2 = int(m2["amount"].isna().sum())
print(f"   amount 范围: {m2['amount'].min():.3g} ~ {m2['amount'].max():.3g}  NaN={n_nan2}")
print(f"   nonfinite_cleaned: {rep2['nonfinite_cleaned']}")
ok3 = n_nan2 == 0 and not rep2["nonfinite_cleaned"]
print(f"   → 均匀大数值未被误清: {ok3}")
results.append(("均匀大数值不误清", ok3))

print("\n=== 4) 干净数据（demo-comp）不受影响 ===")
w = Path("work/demo-comp")
if (w / "train.csv").exists():
    tr = pd.read_csv(w / "train.csv")
    meta_d = {"id_col": "row_id", "target": "target", "target_cols": ["target"]}
    (md,), fd, cd, repd = build_matrix([tr], tr, meta_d)
    print(f"   demo-comp nonfinite_cleaned: {repd['nonfinite_cleaned']}")
    ok4 = not repd["nonfinite_cleaned"]
    print(f"   → 干净数据零清洗: {ok4}")
    results.append(("干净数据零清洗", ok4))
else:
    print("   (缺 work/demo-comp，跳过)")
    results.append(("干净数据零清洗", None))

print("\n=== 汇总 ===")
bad = 0
for n_, ok in results:
    mark = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    print(f"  {mark:5s} {n_}")
    if ok is False:
        bad += 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
