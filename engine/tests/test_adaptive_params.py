"""LightGBM 参数必须随数据规模自适应。

## 实测

`digit-recognizer`（42000 行 / 784 特征 / **10 类**）跑 baseline：
**15.6 分钟仍在跑**（CPU 930 秒），没结束。

根因：参数写死。

```python
common = dict(n_estimators=400, num_leaves=63, ...)
```

多分类上 LightGBM **每类训练 n_estimators 棵树** —— 10 类 × 400 = 4000 棵，
再乘 5 折 CV = **20000 棵**。基线慢到这个程度就失去了"先有参照分"的意义。

## 修法

| 触发 | 调整 |
|---|---|
| 类别 > 2 | `n_estimators` 按类数摊薄（`400/(n_classes-1)`，下限 60）|
| 特征 > 200 | `num_leaves` 63 → 31 |
| 行数 > 20000 | `n_estimators` 再 × 0.6 |

## 实测效果

| 项 | 修复前 | 修复后 |
|---|---|---|
| digit baseline 耗时 | **>15.6 分钟未完成** | **307.2s（5.1 分钟）** |
| CV accuracy | — | **0.955536** |
| demo-comp（小数据） | 400×63 | **仍 400×63（不受影响）** |
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import model

results = []

print("=== 1) 小数据参数不变（不能被降配误伤）===")
m_small = model._lgb("classification", "binary", 42, n_rows=1200, n_feat=7, n_classes=2)
print(f"   demo 尺度: n_estimators={m_small.n_estimators}, num_leaves={m_small.num_leaves}")
ok1 = m_small.n_estimators == 400 and m_small.num_leaves == 63
print(f"   → 保持 400/63: {ok1}")
results.append(("小数据不变", ok1))

print("\n=== 2) 大 + 多分类必须有明显降配 ===")
m_big = model._lgb("classification", "multiclass", 42,
                   n_rows=33600, n_feat=784, n_classes=10)
print(f"   digit 尺度: n_estimators={m_big.n_estimators}, num_leaves={m_big.num_leaves}")
tot_small = m_small.n_estimators * 2
tot_big = m_big.n_estimators * 10
print(f"   等价总树数: 小={tot_small}  大={tot_big}")
ok2 = m_big.n_estimators <= 100 and m_big.num_leaves <= 31
print(f"   → n_estimators≤100 且 num_leaves≤31: {ok2}")
results.append(("大+多分类降配", ok2))

print("\n=== 3) 降配比例应显著（≥3 倍）===")
# 对比"如果不降配"的等价总树数
naive = 400 * 10
print(f"   朴素参数总树数={naive}  实际={tot_big}  降幅={naive/tot_big:.1f}x")
ok3 = naive / tot_big >= 3
print(f"   → 降幅 ≥3x: {ok3}")
results.append(("降幅显著", ok3))

print("\n=== 4) 高维但少类（如特征多、二分类）也应减叶子 ===")
m_hd = model._lgb("classification", "binary", 42, n_rows=5000, n_feat=500, n_classes=2)
print(f"   高维二分类: n_estimators={m_hd.n_estimators}, num_leaves={m_hd.num_leaves}")
ok4 = m_hd.num_leaves <= 31
print(f"   → num_leaves 减到 31: {ok4}")
results.append(("高维减叶子", ok4))

print("\n=== 5) 有下限，不能被降得过头 ===")
m_ext = model._lgb("classification", "multiclass", 42,
                   n_rows=200000, n_feat=2000, n_classes=50)
print(f"   极端尺度: n_estimators={m_ext.n_estimators}")
ok5 = m_ext.n_estimators >= 60
print(f"   → 不低于 60: {ok5}")
results.append(("有下限", ok5))

print("\n=== 6) 真实 digit baseline 分数合理 ===")
import json
bj = Path("work/digit-recognizer/baseline.json")
if bj.exists():
    b = json.loads(bj.read_text(encoding="utf-8"))
    cv = b.get("cv_mean")
    sec = b.get("seconds")
    print(f"   CV={cv}  metric={b.get('metric')}  耗时={sec}s")
    ok6 = (b.get("metric") == "accuracy" and cv is not None
           and 0.93 <= cv <= 0.99 and sec is not None and sec < 900)
    print(f"   → accuracy 且 CV∈[0.93,0.99] 且耗时<900s: {ok6}")
    results.append(("digit baseline 合理", ok6))
else:
    print("   (缺 baseline，跳过)")
    results.append(("digit baseline 合理", None))

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
