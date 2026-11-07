"""方案树：把每个候选方案记成一个节点，维护父子关系与择优。

为什么需要这个而不是"每轮丢弃重来"：
现成的强基线 AIDE（arXiv 2502.13138，在 OpenAI MLE-Bench 的 75 个 Kaggle 赛上
拿到比最强线性 agent 多 4 倍的奖牌）的核心不是更聪明的 prompt，而是**搜索结构**：

- 每个方案是一个节点，`_improve(parent)` 会把**父节点的完整代码**喂给模型，
  要求"做一处原子改进"，于是每轮都在上一轮成果之上加东西，而不是从零重写。
- 失败的方案不丢弃：`_debug(parent)` 把**父代码 + 执行输出（报错）**喂回去修，
  `debug_depth` 有上限，防止在一个坑里无限调。
- Memory 是结构化的 `plan + analysis + metric`，不是流水账。

本模块只负责这套结构，不碰模型调用。对应 AIDE 的 journal.py / Node。
"""
from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import storage


@dataclass
class Node:
    """一个方案。code 可直接执行；parent 为空即初始草稿。"""

    id: str
    code: str
    plan: str = ""
    parent: str | None = None
    stage: str = "draft"          # draft | improve | debug
    score: float | None = None
    error: str = ""
    analysis: str = ""
    round_no: int = 0
    seconds: float = 0.0
    accepted: bool = False
    leak: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def is_buggy(self) -> bool:
        """跑不起来、评不出分、或分数是 NaN，都算 buggy。

        NaN 必须算 buggy —— 这是实测出来的坑：`float('nan')` 参与比较时
        任何 `>` 都返回 False，于是 `max()` 会返回**最先出现**的那个节点。
        后果是某个方案一旦给出 NaN（例如全列被清洗成 nan，rmse 直接算出 nan），
        它就永久占据"最优"位置，搜索再也无法前进，而日志里看起来一切正常。
        AIDE 用 `MetricValue` 把 NaN 折成 worst 哨兵值，这里用同样的语义。
        """
        if self.score is None or bool(self.error):
            return True
        try:
            return math.isnan(float(self.score))
        except (TypeError, ValueError):
            return True

    @property
    def is_leaf(self) -> bool:
        return True  # 由 Journal 在 append 时维护，节点自身不知道子节点

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("created_at", None)
        return d


