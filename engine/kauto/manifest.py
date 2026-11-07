"""运行清单：记录一次运行的全部可复现要素。

## 为什么需要

`baseline.json` / `iterate.json` / `blend.json` 各自只记自己的结果，
没有一个统一的地方能回答「这次跑分，是在什么环境、什么数据切分、什么版本下得到的」。

更要紧的是**跨会话复现**：隔着几天回头想重跑一次同分，
需要知道种子、折分配、特征列表、包版本。少一样就可能复现不出来，
而你又分不清是"代码变了"还是"环境变了"。

清单不做执行，只做记录 —— 采集失败一律降级成字段缺失，绝不因为
某个信息取不到就让整条流水线失败。
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import storage


def _pkg_versions() -> dict:
    """采集关键包版本。

    注意 `scikit-learn` 的 import 名是 `sklearn`，
    直接用包名 import 会失败并静默记成 None（实测踩到）。
    所以显式给出 import 名映射，而不是靠字符串替换。
    """
    IMPORT_NAMES = {
        "pandas": "pandas",
        "numpy": "numpy",
        "scikit-learn": "sklearn",
        "lightgbm": "lightgbm",
        "scipy": "scipy",
    }
    out = {}
    for name, mod_name in IMPORT_NAMES.items():
        try:
            mod = __import__(mod_name, fromlist=["__version__"])
            out[name] = getattr(mod, "__version__", "?")
        except Exception:
            out[name] = None
    return out


def _code_fingerprint() -> str | None:
    """对 kauto 包的关键源码做指纹。

    ## 为什么不是只靠 git

    实测：本项目**不是 git 仓库**（`git rev-parse` 返回 128），
    于是 manifest 里的 `git_commit` 永远是 null —— 代码版本无法追溯。
    而"能不能复现"恰恰需要知道"当时用的是哪版代码"。

    指纹取所有 .py 文件的内容 hash（排除 __pycache__），
    任何文件改动都会让它变。它比 git commit 粗糙，但在无 git 时是唯一选项。
    两边都记：有 git 时给 commit，无 git 时给指纹。
    """
    import hashlib
    try:
        root = Path(__file__).resolve().parent
        h = hashlib.sha256()
        for f in sorted(root.glob("*.py")):
            if f.name == "__init__.py":
                continue
            h.update(f.name.encode())
            h.update(f.read_bytes())
        return h.hexdigest()[:16]
    except Exception:
        return None


def _git_commit(cwd: Path) -> str | None:
    try:
        p = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=str(cwd), capture_output=True, text=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None


def _file_digest(path: Path) -> str | None:
    """数据文件指纹：用来判断"两次跑的是不是同一份数据"。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return None


def write_manifest(work: Path, *, slug: str, stage: str, extra: dict | None = None) -> Path:
    """把当前运行的可复现要素写成 run_manifest.json（覆盖写，保留最近一次）。

    每个阶段（baseline / iterate / blend）调用时都会更新这个文件，
    因此它总是反映"最近一次跑的状态"。
    """
    work = Path(work)
    meta_path = work / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    # 实际使用的指标在 baseline.json 里（meta 里的 metric_declared 在本地推断时为 null）。
    # 清单的价值就在于"这次是按什么指标算的分"，所以优先读 baseline 的结果。
    effective_metric = None
    bl = work / "baseline.json"
    if bl.exists():
        try:
            effective_metric = json.loads(bl.read_text(encoding="utf-8")).get("metric")
        except (OSError, json.JSONDecodeError):
            effective_metric = None

    data_files = {}
    for name in ("train.csv", "valid.csv", "test.csv"):
        f = work / name
        if f.exists():
            data_files[name] = {"bytes": f.stat().st_size, "sha256_16": _file_digest(f)}

    manifest = {
        "slug": slug,
        "stage": stage,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": _pkg_versions(),
        "git_commit": _git_commit(Path(__file__).resolve().parents[1]),
        # 无 git 时的替代：代码指纹。两者都记，复现时至少有一个可用。
        "code_fingerprint": _code_fingerprint(),
        "data": data_files,
        "split": meta.get("split_strategy"),
        "seed": meta.get("seed"),
        "n_train_rows": meta.get("n_train_rows"),
        "n_valid_rows": meta.get("n_valid_rows"),
        "metric": effective_metric or meta.get("metric_declared"),
        "metric_source": meta.get("metric_source"),
        "task": meta.get("task"),
        "kind": meta.get("kind"),
        "id_col": meta.get("id_col"),
        "target": meta.get("target"),
        **(extra or {}),
    }
    path = work / "run_manifest.json"
    storage.write_json(path, manifest)
    return path
