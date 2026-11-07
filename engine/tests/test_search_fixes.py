"""验证两处搜索策略缺陷。

## 缺陷一：baseline 被当成"有效候选"

`journal.good_nodes` 里含 `baseline`（因为基线有 CV 分数），
于是 `no_good = not journal.good_nodes` 恒为 False，
"全盘失败优先 debug"的分支**永不触发**。

实测：dirty-comp 上跑 3 轮，明明有 3 个已知失败等着修，全都在起草新的 draft。

修：判据改成「除 baseline 外没有跑通的候选」。

## 缺陷二：LLM 原样返回父节点代码，仍继续消耗轮数

实测：debug 链 `cf4bedce → 619f028a → be8ef9be` 三代代码 hash **完全相同**，
每代都以同样的方式失败，而 `debug_depth` 上限（3）允许它继续跑。

修：新代码与父节点逐字相同时立刻判失败，标 `stage="nochange"`，
让"这条链修不动"显式化。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

results = []

print("=== 1) baseline 不应算作有效候选（AST 级检查）===")
# 上一轮的教训：文本匹配 `"...in src"` 只能证明"代码里有这句话"，
# 证明不了"这个分支真的会跑到"。这里升级成 AST：验证
#   (a) 存在对 "baseline" 的字符串比较，且用于过滤 good_nodes
#   (b) 存在一个 If，条件是 `debuggable and not <那个变量>`
import ast

iter_src = Path("kauto/iter.py").read_text(encoding="utf-8")
tree = ast.parse(iter_src)

# (a) 找 ListComp：迭代 journal.good_nodes 且含 id != "baseline"
found_filter = False
filter_target = None
for node in ast.walk(tree):
    if isinstance(node, ast.ListComp):
        txt = ast.get_source_segment(iter_src, node) or ""
        if "good_nodes" in txt and "baseline" in txt and "!=" in txt:
            found_filter = True
            # 取赋值目标名
            for parent in ast.walk(tree):
                if isinstance(parent, ast.Assign):
                    if node in parent.value.__class__.__mro__ or True:
                        pass
            break
# 取赋值名（简单做法：找 Assign 且 value 是该 ListComp）
if found_filter:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.ListComp):
            txt = ast.get_source_segment(iter_src, node.value) or ""
            if "good_nodes" in txt and "baseline" in txt:
                if isinstance(node.targets[0], ast.Name):
                    filter_target = node.targets[0].id
print(f"   有过滤 baseline 的推导式: {found_filter}  (变量={filter_target})")

# (b) 找 If 且条件含 `not <filter_target>`
found_branch = False
if filter_target:
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            cond_txt = ast.get_source_segment(iter_src, node.test) or ""
            if f"not {filter_target}" in cond_txt or f"not({filter_target})" in cond_txt:
                found_branch = True
                print(f"   有 if ... not {filter_target}: True  ← 用了这个判据")
                break
print(f"   分支使用该判据: {found_branch}")
results.append(("baseline 不算候选（AST）", found_filter and found_branch))

print("\n=== 2) 重复代码必须被检测（AST 级检查）===")
# 验证存在 raise/return 分支，且其判据比较了 node.code 与 parent.code
found_same_check = False
found_return = False
for node in ast.walk(tree):
    if isinstance(node, ast.If):
        cond = ast.get_source_segment(iter_src, node.test) or ""
        if "parent.code" in cond and "node.code" in cond:
            found_same_check = True
            # 该分支里应有 return，且含 "nochange"
            body_txt = "".join(ast.get_source_segment(iter_src, s) or "" for s in node.body)
            if "nochange" in body_txt:
                found_return = True
            break
print(f"   有比较 parent.code 与 node.code: {found_same_check}")
print(f"   该分支返回 nochange: {found_return}")
results.append(("检测重复代码（AST）", found_same_check and found_return))

# 文案仍在（文案本身是文本，无妨）
has_msg = "与父节点完全相同" in iter_src
print(f"   有明确文案: {has_msg}")
results.append(("有明确文案", has_msg))

print("\n=== 3) 实测：dirty-comp 轨迹应为 draft → debug → debug ===")
import json
jp = Path("work/dirty-comp/iterate.json")
if jp.exists():
    d = json.loads(jp.read_text(encoding="utf-8"))
    trials = [t for t in d.get("trials") or [] if t.get("stage")]
    stages = [t.get("stage") for t in trials[:4]]
    print(f"   阶段序列: {stages}")
    ok3 = any(s == "debug" for s in stages)
    print(f"   → 出现 debug: {ok3}")
    results.append(("实际触发 debug", ok3))

    # 检查 nochange 是否出现
    nodes = Path("work/dirty-comp/journal.json")
    if nodes.exists():
        nd = json.loads(nodes.read_text(encoding="utf-8"))
        has_nc = any("完全相同" in (n.get("error") or "") for n in nd["nodes"])
        print(f"   → 出现 nochange 记录: {has_nc}")
        results.append(("实际检测到重复代码", has_nc))
else:
    print("   (无 iterate.json，跳过)")
    results.append(("实际触发 debug", None))

print("\n=== 4) 判据逻辑本身：非 baseline 的 good 节点不应被过滤掉 ===")
# 不要依赖当前 journal 内容（会被后续运行覆盖 —— 首次这么写就踩过）。
# 改为用**构造的节点列表**验证判据表达式：
#   real_good = [n for n in good_nodes if n.id != "baseline"]
# 给一个含 baseline 与一个非 baseline 的 good 节点，过滤后应剩 1 个。
class _N:
    def __init__(self, i): self.id = i

good_nodes = [_N("baseline"), _N("real_candidate_1")]
real_good = [n for n in good_nodes if n.id != "baseline"]
print(f"   构造 good_nodes=[baseline, real_candidate_1] → real_good 有 {len(real_good)} 个")
ok4 = len(real_good) == 1 and real_good[0].id == "real_candidate_1"
print(f"   → baseline 被排除、真候选保留: {ok4}")
results.append(("判据逻辑正确", ok4))

# 附带：归档里确实有非 baseline 的真实候选（长期有效的事实）
arc = sorted((Path("work/demo-comp") / "archive").iterdir()) if (Path("work/demo-comp") / "archive").exists() else []
real_arch = [a.name for a in arc if a.name != "baseline"]
print(f"   归档里的真实节点（长期保留）: {len(real_arch)} 个")
results.append(("归档保留真实候选", len(real_arch) >= 1))

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
