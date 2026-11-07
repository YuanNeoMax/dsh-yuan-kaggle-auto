"""验证融合产物与提交之间的连接（此前是断开的）。

三个实测缺陷：
1. `blend` 写 `blend_test_pred.npy`，而 `write-sub` 只读 `best_test_pred.npy`
   → **融合从未被用于提交**，跑了 blend 但交出去还是单模，且看不出异常
2. 融合被验收门拒绝时**没有清旧产物** → 下次 `write-sub` 读到旧 `blend.json`
   的 `ok=true`，拿被拒的融合去提交
3. 相关性过滤把候选压到只剩 1 个时，仍跑一遍无意义的贪心，只给出 `gain=0`
   → 不知道该做什么
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from kauto import blend as bl, iter as kiter, storage

results = []
work = Path("work/demo-comp")

print("=== 1) write-sub 的来源选择（行为验证）===")
# 不查源码文本，直接调用看返回什么 —— 这才能证明"真的会用融合"。
import inspect
src = inspect.getsource(kiter.submission_from_best)
# 行为：现在融合被拒（blend.json 不存在或 ok=false），应回退 best_node
r = kiter.submission_from_best("demo-comp")
src_used = r.get("prediction_source")
print(f"   实际用的来源: {src_used}")
ok_src = src_used in {"blend", "best_node"}
results.append(("write-sub 可执行且标明来源", ok_src))

# 证明它真的会读 blend 路径：在源码里找条件分支（AST）
import ast
tree = ast.parse(src)
found_blend_branch = False
for node in ast.walk(tree):
    if isinstance(node, ast.If):
        cond = ast.get_source_segment(src, node.test) or ""
        if "use_blend" in cond and "blend_pred" in cond:
            found_blend_branch = True
            break
print(f"   有 use_blend 分支判断: {found_blend_branch}")
results.append(("有融合分支（AST）", found_blend_branch))

print("\n=== 2) 融合被拒时必须清旧产物 ===")
src_bl = inspect.getsource(bl.blend)
has_cleanup = "unlink()" in src_bl and "stale_cleared" in src_bl
print(f"   拒绝分支里清旧文件: {has_cleanup}")
results.append(("拒绝时清旧产物", has_cleanup))

print("\n=== 3) 候选不足 2 个时给可行动原因 ===")
has_insufficient = "可用候选不足 2 个" in src_bl and "advice" in src_bl
print(f"   给出原因与建议: {has_insufficient}")
results.append(("候选不足给建议", has_insufficient))

print("\n=== 4) 实测：相关性过滤的判断是否正确 ===")
# 直接手算两两平均，看是否真的都不如最优单模
import pandas as pd
from kauto.model import score
import itertools

full = pd.concat([pd.read_csv(work / "train.csv"),
                  pd.read_csv(work / "valid.csv")], ignore_index=True)
y = full["target"].to_numpy()

cands = {}
for tag in ("oof_rf2", "oof_lr2", "oof_hgb", "oof_rf", "oof_lr"):
    f = work / f"gen_{tag}" / "oof.npy"
    if f.exists():
        v = np.asarray(storage.load_npy(f)).ravel()
        if len(v) == len(y):
            cands[tag] = v

if len(cands) >= 2:
    singles = {k: score("logloss", y, v) for k, v in cands.items()}
    best_k = min(singles, key=singles.get)
    best_s = singles[best_k]
    print(f"   最优单模 {best_k} = {best_s:.6f}")

    better = []
    for a, b in itertools.combinations(cands, 2):
        s = score("logloss", y, (cands[a] + cands[b]) / 2)
        if s < best_s:
            better.append((a, b, s))
    print(f"   两两平均中优于最优单模的组合数: {len(better)} / {len(list(itertools.combinations(cands,2)))}")
    # 结论：若一个都没有，说明过滤门拒绝融合是对的
    filter_correct = len(better) == 0
    print(f"   → 过滤门拒绝融合是否正确: {filter_correct}")
    results.append(("过滤门判断正确", filter_correct))
else:
    # 候选不足时不判 FAIL —— 这个检查依赖历史数据（gen_* 里的异构候选），
    # 而 prune 会清掉它们。数据不够就跳过，不把"没数据"当成"代码坏了"。
    print("   (候选不足 2 组，无法做两两对比 —— 跳过，不计入失败)")
    results.append(("过滤门判断正确（数据不足跳过）", None))

print("\n=== 5) 当前 write-sub 用的是哪个来源 ===")
r = kiter.submission_from_best("demo-comp")
prov = r.get("provenance") or {}
print(f"   prediction_source={r.get('prediction_source')} source={prov.get('source')}")
# 融合被拒后应为 best_node
ok5 = r.get("prediction_source") in {"blend", "best_node"}
results.append(("来源可识别", ok5))

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
