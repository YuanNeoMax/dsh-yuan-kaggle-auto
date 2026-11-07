"""数据概览：把"数据长什么样"压缩成一段可进 prompt 的文本。

## 为什么需要（对照 AIDE 的 data_preview.py）

现在 prompt 里只说「train 960 行 / 6 个特征」。LLM 在**不知道列名、dtype、
取值范围**的情况下写特征工程，只能靠猜 —— 它会写出 `df['x1'] * df['x2']`
这种名字对得上的侥幸情况，真实比赛里列名是 `OverallQual`、`Neighborhood`，
猜不中就整段作废。

AIDE 的做法是把每个 csv 的列名、样本值、大小写进 prompt。这里照做，
但加两个自己的取舍：

1. **控制长度**：推理模型上下文宝贵，概览按列数自适应截断。
2. **标注退化列**：把 build_matrix 已经判定为「编码退化」的列也写进来，
   让 LLM 知道"这列被压平了，你需要重新处理"。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def _fmt(v) -> str:
    """把样本值格式化成干净的字面量。

    直接 repr(numpy 标量) 会给出 `np.float64(0.36)` —— 这既长又会让 LLM
    误以为要写 numpy 类型注解。这里统一成普通数字/字符串字面量。
    """
    try:
        import numpy as np
        if isinstance(v, (np.floating,)):
            return f"{float(v):.4g}"
        if isinstance(v, (np.integer,)):
            return str(int(v))
        if isinstance(v, (np.bool_)):
            return str(bool(v))
    except Exception:
        pass
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, str):
        return repr(v[:24])
    return repr(v)[:24]


def _col_line(s: pd.Series, name: str, max_samples: int = 4,
              coerced: bool = False, cleaned_extreme: bool = False) -> str:
    """单列的一行描述：dtype / 缺失率 / 唯一值数 / 样本值。

    两个标注是实测补上的 —— 不加会误导 LLM：
    - `coerced`：这列原看像字符串（"1,200" / "12%"），会被解析成数值。
      不标出来 LLM 会以为要自己处理字符串，或干脆跳过这列。
    - `cleaned_extreme`：这列含被当异常置空的极端值。
      不标出来 LLM 只看到干净样本，不知道原始数据里有 1e9 这种量级。
    """
    n = len(s)
    miss = float(s.isna().mean()) if n else 0.0
    uniq = int(s.nunique(dropna=True))
    dtype = str(s.dtype)

    vals = s.dropna().unique()[:max_samples]
    shown = ", ".join(_fmt(v) for v in vals)
    if uniq > max_samples:
        shown += f", … 共 {uniq} 种"

    parts = [f"`{name}` ({dtype}{' → 按数值使用' if coerced else ''})"]
    if cleaned_extreme:
        parts.append("⚠ 含极端值（已按异常置空）")
    if miss > 0:
        parts.append(f"缺失 {miss:.1%}")
    parts.append(f"{uniq} 种取值")
    if shown:
        parts.append(f"样本: {shown}")
    return "  - " + " | ".join(parts)


def build(train: pd.DataFrame, valid: pd.DataFrame, test: pd.DataFrame,
          *, target: str, id_col: str, degraded: list[str] | None = None,
          numeric_from_text: list[str] | None = None,
          cleaned: list[str] | None = None,
          max_cols: int = 24) -> str:
    """生成数据概览文本。

    max_cols 按列数自适应：列很多时只挑「最有信息量」的（非 id、非常量）。

    `numeric_from_text` / `cleaned` 来自 `build_matrix` 的列体检报告：
    前者是「看着像字符串、实际按数值用」的列，后者是「含被置空极端值」的列。
    两者都必须标注，否则 LLM 会按错误的前提写代码（实测见 ROADMAP 第 30 轮）。
    """
    _coerced = set(numeric_from_text or [])
    _cleaned = set(cleaned or [])
    feat_cols = [c for c in train.columns if c not in {target, id_col}]
    # 常量列对建模无用，先剔掉，省下的额度留给有用的列
    useful = [c for c in feat_cols if train[c].nunique(dropna=True) > 1]
    dropped_const = len(feat_cols) - len(useful)

    shown = useful[:max_cols]
    lines = [
        f"训练集 {len(train)} 行 × {len(train.columns)} 列；验证集 {len(valid)} 行；"
        f"测试集 {len(test)} 行。",
        f"标签列 `{target}`，id 列 `{id_col}`。",
    ]
    if dropped_const:
        lines.append(f"（另有 {dropped_const} 个常量列已略去，它们无建模价值）")
    if len(useful) > max_cols:
        lines.append(f"（特征共 {len(useful)} 个，以下只列前 {max_cols} 个）")

    lines.append("")
    lines.append("特征列概览：")
    for c in shown:
        lines.append(_col_line(train[c], c,
                               coerced=(c in _coerced),
                               cleaned_extreme=(c in _cleaned)))

    # 训练/测试分布漂移：真实比赛的关键信号，AIDE 没做而这里做了。
    #
    # 判据必须用**标准化差异**而不是相对差：
    # 实测踩到 —— x1 均值 -0.049 vs -0.124，都在 0 附近，
    # 相对差算出 152% 触发误报；实际上这点差异完全在噪声范围内。
    # 正确做法：差异要同时超过「0.2 个标准差」和「相对 20%」才算漂移。
    drift = []
    for c in shown:
        if not (pd.api.types.is_numeric_dtype(train[c]) and pd.api.types.is_numeric_dtype(test[c])):
            continue
        a, b = float(train[c].mean()), float(test[c].mean())
        sd = float(train[c].std())
        if not (np.isfinite(a) and np.isfinite(b)):
            continue
        scale = sd if sd > 1e-9 else max(abs(a), 1e-9)
        gap = abs(b - a)
        rel = gap / max(abs(a), 1e-9)
        if gap > 0.2 * scale and rel > 0.2:
            drift.append(f"`{c}` 均值 train={a:.4g} vs test={b:.4g}"
                         f"（差 {gap/scale:.2f} 个标准差）")
    if drift:
        lines.append("")
        lines.append("训练/测试分布差异（注意这可能影响泛化）：")
        for d in drift[:6]:
            lines.append("  - " + d)

    if degraded:
        lines.append("")
        lines.append("⚠ 这些列在原实现里被压成了常数（编码退化），需要重新处理：")
        for c in degraded[:8]:
            lines.append(f"  - `{c}`")

    return "\n".join(lines)


def for_work(work: Path, meta: dict, degraded: list[str] | None = None) -> str:
    """从 work 目录的 csv 生成概览。读不到就返回空串，不阻塞迭代。

    列体检信息（哪些列"看着像字符串实际按数值用"、哪些含被置空的极端值）
    从 `baseline.json` 的 `columns` 里读 —— 那是 `build_matrix` 落盘的报告。
    读不到就不标注，不影响主流程。
    """
    numeric_from_text: list[str] = []
    cleaned: list[str] = []
    try:
        import json
        bl = Path(work) / "baseline.json"
        if bl.exists():
            cols_rep = json.loads(bl.read_text(encoding="utf-8")).get("columns") or {}
            numeric_from_text = list(cols_rep.get("numeric_from_text") or [])
            cleaned = list(cols_rep.get("nonfinite_cleaned") or [])
    except Exception:
        pass

    try:
        tr = pd.read_csv(Path(work) / "train.csv", low_memory=False, nrows=5000)
        va = pd.read_csv(Path(work) / "valid.csv", low_memory=False, nrows=5000)
        te = pd.read_csv(Path(work) / "test.csv", low_memory=False, nrows=5000)
    except Exception:
        return ""
    try:
        return build(tr, va, te, target=meta["target"], id_col=meta["id_col"],
                     degraded=degraded, numeric_from_text=numeric_from_text,
                     cleaned=cleaned)
    except Exception:
        return ""
