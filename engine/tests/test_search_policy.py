"""验证 search_policy 的三条分支：draft / debug / improve。

不跑 LLM，直接构造 journal 状态，检查选动作的决策是否符合 AIDE 语义：
- 有 buggy 叶子且调试深度未超限 → 应优先去 debug
- 调试深度超限 → 不能再 debug，回到 improve
- 没有可用父节点（只剩空代码的 baseline）→ draft
"""
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kauto.journal import Journal, Node

_TMP = Path(tempfile.mkdtemp(prefix="kauto_sp"))


def make_journal() -> Journal:
    j = Journal("t", _TMP)
    j.metric_maximize = True
    j.append(Node(id="baseline", code="", score=0.90, stage="draft", round_no=0))
    n1 = Node(id="n1", code="c1", parent="baseline", score=0.93, stage="improve", round_no=1)
    j.append(n1)
    return j


# ---- 分支 1：只剩 baseline（无可用父节点）→ draft ----
j = make_journal()
best = j.get_best_node()
print("分支1 无可用父节点时 best.code 为空:", not best.code, "→ 期望 draft")

# ---- 分支 2：有 buggy 叶子 → 优先 debug ----
j = make_journal()
bad = Node(id="bad", code="bad", parent="n1", stage="improve",
           error="KeyError: 'x9'", score=None, round_no=2)
j.append(bad)
buggy_leaves = [n for n in j.buggy_nodes
                if j.debug_depth(n) < 3 and n.id not in j.children]
print("分支2 可调试节点:", [n.id for n in buggy_leaves], "→ 期望 debug bad")

# ---- 分支 3：调试深度达上限 → 不再 debug ----
j = make_journal()
bad = Node(id="bad", code="bad", parent="n1", stage="improve", error="e1", round_no=2)
j.append(bad)
d1 = Node(id="d1", code="d1", parent="bad", stage="debug", error="e2", round_no=3)
j.append(d1)
d2 = Node(id="d2", code="d2", parent="d1", stage="debug", error="e3", round_no=4)
j.append(d2)
d3 = Node(id="d3", code="d3", parent="d2", stage="debug", error="e4", round_no=5)
j.append(d3)
print("分支3 debug_depth(d3) =", j.debug_depth(d3), "(上限 3) → 期望超限，不再 debug")
still = [n for n in j.buggy_nodes if j.debug_depth(n) < 3 and n.id not in j.children]
print("        仍可调试:", [n.id for n in still], "→ 期望空")

# ---- 分支 4：好消息不覆盖 —— good node 分数更高时仍取最优 ----
j = make_journal()
n2 = Node(id="n2", code="c2", parent="n1", score=0.96, stage="improve", round_no=2)
j.append(n2)
print("分支4 best:", j.get_best_node().id, "score:", j.get_best_node().score,
      "→ 期望 n2/0.96")

# ---- 分支 5：指标方向（越小越好） ----
j = Journal("t", _TMP)
j.metric_maximize = False
j.append(Node(id="a", code="a", score=0.5, stage="draft", round_no=1))
j.append(Node(id="b", code="b", score=0.3, stage="draft", round_no=2))
print("分支5 越小越好时 best:", j.get_best_node().id, "→ 期望 b/0.3")
