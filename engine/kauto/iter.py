"""自动迭代：LLM 读 CV 报告 → 写特征工程/模型 → 本地跑 → 只留提升版本。

三层保险：
1. 候选代码在独立子进程里跑，超时/崩溃只丢一轮
2. 只准改 build_features / fit_predict，其余（切分/编码/评分）都是确定性代码
3. 只按本地 CV 判定，绝不消耗线上提交配额
"""
from __future__ import annotations

import json
import os
import random
import shutil
import textwrap
import time
import urllib.error
import urllib.request
import http.client
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import _runner, archive, leakcheck, prep, storage
from .journal import Journal, Node
from .kapi import KaggleError


def _stamp_features_fingerprint(work: Path, pred_name: str) -> None:
    """给预测文件写一份 sidecar，记下"用哪套特征算的"。

    ## 这个 sidecar 是为了解决一次真实的静默错误

    实测：把频次编码的退化判据修好之后重跑 `baseline`（特征 10 列 → 7 列），
    再跑 `write-sub` —— 它复用 `best_test_pred.npy`（旧特征集算的），
    产出的 submission.csv sha256 与上一次提交**完全一致**（1e563a50…），
    分数自然还是 0.76076。命令都成功了，日志也写了溯源，
    但"重跑了 baseline 再写提交"这件事实际上什么都没发生。

    根因：预测文件只记"哪个节点算的"，不记"用哪些特征算的"。
    现在写入侧盖戳、读取侧（`submission_from_best`）比对，不一致就拒绝复用。

    指纹来源是 `baseline.json.features_fingerprint`；取不到就写 null，
    此时读取侧也不做拦截（宁可放过，也不因缺字段误报陈旧）。
    """
    fp = None
    bl = work / "baseline.json"
    if bl.exists():
        try:
            fp = json.loads(bl.read_text(encoding="utf-8")).get("features_fingerprint")
        except Exception:
            fp = None
    try:
        storage.write_json(
            work / f"{pred_name}.meta.json",
            {"features_fingerprint": fp, "written_at": time.time()},
        )
    except Exception:
        pass   # sidecar 只是诊断信息，写不进去不该让迭代失败

DRAFT_PROMPT = """你是 Kaggle 竞赛的建模工程师。比赛：{slug}
任务：{task}（{kind}），标签列 `{target}`，指标 {metric}（{direction}）
{overview}
已知基线：本地 LightGBM 基线 CV = {baseline_cv}（{direction}）。
目标：做出一个能跑通、且该项指标尽可能好的方案（注意上面的好坏方向，别搞反）。
第一版要相对简单，不要一上来就堆 ensemble 或大范围调参。
{intel}
失败过的方案（避免重犯）：
{failures}

【已试过的方案（Memory）】
{memory}

只输出一个 Python 文件，正好含两个函数，前后不要任何解释文字：

def build_features(train, valid, test):
    # 入参是三个 DataFrame，**都不含标签列**（标签已由执行器剥离）
    # 试图读取 valid['{target}'] 会 KeyError —— 这是设计上的防泄漏
    # 返回 (X_train, X_valid, X_test, None, None)
    # 第 4/5 个返回值会被忽略，标签由执行器通过独立通道交给 fit_predict
    # 允许 pandas / numpy / sklearn

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    # y_train / y_valid 是执行器传入的权威标签
    # 返回 (valid_pred, test_pred)
    # valid_pred 必须能被 {metric} 直接评分：{pred_spec}
    # 允许 lightgbm / sklearn

硬约束：不许联网、不许读写文件、不许 import 上面之外的库、不许 inplace 改入参 DataFrame。
"""

IMPROVE_PROMPT = """你是 Kaggle 竞赛的建模工程师。下面是一个**已经跑通**的方案，你的任务是让它更好。
比赛：{slug}　指标 {metric}（{direction}）　当前该方案 CV = {parent_score}

【将被改进的方案】CV={parent_score}
{parent_code}

【已知的其他尝试（Memory）】
{memory}

【失败过的方案（避免重犯）】
{failures}
{intel}
【数据实况（列名/dtype/取值，加特征前先看这里）】
{overview}

要求：
1. **只做一处原子改进**，但改进的方向要按下面的优先级选：
   - **优先换模型族/换归纳偏置**（例如 LightGBM → 随机森林 / 直方图 GBDT /
     正则化线性模型 / 核方法）。同一族里反复调参得到的方案彼此高度相关，
     后续做融合时没有互补价值 —— 实测 4 个只换随机种子的 LightGBM，
     融合后反而比最好的单模更差；换成异构族才真正提升。
   - 其次才考虑加特征（交叉、目标编码、聚合统计）。
   - 最后才调超参。
2. 保留原方案中有效的一切，不要推倒重写。
3. 先在脑子里说明这处改进是什么（不要输出说明文字）。
4. 只输出完整可运行的 Python 文件（两个函数齐全）：

def build_features(train, valid, test):
    # 三个入参都不含标签列（执行器已剥离，防结构性泄漏）
    # 返回 (X_train, X_valid, X_test, None, None)
    ...

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    # valid_pred 交什么：{pred_spec}

硬约束：不许联网、不许读写文件、不许 import 上面之外的库、不许 inplace 改入参 DataFrame。
"""

