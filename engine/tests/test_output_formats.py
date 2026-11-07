"""验证「多列输出的提交形态」能被正确处理。

实测缺陷：`sample_submission` 形如 `[id, a, b, c]`（每类一列概率）而 train 的
标签列叫 `label` 时，`infer_schema` 把 `a/b/c` 当 target 列去 train 里查，
找不到就抛「无法定位标签列」—— 于是这类比赛根本进不来。

修好第一层后又撞上第二层：`multi_target` 被当成「sample 有多列」，
于是直接判「多列标签暂未支持」再拒一次。

两层都需要修，且语义要分清：
    multi_target   = train 里有多个标签列（真正的多目标，不支持）
    output_cols    = sample 的多列输出形态（概率矩阵/one-hot，支持）
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from kauto import prep

results = []

print("=== 1) 多分类概率提交能进 prepare ===")
d = Path(tempfile.mkdtemp(prefix="mcp_"))
n = 240
rng = np.random.default_rng(9)
df = pd.DataFrame({"id": [f"{i:05d}" for i in range(n)],
                   "x1": rng.normal(size=n), "x2": rng.normal(size=n)})
y = np.where(df.x1 > 0.5, 2, np.where(df.x1 < -0.5, 0, 1))
labels = ["a", "b", "c"]
df["label"] = [labels[v] for v in y]
df.to_csv(d / "train.csv", index=False)
te = df.drop(columns=["label"]).copy()
te["id"] = [f"{10000+i:05d}" for i in range(n)]
te.to_csv(d / "test.csv", index=False)
pd.DataFrame({"id": te["id"], "a": [0.0] * n, "b": [0.0] * n,
              "c": [0.0] * n}).to_csv(d / "sample_submission.csv", index=False)

meta = prep.prepare("mcp-test", local_dir=str(d), use_skill=False)
print(f"   target={meta['target']}  output_cols={meta['output_cols']}")
print(f"   multi_target={meta['multi_target']}  kind={meta['kind']}  n_classes={meta['n_classes']}")
ok1 = (meta["target"] == "label" and meta["output_cols"] == ["a", "b", "c"]
       and meta["multi_target"] is False and meta["kind"] == "multiclass")
print(f"   → 识别正确: {ok1}")
results.append(("多列输出型可进入 prepare", ok1))

print("\n=== 2) 列名与 sample 完全一致 ===")
work = Path(meta["work_dir"])
sample = pd.read_csv(work / "sample_submission.csv")
print(f"   sample 列: {list(sample.columns)}")
results.append(("output_cols 已记录", meta.get("output_cols") == list(sample.columns)[1:]))

print("\n=== 3) submission 组装成多列 ===")
from kauto import model
r = model.write_submission("mcp-test")
sub = pd.read_csv(work / "submission.csv")
print(f"   submission 列: {list(sub.columns)}")
print(f"   rows={len(sub)}")
ok3 = list(sub.columns) == list(sample.columns)
print(f"   → 列结构与 sample 一致: {ok3}")
results.append(("submission 列结构正确", ok3))

# 概率矩阵每行和应≈1
if "a" in sub.columns:
    s = sub[["a", "b", "c"]].sum(axis=1)
    near_one = bool((s - 1.0).abs().max() < 0.01)
    print(f"   每行概率和: mean={s.mean():.4f} max偏差={float((s-1).abs().max()):.2e}")
    results.append(("概率矩阵归一", near_one))

print("\n=== 4) 真正的多目标仍应被拒 ===")
d2 = Path(tempfile.mkdtemp(prefix="multi_"))
n2 = 100
df2 = pd.DataFrame({"id": [f"{i:03d}" for i in range(n2)],
                    "x1": rng.normal(size=n2),
                    "y1": rng.normal(size=n2),
                    "y2": rng.normal(size=n2)})
df2.to_csv(d2 / "train.csv", index=False)
pd.DataFrame({"id": df2["id"], "y1": [0.0] * n2, "y2": [0.0] * n2}).to_csv(
    d2 / "sample_submission.csv", index=False)
try:
    prep.prepare("multi-test", local_dir=str(d2), use_skill=False)
    print("   FAIL：多目标被放过了")
    results.append(("真多目标被拒", False))
except Exception as e:
    msg = str(e)
    print(f"   正确拒收：{msg[:110]}")
    results.append(("真多目标被拒", "多列标签" in msg))

print("\n=== 汇总 ===")
bad = 0
for n_, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n_}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
