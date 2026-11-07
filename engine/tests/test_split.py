"""验证切分策略检测：时间列 / 分组列 / 普通表格三种情形。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from kauto.prep import detect_split_strategy

rng = np.random.default_rng(7)

# 1) 有时间列
t = pd.DataFrame({
    "row_id": range(500),
    "date": pd.date_range("2020-01-01", periods=500, freq="D").astype(str),
    "user_id": rng.integers(0, 400, 500),
    "x1": rng.normal(size=500),
    "target": (rng.normal(size=500) > 0).astype(int),
})
m = {"id_col": "row_id", "target": "target", "target_cols": ["target"],
     "task": "classification", "kind": "binary"}
print("1) 有 date 列 ->", detect_split_strategy(t, m))

# 2) 无时间列，但有高基数分组列
g = pd.DataFrame({
    "row_id": range(500),
    "user_id": rng.integers(0, 60, 500),
    "x1": rng.normal(size=500),
    "target": (rng.normal(size=500) > 0).astype(int),
})
print("2) 高基数 user_id ->", detect_split_strategy(g, m))

# 3) 普通表格
p = pd.DataFrame({
    "row_id": range(500),
    "x1": rng.normal(size=500),
    "x2": rng.normal(size=500),
    "target": (rng.normal(size=500) > 0).astype(int),
})
print("3) 普通表格 ->", detect_split_strategy(p, m))

# 4) 回归任务
r = pd.DataFrame({
    "row_id": range(500),
    "x1": rng.normal(size=500),
    "y": rng.normal(size=500),
})
mr = {"id_col": "row_id", "target": "y", "target_cols": ["y"],
      "task": "regression", "kind": "single"}
print("4) 回归 ->", detect_split_strategy(r, mr))