class Journal:
    """方案集合。对应 AIDE 的 Journal。"""

    def __init__(self, slug: str, work: Path):
        self.slug = slug
        self.work = Path(work)
        self.nodes: list[Node] = []
        self.children: dict[str, list[str]] = {}
        self.metric_maximize: bool | None = None

    # ------------------------------------------------------------ 增
    def new_node(self, code: str, *, parent: Node | None = None, stage: str = "draft",
                 plan: str = "", round_no: int = 0) -> Node:
        return Node(id=uuid.uuid4().hex[:12], code=code,
                    parent=parent.id if parent else None,
                    stage=stage, plan=plan, round_no=round_no)

    def append(self, node: Node) -> None:
        self.nodes.append(node)
        if node.parent:
            self.children.setdefault(node.parent, []).append(node.id)

    def leaf_nodes(self) -> list[Node]:
        """没有子节点的节点 —— 它们才值得继续改进或调试。"""
        parents = set(self.children.keys())
        return [n for n in self.nodes if n.id not in parents]

    # ------------------------------------------------------------ 查
    @property
    def draft_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.parent is None]

    @property
    def buggy_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.is_buggy]

    @property
    def good_nodes(self) -> list[Node]:
        return [n for n in self.nodes if not n.is_buggy]

    def _rank(self, n: Node) -> tuple[int, float]:
        """可比较的排序键：不可用的一律垫底，其余按指标方向。

        三重防护缺一不可：
        - score 为 None / NaN → 排序键 0（最差）
        - `metric_maximize` 未确定时按"越大越好"兜底，避免 -v 变成 NaN
        - 返回值保证是有限 float，杜绝 NaN 参与 max() 比较
        """
        if n.score is None or n.is_buggy:
            return (0, 0.0)
        try:
            v = float(n.score)
        except (TypeError, ValueError):
            return (0, 0.0)
        if not math.isfinite(v):
            return (0, 0.0)
        maximize = True if self.metric_maximize is None else self.metric_maximize
        return (1, v if maximize else -v)

    def get_best_node(self, only_good: bool = True) -> Node | None:
        pool = self.good_nodes if only_good else self.nodes
        if not pool:
            return None
        return max(pool, key=self._rank)

    def debug_depth(self, node: Node) -> int:
        """连续调试了几代。上限用来防止在一个坑里无限调。"""
        depth, cur = 0, node
        while cur.stage == "debug" and cur.parent:
            depth += 1
            nxt = self.by_id(cur.parent)
            if nxt is None:
                break
            cur = nxt
        return depth

    def by_id(self, nid: str) -> Node | None:
        for n in self.nodes:
            if n.id == nid:
                return n
        return None

    # ------------------------------------------------------------ Memory
    def summary(self, include_code: bool = False, code_limit: int = 2500,
                max_nodes: int = 6) -> str:
        """喂回 prompt 的"试过什么"。

        只取 good nodes，且按分数从好到次 —— 对应 AIDE 的 generate_summary，
        但截断代码长度：推理模型上下文宝贵，全量代码会把真正有用的信息挤掉。
        """
        good = sorted(self.good_nodes, key=self._rank, reverse=True)[:max_nodes]
        if not good:
            return "（还没有成功的方案）"
        parts = []
        for n in good:
            seg = [f"方案#{n.round_no}（{n.stage}, CV={n.score:.6f}）"]
            if n.plan:
                seg.append(f"  思路：{n.plan}")
            if n.analysis:
                seg.append(f"  结论：{n.analysis}")
            if include_code:
                code = n.code
                if len(code) > code_limit:
                    code = code[:code_limit] + "\n…（已截断）"
                seg.append(f"  代码：\n{code}")
            parts.append("\n".join(seg))
        return "\n".join(parts)

    def failures(self, max_nodes: int = 4) -> str:
        """失败方案只留"错在哪"，不留代码 —— 它的价值是避免重犯。"""
        bad = sorted(self.buggy_nodes, key=lambda n: n.round_no, reverse=True)[:max_nodes]
        if not bad:
            return ""
        out = []
        for n in bad:
            err = (n.error or "").strip().splitlines()
            head = err[-1] if err else "未知错误"
            out.append(f"方案#{n.round_no}（{n.stage}）失败：{head[:220]}")
        return "\n".join(out)

    # ------------------------------------------------------------ 落盘
    def _node_payload(self, n: "Node") -> dict:
        """序列化节点；已归档且有同源代码时只留引用，避免 journal 膨胀。"""
        d = n.to_dict()
        code = (n.code or "").strip()
        if code:
            try:
                cand = self.work / "archive" / n.id / "candidate.py"
                if cand.exists() and cand.read_text(encoding="utf-8").strip() == code:
                    d["code"] = ""
                    d["code_in_archive"] = True
            except Exception:
                pass
        return d

    def save(self) -> Path:
        path = self.work / "journal.json"
        # ── 已归档节点的代码不重复写进 journal ──────────────────────────────
        #
        # 实测：每个节点的 code 约 6.6 KB，titanic 的 4 个节点让 journal 到 25.8 KB，
        # 其中 21.9 KB 是代码。按 100 节点算会到 646 KB，且**每次 save 全量重写**。
        #
        # 而跑通的节点已经把代码存到 `archive/<id>/candidate.py`（实测逐字相同）。
        # 所以 journal 里对这类节点只留 `code_in_archive: true`，不再存一份正文。
        #
        # 例外：**失败节点不归档**（只留在 journal 里），它们的代码必须保留 ——
        # 否则 debug 时拿不到父节点代码，`one_child` 的"重复代码检测"也会失效。
        payload = {
            "slug": self.slug,
            "metric_maximize": self.metric_maximize,
            "nodes": [self._node_payload(n) for n in self.nodes],
            "children": self.children,
        }
        storage.write_json(path, payload)
        return path

    def load(self) -> bool:
        path = self.work / "journal.json"
        if not path.exists():
            return False
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        self.metric_maximize = d.get("metric_maximize")
        self.children = d.get("children") or {}
        self.nodes = []
        for raw in d.get("nodes") or []:
            # `code_in_archive` 不是 Node 字段 —— 先取出来再从 raw 里剔除。
            in_archive = bool(raw.get("code_in_archive"))
            raw = {k: v for k, v in raw.items()
                   if k in Node.__dataclass_fields__}
            node = Node(**raw)
            # 保存时代码留了引用，读回时补上正文 —— 否则 debug 拿不到父节点代码，
            # `one_child` 的"重复代码检测"也会失效（父代码为空则永远判"有修改"）。
            if in_archive and not node.code:
                try:
                    cand = self.work / "archive" / node.id / "candidate.py"
                    if cand.exists():
                        node.code = cand.read_text(encoding="utf-8")
                except Exception:
                    pass
            self.nodes.append(node)
        return bool(self.nodes)
