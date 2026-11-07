"""Journal 自检：树结构、择优、调试深度、Memory 生成。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kauto.journal import Journal, Node

# 用临时目录而不是 Path(".")：早先直接写当前目录，
# 会在仓库根留下 journal.json（测试污染，实测踩到）。
_TMP = Path(tempfile.mkdtemp(prefix="kauto_journal_"))

j = Journal("demo", _TMP)
j.metric_maximize = True

# 基线节点
j.append(Node(id="baseline", code="", score=0.90, stage="draft", round_no=0))

# 两个改进子节点
n1 = Node(id="n1", code="code1", parent="baseline", score=0.93, stage="improve", round_no=1,
          plan="加特征交叉")
n2 = Node(id="n2", code="code2", parent="n1", score=0.95, stage="improve", round_no=2,
          plan="换模型族")
j.append(n1)
j.append(n2)

# 一个失败节点 + 一次调试 + 再失败
bad = Node(id="bad", code="badcode", parent="n2", stage="improve",
           error="KeyError: 'x9'\nTraceback ...")
j.append(bad)
d1 = Node(id="d1", code="fix1", parent="bad", stage="debug", error="ValueError: nan")
j.append(d1)

print("nodes:", len(j.nodes))
print("good:", len(j.good_nodes), "| buggy:", len(j.buggy_nodes))
print("draft:", len(j.draft_nodes))
print("best:", j.get_best_node().id, j.get_best_node().score)
print("debug_depth(d1):", j.debug_depth(d1))
print("debug_depth(n2):", j.debug_depth(n2))
print("leaf nodes:", [n.id for n in j.leaf_nodes()])
print()
print("=== Memory 摘要 ===")
print(j.summary())
print()
print("=== 失败清单 ===")
print(j.failures())
print()
# 落盘/回读
p = j.save()
print("saved:", p)
j2 = Journal("demo", _TMP)
print("reload ok:", j2.load(), "| nodes:", len(j2.nodes))
print("reload best:", j2.get_best_node().id)
