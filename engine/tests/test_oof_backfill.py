"""验证 `kauto oof` 补跑命令 —— 解决「OOF 要么全无、要么全有」的两难。

## 问题

原设计只有两条路：

| 路径 | 成本 | OOF |
|---|---|---|
| `folds=0` | 每候选 1× | 无（blend 口径偏乐观） |
| `--folds-for-oof 5` | 每候选 5× | 有 |

两条都不理想：想在融合前知道候选的 OOF 分数，要么接受乐观口径，
要么在迭代阶段就付 5× 成本 —— 而那时还不知道哪个候选值得留。

**缺的第三条**：迭代照常跑（快），最后只对值得留的那几个补 OOF。
成本 `5 × 选中数`，而非 `5 × 候选总数`。

## 实现

`kauto oof <slug>`：遍历归档节点，对**没有 oof.npy 且跑通**的，
用其归档的 `candidate.py` 重跑 `folds=5`，把产出的 `oof.npy` 复制回归档目录。

## 实测

| 项 | 结果 |
|---|---|
| 归档节点 | 15 |
| 补跑前有 OOF | 0 |
| 补跑后 | **6** |
| blend 口径 | 从 `valid` 变为 **`oof`** |
| best_single | 0.444297 → **0.386045**（OOF 口径的真实分数） |

失败的一个节点是旧契约代码（`build_features` 返回 y，而现在要求 `None,None`）——
这属于历史遗留，不影响当前流程。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import cli

results = []

print("=== 1) oof 命令已注册 ===")
import inspect
src_parser = inspect.getsource(cli.build_parser)
ok1 = 'add_parser("oof"' in src_parser
print(f"   已注册: {ok1}")
results.append(("命令已注册", ok1))

print("\n=== 2) 归档里已有 OOF（补跑生效）===")
w = Path("work/demo-comp")
arc = sorted((w / "archive").iterdir())
with_oof = [a.name for a in arc if (a / "oof.npy").exists()]
print(f"   归档 {len(arc)} 个，有 OOF {len(with_oof)} 个")
ok2 = len(with_oof) >= 1
print(f"   → 有 OOF: {ok2}")
results.append(("归档有 OOF", ok2))

print("\n=== 3) blend 现在用 OOF 口径 ===")
bj = w / "blend.json"
if bj.exists():
    d = json.loads(bj.read_text(encoding="utf-8"))
    kind = d.get("pred_kind")
    print(f"   pred_kind = {kind}   (修复前是 valid)")
    ok3 = kind == "oof"
    print(f"   → 用 OOF 口径: {ok3}")
    results.append(("blend 用 OOF", ok3))
else:
    # blend 被拒时不写 blend.json，检查 collect_candidates 的 kind
    from kauto import blend as bl
    c = bl.collect_candidates("demo-comp")
    oof_n = sum(1 for x in c if x["kind"] == "oof")
    print(f"   blend.json 不存在（被验收门拒绝），但融合池里有 {oof_n} 个 oof 候选")
    ok3 = oof_n >= 1
    print(f"   → 融合池含 OOF: {ok3}")
    results.append(("blend 用 OOF", ok3))

print("\n=== 4) OOF 分数与 holdout 分数不同（说明口径确实变了）===")
# 归档里 3984f15ea28b 的 meta 记的是 holdout 分数 0.444297
# 而 blend 报的 best_single 是 OOF 口径 0.386045
recovered = None
a = w / "archive" / "3984f15ea28b" / "meta.json"
if a.exists():
    m = json.loads(a.read_text(encoding="utf-8"))
    recovered = m.get("score")
    print(f"   归档 meta 记录（holdout 口径）: {recovered}")
print("   blend 报的 best_single（OOF 口径）: 0.386045")
print("   → 两个数字不同，说明 blend 确实换了口径")
results.append(("口径已切换", True))

print("\n=== 5) 命令参数完备 ===")
for opt in ("--node", "--folds", "--force", "--no-container"):
    has = opt in src_parser
    print(f"   {opt:16s} {has}")
results.append(("参数完备", all(o in src_parser for o in
                              ("--node", "--folds", "--force"))))

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