DEBUG_PROMPT = """你是 Kaggle 竞赛的建模工程师。下面这个方案**跑失败了**，请修好它。
比赛：{slug}　指标 {metric}　已连续调试 {debug_depth} 代

【失败方案的代码】
{parent_code}

【执行报错（末尾最重要）】
{error}

【数据实况（列名/dtype/取值 —— 报错若是 KeyError 之类，多半是列名假设错了）】
{overview}

【已知的其他尝试（Memory）】
{memory}

要求：
1. 定位并修掉这个错误。不要借机大改方案结构 —— 先让它跑起来。
2. 如果错误源于数据假设（列名/类型/缺失/类别基数），按真实数据修正。
3. 只输出完整可运行的 Python 文件（两个函数齐全）：

def build_features(train, valid, test):
    # 三个入参都不含标签列（执行器已剥离，防结构性泄漏）
    # 返回 (X_train, X_valid, X_test, None, None)
    ...

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    # valid_pred 交什么：{pred_spec}

硬约束：不许联网、不许读写文件、不许 import 上面之外的库、不许 inplace 改入参 DataFrame。
"""

INTEL_TMPL = """
公开解法情报（来自 Kaggle 榜单 writeup，仅作方向参考、不要照抄结论）：
{notes}
"""


# ── 已拆分到 llmio.py ─────────────────────────────────────────────────────
# prompt 模板与 LLM 通信改动频繁，而搜索循环很稳定，混在一起会让每次改模板
# 都要读一屏无关代码。拆出去后这里只留搜索循环；名字仍从这里导出，
# 已有调用方（cli.py / model.py / tools/*.py）不用改。
from .llmio import _consume_sse, extract_code, llm_call, pred_spec_for  # noqa: F401,E402




def _writeup_line(it: dict) -> str | None:
    """把一条 writeup 记录压成一行。skill 的 preview 有时是字符串、有时是
    {title, excerpt} 的 dict，两种都要认，否则 `--preview` 一开就 AttributeError。"""
    rank = it.get("rank")
    prev = it.get("preview")
    title = ""
    excerpt = ""
    if isinstance(prev, dict):
        title = (prev.get("title") or "").strip()
        excerpt = (prev.get("excerpt") or "").strip()
    elif isinstance(prev, str):
        excerpt = prev.strip()
    title = title or (it.get("title") or "").strip()
    # excerpt 也要有顶层回退 —— 早期只找了 `snippet`，漏了顶层的 `excerpt`。
    # 实测：`{"rank":2,"title":"T","excerpt":"I blend GBDT and NN"}` 压出来只有标题，
    # 摘要被整个丢掉；而摘要是"用什么方法"的真正信息，标题通常只是模型族罗列。
    excerpt = (excerpt
               or (it.get("excerpt") or "").strip()
               or (it.get("snippet") or "").strip())
    excerpt = " ".join(excerpt.split())
    team = (it.get("team_name") or "").strip()
    score = (it.get("score") or "").strip()
    if not (title or excerpt or team):
        return None
    head = f"#{rank}" if rank else ""
    if team:
        head += f" {team}"
    if score:
        head += f" ({score})"
    line = head.strip()
    if title:
        line += f" —— {title}"
    if excerpt:
        line += f"：{excerpt[:260]}"
    return line


def _writeup_notes(slug: str, top: int = 5, limit: int = 5, timeout: int = 90) -> list[str]:
    """从 skill 的榜单 writeup 里取"谁用什么招拿了多少分"，压成几行提示。

    只喂标题+摘要片段，不是让 LLM 照抄方案——本地数据分布和榜单团队未必一致，
    照抄容易过拟合到别人的假设上。它的作用是给出方向（该试什么模型族、该怎么处理特征）。
    """
    from . import skillbridge

    items = skillbridge.solutions(slug, top=top, preview=True, fallback=False, timeout=timeout)
    notes = [n for n in (_writeup_line(it) for it in items[:limit]) if n]
    return notes


def classify_family(code: str) -> str:
    """从候选代码里判定它属于哪个模型族。

    用途：让搜索策略知道"现在是不是全都在同一族里"。
    只看 import / 构造调用，不看变量名 —— 名字是 LLM 随便起的，不可靠。
    """
    low = (code or "").lower()
    # 顺序有意义：一个方案可能同时用到多个，取最"主要"的那个
    checks = [
        ("catboost", "catboost"),
        ("xgboost", "xgboost"),
        ("lightgbm", "lightgbm"),
        ("lgbm", "lightgbm"),
        ("histgradientboosting", "sklearn-hgb"),
        ("randomforest", "random-forest"),
        ("extratrees", "extra-trees"),
        ("gradientboostingclassifier", "sklearn-gbdt"),
        ("ridge", "linear"),
        ("logisticregression", "linear"),
        ("linearregression", "linear"),
        ("svc", "kernel"),
        ("svr", "kernel"),
        ("mlpclassifier", "neural"),
        ("torch", "neural"),
    ]
    for needle, fam in checks:
        if needle in low:
            return fam
    return "unknown"


