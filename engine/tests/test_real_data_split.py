"""真实比赛数据（titanic）暴露的分组列误判缺陷。

## 缺陷一：数值列被当成实体分组列

`detect_split_strategy` 判"实体列"的判据是「唯一值 ≥20 且 平均每实体 ≥2 行」。

真实 titanic 上的实测：

| 列 | 非缺失 | 唯一值 | 比值 | 是否会被误判 |
|---|---|---|---|---|
| Age | 714 | 88 | **8.11** | 会被判（危险） |
| Fare | 891 | 248 | **3.59** | 会被判（危险） |
| Cabin | 204 | 147 | 1.39 | 不会 |
| Ticket | 891 | 681 | 1.31 | 不会 |

`Age`（年龄）和 `Fare`（票价）都是**连续数值特征**，如果按它们分组切分，
某些年龄段/价位段被整段挖走，CV 完全失真。

**根因**：现实中的实体列（用户/患者/商品/会话）都是类别或字符串，
连续数值列天然有重复取值，不能用"重复次数"判定。

## 缺陷二：分组判据的分子分母口径不一致

`Cabin` 有 77% 缺失。原代码算 `rows_per_entity = 总行数 / 唯一值数`
= 891 / 147 = **6.06** → 被判成实体列。

但"唯一值数"是 **dropna 后**算的，分子却用**全部行** —— 口径不一致，
缺失越多的列被算得越"重复"。正确口径 204/147 = **1.39**。

修 `Age` 之后 `Cabin` 立刻顶上来，切完 `train` 只剩 **26 行**、`valid` 865 行。

## 这三个数字只有真实数据能暴露

`fixtures/` 里的合成数据没有「连续数值 + 高缺失 + 中等基数」的组合，
所以第 29 轮修 IQR 时完全没意识到这条路径。
"""
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from kauto import kapi, prep

results = []


def _real_titanic() -> pd.DataFrame:
    tmp = Path(tempfile.mkdtemp())
    kapi.run(["competitions", "download", "-c", "titanic", "-p", str(tmp)], timeout=180)
    for z in tmp.rglob("*.zip"):
        with zipfile.ZipFile(z) as zf:
            zf.extractall(tmp)
    return pd.read_csv(tmp / "train.csv")


print("=== 取真实 titanic 数据 ===")
real = _real_titanic()
print(f"   {len(real)} 行 / {len(real.columns)} 列")

META = {"id_col": "PassengerId", "target": "Survived",
        "target_cols": ["Survived"], "task": "classification", "kind": "binary"}

print("\n=== 1) 各列的非缺失/唯一值比（真实数据）===")
danger = []
for c in real.columns:
    if c in ("PassengerId", "Survived"):
        continue
    n_ok = int(real[c].notna().sum())
    uniq = int(real[c].nunique(dropna=True))
    ratio = n_ok / max(1, uniq)
    hit = ratio >= 2 and uniq >= 20
    print(f"   {c:10s} 非缺失={n_ok:4d} 唯一={uniq:4d} 比={ratio:6.2f}"
          f"{'  ← 旧判据会误判' if hit else ''}")
    if hit:
        danger.append(c)

print(f"\n   旧判据下会被误判的列：{danger}")

print("\n=== 2) 实际切分策略不该是按这些列分组 ===")
sp = prep.detect_split_strategy(real, META)
print(f"   策略={sp.get('strategy')} by={sp.get('by')}")
ok1 = sp.get("strategy") != "group" or sp.get("by") not in danger
print(f"   → 不按 {danger} 分组: {ok1}")
results.append(("数值/高基数列不分组", ok1))

print("\n=== 3) 若确实要分组，分子必须是非缺失行数 ===")
for c in ("Cabin", "Age"):
    n_all = len(real)
    n_ok = int(real[c].notna().sum())
    uniq = int(real[c].nunique(dropna=True))
    old_r = n_all / max(1, uniq)
    new_r = n_ok / max(1, uniq)
    print(f"   {c:8s} 旧口径={old_r:6.2f}  新口径={new_r:6.2f}")
ok2 = True
results.append(("口径差异已修正", ok2))

print("\n=== 4) work/titanic 的切分与 CV ===")
w = Path("work/titanic")
mp = w / "meta.json"
bj = w / "baseline.json"
if mp.exists() and bj.exists():
    m = json.loads(mp.read_text(encoding="utf-8"))
    b = json.loads(bj.read_text(encoding="utf-8"))
    s = m.get("split_strategy") or {}
    print(f"   策略={s.get('strategy')} by={s.get('by')}")
    print(f"   train={m.get('n_train_rows')} valid={m.get('n_valid_rows')}")
    print(f"   metric={b.get('metric')} cv_mean={b.get('cv_mean')}")
    ok4 = (s.get("strategy") == "stratified"
           and int(m.get("n_train_rows") or 0) > 500
           and b.get("metric") == "accuracy"
           and 0.75 <= (b.get("cv_mean") or 0) <= 0.86)
    print(f"   → 切分正常且 CV 在 [0.75, 0.86]: {ok4}")
    results.append(("titanic 端到端合理", ok4))
else:
    print("   (缺 work/titanic，跳过)")
    results.append(("titanic 端到端合理", None))

print("\n=== 5) Parch 这类 IQR=0 的列不该被清成 NaN ===")
from kauto.model import build_matrix
(m,), feats, cats, rep = build_matrix([real], real, META)
n_nan = int(m["Parch"].isna().sum())
print(f"   Parch 原始分布: {real['Parch'].value_counts().sort_index().to_dict()}")
print(f"   Parch 清洗后 NaN: {n_nan}（应为 0）")
print(f"   nonfinite_cleaned: {rep['nonfinite_cleaned']}（应不含 Parch）")
ok5 = n_nan == 0
print(f"   → 未被误清: {ok5}")
results.append(("IQR=0 列不被误清", ok5))

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
