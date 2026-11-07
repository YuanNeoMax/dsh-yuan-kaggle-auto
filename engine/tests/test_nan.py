"""复现 NaN 污染择优的缺陷。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kauto.journal import Journal, Node

j = Journal("t", Path("."))
j.metric_maximize = True
j.append(Node(id="nan", code="a", score=float("nan"), stage="improve", round_no=1))
j.append(Node(id="good", code="b", score=0.90, stage="improve", round_no=2))

b = j.get_best_node()
print("nodes:", len(j.nodes))
print("best id:", b.id, "| best score:", b.score)
print("nan node is_buggy:", j.by_id("nan").is_buggy)
print("good_nodes:", [n.id for n in j.good_nodes])
print("buggy_nodes:", [n.id for n in j.buggy_nodes])
print()
print("期望：nan 应当被当作 worst（buggy），best 应为 good")