def iterate(slug: str, rounds: int = 6, model: str | None = None,
            target_score: float | None = None, use_intel: bool = True,
            parallel: int = 3, search: str = "tree",
            leakcheck_on: bool = True, diverse: bool = True,
            folds: int = 0, container: str | None = "kauto.sandbox") -> dict:
    """按 AIDE 的树搜索范式迭代。

    ## 为什么从"每轮重写"改成"树搜索"

    现成最强基线 AIDE（arXiv 2502.13138）在 OpenAI MLE-Bench（75 个 Kaggle 赛）
    拿到比最强线性 agent（OpenHands）**多 4 倍奖牌**。它的关键不是 prompt 更聪明，
    而是搜索结构（见 aide/agent.py 的 search_policy）：

        1. 草稿数不足 → _draft()      从零写一个相对简单的方案
        2. 按概率 → _debug()          挑「buggy + 叶子 + 调试深度未超限」的节点去修
        3. 否则   → _improve()        拿**最优节点的完整代码**，要求"只做一处原子改进"

    第二条是我想不到却最关键的：**失败的方案不丢弃**。旧实现每轮从零生成，
    等于把"上一轮学到的东西"全部扔掉；AIDE 把父代码 + 真实报错喂回去修，
    所以它的代码空间是持续演进的。

    对应本模块的三个 prompt：DRAFT_PROMPT / IMPROVE_PROMPT / DEBUG_PROMPT。

    ## 并发

    目标模型是推理模型，单次 274～410 秒。`parallel` 决定一轮里同时派生几个子节点：
    它们共享同一个父节点（改进）或分别从零起草，各自写 `gen_<node_id>/`，
    跑完统一按 CV 择优。这保留了并发提速，同时不破坏树结构。

    search="flat" 可退回旧的"每轮独立生成"模式，用于对照。
    """
    meta = prep.load_work(slug)
    work = Path(meta["work_dir"])
    bl = work / "baseline.json"
    if not bl.exists():
        raise KaggleError("先跑 baseline：kauto baseline <slug>")
    base = json.loads(bl.read_text(encoding="utf-8"))

    metric = base["metric"]
    hib = base["higher_is_better"]
    baseline_cv = float(base["cv_mean"])
    pred_spec = pred_spec_for(metric, meta["kind"])
    parallel = max(1, int(parallel))

    # 自适应超时：以 baseline 实测耗时为锚。
    # 原来硬编码 900s 且调用处不传 —— 小数据白等、大数据必超时，
    # 而超时会被记为"候选失败"，误判成代码有问题（最费时间的坑）。
    from . import timebudget
    time_budget = timebudget.estimate(work, folds=folds)
    container_name = container

    journal = Journal(slug, work)
    journal.metric_maximize = hib
    # 断点续跑：上次的节点还在就接着用，别把探索过的路径丢掉
    resumed = journal.load()

    # 公开解法情报：标题+摘要片段，作为方向提示而非结论。
    intel_block = ""
    if use_intel:
        try:
            notes = _writeup_notes(slug)
        except Exception:
            notes = []
        if notes:
            intel_block = INTEL_TMPL.format(notes="\n".join(f"- {n}" for n in notes))

    def better(a: float, b: float) -> bool:
        return a > b + 1e-7 if hib else a < b - 1e-7

    # 基线当成 0 号节点：它已经跑通，是树的一个合法起点
    if not resumed:
        journal.append(Node(id="baseline", code="", plan="本地 LightGBM 基线",
                            stage="draft", score=baseline_cv, round_no=0))

    # 数据概览只算一次（读 csv + 统计，每次都算没必要）。
    # 对照 AIDE 的 data_preview：让 LLM 知道列名/dtype/样本值，
    # 否则它写特征工程时只能靠猜列名 —— 真实比赛的列名是
    # `OverallQual` / `Neighborhood` 这种，猜不中就整段作废。
    degraded_cols = []
    bl2 = work / "baseline.json"
    if bl2.exists():
        try:
            degraded_cols = json.loads(bl2.read_text(encoding="utf-8")).get(
                "degraded_columns") or []
        except Exception:
            degraded_cols = []
    from . import dataoverview
    overview_text = dataoverview.for_work(work, meta, degraded=degraded_cols)
    if not overview_text:
        overview_text = (f"数据：train {meta['n_rows']} 行 / {meta['n_features']} 个特征；"
                         f"work 目录里有 train.csv、valid.csv、test.csv。")

    def build_prompt(kind: str, parent: Node | None, hint: str) -> str:
        memory = journal.summary(include_code=False)
        fails = journal.failures()
        if hint:
            memory += f"\n（本轮额外方向）{hint}"
        common = dict(
            slug=slug, metric=metric, pred_spec=pred_spec,
            direction="越大越好" if hib else "越小越好",
            memory=memory or "（无）", failures=fails or "（无）",
            intel=intel_block, overview=overview_text,
        )
        if kind == "debug" and parent is not None:
            return DEBUG_PROMPT.format(
                **common, parent_code=parent.code[:6000],
                error=(parent.error or "")[-1500:],
                debug_depth=journal.debug_depth(parent),
            )
        if kind == "improve" and parent is not None:
            return IMPROVE_PROMPT.format(
                **common, parent_code=parent.code[:7000],
                parent_score=f"{parent.score:.6f}" if parent.score is not None else "?",
            )
        return DRAFT_PROMPT.format(
            **common, task=meta["task"], kind=meta["kind"], target=meta["target"],
            # 必须告诉 LLM 当前基线是多少：
            # 早期 draft prompt 完全没提分数，于是它无法判断"要超越什么"，
            # 写出来的方案可能连基线都不如 —— 而它自己并不知道。
            baseline_cv=f"{baseline_cv:.6f}",
        )

    def select_action() -> tuple[str, Node | None]:
        """AIDE search_policy 的等价实现 + 族多样性约束。"""
        cfg_drafts = 1          # 至少一个草稿（正常已由 baseline 占位）
        cfg_debug_prob = 0.35
        cfg_max_debug = 3       # 同一坑最多连续调试 3 代
        cfg_diverse = diverse   # 是否强制探索不同模型族
        cfg_min_families = 2    # 至少要覆盖这么多个族

        if search == "flat":
            return "draft", None

        if len(journal.draft_nodes) < cfg_drafts and not journal.good_nodes:
            return "draft", None

        # 全盘失败时，优先修已知的失败，而不是继续起草新的。
        #
        # 实测：dirty-comp 上连续两个 draft 都因为列名写错而失败（no good node），
        # 而 debug 只有 35% 概率命中 —— 两轮恰好都没命中，于是系统又去起草第三个，
        # 而不是修已经知道哪里错的那些。**已知的失败比未知的新草稿更值得先投入。**
        debuggable = [n for n in journal.buggy_nodes
                      if journal.debug_depth(n) < cfg_max_debug
                      and n.id not in journal.children]
        # 注意：baseline 节点（id=="baseline"）**不算有效候选**。
        # 实测：journal.good_nodes 里含 baseline（因为基线有 CV 分数），
        # 于是 `not good_nodes` 恒为 False，"全盘失败优先 debug"永不触发 ——
        # 三轮跑完全都是 draft，明明有三个已知失败等着修。
        # 判据改成「除 baseline 外没有跑通的候选」。
        real_good = [n for n in journal.good_nodes if n.id != "baseline"]
        if debuggable and not real_good:
            return "debug", random.choice(debuggable)

        if random.random() < cfg_debug_prob:
            if debuggable:
                return "debug", random.choice(debuggable)

        # 族多样性优先：如果当前所有 good node 都来自同一模型族，
        # 就新起草一个（而不是继续在同一族里改进）。
        #
        # 理由（实测）：同一族只换随机种子的候选，错判高度相关，
        # 后续 blend 融合无收益甚至更差；换族才有互补性。
        # 这是把"融合价值取决于互补性"这条结论前置到搜索策略里，
        # 而不是等融合阶段才发现候选没法用。
        if cfg_diverse and len(journal.good_nodes) >= 2:
            families = {classify_family(n.code) for n in journal.good_nodes}
            if len(families) < cfg_min_families:
                return "draft", None

        best_node = journal.get_best_node()
        if best_node is None:
            return "draft", None
        # 基线节点代码为空，不能拿来改进；它是起点，改进要重新起草
        if not best_node.code:
            return "draft", None
        return "improve", best_node

    def one_child(parent: Node | None, kind: str, hint: str) -> dict:
        """派生一个子节点：LLM → 泄漏审查 → 子进程 → CV。失败也返回节点，交给树记住。"""
        node = journal.new_node("", parent=parent, stage=kind)
        try:
            node.code = extract_code(llm_call(build_prompt(kind, parent, hint),
                                              model=model, retries=1))
        except Exception as e:
            node.error = str(e)[:500]
            return {"node": node, "ok": False, "stage": "llm"}

        # ── 重复代码检测 ────────────────────────────────────────────────────
        #
        # 实测：在 dirty-comp 上 debug 链是串行的（cf4bedce → 619f028a → be8ef9be），
        # 三代节点的代码 hash **完全相同** —— LLM 原样返回了父节点的代码。
        # 于是每一代都以同样的方式失败，而 debug_depth 上限（3）允许它继续消耗轮数。
        #
        # 修：若新代码与父节点**逐字相同**，立刻判失败并明确标出原因。
        # 这比让它跑一遍再失败更省时，也让"这条链修不动"这个事实显式化
        # （search_policy 会据此换父节点或换方向）。
        if parent is not None and parent.code and node.code.strip() == parent.code.strip():
            node.error = ("LLM 返回的代码与父节点完全相同（没有做任何修改）—— "
                          "这条 debug/improve 链修不动，请换个方向或换父节点")
            return {"node": node, "ok": False, "stage": "nochange"}

        # 泄漏审查放在跑之前：泄漏方案的 CV 会虚高，一旦让它进树，
        # search_policy 就会把它当成"最优"继续改进，整轮搜索都被带偏。
        # 拒收的代价只是少一个候选，比污染整棵树便宜得多。
        if leakcheck_on:
            lk = leakcheck.scan(node.code)
            node.leak = lk
            if not lk["ok"]:
                node.error = ("数据泄漏（静态审查拒收）："
                              + (leakcheck.describe(lk, limit=2) or "high 级规则命中"))
                return {"node": node, "ok": False, "stage": "leak"}

        res = _runner.run(work, node.code, tag=node.id, folds=folds,
                          timeout=time_budget["timeout"],
                          container=container_name)
        if not res.get("ok"):
            node.error = res.get("error", "")[:700]
            return {"node": node, "ok": False, "stage": "run"}

        try:
            node.score = float(_runner.evaluate(work, meta, metric, tag=node.id))
        except Exception as e:
            node.error = str(e)[:500]
            return {"node": node, "ok": False, "stage": "eval"}

        node.accepted = node.score is not None
        return {"node": node, "ok": True, "stage": "eval"}

    # 换族/换方向时给 LLM 的提示。
    #
    # 实测缺陷：这里原本是 `["", "", "", ""]` —— **四个全是空串**。
    # 于是 `HINTS[(r+i) % 4]` 永远返回 ""，batch 内所有 draft 拿到同一个空提示，
    # 产出的代码自然同质；"并发去重"也就退化成"发三个一样的"。
    # 而那行代码看起来是有设计的（用了取模轮换），实际轮换的是四个空串。
    #
    # 现在的四条覆盖不同切入点：模型族 / 特征 / 稳健性 / 正则。
    HINTS = [
        "换一个模型族（不同算法家族，而不是同族的另一个种子）",
        "在特征工程上做文章（交互、分组聚合、目标外的派生量）",
        "提升稳健性（处理缺失/异常值/类别编码方式，让模型更不容易过拟合）",
        "调整正则与容量（限制复杂度，或反过来让模型更有表达力）",
    ]
    trials: list[dict] = []
    best = baseline_cv
    keep_node: Node | None = journal.get_best_node(only_good=False)

    # ── 收敛检测 ─────────────────────────────────────────────────────────────────
    # 实测问题：连续四轮分数完全相同（[0.650527 ×4]），系统继续烧时间，
    # 而每轮真实成本约 400s（LLM 延迟）。真实场景下这是纯浪费。
    #
    # 判据用"最近 N 轮无提升"而不是"分数相同"：
    # 分数相同可能是巧合（不同方案恰好同分），而连续无提升才是真的到顶了。
    # 触顶后不直接停 —— 先换族再试一批，因为同族耗尽的典型表现就是无提升。
    STALL_LIMIT = 3          # 连续这么多轮无提升 → 触发换族
    stall = 0
    forced_draft_rounds = 0  # 已经强制换族过几次

    r = 0
    while r < rounds:
        batch = min(parallel, rounds - r)

        # 触顶：把动作全部改成 draft（新起草 = 换族），避免继续在旧方案上微调
        stall_branch = stall >= STALL_LIMIT
        if stall_branch:
            trials.append({"round": r + 1, "note": f"连续 {stall} 轮无提升，强制换族起草"})
            forced_draft_rounds += 1
            stall = 0

        plans: list[tuple[Node | None, str, str]] = []
        seen_plans: set[tuple[str, str]] = set()
        for i in range(batch):
            if stall_branch:
                plans.append((None, "draft", HINTS[(r + i) % len(HINTS)]))
            else:
                k, p = select_action()
                # ── 并发去重 ────────────────────────────────────────────────
                # 实测缺陷：parallel=3 时 loop 里每次都调 select_action()，
                # 但 journal 还没更新（新节点在 batch 结束后才 append），
                # 于是三次都读到同一份快照 → **选出完全相同的动作**。
                # 实测轨迹：round1/2/3 全是 stage=draft parent=null，
                # 三个候选白跑（其中两个必然高度同质，后续融合也没价值）。
                #
                # 处理：同一批内若撞上已用过的 (动作, 父节点) 组合，
                # 就换一个动作试；实在都撞了就退化成 draft 换族，
                # 保证这一批产出的是不同方向的探索。
                key = (k, p.id if p is not None else "-")
                if key in seen_plans:
                    # 换动作时必须保证该动作有可用的父节点：
                    # `improve` / `debug` 都要求 parent 不为空，否则 build_prompt
                    # 会静默落到 draft 模板（实测：动作名写着 improve，实际按 draft 生成），
                    # 于是"去重"反而变成了"三个都是 draft"。
                    picks: list[tuple[str, Node | None]] = []
                    if p is not None:
                        for alt in ("improve", "debug"):
                            if alt != k:
                                picks.append((alt, p))
                    if keep_node is not None and keep_node is not p:
                        picks.append(("improve", keep_node))
                    buggy = journal.buggy_nodes
                    if buggy:
                        picks.append(("debug", buggy[-1]))
                    # 允许「同父节点 + 不同提示方向」的重复 improve：
                    # 候选池小的时候（例如刚跑 baseline 只有一个节点），
                    # 逼着换动作只能换来一堆 draft，反而降低这一批的探索价值。
                    # key 里带上提示索引，让同父的不同方向算作不同方案。
                    picks.append(("draft", None))

                    chosen = None
                    for alt, alt_parent in picks:
                        alt_key = (alt, alt_parent.id if alt_parent is not None else "-")
                        if alt_key not in seen_plans:
                            k, p, key, chosen = alt, alt_parent, alt_key, (alt, alt_parent)
                            break
                    if chosen is None:
                        k, p, key = "draft", None, ("draft", "-")
                seen_plans.add(key)
                plans.append((p, k, HINTS[(r + i) % len(HINTS)]) if k != "draft" or p is not None
                             else (None, "draft", HINTS[(r + i) % len(HINTS)]))

        # 记录本批实际下发的动作，便于事后核对（并发下"发了三个一样的"看不出来）
        if batch > 1:
            trials.append({"round": r + 1,
                           "note": "本批动作：" + " / ".join(
                               f"{k}{(p.id[:8] if p is not None else '-')}" for p, k, _ in plans)})

        results: list[dict] = []
        if batch == 1:
            results.append(one_child(*plans[0]))
        else:
            with ThreadPoolExecutor(max_workers=batch) as pool:
                futs = [pool.submit(one_child, p, k, h) for p, k, h in plans]
                for f in as_completed(futs):
                    results.append(f.result())

        for res in results:
            node: Node = res["node"]
            r += 1
            node.round_no = r
            journal.append(node)

            entry = {"round": r, "stage": node.stage, "ok": res["ok"],
                     "node": node.id, "parent": node.parent,
                     "debug_depth": journal.debug_depth(node)}
            if res["ok"]:
                entry["score"] = round(node.score, 6)
                entry["improved"] = better(node.score, best)
                # 每个跑通的节点都归档（不只最优）：
                # 只留 best_* 的话，下一次变差就回不去了，
                # 也没法事后回答"那个 0.26 的方案长什么样"。
                fam = classify_family(node.code)
                arc = archive.archive_node(
                    work, node.id, code=node.code,
                    meta={"score": round(float(node.score), 6),
                          "stage": node.stage, "round": r,
                          "parent": node.parent, "family": fam,
                          "metric": metric},
                )
                entry["family"] = fam
                entry["archived"] = arc.get("ok", False)
                if arc.get("missing"):
                    entry["archive_missing"] = arc["missing"]
                if entry["improved"]:
                    best = node.score
                    keep_node = node
                    stall = 0          # 有提升 → 收敛计数清零
                    # 立即把该节点产物落成 best_*，避免进程中断时丢掉成果
                    grep = work / f"gen_{node.id}"
                    storage.copy(grep / "candidate.py", work / "best_fe.py")
                    storage.copy(grep / "valid_pred.npy", work / "best_valid_pred.npy")
                    storage.copy(grep / "test_pred.npy", work / "best_test_pred.npy")
                    # 预测文件必须自述"用哪套特征算的"，否则改了特征集之后
                    # write-sub 无法分辨这份 npy 是否已陈旧（实测踩过：
                    # 重跑 baseline 后 write-sub 仍复用旧预测，提交 sha256 一模一样）。
                    _stamp_features_fingerprint(work, "best_test_pred.npy")
                    storage.write_text(work / "best_node.txt", node.id)
                else:
                    stall += 1         # 无提升 → 累加，到阈值就换族
            else:
                entry["error"] = node.error[:300]
                stall += 1             # 失败也算无信息，同样累加
            trials.append(entry)

            if target_score is not None and node.score is not None:
                if node.score >= target_score if hib else node.score <= target_score:
                    trials.append({"round": r, "note": "已达目标分，提前收工"})
                    journal.save()
                    r = rounds
                    break

        journal.save()

    journal.save()
    leak_rejected = [n for n in journal.nodes if n.leak and not n.leak.get("ok", True)]
    result = {
        "slug": slug,
        "search": search,
        "metric": metric,
        "pred_spec": pred_spec,
        "intel_used": bool(intel_block),
        "parallel": parallel,
        "leakcheck": bool(leakcheck_on),
        "leak_rejected": len(leak_rejected),
        "resumed_from_disk": resumed,
        "baseline_cv": round(baseline_cv, 6),
        "final_cv": round(best, 6),
        "delta": round(abs(best - baseline_cv), 6),
        "better_than_baseline": better(best, baseline_cv),
        "nodes_total": len(journal.nodes),
        "good_nodes": len(journal.good_nodes),
        "buggy_nodes": len(journal.buggy_nodes),
        "kept_node": keep_node.id if keep_node else None,
        "rounds_run": len([t for t in trials if "score" in t]),
        "diverse": bool(diverse),
        "folds_for_oof": int(folds),
        "timeout_seconds": time_budget["timeout"],
        "timeout_reason": time_budget["reason"],
        # 收敛情况：强制换族了几次 = 触顶了几次。
        # 这个数字直接告诉你"这批候选是否还有探索空间"：
        # 0 次说明一路上升，多次说明反复触顶（要么难度高、要么提示词给的方向已耗尽）
        "forced_drafts_on_stall": forced_draft_rounds,
        "converged": forced_draft_rounds > 0,
        "families": sorted({classify_family(n.code) for n in journal.good_nodes if n.code}),
        "trials": trials,
    }
    if leak_rejected:
        result["leak_findings"] = [
            {"node": n.id, "why": (n.error or "")[:200]} for n in leak_rejected[:5]
        ]
    storage.write_json(work / "iterate.json", result)
    try:
        from . import manifest
        manifest.write_manifest(
            work, slug=slug, stage="iterate",
            extra={"rounds": rounds, "parallel": parallel, "search": search,
                   "diverse": bool(diverse), "folds_for_oof": int(folds),
                   "final_cv": result["final_cv"],
                   "families": result["families"],
                   "archived_nodes": len(archive.list_archived(work))})
    except Exception:
        pass
    return result


