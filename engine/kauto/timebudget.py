"""自适应超时：按数据规模推算合理的执行时限。

## 为什么要这个

原来 `_runner.run(timeout=900)` 硬编码，而调用处**根本没传 timeout**，
所以永远是 900s。问题有两面：

- **小数据**：900s 太长，一个死循环要等 15 分钟才判失败
- **大数据**：真实比赛 100 万行 × 200 列，比 demo 大 1000 倍，
  900s 必超时；而超时会被记为「候选失败」，看起来像代码有问题，
  实际是时限给错了 —— 这种误判最费时间

## 做法

以 **baseline 实测耗时**为锚，按候选天然更慢的倍数放大：
候选里有特征工程、调参、多模型，通常比 baseline 慢 3~10 倍。

再叠加规模因子：行数/特征数超过基线规模时按比例上调（有上限，防爆）。

读不到 baseline 耗时就用保守默认值 —— 宁可给宽一点，也不要把
正常候选判成超时。
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_BASE_SECONDS = 30.0    # baseline 耗时缺失时的保守假设
MIN_TIMEOUT = 120              # 再快也别低于 2 分钟
MAX_TIMEOUT = 7200             # 上限 2 小时，防单个候选吃满整场
CANDIDATE_FACTOR = 8.0         # 候选通常比 baseline 慢这么多倍


def estimate(work: Path, folds: int = 0) -> dict:
    """推算本次候选应给的超时秒数，返回 {timeout, reason, baseline_seconds}。"""
    work = Path(work)

    base_s = None
    bl = work / "baseline.json"
    if bl.exists():
        try:
            base_s = float(json.loads(bl.read_text(encoding="utf-8")).get("seconds") or 0) or None
        except Exception:
            base_s = None

    reason_bits = []
    if base_s:
        # baseline 自己跑的是 5 折；候选通常单折 + 特征工程 + 调参
        base = base_s * CANDIDATE_FACTOR
        reason_bits.append(f"baseline 实测 {base_s:.1f}s × {CANDIDATE_FACTOR:g}")
    else:
        base = DEFAULT_BASE_SECONDS * CANDIDATE_FACTOR
        reason_bits.append(f"无 baseline 耗时，保守取 {DEFAULT_BASE_SECONDS:g}s × {CANDIDATE_FACTOR:g}")

    # OOF 会让候选多跑 folds 次
    if folds and folds > 1:
        base *= folds
        reason_bits.append(f"OOF {folds} 折 ×{folds}")

    # 规模因子：只在**没有 baseline 实测**时才用。
    #
    # 实测缺陷：digit-recognizer 的超时被算成 7200s（顶到上限）。
    # 那里 baseline 实测 307s —— 这个数字**已经包含了 42000 行 / 784 列的成本**，
    # 再乘规模因子 ×17.75 等于**同一因素算两次**：
    #   307 × 8 × 17.75 = 43600s → 被封顶到 7200s（2 小时）
    # 后果是超时形同虚设：死循环候选会白等 2 小时。
    #
    # 有实测时只做**温和上下限**（防止 baseline 恰好很慢/很快时过度外推），
    # 无实测时才用规模因子估计。
    scale = 1.0
    if base_s:
        # 有实测：只做温和钳制，不做规模外推
        scale = 1.0
        reason_bits.append("（已按实测换算，不再叠加规模因子）")
    else:
        try:
            meta = json.loads((work / "meta.json").read_text(encoding="utf-8"))
            n_rows = int(meta.get("n_rows") or 0)
            n_feat = int(meta.get("n_features") or 0)
            if n_rows > 0:
                # 行数按平方根缩放：线性缩放会把百万行判成几小时的超时
                scale = max(1.0, (n_rows / 1200.0) ** 0.5)
            if n_feat > 20:
                scale *= 1.0 + min(2.0, (n_feat - 20) / 180.0)
            if scale > 1.05:
                reason_bits.append(f"规模因子 ×{scale:.2f}（{n_rows} 行 / {n_feat} 列）")
        except Exception:
            pass

    timeout = int(base * scale)
    timeout = max(MIN_TIMEOUT, min(MAX_TIMEOUT, timeout))
    return {
        "timeout": timeout,
        "baseline_seconds": base_s,
        "factor": CANDIDATE_FACTOR,
        "scale": round(scale, 3),
        "reason": "；".join(reason_bits),
    }
