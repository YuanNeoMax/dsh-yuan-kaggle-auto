"""数据准备：定位 train/test/sample_submission，推断 id 与 target，切出本地验证集。

本地验证集是所有自动迭代的地基——线上 LB 一天只能提交几次，CV 却能跑几百次。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import kapi, storage


def _pick(csvs: list[Path], patterns: list[str]) -> Path | None:
    for pat in patterns:
        for p in csvs:
            if pat in p.name.lower():
                return p
    return None


def locate(data_dir: Path) -> dict:
    csvs = sorted(data_dir.rglob("*.csv"))
    if not csvs:
        raise kapi.KaggleError(f"{data_dir} 无 csv")
    train = _pick(csvs, ["train"])
    test = _pick(csvs, ["test"])  # 注意：sample_submission 里也有 test 字样，下面再排除
    sub = _pick(csvs, ["sample_submission", "sample-submission", "submission"])
    # 排除把 sample_submission 误判成 test 的情况
    if test and sub and test == sub:
        test = None
        for p in csvs:
            if "test" in p.name.lower() and p != sub:
                test = p
                break
    if train is None:
        raise kapi.KaggleError(f"{data_dir} 找不到 train*.csv，文件列表: {[p.name for p in csvs]}")
    return {"train": train, "test": test, "sample_sub": sub, "all": [str(p) for p in csvs]}


def infer_schema(train: pd.DataFrame, sample_sub: pd.DataFrame | None,
                 test: pd.DataFrame | None = None) -> dict:
    """用 sample_submission 的结构反推 id 列与 target 列，这是最稳的来源。

    ## 一个实测踩到的赛型：多分类概率提交

    sample_submission 可能形如 `[id, a, b, c]`（每类一列概率），而 train 里的
    标签列叫 `label` —— 三列概率名在 train 里当然找不到。
    早期实现直接把 `cols[1:]` 当 target 列去 train 里查，找不到就抛
    「无法定位标签列」，于是这类比赛根本进不来。

    修复：sample 首列之后的列在 train 里**全都找不到**时，判定为
    "多列输出型提交"（概率矩阵 / one-hot），此时从 train 自行推断标签列，
    并把 sample 的多列结构记进 `output_cols`，供 write-sub 组装。
    """
    output_cols: list[str] = []
    id_in_train = None
    if sample_sub is not None:
        cols = list(sample_sub.columns)
        # ── sample 的首列不一定存在于 train ────────────────────────────────
        #
        # 实测（digit-recognizer）：sample_submission 是 `ImageId,Label`，
        # 但 **train.csv 只有 `label,pixel0..783`，没有 ImageId**。
        # 原代码无脑取 `cols[0]` 当 id_col，随后 `train[id_col]` 直接 KeyError。
        #
        # 正确做法：只有当那一列**确实存在于 train 或 test**时才当 id_col；
        # 否则说明该比赛用测试集行号做 id（digits 就是这种），
        # id_col 应置空并在 write-sub 时按行号生成。
        cand_id = cols[0]
        if cand_id in train.columns or (test is not None and cand_id in test.columns):
            id_col = cand_id
        else:
            id_col = ""          # 无显式 id 列，submission 用行号
            id_in_train = None
        targets = cols[1:] if len(cols) > 1 else []
    else:
        cols = list(train.columns)
        id_col = cols[0]
        targets = [cols[-1]]

    # 多列输出型：sample 的首列之后没有一列出现在 train 里
    matched = [c for c in targets if c in train.columns]
    if targets and not matched:
        output_cols = list(targets)
    elif len(targets) > 1 and len(matched) < len(targets):
        # 部分对上也算多列输出（例如 one-hot 少了一类）
        output_cols = list(targets)

    if len(matched) == 1:
        target = matched[0]
    elif len(matched) > 1:
        # 真正的多列标签（如多目标回归）——目前不支持，交给上层报错
        target = matched[0]
    else:
        # 多列输出型，或名字对不上：从 train 自行推断标签列。
        # 判据：排除 id 与看起来像特征的列，优先取"非数值且基数小"或"整数小基数"的列。
        # 从 train 推断标签列。
        #
        # 实测（digit-recognizer）：sample 的 `Label` 不在 train 里，
        # 于是走这条 fallback。原判据 `1 <= nunique <= 20` 把 **pixel783 选中** ——
        # 它在数据里是常数列（nunique=1），恰好落进区间，结果 `label` 反被当特征。
        #
        # 两条修正：
        #   1. **排除常数（nunique<=1）** —— 常数列不可能是标签
        #   2. 优先"非数值"或"整数且基数适中"的列，其次才退回其它
        cands = [c for c in train.columns if c != id_col]
        pick = None
        # 第一优先：非数值列且基数适中
        for c in cands:
            y = train[c]
            nun = int(y.nunique(dropna=True))
            if nun <= 1 or not nun:
                continue
            if not pd.api.types.is_numeric_dtype(y) and 2 <= nun <= 20:
                pick = c
                break
        # 第二优先：整数列且基数适中（分类标签的常见形态）
        if pick is None:
            for c in cands:
                y = train[c]
                nun = int(y.nunique(dropna=True))
                if nun <= 1:
                    continue
                if pd.api.types.is_numeric_dtype(y) and y.dtype.kind in "iu" and 2 <= nun <= 20:
                    pick = c
                    break
        # 第三优先：任何非常数数值列（回归目标）
        if pick is None:
            for c in cands:
                y = train[c]
                nun = int(y.nunique(dropna=True))
                if nun > 1 and pd.api.types.is_numeric_dtype(y):
                    pick = c
                    break
        target = pick if pick else (cands[-1] if cands else None)

    target_cols = [target] if target else []
    if not target_cols:
        raise kapi.KaggleError(
            f"无法定位标签列：sample_submission 头 {cols[:5]}，train 头 {list(train.columns)[:8]}"
        )

    y = train[target_cols[0]]
    n_unique = int(y.nunique(dropna=True))
    numeric = bool(pd.api.types.is_numeric_dtype(y))
    if not numeric:
        task, kind = "classification", "multiclass" if n_unique > 2 else "binary"
    elif n_unique <= 20 and y.dtype.kind in "iu" and n_unique > 2:
        task, kind = "classification", "multiclass"   # 整数小基数按分类更合理
    elif n_unique == 2:
        task, kind = "classification", "binary"
    else:
        task, kind = "regression", "single"

    return {
        "id_col": id_col,
        "target": target_cols[0],
        "target_cols": target_cols,
        # 提交形态：多列 = 概率矩阵 / one-hot（由 write-sub 按 sample 列结构组装）
        "output_cols": output_cols,
        # `multi_target` 的语义必须是「train 里有多个标签列」（真正多目标回归）。
        # 早期误把它当成「sample 有多列」，于是多分类概率提交被拒。
        # 这里只有 matched（在 train 里找到的 target 列）多于 1 才算。
        "multi_target": len(matched) > 1,
        "task": task,
        "kind": kind,
        "n_classes": n_unique,
        "n_rows": int(len(train)),
        "n_features": int(len(train.columns) - len(target_cols)),
    }


def detect_split_strategy(train: pd.DataFrame, meta: dict) -> dict:
    """决定怎么切验证集 —— 这一步选错，本地 CV 就是假的。

    行业共识（agentic-kaggle-skill 的 cross-validation-and-metrics.md）：
    - 数据里有时间列 → 必须按时间切，随机切会拿到"未来信息"
    - 有高基数实体列（用户/商品/会话）→ 应按组隔离，否则同一实体跨折泄漏
    - 普通表格 → 按标签分层即可

    这里的取舍是有意的：**宁可用更保守的切法，也不要一个虚高的 CV**。
    虚高 CV 会让人在错误的方案上迭代十几轮，比慢一点糟得多。
    """
    cols = [c for c in train.columns if c not in {meta["id_col"], *meta["target_cols"]}]

    # 1) 时间列：名字像时间，或 dtype 是 datetime
    time_like = []
    for c in cols:
        low = c.lower()
        if pd.api.types.is_datetime64_any_dtype(train[c]):
            time_like.append(c)
        elif any(k in low for k in ("date", "time", "datetime", "timestamp", "ymd")) \
                and train[c].nunique() > max(10, len(train) * 0.05):
            time_like.append(c)

    # 2) 实体/分组列：同一实体重复出现就应整组隔离。
    #
    # 判据不是"唯一值很多"，而是"**平均一个实体出现多次**"：
    #   user_id 有 60 个值、500 行 → 每实体 8.3 行 → 典型跨折泄漏，必须按组切
    #   x1 有 500 个值、500 行 → 每实体 1 行 → 随机拆开无害
    # 上一版用 nunique > max(50, n*0.2) 判，会把 60/500 这种真实泄漏场景漏掉。
    #
    # 关键排除：**低基数类别列不是分组**。`segment` 只有 a/b/c/d 四个值、
    # 每值 300 行 —— 按 "每实体行数" 算会命中，但它是普通类别特征，
    # 整组切走等于把 25% 的类别完全排除出训练集（实测 CV 立刻从 0.55 恶化到 0.47）。
    # 真正的分组列（用户/患者/商品/会话）基数通常远大于类别数，用 20 作为下限。
    entity_like = []
    n = len(train)
    id_like_names = {"id", "row_id", "index", "idx", "pk", "uuid", "guid"}
    for c in cols:
        low = c.lower()
        if low in id_like_names or (low.endswith("_id") and train[c].nunique() > n * 0.9):
            continue    # 纯行号不构成分组
        uniq = int(train[c].nunique(dropna=True))
        if uniq < 2:
            continue
        # 低基数 = 普通类别特征，交给 stratified，不做组隔离
        if uniq < 20:
            continue

        # ── 数值列几乎不可能是实体 ID ──────────────────────────────────────
        #
        # 实测真实比赛才暴露：titanic 上 `Age`（年龄）被误判成实体分组列 ——
        #   Age 有 71 个唯一值、891 行 → 每"实体"7.54 行，恰好落在判据里。
        # 于是切分策略变成 `group by Age`：训练集里某些年龄段被整段挖走，
        # CV 完全失真（Titanic 的 CV 应该 ~0.83，按年龄分组会乱掉）。
        #
        # 现实中的实体列是用户/患者/商品/会话 —— 都是**类别或字符串**。
        # 连续数值列（年龄/金额/评分）天然会有重复取值，不能用"重复次数"判定。
        # 唯一例外：整数编码的用户 ID（如 `user=10086`），但它通常名字带 id/uid。
        if pd.api.types.is_numeric_dtype(train[c]):
            low2 = c.lower()
            looks_like_id = (low2.endswith("_id") or low2.endswith("id")
                             or low2 in id_like_names)
            if not looks_like_id:
                continue
        # 分母必须是**非缺失行数**，不是总行数。
        #
        # 实测才暴露：`Cabin`（77% 缺失）用 891/147 = 6.06 → 被判成实体列，
        # 于是 group by Cabin，切完后 train 只剩 26 行、valid 865 行 —— 完全崩坏。
        # 而正确口径 204/147 = 1.39，几乎一行一个实体，根本不该分组。
        # 【坑】`uniq` 是 dropna 后算的，分子却用全部行 —— 两个口径不一致，
        # 缺失越多的列被算得越"重复"。
        n_valid_rows = int(train[c].notna().sum())
        if n_valid_rows < 2:
            continue
        rows_per_entity = n_valid_rows / uniq
        # 每个实体平均出现 ≥2 次，且实体数不太多（否则等于每行一个）
        if rows_per_entity >= 2 and uniq <= n_valid_rows * 0.5:
            entity_like.append((c, uniq, round(rows_per_entity, 2)))

    if time_like:
        return {"strategy": "time", "by": time_like[0],
                "reason": f"检测到时间列 `{time_like[0]}`，按时间切避免未来信息泄漏"}
    if entity_like:
        # 取"每实体行数最多"的列：重复越密，跨折泄漏越重
        col, uniq, rpe = max(entity_like, key=lambda t: t[2])
        return {"strategy": "group", "by": col,
                "reason": f"`{col}` 有 {uniq} 个取值、平均每实体 {rpe} 行，"
                          f"按组隔离避免同一实体跨折泄漏"}
    task = meta.get("task")
    kind = meta.get("kind")
    # 分类任务一律分层，不只是二分类。
    #
    # 实测踩到：早期只对 binary 分层，多分类落到 random 分支。
    # 造了一个 3 类 fixture（low 54.6% / mid 24.2% / high 21.2%），
    # random 切分后验证集变成 low 58.3% / high 26.7% / **mid 15%** ——
    # 少数类被抽少，于是 valid_score 0.9333 与 cv_mean 0.9708 差 3.8 个点。
    # 这个差距看起来像"模型不稳"，实际是切分没分层。
    if task == "classification":
        n_cls = meta.get("n_classes")
        label = "二分类" if kind == "binary" else f"{n_cls} 分类" if n_cls else "分类"
        return {"strategy": "stratified", "by": meta["target"],
                "reason": f"{label}按标签分层，保持各类别比例一致"}
    return {"strategy": "random", "by": None, "reason": "普通表格，随机切分"}


def prepare(slug: str, valid_frac: float = 0.2, seed: int = 42, local_dir: str | None = None,
            use_skill: bool = True, split: str | None = None) -> dict:
    """use_skill=True 时先问 skill 要官方指标，拿不到再退回本地推断。

    split 可强制指定 time / group / stratified / random；默认自动检测。
    """
    from . import skillbridge

    data_dir = Path(local_dir) if local_dir else kapi.download(slug)
    paths = locate(data_dir)

    # ── id 列必须在 read_csv 阶段就按字符串读 ────────────────────────────────
    #
    # 实测踩坑：`pd.read_csv` 会把 "00123" 推断成 int64 → 变 123，
    # 之后 `astype(str)` **救不回来**（零已经没了，不是格式问题而是数据丢了）。
    # 后果：整条链路带着错的 id，最终 submission 与 sample_submission 对不上，
    # Kaggle 判 "id 不匹配"，而本地校验可能全部通过。
    #
    # 所以：先只读表头拿到列名 → 用 sample_submission 的第一列判定哪个是 id
    # → 再用 dtype={id: str} 重读三个文件。
    probe = pd.read_csv(paths["train"], nrows=0)
    id_name = None
    if paths["sample_sub"]:
        sub_cols = list(pd.read_csv(paths["sample_sub"], nrows=0).columns)
        if sub_cols and sub_cols[0] in probe.columns:
            id_name = sub_cols[0]
    if id_name is None and len(probe.columns):
        id_name = probe.columns[0]

    str_dtype = {id_name: str} if id_name else None
    train = pd.read_csv(paths["train"], low_memory=False, dtype=str_dtype)
    # test 也提前读进来 —— `infer_schema` 需要用它判断 sample 首列是否真的
    # 存在于数据里（digit-recognizer 的 ImageId 只出现在 test/sample，不在 train）。
    test = (pd.read_csv(paths["test"], low_memory=False, dtype=str_dtype)
            if paths.get("test") else None)
    sample_sub = (pd.read_csv(paths["sample_sub"], dtype=str_dtype)
                  if paths["sample_sub"] else None)

    schema = infer_schema(train, sample_sub, test)
    id_col = schema["id_col"]
    if id_col != id_name:
        # 推断出的 id 与预判不一致时，也保证它是字符串
        if id_col and id_col in train.columns:
            train[id_col] = train[id_col].astype(str)
        if sample_sub is not None and id_col in sample_sub.columns:
            sample_sub[id_col] = sample_sub[id_col].astype(str)

    if schema["multi_target"]:
        # 注意：现在的 `multi_target` 指「train 里有多个标签列」（真正的多目标），
        # 不再指「sample_submission 有多列」。后者是**多列输出型**（概率矩阵/one-hot），
        # 本工具支持：target 仍是单列，write-sub 按 sample 的列结构组装。
        # 实测踩到过这个混淆：多分类概率提交被当成"多列标签"直接拒掉。
        raise kapi.KaggleError(
            "多列标签（train 里有多个 target 列，如多目标回归）暂未支持。"
            f"已识别 target={schema.get('target')}，"
            f"sample 输出列={schema.get('output_cols')}。"
        )

    # 官方比赛事实优先：指标猜错会让整轮迭代朝反方向优化。
    comp: dict = {}
    if use_skill and skillbridge.available()["vendored"]:
        try:
            comp = skillbridge.profile(slug)
        except Exception as e:
            comp = {"skill_error": str(e)[:400]}

    # 切分策略：决定 CV 是否可信
    split_info = detect_split_strategy(train, schema)
    if split:
        split_info["strategy"] = split
        split_info["forced"] = True

    strategy = split_info["strategy"]
    by = split_info["by"]
    n = len(train)
    n_valid = max(1, int(n * valid_frac))

    if strategy == "time" and by in train.columns:
        order = train.sort_values(by).index.to_numpy()
        valid_idx, train_idx = order[-n_valid:], order[:-n_valid]
    elif strategy == "group" and by in train.columns:
        # 整组进验证集：同一实体不会同时出现在两边
        rng = np.random.default_rng(seed)
        groups = train[by].astype(str)
        uniq = groups.unique()
        rng.shuffle(uniq)
        chosen, acc = [], 0
        for g in uniq:
            if acc >= n_valid:
                break
            chosen.append(g)
            acc += int((groups == g).sum())
        mask = groups.isin(chosen)
        valid_idx = train.index[mask].to_numpy()
        train_idx = train.index[~mask].to_numpy()
    elif strategy == "stratified" and by in train.columns:
        from sklearn.model_selection import train_test_split
        train_idx, valid_idx = train_test_split(
            train.index.to_numpy(), test_size=valid_frac,
            random_state=seed, stratify=train[by])
    else:
        rng = np.random.default_rng(seed)
        idx = rng.permutation(n)
        valid_idx, train_idx = idx[:n_valid], idx[n_valid:]

    if len(train_idx) == 0 or len(valid_idx) == 0:
        raise kapi.KaggleError(
            f"切分失败：train={len(train_idx)} / valid={len(valid_idx)}（策略 {strategy}，by={by}）")

    work = kapi.WORK_DIR / slug
    work.mkdir(parents=True, exist_ok=True)
    train.iloc[train_idx].to_csv(work / "train.csv", index=False)
    train.iloc[valid_idx].to_csv(work / "valid.csv", index=False)

    test_path = paths["test"]
    if test_path:
        # 公开 test 无标签，直接把原文件挂过来供 agent 读。
        # id 列与 train/sample_submission 一样必须字符串读入，
        # 否则三者只要有一处丢前导零，最终 submission 就对不上。
        df_test = pd.read_csv(test_path, low_memory=False, dtype=str_dtype)
        for c in dict.fromkeys([id_col, id_name]):
            if c and c in df_test.columns and df_test[c].dtype != object:
                df_test[c] = df_test[c].astype(str)
        df_test.to_csv(work / "test.csv", index=False)

    if sample_sub is not None:
        sample_sub.to_csv(work / "sample_submission.csv", index=False)

    declared = comp.get("kauto_metric")
    meta = {
        "slug": slug,
        **schema,
        "valid_frac": valid_frac,
        "seed": seed,
        "split_strategy": split_info,
        "n_train_rows": int(len(train_idx)),
        "n_valid_rows": int(len(valid_idx)),
        "data_dir": str(data_dir),
        "work_dir": str(work),
        "has_public_test": test_path is not None,
        # skill 提供的官方事实；缺失时字段为空，由 baseline 回退到本地推断
        "metric_declared": declared,
        "metric_source": "kaggle-skill" if declared else "local-inference",
        "official_metric_raw": comp.get("raw_metric"),
        "official_higher_is_better": comp.get("higher_is_better"),
        "notebook_only": comp.get("notebook_only"),
        "max_daily_submissions": comp.get("max_daily_submissions"),
        "competition": {k: comp.get(k) for k in
                        ("title", "url", "deadline", "team_count", "tags", "evaluation")} if comp else {},
        "skill_error": comp.get("skill_error"),
    }
    work.mkdir(parents=True, exist_ok=True)

    # ── 切分指纹：防止重新切分后旧产物被当成有效 ──────────────────────────────
    #
    # 实测问题：重复跑 prepare（换了 seed / valid_frac）会重新切分数据，
    # 而 baseline_oof.npy、best_*.npy、archive/* 全是基于**旧切分**算出来的。
    # 两者混用会得到：看起来正常、实际对不上的产物 —— 而且没有任何报错。
    #
    # 做法：把 train/valid 的指纹写进 meta；下次 prepare 发现指纹变了，
    # 就**清掉所有派生产物**并告警。宁可重跑，也不要拿错位的产物。
    import hashlib

    def _fp(p: Path) -> str | None:
        try:
            return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        except OSError:
            return None

    new_fp = {"train": _fp(work / "train.csv"), "valid": _fp(work / "valid.csv")}

    old_meta_p = work / "meta.json"
    old_fp = None
    if old_meta_p.exists():
        try:
            old_fp = json.loads(old_meta_p.read_text(encoding="utf-8")).get("split_fingerprint")
        except Exception:
            old_fp = None

    stale = []
    if old_fp and old_fp != new_fp:
        # 切分变了：旧的派生产物全部失效
        derived = [
            "baseline.json", "baseline_oof.npy",
            "best_fe.py", "best_valid_pred.npy", "best_test_pred.npy", "best_node.txt",
            "blend.json", "blend_test_pred.npy",
            "iterate.json", "journal.json",
            "submission.csv", "submission_provenance.json",
        ]
        for name in derived:
            f = work / name
            if f.exists():
                try:
                    f.unlink()
                    stale.append(name)
                except OSError:
                    pass
        # archive 与 gen_* 目录也基于旧切分
        import shutil as _sh
        for pat, is_dir in (("archive", True), ("gen_*", True)):
            for d in work.glob(pat):
                try:
                    _sh.rmtree(d, ignore_errors=True)
                    stale.append(d.name)
                except Exception:
                    pass

        print(f"[kauto] ⚠ 切分变了（seed/valid_frac 与上次不同），已清理 {len(stale)} 项"
              f"旧派生产物：{', '.join(stale[:6])}{'…' if len(stale) > 6 else ''}",
              file=__import__("sys").stderr)
        print("        这些产物基于旧切分，混用会得到对不上的结果。需要重跑 baseline。",
              file=__import__("sys").stderr)

    meta["split_fingerprint"] = new_fp
    meta["stale_cleared"] = stale

    storage.write_json(work / "meta.json", meta)

    # notebook-only 比赛：只能提交 notebook，不能提交 csv。
    #
    # 早期只是把 `notebook_only` 记进 meta，**没有任何拦截** ——
    # 于是这类比赛会正常下载数据、跑完 baseline/iterate/blend/write-sub，
    # 直到 submit 那一刻才发现根本交不上去，前面全白跑。
    # 这里在 prepare 阶段就明示，让人早点换赛。
    if meta.get("notebook_only"):
        print("[kauto] ⚠ 这场比赛是 notebook-only：只能提交 notebook，不能提交 csv。",
              file=__import__("sys").stderr)
        print("        本工具的产物（submission.csv）无法直接提交。"
              "建议换一场普通比赛，或改用 notebook 工作流。",
              file=__import__("sys").stderr)
        meta["submittable"] = False
        meta["warn"] = "notebook_only：csv submission 无法提交"
    else:
        meta["submittable"] = True

    return meta


def load_work(slug: str) -> dict:
    """读 meta.json，并把其中过期的绝对路径修正为**当前**可写根。

    ## 为什么必须修正 `work_dir`

    `prepare` 会把 `work_dir` 写成当时的绝对路径。这个值一旦落盘就固定在
    文件里，而这带来两个真实问题：

    1. **插件包分发**：`engine/work/<slug>/meta.json` 随 npm 包发布，
       里面写的是**打包者机器**的路径（`D:\\mumu\\kaggle-auto\\work\\...`）。
       别人装上后 `work_dir` 指向一个不存在的目录 —— 夹具与回归测试全部失效。

    2. **`KAUTO_ROOT` 重定向**：宿主插件按用户设置注入可写根，测试也会用
       临时根隔离现场。但 meta 里的旧路径会让 `iterate` / `journal` 等
       仍然写回**原目录** —— 隔离失效，测试污染真实工作区
       （本轮实测踩到：用临时根跑一次，真实夹具的 journal 从 5 个节点变成 7 个）。

    判据：只要 meta 里的 `work_dir` 不等于"当前 `KAUTO_ROOT` 下的 work/<slug>"，
    就以当前根为准。用户在工作区里正常跑出来的路径本来就和该值相等，
    所以修正是幂等的、对正常使用零影响。

    `data_dir` 同理：相对路径按包根解析；绝对路径且不存在时保持原值，
    交给调用方报错（不掩盖真实缺失）。
    """
    work = kapi.WORK_DIR / slug
    meta_path = work / "meta.json"
    if not meta_path.exists():
        raise kapi.KaggleError(f"未准备数据：先跑 prepare（缺 {meta_path}）")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    recorded = str(meta.get("work_dir") or "")
    if recorded and Path(recorded) != work:
        meta["work_dir"] = str(work)
        meta["work_dir_relocated_from"] = recorded

    data_dir = str(meta.get("data_dir") or "")
    if data_dir and not Path(data_dir).is_absolute():
        meta["data_dir"] = str(kapi.ROOT / data_dir)

    return meta