def _validate_sub(out, sample, id_col: str, test, work: Path) -> dict:
    """兼容层：真正的校验逻辑在 subcheck.py。"""
    from . import subcheck
    return subcheck.check(out, sample, id_col, test)


def submission_from_best(slug: str, use_blend: bool | None = None) -> dict:
    """把预测落成对齐 sample_submission 表头的 submission.csv。

    ## 来源优先级（这一版才修对了）

    实测发现的断裂：`blend` 把融合结果写到 `blend_test_pred.npy`，
    而这里只读 `best_test_pred.npy` —— **融合从未被用于提交**。
    即"跑了 blend 但交出去的还是单模"，而且看不出任何异常。

    现在按优先级选来源：
        1. `blend_test_pred.npy`（若存在且 blend.json 标记 ok）
        2. `best_test_pred.npy`

    `use_blend=False` 可强制用单模（例如怀疑融合不稳时）。
    返回里会写明 `prediction_source`，让调用方与日志都能看到用的是哪个。

    另一个关键点：**按官方指标决定交什么**。指标是 accuracy/f1 就交硬标签，
    是 auc/logloss 就交概率。交错了 Kaggle 要么判废，要么把 0.98 的 AUC 压成 0.5。
    """
    import json as _json

    import numpy as np
    import pandas as pd

    from .model import higher_is_better, needs_proba

    meta = prep.load_work(slug)
    work = Path(meta["work_dir"])

    # 选来源：有融合且被标记为 ok 就优先用融合
    blend_pred = work / "blend_test_pred.npy"
    blend_meta = work / "blend.json"
    blend_ok = False
    if blend_pred.exists() and blend_meta.exists():
        try:
            blend_ok = bool(_json.loads(blend_meta.read_text(encoding="utf-8")).get("ok"))
        except Exception:
            blend_ok = False

    if use_blend is None:
        use_blend = blend_ok

    if use_blend and blend_pred.exists():
        p, source = blend_pred, "blend"
    else:
        p, source = work / "best_test_pred.npy", "best_node"

    if not p.exists():
        # 早期报错是「跑 iterate 或 baseline 后 write-sub」，但 **baseline 产不出 test 预测**
        # —— 它只存 baseline_oof.npy（验证侧）。照那句提示跑完 baseline 再来，仍然失败。
        # 改成说清两条真正可行的路径。
        raise KaggleError(
            "没有可用的 test 预测文件。两条可行路径：\n"
            "  1) 跑一次迭代（会产出 best_test_pred.npy）：kauto iterate <slug>\n"
            "  2) 用默认 LightGBM 全量重训：kauto write-sub <slug> 会自动走重训路径\n"
            "  （已查找：best_test_pred.npy / blend_test_pred.npy，均不存在）")

    # ── 陈旧预测检查：特征集变了，旧 npy 就不能再用 ─────────────────────────
    #
    # 实测踩到：改掉频次编码退化判据后重跑 `baseline`（特征 10 列 → 7 列），
    # 再跑 `write-sub` —— 它看到 best_test_pred.npy 存在就复用，
    # 而那份 npy 是旧特征集算的。结果是：命令都跑了、溯源也写了，
    # 但提交的 csv 与旧提交 sha256 完全一致（1e563a50…），分数纹丝不动。
    # **"重跑了 baseline + write-sub" 变成了纯仪式。**
    #
    # 现在用 baseline.json 的 features_fingerprint 对照预测文件的记录：
    # 不一致 → 判定陈旧 → 调用方（cli.write-sub）改走全量重训。
    # 预测文件自己写字迹，因为它是"谁算的、用哪些特征算的"唯一载体。
    #
    # ## 缺 sidecar = 不可信，不是"没问题"
    #
    # 第一版写成 `cur_fp and recorded_fp and 不等` —— 于是**没有 sidecar 的
    # 老预测文件直接被放行**。而真实场景恰恰就是老文件：本次修复前产出的
    # best_test_pred.npy 全都是无 sidecar 的，判据对它们全失效，
    # 重跑 write-sub 依旧复用旧预测（实测：sha256 仍是 1e563a50…）。
    #
    # 正确语义是 fail-closed：**只要不能证明"这份预测是当前特征集算的"，
    # 就当它陈旧**。代价是升级后第一次 write-sub 会走一次重训（秒级），
    # 换来的是"绝不提交与 CV 脱节的文件"。
    bl_path = work / "baseline.json"
    cur_fp = None
    if bl_path.exists():
        try:
            cur_fp = _json.loads(bl_path.read_text(encoding="utf-8")).get("features_fingerprint")
        except Exception:
            cur_fp = None
    sidecar = p.with_suffix(p.suffix + ".meta.json")
    recorded_fp = None
    if sidecar.exists():
        try:
            recorded_fp = _json.loads(sidecar.read_text(encoding="utf-8")).get("features_fingerprint")
        except Exception:
            recorded_fp = None
    stale = False
    if cur_fp:                       # 没有当前指纹就无从比对，按放行（不误拦）
        stale = (recorded_fp != cur_fp)
    if stale:
        why = "缺少特征指纹记录（旧产物）" if recorded_fp is None else "指纹不一致"
        raise KaggleError(
            f"预测文件与当前特征集不一致（{why}），拒绝复用 —— 否则会生成与 CV 脱节的提交：\n"
            f"  {p.name}: features_fingerprint={recorded_fp}\n"
            f"  baseline.json:    features_fingerprint={cur_fp}\n"
            f"  改跑了 baseline 之后必须重算预测：kauto write-sub {slug}（会自动走重训路径）\n"
            f"  或重新 iterate。")


    pred = np.asarray(storage.load_npy(p))
    test = pd.read_csv(work / "test.csv", low_memory=False)
    sample_path = work / "sample_submission.csv"
    id_col = meta["id_col"]

    bl = work / "baseline.json"
    metric = json.loads(bl.read_text(encoding="utf-8"))["metric"] if bl.exists() else None
    converted = None

    if sample_path.exists():
        sample = pd.read_csv(sample_path, low_memory=False)
        if len(sample.columns) > 2 and pred.ndim == 2:
            cols = [str(c) for c in sample.columns[1:]]
            out = pd.DataFrame(pred, columns=cols[: pred.shape[1]])
            missing = [c for c in cols if c not in out.columns]
            for c in missing:
                out[c] = 0.0
            out.insert(0, sample.columns[0], test[id_col].values)
            out = out[list(sample.columns)]
        else:
            flat = pred.ravel()
            if metric and not needs_proba(metric) and flat.dtype.kind == "f" and flat.min() >= 0 and flat.max() <= 1:
                flat = (flat >= 0.5).astype(int)
                converted = "probability->label@0.5"
            out = pd.DataFrame({id_col: test[id_col].values, sample.columns[1]: flat})
    else:
        flat = pred.ravel()
        if metric and not needs_proba(metric) and flat.dtype.kind == "f" and flat.min() >= 0 and flat.max() <= 1:
            flat = (flat >= 0.5).astype(int)
            converted = "probability->label@0.5"
        out = pd.DataFrame({id_col: test[id_col].values, meta["target"]: flat})

    path = work / "submission.csv"
    out.to_csv(path, index=False)
    check = _validate_sub(out, sample if sample_path.exists() else None, id_col, test, work)

    # 溯源里必须写明用的是哪个来源 —— 否则"交的是融合还是单模"事后无从判断
    extra = {"prediction_source": source}
    if source == "blend":
        try:
            bj = _json.loads(blend_meta.read_text(encoding="utf-8"))
            extra["blend_weights"] = bj.get("weights")
            extra["blend_score"] = (bj.get("blend_score_on_oof")
                                    if bj.get("pred_kind") == "oof"
                                    else bj.get("blend_score_on_valid"))
        except Exception:
            pass

    provenance = _record_provenance(work, source=source, metric=metric,
                                    converted=converted, extra=extra)
    return {"submission": str(path), "metric": metric, "converted": converted,
            "prediction_source": source, "provenance": provenance, **check}


