"""`digit-recognizer` 暴露的两个标签/ID 推断缺陷。

## 缺陷一：sample 首列不在 train 里，却当成了 id_col

```
KeyError: 'ImageId'
```

`sample_submission.csv` 是 `ImageId,Label`，但 **`train.csv` 只有 `label,pixel0..783`**
—— 没有 `ImageId`（它只在 test/sample 里）。

原代码无脑取 `sample_sub.columns[0]` 当 `id_col`，随后 `train[id_col]` 直接 KeyError。

修：只有当那一列**确实存在于 train 或 test** 时才当 id_col；
否则置空，表示"该比赛用测试集行号做 id"。

## 缺陷二：常数列被当成标签列

修完 id 后，`target` 被推断成 `pixel783`（应该是 `label`）。

原因：fallback 判据是 `1 <= nunique <= 20`，而 `pixel783` 是**常数列**
（`nunique=1`）—— 恰好落进区间的下界，而且它是从后往前扫到的第一个。

修：排除 `nunique<=1` 的常数；优先非数值列、其次整数小基数列、最后才退回数值列。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from kauto import prep

results = []

print("=== 取真实 digit-recognizer 数据 ===")
D = Path("data/digit-recognizer")
if not (D / "train.csv").exists():
    print("   缺数据，跳过（需先 kauto prepare）")
    print("\n=== 汇总 ===")
    print("  SKIP  全部")
    raise SystemExit(0)

train = pd.read_csv(D / "train.csv", nrows=800)
test = pd.read_csv(D / "test.csv", nrows=800)
sample = pd.read_csv(D / "sample_submission.csv")
print(f"   train 列: {list(train.columns)[:4]}… 共 {len(train.columns)}")
print(f"   sample 列: {list(sample.columns)}")

print("\n=== 1) id_col 不能指向 train 里不存在的列 ===")
sc = prep.infer_schema(train, sample, test)
print(f"   id_col = {sc['id_col']!r}")
ok1 = sc["id_col"] == "" or sc["id_col"] in train.columns
print(f"   → 合法（空串或真实存在）: {ok1}")
results.append(("id_col 合法", ok1))

print("\n=== 2) target 必须是 label，不能是常数列 ===")
print(f"   target = {sc['target']!r}")
print(f"   → 是 label: {sc['target'] == 'label'}")
results.append(("target 是 label", sc["target"] == "label"))

print("\n=== 3) 常数列不该被选中（一般性验证）===")
df = pd.DataFrame({
    "id": [str(i) for i in range(100)],
    "const_a": [1] * 100,          # 常数
    "const_b": [0] * 100,          # 常数
    "y": [i % 5 for i in range(100)],
    "f1": [i * 0.5 for i in range(100)],
})
sam = pd.DataFrame({"id": df["id"], "y": [0] * 100})
sc2 = prep.infer_schema(df, sam, None)
print(f"   target = {sc2['target']!r}（应为 y）")
ok3 = sc2["target"] == "y"
print(f"   → 跳过常数列: {ok3}")
results.append(("跳过常数列", ok3))

print("\n=== 4) kind 应为 multiclass（10 类）===")
print(f"   kind = {sc.get('kind')!r}")
ok4 = sc.get("kind") == "multiclass"
print(f"   → 多分类: {ok4}")
results.append(("kind 为 multiclass", ok4))

print("\n=== 5) output_cols 应为 ['Label']（提交时的列名）===")
print(f"   output_cols = {sc.get('output_cols')}")
ok5 = sc.get("output_cols") == ["Label"]
print(f"   → 正确: {ok5}")
results.append(("output_cols 正确", ok5))

print("\n=== 6) 回归：普通比赛（titanic 形态）不受影响 ===")
T = Path("work/titanic/train.csv")
if T.exists():
    tr2 = pd.read_csv(T, nrows=200)
    ss2 = pd.read_csv(Path("work/titanic/sample_submission.csv"))
    sc3 = prep.infer_schema(tr2, ss2, None)
    print(f"   titanic: id_col={sc3['id_col']!r} target={sc3['target']!r}")
    ok6 = sc3["id_col"] == "PassengerId" and sc3["target"] == "Survived"
    print(f"   → 未受影响: {ok6}")
    results.append(("titanic 不受影响", ok6))
else:
    results.append(("titanic 不受影响", None))

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
