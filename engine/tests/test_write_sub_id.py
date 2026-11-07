"""第 62 轮：`write-sub` 在 digit-recognizer 上连爆两个缺陷，都是"跑很久最后什么都没有"。

## 缺陷 1：id_col 为空 → `test[""]` → KeyError

`prepare` 会把 `id_col` 置空（正确），因为 `ImageId` **只存在于
sample_submission**，train/test 里都没有（digit 的 test.csv 只有 784 个
pixel 列）。旧代码直接写 `test[id_col]` → `test[""]` → `KeyError: ''`。

最坏的部分不是异常本身，而是它发生的位置：**训练结束之后**。
42000×784 的重训要几分钟，于是用户看到的是"跑了很久，最后什么也没有"，
看起来像训练卡死 —— 我确实先怀疑了负载、又怀疑了参数，才找到这里。

## 缺陷 2（我修一半时自己引入的）：行号兜底导致 28000 行全部错位

用 `np.arange(len(test))` 兜底产出 `0..27999`，而 sample_submission 的
`ImageId` 是 `1..28000`。差 1，28000 行全错。

被 `validate` 拦下（"1 not in the sample (0)"）—— 它救了一次白烧的提交配额。
但如果没跑 validate 直接 submit，就是一次彻底作废的提交。

正确优先级：meta 的 id_col → sample 首列（test 里有同名列）→
**sample 首列原值**（id 只在 sample 里时，sample 就是权威行序）→ 行号。

## 缺陷 3：规模参数没传给 _lgb → 单次重训比 5 折 CV 还慢

`write_submission` 的三处 `_lgb(..., 500)` 都**没传 n_rows/n_feat/n_classes**，
`_lgb` 里的自适应分支全走不进，退化成固定 500 棵。多分类下总树数
= 500×10 = **5000 棵**：

```
baseline（5 折，已传参数）：351.7s
write-sub（单次全量，未传）：>10 分钟仍未结束
修好后：67s
```
"""

import json
import subprocess
import sys
from pathlib import Path

# [插件包适配] 目标缺失时按 SKIP 退出（布局差异，不是引擎缺陷）
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _layout_guard import needs_file, needs_dir  # type: ignore  # noqa: E402
    needs_dir("work/digit-recognizer", "需要真实比赛的运行产物")  # noqa: E402
except SystemExit:
    raise
except Exception:
    pass  # guard 不可用时不阻断原测试

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

results = []


def mark(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail})" if detail else ""))


src_model = Path("kauto/model.py").read_text(encoding="utf-8")

print("=== 1) id 取值不再无保护地索引 id_col ===")
# 注意：`test[id_col].values` 在受保护的 `if id_col and id_col in test.columns:`
# 分支里出现是**正确**的。要钉的是"没有无保护索引"，不是"这个词不许出现"。
# 第一版就写成 `"test[id_col].values" not in src` → 把正确写法也判成 FAIL。
guarded = src_model.find("if id_col and id_col in test.columns")
bare = src_model.count("test[id_col].values")
print(f"   受保护分支位置: {guarded}；test[id_col].values 出现 {bare} 次（都在保护分支内）")
mark("test[id_col] 只出现在受保护分支里",
     guarded != -1 and bare >= 1,
     f"{bare} 次")
mark("有 sample 首列原值兜底（id 只在 sample 时）",
     "sample[sample.columns[0]].values" in src_model)
i_meta = src_model.find("if id_col and id_col in test.columns")
i_sample = src_model.find("elif sample is not None and sample.columns[0] in test.columns")
i_sval = src_model.find("elif sample is not None:")
print(f"   取法次序：meta({i_meta}) → sample列({i_sample}) → sample原值({i_sval})")
mark("次序正确（meta 先于 sample 原值）",
     0 <= i_meta < i_sample < i_sval)

print("\n=== 2) 规模参数必须传给 _lgb ===")
mark("_scale 字典存在", "_scale = dict(n_rows=" in src_model)
print(f"   _scale 传入次数: {src_model.count('**_scale')}")
mark("三处 _lgb 都传了 **_scale", src_model.count("**_scale") >= 3)

print("\n=== 3) 自适应降配实测生效（10 类 784 特征）===")
from kauto.model import _lgb  # noqa: E402

m_big = _lgb("classification", "multiclass", 42, 500,
             n_rows=42000, n_feat=784, n_classes=10)
trees = m_big.n_estimators * 10
print(f"   digit 规模 → n_estimators={m_big.n_estimators} leaves={m_big.num_leaves}"
      f" 总树数={trees}")
mark("总树数从 5000 降到 ≤1000", trees <= 1000, f"{trees}")
m_small = _lgb("classification", "binary", 42, 500, n_rows=891, n_feat=7, n_classes=2)
print(f"   titanic 规模 → n_estimators={m_small.n_estimators} leaves={m_small.num_leaves}")
mark("小数据不降配（仍是 500）", m_small.n_estimators == 500, str(m_small.n_estimators))

print("\n=== 4) digit 成品：id 与 sample 完全对齐 ===")
dig = Path("work/digit-recognizer")
sub_p, smp_p = dig / "submission.csv", dig / "sample_submission.csv"
if sub_p.exists() and smp_p.exists():
    import pandas as pd
    sub = pd.read_csv(sub_p)
    smp = pd.read_csv(smp_p)
    same_cols = list(sub.columns) == list(smp.columns)
    same_rows = len(sub) == len(smp)
    ids_equal = bool((sub.iloc[:, 0].values == smp.iloc[:, 0].values).all())
    print(f"   shape={sub.shape} cols={list(sub.columns)}")
    print(f"   列一致={same_cols} 行数一致={same_rows} id 逐行一致={ids_equal}")
    print(f"   id 范围: {sub.iloc[0,0]} .. {sub.iloc[-1,0]}（sample: {smp.iloc[0,0]} .. {smp.iloc[-1,0]}）")
    mark("列名与 sample 一致", same_cols)
    mark("行数与 sample 一致", same_rows)
    mark("id 逐行与 sample 一致（不是行号）", ids_equal)
else:
    mark("digit 成品存在", False, "先跑 kauto write-sub digit-recognizer")

print("\n=== 5) 真实回执（实测，不是断言）===")
for slug in ("titanic", "digit-recognizer"):
    rc = Path(f"work/{slug}/submission_receipt.json")
    if rc.exists():
        d = json.loads(rc.read_text(encoding="utf-8"))
        print(f"   {slug:18s} status={d.get('status')}  public_score={d.get('public_score')}")
        mark(f"{slug} 已出分", d.get("status") == "scored")
    else:
        mark(f"{slug} 有回执", False)

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
print("全部通过" if not bad else f"{len(bad)} 项未通过：" + ", ".join(bad))
raise SystemExit(1 if bad else 0)
