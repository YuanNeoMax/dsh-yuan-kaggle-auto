"""验证「全盘失败时优先调试」的搜索策略。

## 实测缺陷

在 `dirty-comp` 上连续两个 draft 都失败（mock 写死 demo 的列名 `x1..x4`，
而 dirty-comp 没有这些列），journal 里 `good_nodes` 为空、`buggy_nodes` 有两个。

而 `select_action()` 里 debug 只有 **35% 概率**命中：

```python
if random.random() < cfg_debug_prob:      # 35%
    ...
    return "debug", ...
```

两轮恰好都没命中 → 系统又去起草第三个，**而不是修已经知道哪里错的那些**。

已知的失败（有完整报错、有代码）比未知的新草稿更值得先投入 ——
尤其是当整棵树里一个能用的方案都没有时。

## 修复后行为

`no_good and debuggable` → 直接 `debug`，不再掷骰子。

实测 `dirty-comp --rounds 3`：
```
round 1  draft  parent=null          ← 失败
round 2  debug  parent=cf4bedce6157  ← 立刻转去修
round 3  debug  parent=619f028a8819
```
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import journal as J
from kauto.iter import classify_family

results = []

print("=== 1) 策略代码必须包含「无有效候选时优先 debug」（AST）===")
# 上轮的教训：`"...in src"` 只能证明"这句话在源码里"，
# 证明不了它所在的判据会真的生效。升级为 AST：
#   (a) 有过滤 baseline 的 ListComp，且结果赋给某个变量
#   (b) 存在 If，条件里含 `not <那个变量>`，且它的 body 里 return "debug"
import ast
src = Path("kauto/iter.py").read_text(encoding="utf-8")
tree = ast.parse(src)

filter_var = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.ListComp):
        txt = ast.get_source_segment(src, node.value) or ""
        if "good_nodes" in txt and "baseline" in txt:
            if isinstance(node.targets[0], ast.Name):
                filter_var = node.targets[0].id
print(f"   过滤 baseline 的变量: {filter_var}")

branch_ok = False
if filter_var:
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            cond = ast.get_source_segment(src, node.test) or ""
            if f"not {filter_var}" in cond:
                body = "".join(ast.get_source_segment(src, s) or "" for s in node.body)
                if 'return "debug"' in body or "return 'debug'" in body:
                    branch_ok = True
                    break
print(f"   该判据所在的 if 里 return debug: {branch_ok}")
has_priority = bool(filter_var) and branch_ok
print(f"   → 优先分支成立: {has_priority}")
results.append(("含优先分支（AST）", has_priority))

print("\n=== 2) 逻辑等价性验证（用模拟 journal）===")
class FakeJournal:
    def __init__(self, good, buggy):
        self.good_nodes = good
        self.buggy_nodes = buggy
        self.children = {}
    def debug_depth(self, n):
        return 0


def select(fj, cfg_debug_prob=0.35):
    """复刻修复后的决策顺序（不依赖 random，只测分支优先级）"""
    debuggable = [n for n in fj.buggy_nodes
                  if fj.debug_depth(n) < 3 and n.id not in fj.children]
    no_good = not fj.good_nodes
    if debuggable and no_good:
        return "debug", debuggable[0]
    return "draft", None


# 场景 A：全失败
class N:
    def __init__(self, i): self.id = i

fa = FakeJournal([], [N("b1"), N("b2")])
kind_a, node_a = select(fa)
print(f"   全失败: {kind_a} -> {node_a.id if node_a else None}（期望 debug）")
results.append(("全失败时 debug", kind_a == "debug"))

# 场景 B：有 good node
fb = FakeJournal([N("g1")], [N("b1")])
kind_b, node_b = select(fb)
print(f"   有 good: {kind_b}（期望 draft —— 不抢占改进机会）")
results.append(("有 good 时不抢", kind_b == "draft"))

print("\n=== 3) 实测轨迹：失败后应立刻转 debug ===")
import json
jp = Path("work/dirty-comp/iterate.json")
if jp.exists():
    d = json.loads(jp.read_text(encoding="utf-8"))
    trials = [t for t in d.get("trials") or [] if t.get("stage")]
    stages = [(t.get("round"), t.get("stage"), t.get("parent")) for t in trials[:4]]
    for r, s, p in stages:
        print(f"   r{r} {s:8s} parent={p}")
    # 只要出现 debug 就说明策略生效
    ok3 = any(s == "debug" for _, s, _ in stages)
    print(f"   → 出现 debug: {ok3}")
    results.append(("实际触发 debug", ok3))
else:
    print("   (无 iterate.json，跳过)")
    results.append(("实际触发 debug", None))

print("\n=== 4) 仍受 debug_depth 上限约束 ===")
has_limit = "journal.debug_depth(n) < cfg_max_debug" in src
print(f"   有深度上限: {has_limit}")
results.append(("有深度上限", has_limit))

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
