"""`DRAFT_PROMPT` 丢弃了 `memory` —— hint 与历史一起消失。

## 缺陷（比上一轮的 HINTS 更严重）

`build_prompt` 里：

```python
memory = journal.summary(include_code=False)
if hint:
    memory += f"\\n（本轮额外方向）{hint}"
```

提示被拼进 `memory`。但 `DRAFT_PROMPT` 模板里**没有 `{memory}` 占位符** ——
它只有 `{intel}` / `{failures}` / `{baseline_cv}` 等。

于是：**即使 HINTS 有内容，draft 也永远收不到**。

## 实测

```python
p = DRAFT_PROMPT.format(..., memory="MY_HINT_MARKER", ...)
"MY_HINT_MARKER" in p   # → False（修复前）
```

三个模板的占位符对比：

| 模板 | 有 `{memory}` |
|---|---|
| `DRAFT_PROMPT` | **False** |
| `IMPROVE_PROMPT` | True |
| `DEBUG_PROMPT` | True |

只有 draft 缺 —— 而 draft 恰恰是"换族探索"时最需要方向提示的场合。

## 与上一轮 HINTS 缺陷的关系

两轮合起来才完整：

1. 第 52 轮：`HINTS` 是空的 → 提示为空
2. 第 53 轮：即使提示非空，`DRAFT_PROMPT` 也不用它 → 提示被丢弃

**修复第 52 轮时如果只验证"HINTS 非空了"就收工，这个缺陷仍然会让 HINTS 完全无效。**
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import iter as kiter

results = []

print("=== 1) 三个模板都必须有 {memory} 占位 ===")
for name in ("DRAFT_PROMPT", "IMPROVE_PROMPT", "DEBUG_PROMPT"):
    t = getattr(kiter, name)
    has = "{memory}" in t
    print(f"   {'OK  ' if has else 'FAIL'} {name:16s}")
    results.append((f"{name} 含 memory", has))

print("\n=== 2) 实测：draft 渲染后 hint 必须出现 ===")
p = kiter.DRAFT_PROMPT.format(
    slug="demo", task="classification", kind="binary", target="target",
    metric="accuracy", direction="越大越好", overview="概览",
    pred_spec="正类概率", intel="", failures="（无）",
    memory="MARKER_XYZ", baseline_cv="0.8")
print(f"   渲染含 MARKER_XYZ: {'MARKER_XYZ' in p}")
print(f"   渲染含「已试过的方案」标题: {'已试过的方案' in p}")
ok2 = "MARKER_XYZ" in p
results.append(("draft 保留 memory", ok2))

print("\n=== 3) memory 内容要落在有标题的段落，不是裸拼 ===")
has_header = "【已试过的方案（Memory）】" in p
print(f"   有独立标题: {has_header}")
results.append(("memory 有标题", has_header))

print("\n=== 4) 模拟 build_prompt 的 hint 注入能到达最终 prompt ===")
# 复刻 build_prompt 的拼接逻辑
base_memory = "方案#0（draft, CV=0.81）"
hint = "换一个模型族（不同算法家族）"
memory = base_memory + f"\n（本轮额外方向）{hint}"
p2 = kiter.DRAFT_PROMPT.format(
    slug="demo", task="classification", kind="binary", target="target",
    metric="accuracy", direction="越大越好", overview="概览",
    pred_spec="正类概率", intel="", failures="（无）",
    memory=memory, baseline_cv="0.8")
reached = hint in p2 and base_memory in p2
print(f"   hint 到达 prompt: {hint in p2}")
print(f"   历史到达 prompt: {base_memory in p2}")
results.append(("hint 与历史都到达", reached))

print("\n=== 5) 回归：不给 memory 时不该崩 ===")
p3 = kiter.DRAFT_PROMPT.format(
    slug="d", task="c", kind="b", target="t", metric="m", direction="d",
    overview="o", pred_spec="p", intel="", failures="（无）",
    memory="（无）", baseline_cv="0.5")
ok5 = "（无）" in p3
print(f"   memory 为「（无）」仍正常渲染: {ok5}")
results.append(("空 memory 不崩", ok5))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
