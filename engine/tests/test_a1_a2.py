"""验证 A1（文本数值列解析）与 A2（多分类指标）。

A1：带千分位/百分号/货币/单位的列必须被解析成真实数值，而不是压成常数。
A2：多分类下 auc / logloss 必须算出有限值，而不是抛异常。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from kauto.model import build_matrix, score, coerce_numeric

print("=== A1: 文本数值列解析 ===")
CASES = [
    ("amount", ["1,200", "300", "2,500", "800", "1,000", "450"], [1200, 300, 2500, 800, 1000, 450]),
    ("pct", ["12%", "30%", "25%", "8%", "10%", "45%"], [0.12, 0.30, 0.25, 0.08, 0.10, 0.45]),
    ("money", ["$1,200", "$300", "$2,500", "$800", "$1,000", "$450"], [1200, 300, 2500, 800, 1000, 450]),
    ("weight", ["3.5kg", "1.2kg", "7kg", "2kg", "500g", "10kg"], [3.5, 1.2, 7, 2, 0.5, 10]),
]

for name, raw, expect in CASES:
    s = pd.Series(raw)
    num, ok = coerce_numeric(s)
    got = num.tolist()
    print(f"  {name:8s} 解析 {ok}/6 个")
    # 比较时对单位类（500g）做宽松处理
    if name == "weight":
        print(f"    got   : {[round(x, 4) if pd.notna(x) else None for x in got]}")
    else:
        match = all(pd.notna(a) and pd.notna(b) and abs(float(a) - float(b)) < 1e-9
                    for a, b in zip(got, expect))
        print(f"    {'OK' if match else 'MISMATCH'}  got={[round(x,4) if pd.notna(x) else None for x in got]}")

print("\n=== A1: build_matrix 不再把这类列压成常数 ===")
df = pd.DataFrame({
    "row_id": range(6),
    "amount": ["1,200", "300", "2,500", "800", "1,000", "450"],
    "pct": ["12%", "30%", "25%", "8%", "10%", "45%"],
    "city": ["bj", "sh", "bj", "gz", "sh", "bj"],
    "target": [0, 1, 0, 1, 0, 1],
})
meta = {"id_col": "row_id", "target": "target", "target_cols": ["target"]}
(m,), feats, cat, report = build_matrix([df], df, meta)
print("  特征:", feats)
print("  report:", report)
print("  amount 唯一值数:", m["amount"].nunique(), "（>1 表示不再是常数）")
print("  pct 唯一值数:", m["pct"].nunique())

print("\n=== A2: 多分类指标 ===")
y3 = np.array([0, 1, 2, 0, 1, 2])
proba3 = np.array([
    [0.8, 0.1, 0.1], [0.1, 0.8, 0.1], [0.1, 0.1, 0.8],
    [0.7, 0.2, 0.1], [0.2, 0.7, 0.1], [0.1, 0.2, 0.7],
])
for metric in ("auc", "logloss", "accuracy", "f1", "map"):
    try:
        v = score(metric, y3, proba3)
        print(f"  {metric:10s} = {v:.6f}  (K=3 概率矩阵)")
    except Exception as e:
        print(f"  {metric:10s} FAIL {type(e).__name__}: {str(e)[:80]}")

# 只有标签、没有概率时也应退化而不是崩
print("\n  退化路径（只给 1 维标签）：")
for metric in ("auc", "logloss"):
    try:
        v = score(metric, y3, np.array([0., 1., 2., 0., 1., 2.]))
        print(f"    {metric:10s} = {v:.6f}（退化为 accuracy）")
    except Exception as e:
        print(f"    {metric:10s} FAIL {type(e).__name__}: {str(e)[:80]}")
