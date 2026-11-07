"""多方案融合（blend）。

为什么需要：迭代会产出多个达到相近 CV 的方案，它们各自的错判并不相同，
简单加权平均常常比任何单个方案都稳。MLE-STAR 专门有一个 ensemble 子 agent
做这件事，Kaggle 高分方案也几乎都在做。

我做的取舍：**不学 MLE-STAR 让 LLM 写融合代码**。
理由是每个候选只暴露了 valid/test 预测，重新让 LLM 跑一遍融合等于又花 6 分钟
且可能引入新 bug；而这批预测已经在手，用确定性的算术搜权重又快又可验证。

方法：在 OOF 上做贪心前向选择（Caruana ensemble selection 的简化版）——
每次挑一个"加入后 OOF 变最好"的方案（可重复挑，等价于加权），
权重就是被挑中的次数。全程只在 OOF 上拟合，避免用验证集调权重导致虚高。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import prep, storage
from .model import higher_is_better, needs_proba, score


def collect_candidates(slug: str, min_score: float | None = None) -> list[dict]:
    """收集有 OOF 的方案。只有 baseline 有 OOF；候选方案目前只存 valid/test 预测。

    这是一处已知限制：候选方案走的是"单次留出集"评估，没有 K 折 OOF，
    所以它们不能安全参与权重拟合。这里退一步用两个来源：
      1. baseline 的 OOF（可信，可拟合权重）
      2. 各 good node 的 valid_pred（留出集，仅用于"是否值得融合"的初筛）
    真正高质量的多方案 blend 需要让候选也产出 OOF，见 README 的待办。
    """
    meta = prep.load_work(slug)
    work = Path(meta["work_dir"])
    cands: list[dict] = []

    oof_path = work / "baseline_oof.npy"
    if oof_path.exists():
        cands.append({
            "id": "baseline", "kind": "oof",
            "pred": storage.load_npy(oof_path),
            "test": None,
        })

    jr = work / "journal.json"
    scored_ids: set[str] = set()
    if jr.exists():
        d = json.loads(jr.read_text(encoding="utf-8"))
        for n in d.get("nodes") or []:
            if n.get("score") is not None:
                scored_ids.add(n["id"])

    # 直接扫 gen_* 目录，而不是只认 journal 里的 id。
    # 理由：journal 只记录"本次迭代"产生的节点；跨次运行、手工补的候选、
    # 或者被 resume 覆盖掉的 node 都可能只剩产物没有记录。
    # 产物在就是可融合的，不该因为 journal 里没登记就丢掉。
    #
    # 优先用 oof.npy（K 折折外预测）：用它拟合权重才是对的。
    # 没有 OOF 的候选退回 valid_pred，但那是单次留出集，
    # 在留出集上拟合权重会拟合噪声 —— 这种候选会被标注 pred_kind="valid"，
    # 由调用方决定是否降低信任度。
    for g in sorted(work.glob("gen_*")):
        nid = g.name[len("gen_"):]
        # `gen_oof_<id>` 是 `kauto oof` 的**中间产物**，不是独立候选 ——
        # 它会被解析成 id `oof_<id>`，与归档里的 `<id>` 重复出现在池里。
        # 实测：titanic 上补了 OOF 后，池里出现 `oof_fa7eba7956` 与 `fa7eba79563a`
        # 两条同源候选，blend 误以为有两个候选。
        if nid.startswith("oof_") or nid.startswith("timing_") or nid.startswith("to_"):
            continue
        tp = g / "test_pred.npy"
        if not tp.exists():
            continue
        oof, vp = g / "oof.npy", g / "valid_pred.npy"
        if oof.exists():
            pred, kind = storage.load_npy(oof), "oof"
        elif vp.exists():
            pred, kind = storage.load_npy(vp), "valid"
        else:
            continue
        if any(c["id"] == nid for c in cands):
            continue
        cands.append({
            "id": nid, "kind": kind,
            "pred": pred,
            "test": storage.load_npy(tp),
            "cv": None,
        })

    # 归档节点也纳入融合池。
    #
    # 早期只扫 gen_*，于是「产物被后续运行覆盖、但已归档」的节点
    # 明明留着完整产物，却永远进不了融合（实测：11 个归档节点全部缺席）。
    # 归档的意义本来就是"只增不删、可复现"，不拿它参与融合等于白存。
    # 优先 oof.npy，没有就退 valid_pred（kind 会标出来，调用方据此判断可信度）。
    for a in sorted(work.glob("archive/*")):
        nid = a.name
        if not a.is_dir():
            continue
        # ── 重复 id 时优先归档 ──────────────────────────────────────────────
        #
        # 实测缺陷：`gen_<nid>/` 里只有 valid_pred（没有 oof.npy），
        # 而归档目录里补过 OOF。早期这里是 `if any(c["id"] == nid ...): continue`
        # —— 于是 gen_* 先占位，归档的 OOF **被整个跳过**，融合池里只剩 valid 口径。
        #
        # 处理：同 id 已存在时，**如果归档有 OOF 而现有条目没有，就替换掉**。
        _oof, _vp = a / "oof.npy", a / "valid_pred.npy"
        _dup_idx = next((i for i, c in enumerate(cands) if c["id"] == nid), None)
        if _dup_idx is not None and not _oof.exists():
            continue        # 归档里也没 OOF，保留原有那条（gen_* 的）
        if _dup_idx is not None:
            cands.pop(_dup_idx)     # 归档有 OOF，替换掉 gen_* 那条
        tp = a / "test_pred.npy"
        if not tp.exists():
            continue
        if _oof.exists():
            pred, kind = storage.load_npy(_oof), "oof"
        elif _vp.exists():
            pred, kind = storage.load_npy(_vp), "valid"
        else:
            continue
        cands.append({
            "id": nid, "kind": kind,
            "pred": pred,
            "test": storage.load_npy(tp),
            "cv": None,
        })

    # ── 内容去重 ─────────────────────────────────────────────────────────────
    #
    # 实测问题：11 个归档节点的 test_pred **指纹完全相同**（9b5914872e ×11），
    # 而 archive_node 只按 node_id 去重、不看内容。于是一模一样的预测占满融合池，
    # 把 top_k 名额挤掉，还让"候选数"这个数字失去意义。
    #
    # 这里按「预测内容指纹」去重，保留优先 oof 的那份。
    # 用 numpy 的 bytes 做指纹而不是哈希值：同样的数组内容 -> 同样的 bytes。
    deduped: list[dict] = []
    seen: dict[str, dict] = {}
    for c in cands:
        import hashlib as _h
        arr = np.asarray(c["pred"], dtype=float).ravel()
        fp = _h.sha256(arr.tobytes()).hexdigest()[:16]
        prev = seen.get(fp)
        if prev is None:
            seen[fp] = c
            deduped.append(c)
        else:
            # 同内容：优先保留 oof（权重拟合更可信）
            if c["kind"] == "oof" and prev["kind"] != "oof":
                idx = deduped.index(prev)
                c["cv"] = prev.get("cv")
                c["duplicate_of"] = None
                deduped[idx] = c
                seen[fp] = c
            else:
                prev.setdefault("duplicates", []).append(c["id"])

    return deduped


def blend(slug: str, max_iter: int = 30, top_k: int = 6) -> dict:
    """在留出集上做贪心前向融合，产出 blend_submission。

    诚实说明：因为候选方案只有留出集预测、没有 K 折 OOF，这里的权重是在
    **留出集上**拟合的，因此最终的 blend CV 会略偏乐观。报告里明确标注这一点，
    不把它当成可以无条件相信的分数。
    """
    meta = prep.load_work(slug)
    work = Path(meta["work_dir"])
    metric = None
    bl = work / "baseline.json"
    if bl.exists():
        metric = json.loads(bl.read_text(encoding="utf-8"))["metric"]
    if metric is None:
        raise RuntimeError("先跑 baseline")

    cands = collect_candidates(slug, min_score=None)

    # OOF 候选的标签是 train+valid 全量；留出集候选只有 valid 的标签。
    # 两套口径不能混在一张表里拟合权重，所以**按口径分组**：
    # 只要有一个 OOF 候选就整组用 OOF（继续用留出集候选会污染权重）。
    oof_cands = [c for c in cands if c["kind"] == "oof"]
    valid_cands = [c for c in cands if c["kind"] == "valid"]
    use_oof = bool(oof_cands)
    pool = oof_cands if use_oof else valid_cands
    if not pool:
        return {"ok": False, "reason": "没有可融合的候选方案（需要至少一个跑通的节点）"}

    if use_oof:
        tr = pd.read_csv(work / "train.csv", low_memory=False)
        va = pd.read_csv(work / "valid.csv", low_memory=False)
        y_raw = pd.concat([tr[meta["target"]], va[meta["target"]]], ignore_index=True)
    else:
        va = pd.read_csv(work / "valid.csv", low_memory=False)
        y_raw = va[meta["target"]]

    if meta["task"] == "classification":
        from .model import encode_y
        if use_oof:
            tr = pd.read_csv(work / "train.csv", usecols=[meta["target"]], low_memory=False)
            _, _, cats = encode_y(tr[meta["target"]], y_raw)
        else:
            tr = pd.read_csv(work / "train.csv", usecols=[meta["target"]], low_memory=False)
            _, _, cats = encode_y(tr[meta["target"]], y_raw)
        y = y_raw.map({c: i for i, c in enumerate(cats)})
    y = np.asarray(y)

    # ── 长度对齐：OOF 与 valid 预测覆盖的样本集不同 ─────────────────────────
    #
    # 实测真实比赛才暴露：titanic 上
    #   baseline 的 oof 长度 = 712（**只覆盖 train**，因为它只用 train 拟合）
    #   归档候选的 valid 长度 = 179（**只覆盖 valid**）
    #   标签（train+valid）总长 = 891
    # 于是"长度必须等于 891"这条判据把**所有候选都剔掉**，blend 永远无法工作。
    #
    # 正确做法：OOF 候选与 valid 候选各自配对**对应的标签子集**评分，
    # 而不是要求它们覆盖全集。融合权重仍只在 OOF 上拟合（那是唯一无偏口径），
    # 但 valid 候选可以参与"可用单模"的基线比较。
    n_train = len(tr) if use_oof else 0
    # 重新算出两种标签长度
    y_oof = None
    y_valid = None
    if use_oof:
        _tr = pd.read_csv(work / "train.csv", usecols=[meta["target"]], low_memory=False)
        _va = pd.read_csv(work / "valid.csv", usecols=[meta["target"]], low_memory=False)
        y_oof_raw = _tr[meta["target"]]
        y_valid_raw = _va[meta["target"]]
        if meta["task"] == "classification":
            from .model import encode_y
            _, _, _cats = encode_y(_tr[meta["target"]], pd.concat(
                [y_oof_raw, y_valid_raw], ignore_index=True))
            _map = {c: i for i, c in enumerate(_cats)}
            y_oof = np.asarray(y_oof_raw.map(_map))
            y_valid = np.asarray(y_valid_raw.map(_map))
        else:
            y_oof = np.asarray(y_oof_raw, dtype=float)
            y_valid = np.asarray(y_valid_raw, dtype=float)

    def _labels_for(kind: str):
        if kind == "oof":
            return y_oof if y_oof is not None else y
        return y_valid if y_valid is not None else y

    usable = []
    for c in pool:
        arr = np.asarray(c["pred"]).ravel()
        lab = _labels_for(c["kind"])
        if arr.shape[0] == lab.shape[0]:
            c["_y"] = lab
            usable.append(c)
    if not usable:
        return {"ok": False,
                "reason": f"没有候选的预测长度能对齐标签（pool={len(pool)}，"
                          f"标签 oof={None if y_oof is None else y_oof.shape[0]} "
                          f"valid={None if y_valid is None else y_valid.shape[0]}）"}

    # 预排序：单模型表现好的优先（只影响贪心的起点，不影响最终权重）
    hib = higher_is_better(metric)
    for c in usable:
        # 用该候选自己的标签子集（oof 配 train 标签、valid 配 valid 标签）
        _lab = c.get("_y", y)
        c["single"] = float(score(metric, _lab, np.asarray(c["pred"]).ravel()))
    usable.sort(key=lambda c: c["single"], reverse=hib)
    usable = usable[:top_k]

    # ── 相关性过滤（Step 2）─────────────────────────────────────────────────
    # 参照 agentic-kaggle-skill 的 08-ensemble-safety-patterns：
    # "if correlation > 0.90 between any pair → exclude the weaker one"
    #
    # 为什么必须做：实测过 4 个只换随机种子的 LightGBM，融合后与最优单模同分（增益 0）；
    # 也实测过异构候选融合有 -0.017 的真实收益。差别就在相关性。
    # 提前剔除高度相关的成员，能避免它们在贪心里占据名额、挤掉真正互补的候选。
    dropped_corr = []
    if len(usable) > 1:
        keep = [0]
        for i in range(1, len(usable)):
            p = np.asarray(usable[i]["pred"]).ravel().astype(float)
            too_similar = False
            for j in keep:
                q = np.asarray(usable[j]["pred"]).ravel().astype(float)
                if p.std() < 1e-12 or q.std() < 1e-12:
                    continue
                corr = float(np.corrcoef(p, q)[0, 1])
                if abs(corr) > 0.90:
                    too_similar = True
                    dropped_corr.append({"id": usable[i]["id"], "with": usable[j]["id"],
                                         "corr": round(corr, 4)})
                    break
            if not too_similar:
                keep.append(i)
        usable = [usable[i] for i in keep]

    # 相关性过滤把候选压到只剩一个 —— 这时融合在数学上不可能有收益
    # （就一个成员，"平均"等于它自己）。早期会继续跑一遍贪心/权重扫描，
    # 最后得出 gain=0 的结论并清掉产物，但**没说明为什么**。
    # 直接给出原因和可行动建议，比让人对着 gain=0 猜要有用。
    if len(usable) < 2:
        detail = []
        for d in dropped_corr:
            detail.append(f"{d['id']} 与 {d['with']} 相关 {d['corr']}")
        msg = ("可用候选不足 2 个：相关性与最优单模均 >0.90，融合无意义")
        if detail:
            msg += "（" + "；".join(detail[:4]) + "）"
        return {
            "ok": False,
            "reason": msg,
            "metric": metric,
            "best_single": round(float(usable[0]["single"]), 6) if usable else None,
            "dropped_high_corr": dropped_corr,
            "advice": ("这批候选彼此太像。要融合有用，迭代时应当换模型族"
                       "（随机森林 / 直方图 GBDT / 线性 / 核方法），"
                       "或先用 --folds-for-oof 让候选产出 OOF 再融合"),
        }

    preds = [np.asarray(c["pred"]).ravel().astype(float) for c in usable]
    picks: list[int] = []
    best_sum = np.zeros_like(preds[0])
    best_score = -np.inf if hib else np.inf
    history = []

    for step in range(max_iter):
        trial = None
        for i, p in enumerate(preds):
            cand_sum = best_sum + p
            cand_avg = cand_sum / (len(picks) + 1)
            s = float(score(metric, _ylab, cand_avg))
            improved = (s > best_score + 1e-9) if hib else (s < best_score - 1e-9)
            if improved and (trial is None or
                             (s > trial[1] if hib else s < trial[1])):
                trial = (i, s, cand_sum)
        if trial is None:
            break
        i, s, cand_sum = trial
        picks.append(i)
        best_sum = cand_sum
        best_score = s
        history.append({"step": step + 1, "pick": usable[i]["id"], "score": round(s, 6)})

    # 纯贪心平均不够：实测过 4 个分数相近的候选，等权平均（0.3276）
    # 反而差于最优单模（0.3195）。分数相近时"平均"会稀释掉强模型。
    # 所以再做一轮**权重扫描**：给每个候选一个非负整数权重，用坐标上升搜，
    # 只在 OOF/留出集上真正变好才接受。这等价于允许"偏向某个强模型"。
    weight_scan = None
    if len(preds) >= 2:
        grid = [0, 1, 2, 3, 5, 8]
        w = [0] * len(preds)
        w[0] = 1                      # 从最优单模起步
        cur = preds[0].copy()
        cur_score = float(score(metric, _ylab, cur))
        improved_any = True
        while improved_any:
            improved_any = False
            for i in range(len(preds)):
                base_score = cur_score
                best_local = None
                for g in grid:
                    if g == w[i]:
                        continue
                    trial_sum = cur - preds[i] * w[i] + preds[i] * g
                    tot = sum(w) - w[i] + g
                    if tot <= 0:
                        continue
                    s = float(score(metric, _ylab, trial_sum / tot))
                    better = (s > base_score + 1e-9) if hib else (s < base_score - 1e-9)
                    if better and (best_local is None or
                                   (s > best_local[1] if hib else s < best_local[1])):
                        best_local = (g, s, trial_sum, tot)
                if best_local is not None:
                    g, s, trial_sum, tot = best_local
                    w[i] = g
                    cur = trial_sum
                    cur_score = s
                    improved_any = True
        nonneg = [x for x in w if x > 0]
        if nonneg and cur_score != -np.inf:
            tot = sum(w)
            weight_scan = {
                "weights": {usable[i]["id"]: round(w[i] / tot, 4)
                            for i in range(len(w)) if w[i] > 0},
                "score": round(float(cur_score), 6),
                "combined": cur,
                "total": tot,
            }

    if not picks and weight_scan is None:
        return {"ok": False, "reason": "融合没有带来提升，保持单模型"}

    # 选更优的那一种（贪心平均 vs 权重扫描）
    if picks:
        greedy_weights = {}
        for i in picks:
            greedy_weights[usable[i]["id"]] = greedy_weights.get(usable[i]["id"], 0) + 1
        greedy_total = sum(greedy_weights.values())
        greedy_norm = {k: v / greedy_total for k, v in greedy_weights.items()}
        greedy_score = float(best_score)
    else:
        greedy_norm, greedy_score = {}, (-np.inf if hib else np.inf)

    if weight_scan and (
        (weight_scan["score"] > greedy_score) if hib else (weight_scan["score"] < greedy_score)
    ):
        weights, total, final_score = weight_scan["weights"], weight_scan["total"], weight_scan["score"]
        method = "weight-scan"
        # weight_scan 的 combined 是加权和，需要除 total
        final_pred = weight_scan["combined"] / weight_scan["total"]
    else:
        weights, total, final_score = greedy_norm, 1.0, greedy_score
        method = "greedy-average"
        final_pred = best_sum / max(len(picks), 1)

    # 用同样的权重合成 test 预测（weights 的键是候选 id，不是索引）
    by_id = {c["id"]: c for c in usable}
    # ── 最终验收门（Step 6）──────────────────────────────────────────────────
    # 「Only submit if blend beats best single source on validation」
    #
    # 之前的漏洞：融合结果只要比**起点**好就会写盘，
    # 而起点是"第一个被选中的候选"，未必是最优单模。
    # 于是可能写出一个比最优单模还差的 blend_test_pred.npy，
    # 下游 write-sub 却会用它 —— 用更差的预测去提交。
    best_single = float(usable[0]["single"])
    gain = float(final_score) - best_single
    better_than_single = (gain > 0) if hib else (gain < 0)

    if not better_than_single:
        # 铺陈上的关键一步：**把旧的融合产物清掉**。
        #
        # 实测踩到的坑：上一次 blend 成功时写了 blend.json(ok=true) +
        # blend_test_pred.npy；这一次被验收门拒绝，只返回了 ok=false 但没清旧文件。
        # 于是 write-sub 读到的仍是旧 blend.json 的 ok=true —— **拿着被拒的融合去提交**，
        # 而且从返回值和文件上都看不出异常。
        stale_pred = work / "blend_test_pred.npy"
        stale_meta = work / "blend.json"
        for f in (stale_pred, stale_meta):
            try:
                if f.exists():
                    f.unlink()
            except OSError:
                pass

        return {
            "ok": False,
            "reason": "融合未超过最优单模，不建议使用融合结果（旧融合产物已清除）",
            "metric": metric,
            "blend_score": round(float(final_score), 6),
            "best_single": round(best_single, 6),
            "gain_vs_best_single": round(gain, 6),
            "weights": weights,
            "dropped_high_corr": dropped_corr,
            "stale_cleared": [str(stale_pred), str(stale_meta)],
            "advice": "直接用最优单模：kauto restore <slug> --node <该节点 id>",
        }

    test_preds, test_w = [], []
    for cid, w in weights.items():
        t = by_id[cid].get("test")
        if t is None:
            continue
        test_preds.append(np.asarray(t).ravel().astype(float))
        test_w.append(w)
    if not test_preds:
        return {"ok": False, "reason": "候选缺少 test 预测"}

    test_blend = np.zeros_like(test_preds[0])
    for p, w in zip(test_preds, test_w):
        test_blend += p * w
    test_blend /= sum(test_w)

    storage.save_npy(work / "blend_test_pred.npy", test_blend)
    # 融合预测同样要盖特征指纹 —— 它也是 write-sub 的候选来源，
    # 也会因为特征集变化而变成陈旧产物。
    try:
        from .iter import _stamp_features_fingerprint
        _stamp_features_fingerprint(work, "blend_test_pred.npy")
    except Exception:
        pass
    result = {
        "ok": True,
        "metric": metric,
        "method": method,
        "pred_kind": "oof" if use_oof else "valid",
        "blend_score_on_oof" if use_oof else "blend_score_on_valid":
            round(float(final_score), 6),
        "best_single": round(float(usable[0]["single"]), 6),
        "gain_vs_best_single": round(
            float(final_score) - float(usable[0]["single"]), 6),
        "caveat": (
            "权重在 K 折 OOF 上拟合，该分数是可信的泛化估计"
            if use_oof else
            "权重在单次留出集上拟合，该分数偏乐观；候选若产出 OOF（--folds 5）可消除此偏差"
        ),
        "weights": weights,
        "members": {c["id"]: round(c["single"], 6) for c in usable},
        "dropped_high_corr": dropped_corr,
        "greedy_steps": history,
        "test_pred_saved": str(work / "blend_test_pred.npy"),
    }
    storage.write_json(work / "blend.json", result)
    try:
        from . import manifest
        manifest.write_manifest(work, slug=slug, stage="blend",
                                extra={"pred_kind": result.get("pred_kind"),
                                       "method": method,
                                       "weights": result.get("weights")})
    except Exception:
        pass
    return result