def _record_provenance(work: Path, *, source: str, metric: str | None,
                       converted: str | None = None, extra: dict | None = None) -> dict:
    """记录这份 submission.csv 到底是**从哪来的**。

    ## 为什么要这个

    实测出的问题：`submission.csv` 里没有任何来源信息，
    `best_node.txt` 只存最后一个节点 id，而 blend 路径根本不留痕迹。
    于是「先 restore 换 best_* 却没重跑 write-sub」时，会提交一份过时文件，
    台账里查不到它对应哪次 CV、哪个节点。

    做法：写 `submission_provenance.json`，附在 submission 旁边。
    它**不改变 submission.csv 本身**（表头必须严格对齐 sample_submission，
    多加一列会被 Kaggle 判废），而是旁挂一份说明。
    """
    import hashlib
    import time as _t

    work = Path(work)
    sub = work / "submission.csv"

    best_node = None
    bn = work / "best_node.txt"
    if bn.exists():
        best_node = bn.read_text(encoding="utf-8").strip() or None

    digest = None
    if sub.exists():
        digest = hashlib.sha256(sub.read_bytes()).hexdigest()[:16]

    # 这个节点是否被归档（有归档就能复现）
    archived = bool(best_node and (work / "archive" / best_node / "meta.json").exists())
    node_meta = {}
    if archived:
        try:
            node_meta = json.loads(
                (work / "archive" / best_node / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            node_meta = {}

    bl = work / "baseline.json"
    cv = None
    if bl.exists():
        try:
            cv = json.loads(bl.read_text(encoding="utf-8")).get("cv_mean")
        except Exception:
            cv = None
    it = work / "iterate.json"
    if best_node and it.exists():
        try:
            d = json.loads(it.read_text(encoding="utf-8"))
            for t in d.get("trials") or []:
                if t.get("node") == best_node and t.get("score") is not None:
                    cv = t["score"]
                    break
        except Exception:
            pass

    rec = {
        "source": source,                 # best_node / blend / retrain_default_lgbm
        # 融合路径没有单一节点，但仍要记 best_node 作为参照（那是单模基线）
        "node_id": best_node,
        "node_score": node_meta.get("score") or cv,
        "node_family": node_meta.get("family"),
        "archived": archived,
        "metric": metric,
        "converted": converted,
        "submission_sha256_16": digest,
        "written_at": _t.strftime("%Y-%m-%dT%H:%M:%S"),
        **(extra or {}),
    }
    storage.write_json(work / "submission_provenance.json", rec)
    return rec
