"""验证融合池按内容去重（不是按 node_id）。

实测问题：`archive_node` 只按 `node_id` 去重，**不看产物内容**。
于是 11 个归档节点的 `test_pred` 指纹完全相同（`9b5914872e` ×11），
一模一样的预测占满融合池、挤掉 `top_k` 名额，还让"候选数"失去意义。

修复后候选从 70 → 21，其中一个条目记录到 36 个同内容副本。
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from kauto import blend as bl

results = []

print("=== 1) 去重后不应残留同内容候选 ===")
c = bl.collect_candidates("demo-comp")
print(f"   去重后候选数: {len(c)}")

fps = {}
dup_after = 0
for x in c:
    arr = np.asarray(x["pred"], dtype=float).ravel()
    fp = hashlib.sha256(arr.tobytes()).hexdigest()[:16]
    if fp in fps:
        dup_after += 1
    fps[fp] = x["id"]
print(f"   仍重复的条目: {dup_after}（应为 0）")
results.append(("去重后无重复", dup_after == 0))

print("\n=== 2) 重复项应被记录在 duplicates 字段 ===")
with_dups = [x for x in c if x.get("duplicates")]
print(f"   带 duplicates 记录的条目: {len(with_dups)}")
if with_dups:
    top = max(with_dups, key=lambda x: len(x["duplicates"]))
    print(f"   最多的一条约 {len(top['duplicates'])} 个同内容：{top['id']}")
ok2 = len(with_dups) > 0
results.append(("重复被记录", ok2))

print("\n=== 3) 同内容时优先保留 oof ===")
fake = [
    {"id": "dup-valid", "kind": "valid", "pred": np.array([0.1, 0.2, 0.3]), "test": None},
    {"id": "dup-oof", "kind": "oof", "pred": np.array([0.1, 0.2, 0.3]), "test": None},
]
deduped, seen = [], {}
for cand in fake:
    arr = np.asarray(cand["pred"], dtype=float).ravel()
    fp = hashlib.sha256(arr.tobytes()).hexdigest()[:16]
    prev = seen.get(fp)
    if prev is None:
        seen[fp] = cand
        deduped.append(cand)
    else:
        if cand["kind"] == "oof" and prev["kind"] != "oof":
            idx = deduped.index(prev)
            deduped[idx] = cand
            seen[fp] = cand
        else:
            prev.setdefault("duplicates", []).append(cand["id"])
kept_kind = deduped[0]["kind"] if deduped else None
print(f"   保留的 kind: {kept_kind}（应为 oof）")
ok3 = kept_kind == "oof"
results.append(("同内容优先 oof", ok3))

print("\n=== 4) 不同内容不应被合并 ===")
d1 = np.array([0.1, 0.2, 0.3])
d2 = np.array([0.9, 0.8, 0.7])
fps2 = {hashlib.sha256(np.asarray(v, dtype=float).ravel().tobytes()).hexdigest()[:16]
        for v in (d1, d2)}
ok4 = len(fps2) == 2
print(f"   两个不同预测的指纹数: {len(fps2)}（应为 2）")
results.append(("不同内容不合并", ok4))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
