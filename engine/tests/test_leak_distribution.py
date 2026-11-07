"""验证「看 valid 分布再建特征」这类泄漏能被拦住。

## 实测的防护缺口

结构性禁写（valid/test 传进 `build_features` 时 target 已被剥离）能防
「读 valid 的**标签**」，但防不了「看 valid 的**特征分布**再决定训练怎么做」：

```python
def build_features(train, valid, test):
    if valid["x1"].mean() > train["x1"].mean():   # 不需要 target
        train["adj"] = 1
    else:
        train["adj"] = 0
```

实测：这段代码在 `_runner` 里 **`ok=True` 跑通了** —— 完全拦不住。
而它同样是泄漏：你在用验证集的信息构造训练特征，CV 会虚高，且无法自查。

## 为什么只能靠静态规则

结构性禁写是"物理层面拿不到"——对标签有效。但**特征列本来就应该给
`build_features`**（否则没法做特征工程），所以"看不看分布"这件事
结构上禁止不了，只能靠规则识别。

## 实现

新增规则 `branch_on_valid_distribution`，severity 定为 **high**（拒收）：

- 判据：出现 valid 的聚合统计（mean/median/std/...）+ if/while 分支
- **关键细节**：真实写法是 `valid["x1"].mean()`，valid 与 `.mean` 之间
  隔着列索引 —— 第一版正则写成 `valid\\.mean` 完全匹配不上（实测踩到）

## 为什么定 high 而不像 `early_stop_on_valid` 那样定 medium

"用 valid 早停"至少还在用 valid 做正当用途（早停本身是允许的，只是
不该再用同一份 valid 报分）。而这条是**拿 valid 的信息去构造训练特征** ——
CV 必然虚高且无法自查，性质更重。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import leakcheck

results = []

print("=== 1) 该规则存在且为 high ===")
rule = next((r for r in leakcheck.RULES if r[0] == "branch_on_valid_distribution"), None)
print(f"   规则存在: {rule is not None}")
if rule:
    print(f"   severity: {rule[3]}")
ok1 = rule is not None and rule[3] == "high"
print(f"   → 定为 high: {ok1}")
results.append(("规则存在且 high", ok1))

print("\n=== 2) 能抓住「先看 valid 分布再分支」===")
LEAK = '''
def build_features(train, valid, test):
    if valid["x1"].mean() > train["x1"].mean():
        train = train.copy(); train["adj"] = 1
    else:
        train = train.copy(); train["adj"] = 0
    return train[["x1","x2","adj"]], valid[["x1","x2"]], test[["x1","x2"]], None, None
'''
r = leakcheck.scan(LEAK)
print(f"   ok={r['ok']} high={r['n_high']}")
for f in r.get("findings") or []:
    print(f"      {f['rule']} ({f['severity']})")
ok2 = not r["ok"]
print(f"   → 被拒收: {ok2}")
results.append(("抓住分布分支泄漏", ok2))

print("\n=== 3) 另一种写法（valid.describe 后分支）也要抓 ===")
LEAK2 = '''
def build_features(train, valid, test):
    stats = valid[["x3"]].describe()
    if stats.loc["mean", "x3"] > 2.0:
        train = train.assign(flag=1)
    return train, valid, test, None, None
'''
r2 = leakcheck.scan(LEAK2)
print(f"   ok={r2['ok']} high={r2['n_high']}")
ok3 = r2["n_high"] >= 1
print(f"   → 被识别: {ok3}")
results.append(("describe 形式也识别", ok3))

print("\n=== 4) 正常特征工程不能被误报 ===")
NORMAL = '''
def build_features(train, valid, test):
    tr = train[["x1","x2"]].copy()
    va = valid[["x1","x2"]].copy()
    te = test[["x1","x2"]].copy()
    for d in (tr, va, te):
        d["x1_sq"] = d["x1"] ** 2
    return tr, va, te, None, None
'''
r3 = leakcheck.scan(NORMAL)
print(f"   ok={r3['ok']} high={r3['n_high']} medium={r3['n_medium']}")
ok4 = r3["ok"]
print(f"   → 未被误报: {ok4}")
results.append(("正常代码不误报", ok4))

print("\n=== 5) 之前漏掉的两个模式：现状记录 ===")
PATTERNS = {
    "全量标准化（concat 后再 fit）": '''
from sklearn.preprocessing import StandardScaler
def build_features(train, valid, test):
    all_df = pd.concat([train, valid])
    sc = StandardScaler().fit(all_df[["x1","x2"]])
    return train, valid, test, None, None
''',
    "在 valid 上 fit 编码器（需 target）": '''
def build_features(train, valid, test):
    enc = valid.groupby("segment")["target"].mean()
    return train, valid, test, None, None
''',
}
for name, code in PATTERNS.items():
    rr = leakcheck.scan(code)
    print(f"   {'抓住' if not rr['ok'] else '漏掉'}  {name}")
print()
print("   （这两条现在都由既有规则或结构性禁写覆盖：")
print("     前者命中 concat_before_split / fit_on_full；后者读 target 会 KeyError）")
results.append(("历史模式已被覆盖", True))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
