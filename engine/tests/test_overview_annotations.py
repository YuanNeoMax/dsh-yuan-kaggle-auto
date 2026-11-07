"""验证数据概览会标注两类"看着不像但其实不是"的列。

## 实测缺陷

用脏数据夹具生成概览，输出是这样的：

```
- `num_outlier` (float64) | 622 种取值 | 样本: -1.949, 0.8652, ...
- `text_number` (str)     | 640 种取值 | 样本: '3,306.37', '5,478.21', ...
- `pct_str`     (str)     | 481 种取值 | 样本: '19.2%', '13.5%', ...
```

两处都会误导 LLM：

1. **`num_outlier` 不提示含有极端值**。样本值全是干净的（因为极值已被置空），
   于是 LLM 不知道原始数据里有 1e9 这种量级，可能不会做任何稳健化处理。
2. **`text_number` / `pct_str` 标成 `(str)`**。它们实际会被 `build_matrix`
   解析成数值使用。LLM 看到 str 会以为要自己处理字符串，或干脆跳过这列 ——
   而这两列其实是有信息的数值特征。

修复后输出：
```
- `num_outlier` (float64) | ⚠ 含极端值（已按异常置空） | ...
- `text_number` (str → 按数值使用) | ...
- `pct_str`     (str → 按数值使用) | ...
```
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from kauto import dataoverview, model

results = []

print("=== 1) 构造含两类特殊列的夹具 ===")
rng = np.random.default_rng(99)
n = 300
df = pd.DataFrame({
    "id": [f"{i:05d}" for i in range(n)],
    "num_clean": rng.normal(size=n),
    "num_outlier": np.where(rng.random(n) < 0.03, 1e9, rng.normal(size=n)),
    "text_money": [f"{v:,.2f}" for v in rng.uniform(100, 5000, n)],
    "pct": [f"{v:.1f}%" for v in rng.uniform(0, 100, n)],
    "target": (rng.random(n) > 0.5).astype(int),
})
meta = {"id_col": "id", "target": "target", "target_cols": ["target"]}
(m,), feats, cat, rep = model.build_matrix([df], df, meta)
print(f"   numeric_from_text: {rep['numeric_from_text']}")
print(f"   nonfinite_cleaned: {rep['nonfinite_cleaned']}")

print("\n=== 2) 概览必须标注「按数值使用」===")
ov = dataoverview.build(df, df, df, target="target", id_col="id",
                        numeric_from_text=rep["numeric_from_text"],
                        cleaned=rep["nonfinite_cleaned"])
has_coerced_note = "按数值使用" in ov
print(f"   含「按数值使用」标注: {has_coerced_note}")
for line in ov.splitlines():
    if "text_money" in line or "pct" in line:
        print(f"   {line.strip()[:100]}")
results.append(("标注按数值使用", has_coerced_note))

print("\n=== 3) 概览必须标注「含极端值」===")
has_extreme_note = "含极端值" in ov
print(f"   含「含极端值」标注: {has_extreme_note}")
for line in ov.splitlines():
    if "num_outlier" in line:
        print(f"   {line.strip()[:100]}")
results.append(("标注含极端值", has_extreme_note))

print("\n=== 4) 正常列不该被贴上这些标注 ===")
clean_line = next((l for l in ov.splitlines() if "num_clean" in l), "")
ok4 = ("按数值使用" not in clean_line) and ("含极端值" not in clean_line)
print(f"   num_clean 行: {clean_line.strip()[:90]}")
print(f"   → 无多余标注: {ok4}")
results.append(("正常列无多余标注", ok4))

print("\n=== 5) for_work 能从 baseline.json 读到这些标记 ===")
work = Path("work/dirty-comp")
if (work / "baseline.json").exists():
    md = json.loads((work / "meta.json").read_text(encoding="utf-8"))
    ov2 = dataoverview.for_work(work, md)
    ok5 = "含极端值" in ov2 or "按数值使用" in ov2
    print(f"   脏数据概览含标注: {ok5}")
    results.append(("for_work 读到标记", ok5))
else:
    print("   (缺 work/dirty-comp，跳过)")
    results.append(("for_work 读到标记", None))

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
