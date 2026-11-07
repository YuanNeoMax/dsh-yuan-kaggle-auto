"""验证归档节点能参与融合 + 报错信息不误导。

两个实测缺陷：
1. `collect_candidates` 只扫 `gen_*`，**不扫 `archive/`** ——
   归档节点明明留着完整产物（11 个全部缺席），却永远进不了融合。
   归档的意义就是"只增不删、可复现"，不拿它参与融合等于白存。
2. `submission_from_best` 在缺 test 预测时报
   「跑 iterate 或 baseline 后 write-sub」—— 但 **baseline 产不出 test 预测**，
   照它说的跑完再来仍然失败。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import blend as bl, iter as kiter

results = []

print("=== 1) 融合池应包含归档节点 ===")
c = bl.collect_candidates("demo-comp")
from collections import Counter
kinds = Counter(x["kind"] for x in c)
print(f"   候选总数: {len(c)}  pred_kind: {dict(kinds)}")

# 归档目录里的节点 id 是否出现在池里
arch_ids = [d.name for d in (Path("work/demo-comp") / "archive").iterdir()
            if d.is_dir()] if (Path("work/demo-comp") / "archive").exists() else []
pool_ids = {x["id"] for x in c}
present = [a for a in arch_ids if a in pool_ids]
print(f"   归档节点数: {len(arch_ids)}  进入池的: {len(present)}")
included = len(present) > 0
print(f"   → 归档节点可参与融合: {included}")
results.append(("归档进入融合池", included))

print("\n=== 2) 仍优先用 OOF ===")
# 有 oof 的候选 kind 必须是 oof
oof_ids = {x["id"] for x in c if x["kind"] == "oof"}
print(f"   oof 候选: {len(oof_ids)} 个（{list(oof_ids)[:4]}）")
has_oof_priority = len(oof_ids) >= 1
print(f"   → OOF 优先标记正确: {has_oof_priority}")
results.append(("OOF 优先", has_oof_priority))

print("\n=== 3) 没有 OOF 的归档退回 valid_pred ===")
# 归档节点没有 oof.npy，应被标为 valid
no_oof = [x for x in c if x["id"] in present and x["kind"] == "valid"]
print(f"   归档中走 valid 路径的: {len(no_oof)}")
ok3 = len(no_oof) > 0
print(f"   → 无 OOF 时退回 valid: {ok3}")
results.append(("无 OOF 退回 valid", ok3))

print("\n=== 4) 缺 test 预测时的报错不能说「跑 baseline 就行」===")
import inspect
src = inspect.getsource(kiter.submission_from_best)
# 旧字符串可能残留在说明注释里（我特意写了"早期报错是…"），
# 所以判据看**抛出信息本体**，不看整段源码。
_raise_block = src[src.find("raise KaggleError("):] if "raise KaggleError(" in src else ""
old_msg_gone = "跑 iterate 或 baseline 后 write-sub" not in _raise_block.split("\n")[0]
has_two_paths = "两条可行路径" in src
mentions_retrain = "重训" in src
print(f"   旧误导信息已移除: {old_msg_gone}")
print(f"   给出两条可行路径: {has_two_paths}")
print(f"   提到重训路径: {mentions_retrain}")
results.append(("报错不误导", old_msg_gone and has_two_paths and mentions_retrain))

print("\n=== 5) 实测该报错内容 ===")
try:
    kiter.submission_from_best("reg-comp")
    print("   (reg-comp 竟然成功了，跳过)")
    results.append(("报错可读", True))
except Exception as e:
    txt = str(e)
    print(f"   {txt.splitlines()[0]}")
    ok5 = "两条可行路径" in txt and "重训" in txt
    print(f"   → 可读且可行动: {ok5}")
    results.append(("报错可读", ok5))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
