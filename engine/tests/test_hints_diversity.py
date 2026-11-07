"""`HINTS` 四个提示全是空串 —— 换族机制形同虚设。

## 实测

```python
HINTS = ["", "", "", ""]
...
plans.append((None, "draft", HINTS[(r + i) % len(HINTS)]))
```

这行代码**看起来有设计**：用 `(r+i) % 4` 在批内轮换提示，让并发的几个 draft
从不同角度切入（换模型族 / 做特征 / 提稳健性 / 调正则）。

实际轮换的是**四个空串** —— 于是：

- batch 内每个 draft 拿到同一个空提示
- 产出的代码自然同质
- "并发去重"退化成"发三个一样的"

## 为什么难发现

1. `HINTS` 的用法完全正确（取模轮换），看起来是有意设计
2. 空串是**合法的提示**（等于"没有额外要求"），不会报错
3. 只有真的并发跑、并且去比对批内动作时才能看出来

我是从 `iterate.json` 的 `"本批动作：draft- / improvebaseline / draft-"` 里
看到**两个 draft 一模一样**，才顺着查到 HINTS 的定义。

## 修法

填入四条覆盖不同切入点的提示（模型族 / 特征 / 稳健性 / 正则）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

results = []

print("=== 1) HINTS 不能有空串 ===")
from kauto import iter as kiter
import inspect
src = inspect.getsource(kiter.iterate)
i = src.find("HINTS = [")
block = src[i:i + 400]
has_empty = 'HINTS = ["", "", "", ""]' in src
print(f"   仍是四个空串: {has_empty}（应为 False）")
results.append(("HINTS 非空", not has_empty))

print("\n=== 2) 至少四条不同提示 ===")
import ast
tree = ast.parse(src)
hints = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        if any(getattr(t, "id", "") == "HINTS" for t in node.targets):
            if isinstance(node.value, ast.List):
                hints = [ast.literal_eval(e) if not isinstance(e, ast.Constant)
                         else e.value for e in node.value.elts]
if hints:
    print(f"   共 {len(hints)} 条")
    for h in hints:
        print(f"     - {h[:50]}")
    non_empty = all(h.strip() for h in hints)
    distinct = len(set(hints)) == len(hints)
    print(f"   全非空: {non_empty}   全不同: {distinct}")
    results.append(("HINTS 全非空", non_empty))
    results.append(("HINTS 全不同", distinct))
else:
    print("   未找到 HINTS 定义")
    results.append(("HINTS 全非空", False))

print("\n=== 3) 取模轮换在批内确实产出不同提示 ===")
if hints:
    r = 0
    got = [hints[(r + i) % len(hints)] for i in range(len(hints))]
    print(f"   batch=3 拿到的: {[g[:20] for g in got[:3]]}")
    ok3 = len(set(got[:3])) == min(3, len(hints))
    print(f"   → 批内不同: {ok3}")
    results.append(("批内轮换有效", ok3))

print("\n=== 4) 实测：批内动作不再全是 draft ===")
import json
from pathlib import Path
ip = Path("work/demo-comp/iterate.json")
if ip.exists():
    d = json.loads(ip.read_text(encoding="utf-8"))
    notes = [t["note"] for t in (d.get("trials") or []) if t.get("note") and "本批" in t["note"]]
    print(f"   批次记录: {notes}")
    # 至少有一个批次里动作不是全同
    ok4 = any("/".join(set(n.split("：")[-1].split(" / "))) != n.split("：")[-1].split(" / ")[0]
              for n in notes) if notes else False
    diverse = any(len(set(p.split("-")[0].split("b")[0] for p in n.split("：")[-1].split(" / "))) > 1
                  for n in notes) if notes else False
    print(f"   → 存在多样化批次: {diverse}")
    results.append(("批内动作多样化", diverse))
else:
    print("   (缺 iterate.json，跳过)")
    results.append(("批内动作多样化", None))

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
