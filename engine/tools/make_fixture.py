"""离线演练夹具：造一个假的二分类比赛目录，用来在无 Kaggle 凭据下跑通全链路。

真实比赛结构：train.csv（含 id + 特征 + target）、test.csv（含 id + 特征）、
sample_submission.csv（id + target）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd


def make(dest: Path, n_train: int = 1200, n_test: int = 400, seed: int = 7) -> Path:
    rng = np.random.default_rng(seed)
    dest.mkdir(parents=True, exist_ok=True)

    def block(n: int, start: int) -> pd.DataFrame:
        x1 = rng.normal(size=n)
        x2 = rng.normal(size=n)
        x3 = rng.integers(0, 6, n)          # 类别型（整数但基数低）
        seg = rng.choice(["a", "b", "c", "d"], n)  # 字符串类别
        x4 = rng.uniform(0, 1, n)
        noise = rng.normal(size=n)
        # 真实信号：x1 线性 + x2 分段 + seg 部分有效 + x4 阈值
        logits = 1.6 * x1 + 1.1 * np.where(x2 > 0.4, 1.0, 0.0) + 0.5 * (seg == "b") + 1.5 * (x4 > 0.62) + 0.3 * noise
        y = (logits + rng.normal(scale=0.55, size=n) > 1.1).astype(int)
        return pd.DataFrame({
            "row_id": np.arange(start, start + n),
            "x1": x1, "x2": x2, "x3": x3, "segment": seg, "x4": x4,
            "target": y,
        })

    train = block(n_train, 0)
    test = block(n_test, n_train).drop(columns=["target"])

    train.to_csv(dest / "train.csv", index=False)
    test.to_csv(dest / "test.csv", index=False)
    pd.DataFrame({"row_id": test["row_id"], "target": 0}).to_csv(
        dest / "sample_submission.csv", index=False)
    return dest


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fixtures/demo-comp")
    make(out)
    print(f"fixture ready: {out}")
