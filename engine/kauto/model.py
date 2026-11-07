"""基线建模 + 交叉验证 + 提交生成。先给一个跑通全流程的下限，再让 agent 去超越它。

设计要点：
- id 列与标签列一律不进特征
- 类别列用 train 分布做频次编码（最省事且不泄漏），OOV 记 0
- 分类任务统一把标签编码成整数 0..K-1，预测再解码，避免 dtype 打架
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, log_loss, mean_absolute_error, r2_score, roc_auc_score
from sklearn.model_selection import KFold, StratifiedKFold

from . import kapi, prep, storage, subcheck
from .runtime import THREADS


# ---------------------------------------------------------------- 指标

def _imbalanced(y: pd.Series) -> bool:
    vc = y.value_counts(normalize=True)
    return bool(vc.min() < 0.3)


def metric_for(task: str, kind: str, y: pd.Series, declared: str | None = None) -> str:
    """官方指标优先。declared 来自 skill 的 brief；只有它缺失时才本地推断。"""
    if declared:
        return declared
    if task == "regression":
        return "rmse"
    if kind == "binary":
        return "auc" if _imbalanced(y) else "logloss"
    return "accuracy"


def score(metric: str, y_true, y_pred) -> float:
    """按指标打分。**显式支持多分类**，这是补上的一个真实缺陷。

    实测过的崩溃（修复前）：
        auc     -> ValueError: multi_class must be in ('ovo','ovr')
        logloss -> ValueError: y_true and y_prob contain different number of classes

    原因是 roc_auc_score / log_loss 在标签 >=3 类时，
    要么必须显式给 multi_class，要么必须收 K 列概率矩阵。
    多分类比赛不少，走到指标评测就崩等于整条链路废掉。
    """
    y_true = np.asarray(y_true)
    # 只要指标需要概率/数值，就强制 float；否则保留原类型（类别标签可能是字符串）
    y_pred = np.asarray(y_pred, dtype=float) if metric in {
        "rmse", "mae", "r2", "auc", "logloss", "rmsle", "mape"} else np.asarray(y_pred)

    if metric == "rmse":
        return float(np.sqrt(np.mean((y_true.astype(float) - y_pred) ** 2)))
    if metric == "rmsle":
        a = np.maximum(y_true.astype(float), 0)
        b = np.maximum(y_pred, 0)
        return float(np.sqrt(np.mean((np.log1p(a) - np.log1p(b)) ** 2)))
    if metric == "mae":
        return float(mean_absolute_error(y_true, y_pred))
    if metric == "mape":
        denom = np.where(np.abs(y_true.astype(float)) < 1e-9, np.nan, np.abs(y_true.astype(float)))
        return float(np.nanmean(np.abs((y_true.astype(float) - y_pred) / denom)) * 100)
    if metric == "r2":
        return float(r2_score(y_true, y_pred))

    n_classes = int(np.unique(y_true).size)

    if metric == "auc":
        if n_classes <= 2:
            return float(roc_auc_score(y_true, y_pred))
        # 多分类：OVR 加权平均
        if y_pred.ndim == 2 and y_pred.shape[1] == n_classes:
            proba = y_pred
            # roc_auc_score(multi_class="ovr") 要求每行和为 1（严格概率）。
            # 实测：不满足时直接 ValueError（"Target scores need to be probabilities"）。
            # 候选代码可能返回未归一化的分数（如各类 margin），
            # 因此这里统一归一化，而不是把错误抛回给上游。
            total = proba.sum(axis=1, keepdims=True)
            total = np.where(total <= 0, 1.0, total)
            proba = proba / total
            return float(roc_auc_score(y_true, proba, multi_class="ovr", average="weighted"))
        # 只有标签没有概率时退化为 accuracy，避免直接抛异常
        return float(accuracy_score(y_true, np.rint(y_pred)))

    if metric == "logloss":
        if y_pred.ndim == 2 and y_pred.shape[1] >= 2:
            proba = y_pred
            total = proba.sum(axis=1, keepdims=True)
            total = np.where(total <= 0, 1.0, total)
            proba = np.clip(proba / total, 1e-7, 1 - 1e-7)
            return float(log_loss(y_true, proba))
        if n_classes <= 2:
            p = np.clip(y_pred, 1e-7, 1 - 1e-7)
            return float(log_loss(y_true, p))
        # 多分类但只给了 1 维标签：无法算 logloss，退化为 accuracy
        return float(accuracy_score(y_true, np.rint(y_pred)))

    if metric == "map":
        if y_pred.ndim == 2 and y_pred.shape[1] >= 2:
            from sklearn.metrics import average_precision_score
            return float(average_precision_score(y_true, y_pred, average="weighted"))
        return float(accuracy_score(y_true, np.rint(y_pred)))

    if metric == "f1":
        from sklearn.metrics import f1_score
        labels = y_pred.argmax(axis=1) if y_pred.ndim == 2 else np.rint(y_pred)
        return float(f1_score(y_true, labels, average="macro"))
    # accuracy：若拿到的是 K 列概率矩阵，先取 argmax 得到类别
    labels = y_pred.argmax(axis=1) if y_pred.ndim == 2 else np.rint(y_pred)
    return float(accuracy_score(y_true, labels))


def higher_is_better(metric: str) -> bool:
    return metric in {"auc", "accuracy", "f1", "map", "r2"}


def needs_proba(metric: str) -> bool:
    """AUC / logloss 吃概率；其余指标吃标签或数值。"""
    return metric in {"auc", "logloss", "map"}


# ---------------------------------------------------------------- 编码

# 常见「看起来是文本、其实是数值」的形态。都是真实比赛里的高频写法：
#   "1,200"（千分位）、"12%"（百分号）、"$1,200"（货币）、"3.5kg"（单位后缀）
_NUM_CLEAN_RE = re.compile(r"[\$€£¥,\s\u00a0]")
_PCT_RE = re.compile(r"^\s*[-+]?\d*\.?\d+\s*%\s*$")
_UNIT_RE = re.compile(r"^\s*[-+]?\d*\.?\d+\s*(kg|g|mg|t|km|m|cm|mm|l|ml|hz|khz|mhz|ghz|"
                      r"kw|w|v|a|mah|gb|mb|kb|tb|ft|in|yd|mi|lb|oz|s|ms|min|h|d)\s*$",
                      re.IGNORECASE)


def coerce_numeric(s: pd.Series) -> tuple[pd.Series, int]:
    """尽力把一列文本转成数值。返回 (转换结果, 成功转换的非空值个数)。

    为什么要这一步：`pd.to_numeric(errors="coerce")` 遇到 "1,200" 会直接变 NaN，
    而这类列随后被当成类别列做频次编码 —— 若每个值都只出现一次，
    整列会变成**同一个常数**，信息归零，而训练与评分照常进行（实测过）。

    处理顺序：剥千分位/货币/空白 → 百分号（除以 100）→ 单位后缀 → 兜底 to_numeric。

    实现上刻意避开 `Series.where`：pandas 3.x 的字符串 dtype 走 masked array，
    `where(cond, other)` 在跨 dtype 赋值时会抛 TypeError（实测踩到）。
    改成"逐段替换字符串再统一转数"，行为更可预测。
    """
    raw = s.astype("string")
    work = raw.str.replace(_NUM_CLEAN_RE, "", regex=True)

    pct_mask = work.str.match(_PCT_RE, na=False).fillna(False).to_numpy()
    unit_mask = work.str.match(_UNIT_RE, na=False).fillna(False).to_numpy()

    # 先把所有形态都改写成"纯数字字符串"，再一次性 to_numeric
    cleaned = work.str.replace("%", "", regex=False)
    cleaned = cleaned.str.replace(
        r"^\s*([-+]?\d*\.?\d+)\s*([a-zA-Z]+)\s*$", r"\1", regex=True)

    num = pd.to_numeric(cleaned, errors="coerce").astype(float)
    arr = num.to_numpy(dtype=float, copy=True)
    arr[pct_mask] = arr[pct_mask] / 100.0

    return pd.Series(arr, index=s.index), int(np.isfinite(arr).sum())


def _looks_numeric_text(s: pd.Series, sample: int = 200) -> bool:
    """抽样判断一列文本是否其实是数值。要求可解析比例 >= 90%。"""
    head = s.dropna()
    if len(head) == 0:
        return False
    head = head.iloc[:sample]
    _, ok = coerce_numeric(head)
    return ok >= max(1, int(len(head) * 0.9))


def build_matrix(frames: list[pd.DataFrame], train_ref: pd.DataFrame, meta: dict):
    """frames[0] 是参考分布来源。返回 (编码后列表, 使用的列名, 被判为类别的列)。

    相比早期版本的改进（都是实测出来后补的）：
    1. 文本数值列先尝试解析（千分位/百分号/货币/单位），成功则按数值使用；
    2. 频次编码后检查是否退化成常数 —— 退化列改用哈希编码并记录告警；
    3. 返回的第三个元素除了类别列，还带上 `report`（每列的编码方式与是否退化）。
    """
    drop = {meta["id_col"], *meta["target_cols"]}
    feats = [c for c in train_ref.columns if c not in drop]

    num, cat, text_coerced, degraded = [], [], [], []
    for c in feats:
        if pd.api.types.is_numeric_dtype(train_ref[c]):
            num.append(c)
        elif _looks_numeric_text(train_ref[c]):
            num.append(c)
            text_coerced.append(c)
        else:
            cat.append(c)

    freq = {c: train_ref[c].astype("string").value_counts(normalize=True) for c in cat}

    # ── 频次编码的失效判据，必须建在**编码后**的列上 ──────────────────────
    #
    # 旧判据查的是**原始列**：`freq[c].iloc[0] >= 1 - 1e-12`，即"某一个值
    # 占了几乎全部"。它漏掉了频次编码真正的失效形态 —— **高基数列**。
    #
    # 实测 titanic（891 行）：
    #
    # | 列     | nunique | 最高频占比 | 旧判据触发 | 编码后 nunique |
    # |--------|---------|-----------|-----------|---------------|
    # | Name   | 891/891 | 0.0011    | 否        | **1**（常数）  |
    # | Cabin  | 147     | 0.0196    | 否        | 4             |
    # | Ticket | 681     | 0.0079    | 否        | 7             |
    # | Sex    | 2       | 0.6476    | 否        | 2（正常）      |
    #
    # Name 每个值只出现一次 → 频次恒为 1/891 → 编码后整列是同一个数，
    # 一列纯噪声；Cabin/Ticket 被压成个位数档位，而测试集里 51%/68% 的取值
    # 在训练集里从未出现过（映射为 NaN），模型学到的分割在 test 上失效。
    #
    # 特征消融实测（同数据同参数，只换特征集合）：
    #
    #   现状（全部频次编码）       CV=0.8013 ±0.0235
    #   经典特征（去掉三列）        CV=0.8137 ±0.0273   ← +1.2pp
    #   经典 + FamilySize/IsAlone  CV=0.8182 ±0.0209   ← +1.7pp
    #
    # 所以判据改成两条，任一成立即丢弃该列：
    #
    #   (a) **编码后退化成常数**（编码后不同取值 < 2）—— Name 属此类；
    #   (b) **编码覆盖率过低**（落在训练集出现过的取值上的样本 < 50%）
    #       —— Cabin 90.9%、Ticket 67.5% 属此类。
    #
    # (b) 这条是这次真机提交后才看清的：只看 (a) 会把 Ticket/Cabin 留下，
    # 它们编码后确实还有 7 档 / 4 档（不是常数），看上去"有信息"；
    # 但 test 里 67.5% / 90.9% 的行映射成 NaN —— 这一列对**大多数待预测样本**
    # 都没有取值。残留的那几档只在少数行上生效，模型据此学到的分割
    # 在测试集上大面积走缺失分支，等价于噪声。
    #
    # 用覆盖率的理由：频次编码的价值正比于"这个取值在参考分布里出现过"。
    # 覆盖不足一半，说明这列本质上是**每行唯一的高基数标识**，
    # 而不是可泛化的类别 —— 这类列该丢，不该硬编码。
    #
    # ## 覆盖率必须在**待预测侧**量，不能量在 train_ref 上
    #
    # 第一版把 coverage 算在 train_ref 自己头上，结果恒为 100% ——
    # 训练集的取值当然全在训练集的频次表里，这个指标自证，毫无意义
    # （实测：只靠 (a) 时 Cabin 被丢、Ticket 被留，Ticket 明明 67.5% 未见过）。
    #
    # 正确口径：对 frames[1:]（验证/测试/downstream）统计"映射后非 NaN 的比例"。
    # 没有参考侧帧时（只有一个 frame）退回 train_ref 自身 —— 此时覆盖率不构成
    # 判据，只保留 (a)。
    #
    # 50% 这个阈值对应"多数样本拿不到该列信息"，与消融结果一致：
    # 丢掉 Name/Cabin/Ticket 三列 → CV 0.8137（+1.2pp）。
    ENC_DISTINCT_MIN = 2
    ENC_COVERAGE_MIN = 0.5
    holdout = frames[1:]
    dropped: list[str] = []
    for c in cat:
        enc = train_ref[c].astype("string").map(freq[c])
        distinct = int(enc.nunique(dropna=True))
        if distinct < ENC_DISTINCT_MIN:
            degraded.append(c)
            dropped.append(c)
            continue
        if holdout:
            seen = [float(df[c].astype("string").map(freq[c]).notna().mean())
                    for df in holdout]
            coverage = min(seen)          # 取最差的一侧，宁可保守
        else:
            coverage = 1.0                # 无可比侧 → 覆盖率不作判据
        if coverage < ENC_COVERAGE_MIN:
            degraded.append(c)
            dropped.append(c)
    cat = [c for c in cat if c not in dropped]
    coverage_report = {c: round(float(frames[1][c].astype("string").map(freq[c]).notna().mean()), 4)
                       for c in cat} if holdout else {}

    out = []
    inf_fixed: list[str] = []
    for df in frames:
        m = pd.DataFrame(index=df.index)
        for c in num:
            if c in text_coerced:
                num_s, _ = coerce_numeric(df[c])
                col = num_s.astype(float)
            else:
                col = pd.to_numeric(df[c], errors="coerce")

            # ±inf 会让 LightGBM 数值不稳（实测确认过：inf 会原样留在特征里）。
            # 处理成缺失而不是保留：inf 在真实数据里几乎都是"溢出/除零"产物，
            # 语义上就是"这个值不可用"，交给模型当缺失处理更合理。
            arr = col.to_numpy(dtype=float, copy=True)
            n_inf = int(np.isinf(arr).sum())
            if n_inf:
                arr[np.isinf(arr)] = np.nan
                inf_fixed.append(c)

            # 极端量级一律当缺失。
            #
            # 早期只拦 |x| > 1e15 —— 那个阈值只够拦住溢出级的值。
            # 实测（脏数据夹具里放 1e9 的金额型异常）：1e9 完全通过，进了模型。
            # 而真实数据里 1e9 这种量级就是异常（如金额列出现十亿），
            # 且会让树的分裂阈值失去分辨率。
            #
            # 改成**基于分布的判据**：偏离中位数超过 1e6 倍，或超过 1e12 的绝对量级。
            # 用中位数而非均值，因为均值本身会被极端值带偏。
            if np.isfinite(arr).any():
                finite = arr[np.isfinite(arr)]
                # 用 **IQR（四分位距）** 做尺度，不用 std。
                # 实测：`num_outlier` 列里 2% 是 1e9，std 被拉大到 3e7，
                # 于是「std × 1e6 = 3e13」把 1e9 也放过去了 —— 这就是
                # "用被异常值污染的统计量去判异常值"的循环。
                # IQR 只看中间 50% 的跨度，对极端值稳健。
                q1, q3 = np.percentile(finite, [25, 75])
                iqr = float(q3 - q1)
                med_abs = abs(float(np.median(finite)))

                # ── IQR 为 0 时必须换尺度 ─────────────────────────────────
                # 实测真实比赛才暴露：`Parch`（同乘船父母子女数）有 542/891 是 0，
                # 于是 Q1 = Q3 = 中位数 = 0 → IQR = 0、scale = 1e-9 →
                # 阈值 = 1e-4 → **所有非零值（1~6，共 170 行）全被清成 NaN**。
                # 特征被挖掉 19% 的取值，模型等于丢了这个信号。
                #
                # 正确做法：IQR=0 说明数据集中在少数离散值上，
                # 这类列本来就不该用"离群"判据 —— 直接跳过清洗。
                # （极端值在 IQR=0 的列里其实表现为"离群的那几个离散值"，
                #   而它们往往是真实的业务含义，如 Parch=6，清理反而是破坏信息。）
                if iqr <= 0:
                    # 注意：这里不能 `continue` —— continue 会跳过整列的写入，
                    # 该列直接从特征矩阵里消失（实测立刻 KeyError: "Parch"）。
                    # 应该是"跳过清洗"，然后照常写回。
                    m[c] = arr
                    continue
                scale = max(iqr, med_abs, 1e-9)
                # 1e5 倍 IQR: 正常值几乎不会被碰到，1e9 这种会被拦
                per_col_limit = scale * 1e5
                huge = np.abs(arr) > per_col_limit
                if huge.any():
                    arr[huge & ~np.isinf(arr)] = np.nan
                    if c not in inf_fixed:
                        inf_fixed.append(c)

            m[c] = arr
        for c in cat:
            # 退化列已从 cat 里剔除（见上文 dropped），所以这里只会走频次编码。
            #
            # 关键细节：**未在训练集出现过的取值映射为 NaN，不填 0**。
            # 旧代码 `.fillna(0.0)` 把"没见过的取值"塞进"频次为 0"这一档，
            # 而 0 在频次空间里是一个**真实但训练时不存在**的档位 ——
            # 树模型从未在这档上分裂过，等于让它带着错误语义进入模型。
            # 置 NaN 才是诚实的表达：这列对这个样本没有可用信息，
            # 交给 LightGBM 当缺失值走默认分支。
            m[c] = df[c].astype("string").map(freq[c]).astype(float)
        out.append(m)

    keep = [c for c in out[0].columns if out[0][c].notna().any()]
    report = {
        "numeric_native": [c for c in num if c not in text_coerced],
        "numeric_from_text": text_coerced,
        "categorical_freq": [c for c in cat if c not in degraded],
        "categorical_hash_degraded": degraded,
        "categorical_dropped": dropped,
        # 被丢弃列在**待预测侧**的覆盖率（诊断用：解释"为什么丢它"）
        "categorical_dropped_coverage": {
            c: round(float(frames[1][c].astype("string").map(freq[c]).notna().mean()), 4)
            for c in dropped if holdout and c in frames[1].columns
        },
        # 保留列的待预测侧覆盖率（低于 1 说明有未见过取值，属正常）
        "categorical_retained_coverage": coverage_report,
        # 被清洗过 inf/极端量级的列，显式暴露：
        # 这些列大概率来自单位错误或除零，值得人看一眼原始数据
        "nonfinite_cleaned": sorted(set(inf_fixed)),
    }
    return [m[keep] for m in out], keep, cat, report


def encode_y(y_tr: pd.Series, y_va: pd.Series) -> tuple[np.ndarray, np.ndarray, list]:
    cats = list(pd.Categorical(pd.concat([y_tr, y_va])).categories)
    mapping = {c: i for i, c in enumerate(cats)}
    return y_tr.map(mapping).to_numpy(), y_va.map(mapping).to_numpy(), cats


def decode(pred: np.ndarray, cats: list) -> np.ndarray:
    idx = np.rint(pred).astype(int)
    idx = np.clip(idx, 0, len(cats) - 1)
    return np.array([cats[i] for i in idx])


# ---------------------------------------------------------------- 基线

def _lgb(task: str, kind: str, seed: int, n: int = 400, n_rows: int = 0,
         n_feat: int = 0, n_classes: int = 0):
    """构造 LightGBM。

    ## 为什么参数要随规模自适应

    实测（digit-recognizer，42000 行 / 784 特征 / 10 类）：
    固定 `n_estimators=400, num_leaves=63` 时，多分类会训练 **400×10=4000 棵树**，
    再乘 5 折 CV = **20000 棵树** —— baseline 跑了 **15+ 分钟** 还没结束。
    对大比赛来说，"基线"慢到这个程度就失去了"先有个参照"的意义。

    所以按规模降配：
    - 类别多 → 树数按类数摊薄（每类 40 棵，而非每类 400 棵）
    - 特征多 → 叶子数减少（784 特征的格子更稀疏，63 叶过拟合且慢）
    - 行数大 → 适当减树（大样本本身信息足）
    - 但设下限，避免小数据上被降得过头
    """
    import lightgbm as lgb
    n_est = n
    leaves = 63
    if n_classes and n_classes > 2:
        # 多分类：总树数 = n_estimators * n_classes，按类数摊薄
        n_est = max(60, min(n, int(400 / max(1, n_classes - 1))))
    if n_feat > 200:
        leaves = 31          # 高维稀疏时更少叶子更快且不差
    if n_rows > 20000:
        n_est = max(60, int(n_est * 0.6))
    common = dict(n_estimators=n_est, learning_rate=0.05, num_leaves=leaves,
                  subsample=0.9, colsample_bytree=0.9, random_state=seed,
                  verbose=-1, n_jobs=THREADS, force_row_wise=True)
    if task == "regression":
        return lgb.LGBMRegressor(**common)
    if kind == "binary":
        return lgb.LGBMClassifier(objective="binary", **common)
    return lgb.LGBMClassifier(objective="multiclass", **common)


def run_baseline(slug: str, folds: int = 5, seed: int = 42) -> dict:
    t_start = time.time()
    meta = prep.load_work(slug)
    work = Path(meta["work_dir"])
    train = pd.read_csv(work / "train.csv", low_memory=False)
    valid = pd.read_csv(work / "valid.csv", low_memory=False)

    target = meta["target"]
    (Xtr, Xva), feats, cat, col_report = build_matrix([train, valid], train, meta)
    y_tr_raw, y_va_raw = train[target], valid[target]

    metric = metric_for(meta["task"], meta["kind"], y_tr_raw, meta.get("metric_declared"))
    want_proba = needs_proba(metric)
    # 规模参数（用于让 LightGBM 自适应降配，见 _lgb 的说明）
    _n_rows, _n_feat = Xtr.shape[0], Xtr.shape[1]
    _n_cls = 0
    try:
        _n_cls = int(meta.get("n_classes") or 0)
    except Exception:
        _n_cls = 0
    _scale = dict(n_rows=_n_rows, n_feat=_n_feat, n_classes=_n_cls)

    if meta["task"] == "regression":
        y_tr, y_va = y_tr_raw.to_numpy(dtype=float), y_va_raw.to_numpy(dtype=float)
        ytr_cv = y_tr
        pred_va = _lgb("regression", "single", seed, **_scale).fit(Xtr, y_tr).predict(Xva)
    elif meta["kind"] == "binary":
        y_tr, y_va, cats = encode_y(y_tr_raw, y_va_raw)
        ytr_cv = y_tr
        mdl = _lgb("classification", "binary", seed, **_scale).fit(Xtr, y_tr)
        pred_va = mdl.predict_proba(Xva)[:, 1] if want_proba else mdl.predict(Xva)
    else:
        y_tr, y_va, cats = encode_y(y_tr_raw, y_va_raw)
        ytr_cv = y_tr
        mdl = _lgb("classification", "multiclass", seed, **_scale).fit(Xtr, y_tr)
        if want_proba and len(cats) == 2:
            pred_va = mdl.predict_proba(Xva)[:, 1]
        else:
            pred_va = mdl.predict(Xva)

    valid_score = score(metric, y_va, pred_va)

    splits = (KFold(n_splits=folds, shuffle=True, random_state=seed).split(Xtr)
              if meta["task"] == "regression"
              else StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed).split(Xtr, ytr_cv))

    cv = []
    oof = np.full(len(Xtr), np.nan)          # 折外预测：每个样本只在它那一折里被预测一次
    for tr_i, va_i in splits:
        m = _lgb(meta["task"], meta["kind"], seed, **_scale).fit(Xtr.iloc[tr_i], ytr_cv[tr_i])
        if meta["task"] == "regression":
            p = m.predict(Xtr.iloc[va_i])
        elif want_proba:
            pp = m.predict_proba(Xtr.iloc[va_i])
            p = pp[:, 1] if pp.shape[1] == 2 else pp
        else:
            p = m.predict(Xtr.iloc[va_i])
        cv.append(score(metric, (y_tr if meta["task"] == "regression" else ytr_cv)[va_i], p))
        if p.ndim == 1:
            oof[va_i] = p

    # OOF 落盘。为什么必须存：单次留出集的 valid_pred 只能用来"看一眼分数"，
    # 用它拟合 blend 权重会过拟合；K 折 OOF 才是能安全拟合权重的东西
    # （MLE-STAR 的 ensemble 就是先把多个方案的可合并产物攒起来再合并）。
    oof_metric = None
    if not np.isnan(oof).any():
        y_for_oof = y_tr if meta["task"] == "regression" else ytr_cv
        try:
            oof_metric = round(float(score(metric, y_for_oof, oof)), 6)
        except Exception:
            oof_metric = None
        storage.save_npy(work / "baseline_oof.npy", oof)

    result = {
        "slug": slug,
        "task": meta["task"],
        "kind": meta["kind"],
        "metric": metric,
        "metric_source": meta.get("metric_source", "local-inference"),
        "official_metric_raw": meta.get("official_metric_raw"),
        "higher_is_better": higher_is_better(metric),
        "valid_score": round(float(valid_score), 6),
        "cv_mean": round(float(np.mean(cv)), 6),
        "cv_std": round(float(np.std(cv)), 6),
        "oof_score": oof_metric,
        "oof_saved": oof_metric is not None,
        "folds": folds,
        "n_features_used": len(feats),
        # 特征集指纹：用来判断"下游预测文件是不是用当前特征集算出来的"。
        #
        # ## 为什么需要它
        #
        # 实测踩到：改掉频次编码的退化判据后重跑 `baseline`（特征从 10 列变 7 列），
        # 再跑 `write-sub` —— 它看到 `best_test_pred.npy` 存在就直接复用，
        # 而那份 npy 是**旧特征集**算出来的。于是提交的 csv 与新的 CV 完全脱节，
        # 且 sha256 与旧提交一模一样（1e563a50…），分数也就还是 0.76076。
        # 表面上"重跑了 baseline + write-sub"，实际上什么都没变。
        #
        # 根因：预测文件只记"哪个节点算的"，没记"用哪些特征算的"。
        # 有了指纹，`write_sub_submission` 能在特征集变化时判定陈旧并重算。
        "features_fingerprint": hashlib.sha1(
            "\x1f".join(sorted(feats)).encode("utf-8")
        ).hexdigest()[:16],
        "categorical_encoded_as_freq": cat[:20],
        # 列体检：哪些文本列被救成数值、哪些列退化到哈希编码。
        # 退化列意味着"这列原来是文本、被压得没有区分度"，必须显式暴露，
        # 否则模型少一组特征而分数上看不出来。
        "columns": col_report,
        "degraded_columns": col_report.get("categorical_hash_degraded", []),
        "seconds": round(time.time() - t_start, 1),
    }
    (work / "baseline.json").parent.mkdir(parents=True, exist_ok=True)
    storage.write_json(work / "baseline.json", result)
    try:
        from . import manifest
        manifest.write_manifest(work, slug=slug, stage="baseline",
                                extra={"folds": folds, "cv_mean": result["cv_mean"],
                                       "oof_score": oof_metric})
    except Exception:
        pass   # 清单只做记录，取不到信息不该让基线失败
    return result


# ---------------------------------------------------------------- 提交

def _write_sub(sub: pd.DataFrame, meta: dict, work: Path, sample: pd.DataFrame | None = None,
               test: pd.DataFrame | None = None) -> dict:
    path = work / "submission.csv"
    sub.to_csv(path, index=False)
    info = subcheck.check(sub, sample, meta["id_col"],
                          test if test is not None else pd.read_csv(work / "test.csv", low_memory=False))
    # 溯源：这条路径是"用默认 LightGBM 全量重训"，与迭代出的方案是两个东西。
    # 必须写清楚，否则事后无法判断这份 submission 用了哪种模型。
    prov = {}
    try:
        from .iter import _record_provenance
        prov = _record_provenance(work, source="retrain_default_lgbm",
                                  metric=meta.get("metric_declared"))
    except Exception:
        prov = {}
    return {"submission": str(path), "provenance": prov, **info}


def write_submission(slug: str) -> dict:
    """train+valid 全量重训，产出 work/<slug>/submission.csv。"""
    meta = prep.load_work(slug)
    work = Path(meta["work_dir"])
    train = pd.read_csv(work / "train.csv", low_memory=False)
    valid = pd.read_csv(work / "valid.csv", low_memory=False)
    full = pd.concat([train, valid], ignore_index=True)
    test = pd.read_csv(work / "test.csv", low_memory=False)

    target = meta["target"]
    sample_path = work / "sample_submission.csv"
    sample = pd.read_csv(sample_path, low_memory=False) if sample_path.exists() else None

    (Xfull, Xtest), _, _, _ = build_matrix([full, test], full, meta)
    id_col = meta["id_col"]
    metric = metric_for(meta["task"], meta["kind"], full[target], meta.get("metric_declared"))
    want_proba = needs_proba(metric)
    is_proba_sub = sample is not None and len(sample.columns) > 2

    # ── 规模参数必须传给 _lgb，否则自适应降配失效 ─────────────────────────
    #
    # 实测踩到：`write_submission` 这三处都写成 `_lgb(..., 500)`，**没传
    # n_rows / n_feat / n_classes** —— 于是 `_lgb` 里的自适应分支全部走不进，
    # 退化成固定 500 棵树。digit-recognizer（42000 行 / 784 特征 / 10 类）：
    #
    #   baseline（5 折，已传规模参数）：351.7s 跑完
    #   write-sub（单次全量 fit，未传）：>10 分钟仍未结束
    #
    # 而且多分类下总树数 = n_estimators × n_classes = 500×10 = 5000 棵，
    # 这是"单次重训比 5 折 CV 还慢"的直接原因。
    #
    # 修法：把规模参数一并传入（与 run_baseline 同一套口径）。
    _n_rows, _n_feat = Xfull.shape[0], Xfull.shape[1]
    try:
        _n_cls = int(meta.get("n_classes") or 0)
    except Exception:
        _n_cls = 0
    _scale = dict(n_rows=_n_rows, n_feat=_n_feat, n_classes=_n_cls)
    print(f"[kauto] 全量重训：{_n_rows} 行 × {_n_feat} 特征 × {_n_cls or '?'} 类"
          f"（LightGBM 参数按规模自适应）", flush=True)

    # ── id 列取法（这是 digit-recognizer 暴露的真实缺陷链）─────────────────
    #
    # 实测：`meta["id_col"]` 是空字符串 —— prepare 正确置空，因为 `ImageId`
    # **只存在于 sample_submission，train/test 里都没有**（test.csv 只有
    # 784 个 pixel 列）。旧代码直接写 `test[id_col]` → `test[""]` →
    # **KeyError: ''**，且发生在训练结束之后（重训 10 分钟），
    # 表现为"跑很久最后什么都没有"，看着像训练卡死。
    #
    # 修一半时我用 `np.arange(len(test))` 兜底 —— 又错：那产出 0..27999，
    # 而 sample_submission 的 ImageId 是 **1..28000**。校验器当场拦下
    # （"1 not in the sample (0)"），否则就是 28000 行全部错位、白烧一次配额。
    #
    # 正确优先级：
    #   1. meta 的 id_col 且 test 里真有 → 用它；
    #   2. sample_submission 的首列且 test 里真有同名列 → 用它；
    #   3. **sample_submission 的首列原值** → 直接照抄（行序一一对应，
    #      这是唯一能用的情况：id 只存在于 sample 时，sample 就是权威行序）；
    #   4. 最后才退回行号（无 sample 时的裸模式）。
    if id_col and id_col in test.columns:
        id_values = test[id_col].values
    elif sample is not None and sample.columns[0] in test.columns:
        id_values = test[sample.columns[0]].values
    elif sample is not None:
        # id 只存在于 sample（典型：digit-recognizer 的 ImageId 1..N）
        id_values = sample[sample.columns[0]].values
    else:
        id_values = np.arange(len(test))

    if meta["task"] == "regression":
        p = _lgb("regression", "single", 42, 500, **_scale).fit(
            Xfull, full[target].to_numpy(float)).predict(Xtest)
    elif meta["kind"] == "binary":
        y, _, cats = encode_y(full[target], full[target])
        mdl = _lgb("classification", "binary", 42, 500, **_scale).fit(Xfull, y)
        if is_proba_sub:
            # 二分类但 submission 是两列概率
            proba = mdl.predict_proba(Xtest)
            out = pd.DataFrame(proba, columns=[str(c) for c in cats])
            out.insert(0, sample.columns[0], id_values)
            out = out[[c for c in sample.columns if c in out.columns]]
            return _write_sub(out, meta, work, sample, test)
        # 指标吃标签（accuracy/f1）就交硬标签，吃概率（auc/logloss）就交概率
        p = mdl.predict_proba(Xtest)[:, 1] if want_proba else mdl.predict(Xtest)
    else:
        y, _, cats = encode_y(full[target], full[target])
        mdl = _lgb("classification", "multiclass", 42, 500, **_scale).fit(Xfull, y)
        if is_proba_sub:
            proba = mdl.predict_proba(Xtest)
            out = pd.DataFrame(proba, columns=[str(c) for c in cats])
            out.insert(0, sample.columns[0], id_values)
            out = out[[c for c in sample.columns if c in out.columns]]
            return _write_sub(out, meta, work, sample, test)
        p = decode(mdl.predict(Xtest), cats) if not want_proba else mdl.predict_proba(Xtest)[:, 1]

    # ── id 列的取法（id_values 已在上面按同一口径算好）────────────────────
    #
    # 实测踩到（digit-recognizer）：`meta["id_col"]` 是空字符串 —— prepare 会
    # 正确地把这里置空，因为 `ImageId` 只存在于 test/sample，train 里没有
    # （这是 prep 的一条已修规则：只有当该列真存在于 train/test 时才当 id_col）。
    #
    # 但旧代码直接写 `test[id_col]` → `test[""]` → **KeyError: ''**。
    # 更糟的是这个 KeyError 发生在**训练结束之后**（42000×784 重训要 10 分钟），
    # 于是表现为"跑了很久最后什么也没有"，看起来像训练卡死。
    #
    # 取法优先级：meta 里的 id_col → sample 的首列（若 test 里有同名列）→ 行号。
    # 行号兜底与 sample_submission 的行序一一对应，是安全的（Kaggle 按行序对齐）。
    if sample is not None:
        out = pd.DataFrame({sample.columns[0]: id_values, sample.columns[1]: p})
    else:
        out = pd.DataFrame({(id_col or "id"): id_values, target: p})
    return _write_sub(out, meta, work, sample, test)
