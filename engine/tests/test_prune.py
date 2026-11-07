"""验证 prune 命令：能清、能保、默认安全。

背景：每次迭代新建 `gen_<node_id>/`，从不删除。实测 `work/demo-comp`
累积到 102 个（1.4 MB）。真实比赛上 npy 更大，长期会无限膨胀。

但**清理是有代价的**：`gen_*` 不只是垃圾，它们也是融合候选来源。
实测清理后融合池从 21 个候选掉到 2 个 —— 因为归档里的 11 个节点内容彼此全同。
所以默认必须是 dry-run。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import cli

results = []

print("=== 1) 默认行为是 dry-run（不 --yes 不删）===")
src = inspect.getsource(cli.cmd_prune)
has_dry_default = 'default=True' in inspect.getsource(cli.build_parser) and "dry_run" in inspect.getsource(cli.build_parser)
has_yes_gate = "if not args.yes" in src and "未加 --yes" in src
print(f"   dry_run 默认 True: {has_dry_default}")
print(f"   无 --yes 时不删: {has_yes_gate}")
results.append(("默认 dry-run", has_yes_gate))

print("\n=== 2) 保护清单：已归档 + best node ===")
has_arch_guard = "archived" in src and "in archived" in src
has_best_guard = "best" in src and "nid == best" in src
print(f"   保护已归档节点: {has_arch_guard}")
print(f"   保护 best 节点: {has_best_guard}")
results.append(("保护归档与 best", has_arch_guard and has_best_guard))

print("\n=== 3) 必须把清理代价分类说清 ===")
# 第 51 轮改过文案：原来笼统一句"清理后可能显著减少候选"让人不敢删中间产物，
# 现在分两类说明 —— 中间产物删了无代价，节点目录删了只丢 npy 副本。
has_warn = "不参与融合" in src          # 中间产物无代价
has_keep_note = "代码在 journal.json" in src   # 代码不会丢
print(f"   说明中间产物无代价: {has_warn}")
print(f"   说明代码不丢: {has_keep_note}")
results.append(("提示清理代价", has_warn and has_keep_note))

print("\n=== 4) 实测：受保护节点不会被删 ===")
import json
work = Path("work/demo-comp")
arch_before = {d.name for d in (work / "archive").iterdir()} if (work / "archive").exists() else set()
best_before = (work / "best_node.txt").read_text(encoding="utf-8").strip() if (work / "best_node.txt").exists() else None
gens_before = {g.name[len("gen_"):] for g in work.glob("gen_*")}

# 执行一次（此时已清理过，可清理数应为 0，但逻辑仍应保护）
class Args:
    slug = "demo-comp"
    dry_run = False
    yes = True

cli.cmd_prune(Args())

arch_after = {d.name for d in (work / "archive").iterdir()} if (work / "archive").exists() else set()
best_after = (work / "best_node.txt").read_text(encoding="utf-8").strip() if (work / "best_node.txt").exists() else None
gens_after = {g.name[len("gen_"):] for g in work.glob("gen_*")}

ok4 = (arch_before == arch_after) and (best_before == best_after) and (best_after in gens_after)
print(f"   归档目录不变: {arch_before == arch_after}")
print(f"   best 不变: {best_before == best_after}")
print(f"   best 的 gen_ 仍在: {best_after in gens_after}")
results.append(("受保护节点未被删", ok4))

print("\n=== 5) CLI 已注册 prune ===")
from kauto import cli as _c
src_parser = inspect.getsource(_c.build_parser)
ok5 = 'add_parser("prune"' in src_parser
print(f"   已注册: {ok5}")
results.append(("CLI 已注册", ok5))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
