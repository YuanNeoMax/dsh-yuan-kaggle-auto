"""验证产物归档与回退的等价性 —— 回退后的产出必须与归档时一致。

这是 B 阶段的核心验收：不能只看「restore 返回 ok」，要证明
回退后再生成 submission，数值与归档节点的预测完全一致。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from kauto import archive as arc, prep, storage

slug = "demo-comp"
meta = prep.load_work(slug)
work = Path(meta["work_dir"])

rows = arc.list_archived(work)
print("=== 归档清单 ===")
for r in rows:
    print(f"  {r['node_id'][:14]:16s} score={r.get('score')} family={r.get('family')} "
          f"stage={r.get('stage')} files={len(r.get('files') or [])}")

# 挑一个有完整产物的节点
cand = [r for r in rows if not (r.get("missing") or []) or
        "test_pred.npy" not in (r.get("missing") or [])]
if not cand:
    print("\n没有可用于回退的完整节点")
    raise SystemExit(1)
node = cand[0]["node_id"]
a = work / "archive" / node
print(f"\n=== 回退验证：{node} ===")

before_test = storage.load_npy(a / "test_pred.npy")
before_valid = storage.load_npy(a / "valid_pred.npy")
before_code = (a / "candidate.py").read_text(encoding="utf-8")

r = arc.restore_node(work, node)
print(" restore:", r.get("ok"), "|", r.get("restored"))

after_test = storage.load_npy(work / "best_test_pred.npy")
after_valid = storage.load_npy(work / "best_valid_pred.npy")
after_code = (work / "best_fe.py").read_text(encoding="utf-8")

print(" test_pred 一致 :", np.allclose(before_test, after_test, equal_nan=True))
print(" valid_pred 一致:", np.allclose(before_valid, after_valid, equal_nan=True))
print(" 代码一致       :", before_code == after_code)
print(" best_node.txt  :", (work / "best_node.txt").read_text(encoding="utf-8").strip())

# 再做一次「归档只增不删」的验证：重复归档应跳过
again = arc.archive_node(work, node, code=after_code)
print(" 重复归档被跳过 :", bool(again.get("skipped")))
