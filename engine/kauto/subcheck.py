"""submission 自检：真正提交前的最后一道闸。

宁可在这里报错，也不要交上去一个"行数表头都对、数值全错"的文件——
Kaggle 的提交次数是最贵的东西，一次废提交换不回来。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


class SubmissionInvalid(RuntimeError):
    pass


def check(out: pd.DataFrame, sample: pd.DataFrame | None, id_col: str,
          test: pd.DataFrame, min_classes: int = 2) -> dict:
    problems: list[str] = []

    if sample is not None:
        if list(map(str, out.columns)) != list(map(str, sample.columns)):
            problems.append(f"列名不一致：{list(out.columns)} vs {list(sample.columns)}")
        if len(out) != len(sample):
            problems.append(f"行数不一致：{len(out)} vs {len(sample)}")

        first = sample.columns[0]
        if first in out.columns:
            a = out[first].astype(str).to_numpy()

            # 与 test.csv 对齐（防止顺序错位）
            if first in test.columns or id_col in test.columns:
                tcol = first if first in test.columns else id_col
                b = test[tcol].astype(str).to_numpy()
                if len(a) == len(b) and not np.array_equal(a, b):
                    problems.append("id 列与 test.csv 不对齐")

            # 与 sample_submission 对齐（防止**格式**错位）。
            # 实测踩到的场景：sample 里 id 是 "001","002"（带前导零的字符串），
            # 而生成的 submission 被读成 "1","2"。两者是不同的值，
            # Kaggle 会判"id 不匹配"，但早期只跟 test.csv 比，看不出问题。
            c = sample[first].astype(str).to_numpy()
            if len(a) == len(c) and not np.array_equal(a, c):
                problems.append(
                    "id 列与 sample_submission 不一致"
                    f"（前几个：{a[:3].tolist()} vs {c[:3].tolist()}）"
                    "—— 常见原因是 id 被当成数字读入，丢了前导零或精度")

    # 重复 id：Kaggle 会直接拒绝。正常路径不会产生重复，
    # 但下游任何一次错误的 concat/merge 都可能造成，而本地看不出来。
    dup_col = sample.columns[0] if sample is not None else id_col
    if dup_col in out.columns:
        n_dup = int(out[dup_col].astype(str).duplicated().sum())
        if n_dup:
            problems.append(f"id 列有 {n_dup} 个重复值（Kaggle 会拒绝重复 id）")

    if out.isna().any().any():
        bad = [c for c in out.columns if out[c].isna().any()]
        problems.append(f"存在空值列：{bad}")

    id_names = {id_col}
    if sample is not None:
        id_names.add(sample.columns[0])
    val_cols = [c for c in out.columns if c not in id_names]
    if not val_cols:
        problems.append("没有预测列")
    else:
        col = out[val_cols[0]]
        v = pd.to_numeric(col, errors="coerce")

        # 多分类比赛提交通常是**字符串标签**（"high" / "mid" / "low"），
        # 不是数值。早期实现假定预测列一定是数字，于是多分类 write-sub
        # 在自检这一步就崩了（实测：pd.to_numeric 全 NaN → 判为"无法转为数值"）。
        #
        # 判据改成：只要该列是**非空且取值数合理**的类别/数值，就视为合法。
        # 数值列仍额外检查量级与取值数。
        if v.notna().sum() == 0:
            # 不是数值 → 按类别标签校验
            s = col.astype(str)
            nunique_lbl = int(s.nunique(dropna=True))
            n_rows_lbl = len(s)
            if col.isna().any():
                problems.append("预测列存在空值")
            elif nunique_lbl < 1:
                problems.append("预测列没有有效取值")
            elif n_rows_lbl >= 10 and nunique_lbl >= n_rows_lbl * 0.95:
                # 几乎每行一个不同值 —— 交上去的多半是 id / 自由文本，不是类别标签。
                # 阈值不能用绝对值（早期写成 >10000，20 行的误交样本直接放过了，实测踩到）。
                # 用"唯一值占比 > 95% 且总数 > 50"：小样本不误判，大样本能抓住。
                problems.append(
                    f"预测列有 {nunique_lbl} 种取值、共 {n_rows_lbl} 行（几乎每行都不同），"
                    "看起来不是类别标签（可能误交了 id 或原始文本）")
            n_unique = nunique_lbl
            is_numeric_col = False
        else:
            if v.nunique(dropna=True) < min_classes:
                problems.append(f"预测列只有 {v.nunique(dropna=True)} 个不同取值（模型可能没训练起来）")
            elif float(np.nanmax(np.abs(v.to_numpy(dtype=float)))) > 1e12:
                problems.append("预测列出现异常大值")
            n_unique = int(v.nunique(dropna=True))
            is_numeric_col = True

    if problems:
        raise SubmissionInvalid("submission 自检失败：" + "；".join(problems))

    return {"rows": int(len(out)), "columns": [str(c) for c in out.columns],
            "n_unique_values": int(n_unique),
            "prediction_kind": "numeric" if is_numeric_col else "label"}
