"""结构性防漂移：所有提交路径必须走同一套守卫。

## 为什么需要这个

连续三轮发现的都是**同一类缺陷**：组件存在、但没接到主流程上。

| 轮次 | 缺陷 |
|---|---|
| 11 | blend 产出写到 blend_test_pred.npy，write-sub 只读 best_test_pred.npy |
| 13 | 归档节点不进融合池 |
| 14 | `run --yes` 绕过 `submit --yes` 的溯源/一致性门 |
| 15 | `run --yes` 提交后不写收据（`submit --wait` 会写） |

逐个补会一直漏。这里改成**结构性检查**：用 AST 扫描 CLI，
凡是可能触达 Kaggle 提交的路径，都必须包含同一组关键调用。
"""
import ast
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import cli

results = []


def calls_in(fn) -> set[str]:
    """收集函数体内所有被调用的名字（含属性调用的末段）。"""
    tree = ast.parse(inspect.getsource(fn))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


print("=== 1) 所有提交路径必须过公共门 ===")
# 找出所有会调用 skillbridge.submit 的函数
submit_paths = []
for name, obj in vars(cli).items():
    if not callable(obj) or not inspect.isfunction(obj):
        continue
    try:
        cs = calls_in(obj)
    except Exception:
        continue
    if "submit" in cs and "skillbridge" in inspect.getsource(obj):
        submit_paths.append((name, cs))

print(f"   触达提交的函数: {[n for n, _ in submit_paths]}")
all_gated = all("_pre_submit_gate" in cs for _, cs in submit_paths)
for n, cs in submit_paths:
    print(f"     {n:16s} 过公共门={'_pre_submit_gate' in cs}")
print(f"   → 全部过门: {all_gated}")
results.append(("所有提交路径过门", all_gated))

print("\n=== 2) 提交成功后必须能写收据 ===")
# 有 --wait 的路径必须能调 wait_for_score + _write_receipt
for n, cs in submit_paths:
    has_wait = "wait_for_score" in cs
    has_receipt = "_write_receipt" in cs
    print(f"     {n:16s} 轮询出分={has_wait} 写收据={has_receipt}")
    results.append((f"{n} 可写收据", has_wait and has_receipt))

print("\n=== 3) write-sub 必须支持融合来源 ===")
src = inspect.getsource(cli)
src_iter = Path("kauto/iter.py").read_text(encoding="utf-8")
uses_blend = "blend_test_pred" in src_iter
print(f"   submission_from_best 读 blend_test_pred: {uses_blend}")
results.append(("write-sub 支持融合", uses_blend))

print("\n=== 4) 融合池必须包含归档 ===")
src_blend = Path("kauto/blend.py").read_text(encoding="utf-8")
has_archive = 'work.glob("archive/*")' in src_blend
print(f"   collect_candidates 扫 archive: {has_archive}")
results.append(("融合池含归档", has_archive))

print("\n=== 5) run 全链路步骤完整性 ===")
src_run = inspect.getsource(cli.cmd_run)
steps_needed = {
    "prepare": 'step("prepare"' in src_run,
    "baseline": 'step("baseline"' in src_run,
    "iterate": 'step("iterate"' in src_run,
    "blend": 'step("blend"' in src_run,
    "write-sub": 'step("write-sub"' in src_run,
    # validate 是通过 steps.append({"step": "validate", ...}) 记录的，
    # 没有走 step() 包装（它不抛异常，只是判 passed）。判据要按实际写法。
    "validate": ('"step": "validate"' in src_run),
    "pre-submit-gate": "pre-submit-gate" in src_run,
}
for k, v in steps_needed.items():
    print(f"   {k:16s} {v}")
all_steps = all(steps_needed.values())
print(f"   → 全链路步骤齐备: {all_steps}")
results.append(("全链路步骤齐备", all_steps))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
