"""审计测试本身的质量：有多少检查只是"代码里有这句话"。

## 为什么需要审计测试

第 32 轮的教训：我上一轮写的测试是这样的——

```python
has_priority = "no_good = not journal.good_nodes" in src
```

它验证的是「**代码里有这个分支**」，而事实是这个分支**永远不会跑到**
（`journal.good_nodes` 里含 baseline，`not good_nodes` 恒为 False）。
测试通过、代码存在、逻辑正确 —— 但没有生效。

这类检查的问题不是"错的"，而是"**弱**"：文本存在 ≠ 语义正确 ≠ 行为正确。

## 强度分级

| 级别 | 手法 | 能证明什么 |
|---|---|---|
| L1 最弱 | `"xxx" in src` | 某个字符串出现在源码里 |
| L2 | AST 结构检查 | 某个语法结构存在（如特定的 if 条件组合） |
| L3 | 行为调用 | 函数真的返回了预期的结果 |
| L4 最强 | 端到端轨迹 | 系统跑起来后状态真的变了 |

本脚本统计各测试的强度分布，并列出仍然只靠 L1 的关键检查。
"""
import ast
import re
import sys
from pathlib import Path

# [插件包适配] 目标缺失时按 SKIP 退出（布局差异，不是引擎缺陷）
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _layout_guard import needs_file, needs_dir  # type: ignore  # noqa: E402
    needs_file("run_all_tests.py", "清单审计面向用户工作区布局")  # noqa: E402
except SystemExit:
    raise
except Exception:
    pass  # guard 不可用时不阻断原测试

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

print("=== 各测试的检查强度分布 ===")
rows = []
# 测试目录随布局变化：插件包里是 tests/，原始仓库里是 tools/。
_dirs = [d for d in (Path("tests"), Path("tools")) if d.is_dir() and any(d.glob("test_*.py"))]
_scan = _dirs[0] if _dirs else Path("tests")
for t in sorted(_scan.glob("test_*.py")):
    src = t.read_text(encoding="utf-8")
    l1 = len(re.findall(r'in src[_\w]*\b', src))          # 文本匹配
    # AST 用法：ast.parse / ast.walk / ast.get_source_segment
    l2 = len(re.findall(r'ast\.(parse|walk|get_source_segment)', src))
    # 行为调用：直接调被测函数并断言其返回
    l3 = len(re.findall(r'= (kiter|cli|bl|skillbridge|subcheck|model|prep|dataoverview)\.\w+\(', src))
    rows.append((t.name, l1, l2, l3))

print(f"{'测试文件':36s} {'L1文本':>7s} {'L2 AST':>7s} {'L3行为':>7s}")
for name, l1, l2, l3 in rows:
    print(f"{name:36s} {l1:>7d} {l2:>7d} {l3:>7d}")

total_l1 = sum(r[1] for r in rows)
total_l2 = sum(r[2] for r in rows)
total_l3 = sum(r[3] for r in rows)
print()
print(f"合计: L1 文本 {total_l1} 处 / L2 AST {total_l2} 处 / L3 行为 {total_l3} 处")

results = []

print("\n=== 审计结论 ===")
# 有 AST 或行为验证的测试算"够强"
strong = [r[0] for r in rows if r[2] > 0 or r[3] > 0]
weak_only = [r[0] for r in rows if r[1] > 0 and r[2] == 0 and r[3] == 0]
print(f"   含 AST 或行为验证的测试: {len(strong)}/{len(rows)}")
if weak_only:
    print(f"   仅靠文本匹配的测试: {weak_only}")
else:
    print("   仅靠文本匹配的测试: （无）")
# 这条是**现状指标**，不是"必须达标"的硬判据。
# 一次性把 44 个测试全升级不现实，也没必要 —— 关键检查（提交守卫、搜索策略、
# 融合接线）已经升级就够了。这里只记录比例，不判失败。
ratio = len(strong) / max(1, len(rows))
print(f"   → 实证覆盖率: {ratio:.0%}（记录用，不判失败）")
results.append((f"实证覆盖率 {ratio:.0%}（记录）", None))

print("\n=== 关键检查是否已升级 ===")
# 这几个测试曾用弱检查而漏掉真实缺陷，必须含 AST 或行为
CRITICAL = ["test_search_fixes.py", "test_blend_wiring.py", "test_debug_priority.py"]
for name in CRITICAL:
    row = next((r for r in rows if r[0] == name), None)
    if row:
        ok = row[2] > 0 or row[3] > 0
        print(f"   {'OK  ' if ok else 'WEAK'} {name:28s} AST={row[2]} 行为={row[3]}")
        results.append((f"{name} 已升级", ok))
    else:
        print(f"   缺 {name}")
        results.append((f"{name} 已升级", False))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    mark = "INFO" if ok is None else ("PASS" if ok else "FAIL")
    print(f"  {mark:5s} {n}")
    if ok is False:
        bad += 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
