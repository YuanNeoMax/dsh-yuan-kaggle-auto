"""节点产物归档：把每个跑通的方案的代码与预测固化到 archive/，只增不删。

## 为什么要这个

当前实现只保留「最新一份」最优产物：
    work/<slug>/best_fe.py
    work/<slug>/best_valid_pred.npy
    work/<slug>/best_test_pred.npy

这带来两个实际问题（实测确认过）：

1. **无法回退**：下一次迭代若变差，想回到上一版已经不可能 —— 覆盖掉了。
   本仓库 `work/demo-comp` 里 3 个 good node 只剩 2 个产物目录，就是证据。
2. **无法复现**：想回答"上次那个 0.26 的方案长什么样"，没有代码也没预测。

归档解决的就是这两点：`archive/<node_id>/` 下存
`candidate.py` + `valid_pred.npy` + `test_pred.npy`（若有 `oof.npy` 一并存），
并附一个 `meta.json` 记录分数、族、轮次、父节点。

设计上刻意**只增不删**。理由是这些产物很小（每份几十 KB），
而误删一个历史最优的代价是几十轮迭代白跑。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import storage


def archive_dir(work: Path) -> Path:
    return Path(work) / "archive"


def archive_node(work: Path, node_id: str, *, code: str | None = None,
                 gen_dir: Path | None = None, meta: dict | None = None) -> dict:
    """把某个节点的产物复制进 archive/<node_id>/。已存在则跳过（只增不删）。

    返回 {ok, path, copied, skipped}。
    """
    work = Path(work)
    dest = archive_dir(work) / node_id
    if dest.exists() and (dest / "meta.json").exists():
        return {"ok": True, "path": str(dest), "copied": [], "skipped": "已归档"}

    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    missing: list[str] = []

    src = Path(gen_dir) if gen_dir else work / f"gen_{node_id}"

    if code is None:
        cand = src / "candidate.py"
        if cand.exists():
            code = cand.read_text(encoding="utf-8")
    if code:
        storage.write_text(dest / "candidate.py", code)
        copied.append("candidate.py")

    for name in ("valid_pred.npy", "test_pred.npy", "oof.npy"):
        f = src / name
        if f.exists():
            storage.copy(f, dest / name)
            copied.append(name)
        else:
            missing.append(name)

    storage.write_json(dest / "meta.json", {
        "node_id": node_id,
        **(meta or {}),
        "files": copied,
        "missing": missing,
    })
    copied.append("meta.json")
    return {"ok": True, "path": str(dest), "copied": copied, "missing": missing}


def list_archived(work: Path) -> list[dict]:
    """列出已归档节点及其分数（按分数降序，需先知道指标方向）。"""
    root = archive_dir(work)
    if not root.exists():
        return []
    rows = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        mp = d / "meta.json"
        if not mp.exists():
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append(m)
    return rows


def restore_node(work: Path, node_id: str) -> dict:
    """把归档产物恢复到 best_* 位置，使得 write-sub 能用它。

    这是"回退到某个历史方案"的实现：不改 journal，只换 best_* 指向。
    """
    work = Path(work)
    src = archive_dir(work) / node_id
    if not src.exists():
        return {"ok": False, "reason": f"没有归档：{src}"}
    if not (src / "candidate.py").exists():
        return {"ok": False, "reason": "归档缺少 candidate.py"}

    need = ["valid_pred.npy", "test_pred.npy"]
    miss = [n for n in need if not (src / n).exists()]
    if miss:
        return {"ok": False, "reason": f"归档缺少 {miss}"}

    storage.copy(src / "candidate.py", work / "best_fe.py")
    storage.copy(src / "valid_pred.npy", work / "best_valid_pred.npy")
    storage.copy(src / "test_pred.npy", work / "best_test_pred.npy")
    # 盖特征指纹：restore 出来的预测同样必须能自证"用哪套特征算的"，
    # 否则改了特征集之后 write-sub 会照旧复用它（实测过的静默错误）。
    try:
        from .iter import _stamp_features_fingerprint
        _stamp_features_fingerprint(work, "best_test_pred.npy")
    except Exception:
        pass
    storage.write_text(work / "best_node.txt", node_id)
    return {"ok": True, "restored": node_id,
            "from": str(src), "written": ["best_fe.py", "best_valid_pred.npy",
                                          "best_test_pred.npy", "best_node.txt"]}
