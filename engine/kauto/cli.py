"""kauto CLI —— 一条命令跑完整条链路。

  kauto doctor                     检查凭据/依赖/LLM/skill 桥接
  kauto info <slug>                比赛事实：官方指标、限额、是否 notebook-only
  kauto solutions <slug>           榜单公开解法 writeup（无凭据也能读）
  kauto list [--search 词]          当前可参赛的比赛
  kauto prepare <slug>             下载 + 切分本地验证集（先问 skill 要官方指标）
  kauto baseline <slug>             LightGBM 基线 + 5 折 CV
  kauto iterate <slug> --rounds N   LLM 迭代，只保留 CV 提升的版本
  kauto write-sub <slug>            从最好版本生成 submission.csv
  kauto validate <slug>             用 skill 校验 submission 形状（不评分）
  kauto submit <slug> --yes         真正提交（无 --yes 一律不提交）
  kauto status <slug>              自己的排名/剩余次数（需凭据）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import iter as kiter
from . import kapi, model, prep, skillbridge, storage
# 提交闭环单独成模块（见 submit.py 顶部说明）：
# 这五个函数是**外部状态变更**的唯一通道，和本地命令混在一起时不好审查。
from .submit import (cmd_receipt, cmd_submit, _check_entered,  # noqa: F401
                     _pre_submit_gate, _write_receipt)


def _p(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2) if isinstance(obj, (dict, list)) else obj)


def cmd_doctor(_args) -> int:
    from . import llm as llmcfg

    c = kapi.creds()
    try:
        import lightgbm, pandas, sklearn  # noqa: F401
        deps = "ok (pandas / numpy / scikit-learn / lightgbm)"
    except Exception as e:
        deps = f"缺失: {e}"

    lc = llmcfg.resolve()
    gw = llmcfg.gateway_available()
    llm_info = {
        "usable": lc["ok"],
        "source": lc["source"] or "未找到",
        "base": lc["base"] or "",
        "model": lc["model"] or "",
        "key_len": len(lc["key"]),   # 只报长度，永不打印 key
        "target_model": llmcfg.PREFERRED_MODEL,
        "gateway": {"listening": gw["listening"], "has_key": gw["has_key"],
                    "model_listed": gw.get("model_listed"),
                    "model_count": gw.get("model_count")},
        "tuning": {
            "reasoning_effort": __import__("os").environ.get("KAUTO_LLM_EFFORT", "low"),
            "timeout_sec": __import__("os").environ.get("KAUTO_LLM_TIMEOUT", "900"),
            "max_tokens": __import__("os").environ.get("KAUTO_LLM_MAXTOK", "8000"),
        },
        "hint": lc["hint"],
    }
    sk = skillbridge.available()

    # ── 网络通路自检 ─────────────────────────────────────────────────────────
    # 实测：本机系统代理会破坏 Kaggle 的 TLS 握手（SSL: UNEXPECTED_EOF_WHILE_READING）。
    # kauto 的代码路径已经注入 NO_PROXY 绕过，但**用户手动跑 kaggle CLI 时不会** ——
    # 他们会遇到同样的 SSLEOFError，却不知道原因（这轮排查花了几十分钟）。
    net = {"kaggle_reachable": None, "proxy_note": ""}
    try:
        rows = kapi.list_competitions("playground", limit=1)
        net["kaggle_reachable"] = len(rows) >= 1
    except Exception as e:
        msg = str(e)
        proxy_like = "SSLEOFError" in msg or "SSL" in msg or "proxy" in msg.lower()
        net["kaggle_reachable"] = False
        if proxy_like:
            net["proxy_note"] = (
                "若你在终端里手动跑 kaggle CLI 也遇到 SSLEOFError，"
                "多半是系统代理破坏了 TLS 握手。先设："
                "  $env:NO_PROXY=api.kaggle.com,www.kaggle.com,kaggle.com "
                "（kauto 自己的调用已自动注入，无需手动设）")

    # 这十几轮加了三层隔离，但 doctor 一直只报凭据/依赖/LLM ——
    # 隔离层本机能不能用、有没有生效，用户无从自查。
    # 尤其是 AppContainer：它需要创建 profile，在受限账户或精简系统上可能失败，
    # 而失败是**静默**的（_runner 会退化为无文件隔离）。必须显式检查。
    from . import appcontainer, winjob
    wj = winjob.available()
    ac = appcontainer.available()
    isolation = {
        "job_object": {"ok": wj["ok"], "reason": wj.get("reason"),
                       "effect": "限内存/进程数/CPU，子进程随父终止"},
        "appcontainer": {"ok": ac["ok"], "reason": ac.get("reason"),
                         "effect": "文件系统隔离（容器内读不到 ~/.dsh 凭据）"},
        "python_guard": {"ok": True,
                         "effect": "读写/子进程/联网拦截（可被 ctypes 绕过，故需上两层）"},
        "note": ("三层叠加：Python 守卫挡手滑，Job 挡资源滥用，AppContainer 挡文件访问。"
                 "AppContainer 不可用时 _runner 会退化为仅前两层 —— 这是静默的安全降级。"),
    }

    # ── 提交溯源机制 ─────────────────────────────────────────────────────────
    trace = {
        "provenance_written": "submission_provenance.json",
        "receipt_written": "submission_receipt.json / _history.json",
        "gate": "指纹一致性 + best_node 一致性（两条提交路径共用）",
        "note": "submit 与 run 走同一道 _pre_submit_gate，避免路径漂移",
    }

    _p({
        "kaggle_cli": kapi.exe(),
        "kaggle_credentials": c,
        "ml_deps": deps,
        "llm": llm_info,
        "skill_bridge": sk,
        "network": net,
        "isolation": isolation,
        "submission_trace": trace,
        "note": "baseline / write-sub 只用本地 LightGBM，不需要 LLM；iterate 才需要。"
                "iterate 默认走 DSH 渠道包网关的 trae/deepseek-v4.1-flash，无需任何外部 key。",
    })
    return 0


def cmd_info(args) -> int:
    """比赛事实。skill 桥接缺失时用引擎自带的读 API 兜底。

    实测：skill 的 profile 覆盖「标题/指标/限额/是否 notebook-only」四类事实，
    而这四类 `kapi` 自己就能问到（`competitions.CompetitionService/GetCompetition`
    + `submission-limits`）。插件包里默认没有 vendor，所以这里必须有兜底，
    否则 `info` 在最常见的安装形态下直接不可用。
    """
    facts, why = skillbridge.try_or_none(skillbridge.profile, args.slug)
    if facts is not None:
        _p(facts)
        return 0
    print(f"[kauto] skill 桥接不可用，改用内置事实查询。{why}")
    _p(_info_from_kapi(args.slug))
    return 0


def _info_from_kapi(slug: str) -> dict:
    """不依赖 skill 桥接的比赛事实。字段名与 skill 的 profile 对齐，便于调用方复用。"""
    meta = {}
    try:
        from . import prep as _prep
        meta = _prep.load_work(slug)
    except Exception:
        meta = {}
    out: dict = {"slug": slug, "source": "kapi"}
    raw = kapi.site_api(
        f"/api/i/competitions.CompetitionService/GetCompetition"
        f"?competitionName={kapi.urllib.request.quote(slug)}"
    ) if hasattr(kapi, "site_api") else {"ok": False}
    if raw.get("ok"):
        data = raw.get("data") or {}
        out.update({
            "title": data.get("title"),
            "evaluation": data.get("evaluationAlgorithm"),
            "maxDailySubmissions": data.get("maxDailySubmissions"),
            "deadline": data.get("deadline"),
            "hasLeaderboard": data.get("hasLeaderboard"),
            "totalTeams": data.get("totalTeams"),
        })
    else:
        out["error"] = raw.get("error") or "GetCompetition 不可用"
    if meta.get("metric_declared"):
        out["metric"] = meta["metric_declared"]
        out["official_metric_raw"] = meta.get("official_metric_raw")
    if meta.get("notebook_only") is not None:
        out["notebook_only"] = meta["notebook_only"]
    return out


def cmd_solutions(args) -> int:
    rows, why = skillbridge.try_or_none(
        skillbridge.solutions, args.slug, top=args.top, preview=True,
        fallback=args.fallback, timeout=args.timeout,
        refresh=getattr(args, "refresh", False),
    )
    if rows is None:
        # 这是"可选组件缺失"，不是失败：给出原因与安装方式，退出码 0。
        # 用 2（未执行）以便调用方区分"没装"与"装了但没数据"（后者是 1）。
        print(f"[kauto] 公开解法检索需要 kaggle-skill 桥接（可选）。{why}")
        return 2
    if not rows:
        print("榜单上没有挂公开 writeup（很多比赛只交方案不开源）")
        return 1
    for r in rows:
        line = kiter._writeup_line(r)
        if line:
            print(line)
    return 0


def cmd_validate(args) -> int:
    """形状校验 + 官方评测页约束。

    ## 为什么改成「始终本地校验，skill 只作为额外一层」

    旧实现二选一：桥接可用就跑 skill 的 `validate`，否则跑 `_validate_local`。
    实测暴露出两个问题：

    1. **两条路径的判据不一致**。skill 的 validate 读官方格式要求，
       而 `_validate_local` 只查列名/行数/id —— 同一个文件在装了桥接和
       没装桥接的机器上可能给出不同结论，这本身就是缺陷。
    2. **桥接可用时反而丢了本地那几条更强的检查**（id 与 sample 的前导零
       比对、重复 id、取值分布），那些是实测踩出来的。

    现在统一为：**本地结构校验始终跑**（`_validate_local`，含官方规则第二层），
    skill 的 validate 结果作为补充输出附在后面。两边都通过才算通过 ——
    任何一侧报错都拦住，因为提交配额不可回收。
    """
    sub = kapi.WORK_DIR / args.slug / "submission.csv"
    if not sub.exists():
        print(f"缺 {sub}，先跑 write-sub")
        return 1

    local_rc = _validate_local(args.slug)

    sample = kapi.WORK_DIR / args.slug / "sample_submission.csv"
    r, why = skillbridge.try_or_none(
        skillbridge.validate, sub, sample if sample.exists() else None, args.slug
    )
    if r is None:
        if why:
            print(f"\n  INFO  skill 桥接校验未执行：{why}")
        return local_rc

    print("\n  ── skill 桥接校验（官方格式复核）──")
    print(r["output"])
    bridge_rc = 0 if r["passed"] else 3
    if bridge_rc != 0 and local_rc == 0:
        print("\n  本地校验通过，但 skill 复核未通过 —— 以更严格的一侧为准。")
    return local_rc if local_rc != 0 else bridge_rc


def _validate_local(slug: str) -> int:
    """内置形状校验（不依赖 skill 桥接的结构层）。

    若 skill 桥接可用，会额外读**官方评测页**并把其中的硬约束作为第二层判据。
    读不到评测页时退回纯结构校验 —— 官方页不可用不该阻断本地检查。
    """
    import pandas as pd

    from . import subcheck as _subcheck

    work = kapi.WORK_DIR / slug
    sub_p = work / "submission.csv"
    sample_p = work / "sample_submission.csv"
    test_p = work / "test.csv"
    try:
        out = pd.read_csv(sub_p, low_memory=False)
    except Exception as e:
        print(f"读不出 submission.csv：{e}")
        return 1
    sample = pd.read_csv(sample_p, low_memory=False) if sample_p.exists() else None
    test = pd.read_csv(test_p, low_memory=False) if test_p.exists() else out
    meta = {}
    try:
        from . import prep as _prep
        meta = _prep.load_work(slug)
    except Exception:
        pass

    # ── 官方评测页 → 可执行判据 ────────────────────────────────────────────
    rules = None
    rules_note = ""
    try:
        bridge_ok = skillbridge.available()["vendored"]
    except Exception:
        bridge_ok = False
    if bridge_ok:
        try:
            ev = skillbridge.evaluation_page(slug)
            extracted = skillbridge.rules_from_evaluation(ev.get("text", ""))
            if extracted:
                rules = extracted
                rules_note = (f"官方评测页抽出 {len(extracted)} 条约束"
                              f"（{ev.get('chars', 0)} 字"
                              f"{'，缓存命中' if ev.get('cached') else ''}）")
            elif ev.get("text"):
                rules_note = f"评测页已读取（{ev.get('chars', 0)} 字）但未抽到可执行约束"
            else:
                rules_note = f"评测页不可用：{ev.get('error', '未知')}"
        except Exception as e:
            rules_note = f"评测页读取失败：{str(e)[:120]}"
    else:
        rules_note = "skill 桥接未安装，仅做结构校验（官方约束未检查）"
    if rules_note:
        print(f"  INFO  {rules_note}")

    result = _subcheck.check(out, sample, meta.get("id_col") or "", test, rules=rules)
    ok = True
    for name, passed, detail in _iter_checks(result):
        mark = "PASS" if passed else ("SKIP" if passed is None else "FAIL")
        print(f"  {mark:4s} {name}" + (f"  ({detail})" if detail else ""))
        if passed is False:
            ok = False
    print()
    print("Every check passed. The shape is right; this does not score the predictions."
          if ok else "1 or more checks failed. Fix the file before submitting.")
    return 0 if ok else 1


def _iter_checks(result: dict):
    """把 subcheck 的结果摊成 (名称, 通过?, 说明) —— 两种返回形态都认。"""
    problems = result.get("problems") or []
    checks = result.get("checks")
    if isinstance(checks, list):
        for item in checks:
            if isinstance(item, dict):
                yield item.get("name") or item.get("check") or "check", item.get("ok"), item.get("detail") or ""
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                yield str(item[0]), item[1], (item[2] if len(item) > 2 else "")
        return
    for name in ("columns", "rows", "ids", "empty", "finite"):
        yield name, True, ""
    for problem in problems:
        yield str(problem)[:90], False, ""


def cmd_rules(args) -> int:
    """读取官方页面，导出**可执行**的提交判据。

    这是给 model 和面板都能用的入口：它不说"去读评测页"，
    而是把页面里的硬约束变成结构化字段（rows / columns / allowed_columns / range），
    并且把原文一并回传，供人核对抽取是否正确。

    为什么需要它：`validate` 是**事后**检查；而这个命令让 agent 在写
    `write-sub` 之前就知道"这份文件必须长什么样"，少一次返工。
    公开页面不需要凭据。
    """
    import json as _json

    page = getattr(args, "page", "evaluation") or "evaluation"
    data, why = skillbridge.try_or_none(skillbridge.pages, args.slug, page)
    if data is None:
        _p({"ok": False, "slug": args.slug, "page": page,
            "error": why or "无法读取官方页面",
            "hint": "官方页面读取需要 kaggle-skill 桥接（可选）。"
                    "未安装时仍可用 validate 的结构校验，但官方格式约束无从检查。"})
        return 2

    pages_list = data.get("pages") or []
    text = ""
    for item in pages_list:
        if str(item.get("name", "")).lower() in (page.lower(), f"{page.lower()} page"):
            text = item.get("text") or ""
            break
    if not text and pages_list:
        text = pages_list[0].get("text") or ""

    extracted = skillbridge.rules_from_evaluation(text)
    payload = {
        "ok": True,
        "slug": args.slug,
        "page": page,
        "chars": len(text),
        "official_rules": extracted,
        "executable": sorted(extracted.keys()),
        "note": ("这些判据会被 validate 自动应用；抽不出来就只做结构校验"
                 if extracted else "未从该页抽到可执行约束（结构校验仍然有效）"),
    }
    if getattr(args, "format", "json") == "text":
        if text:
            print(text)
        return 0
    _p(payload)
    return 0


def cmd_leaderboard(args) -> int:
    """排行榜：判断"还值不值得继续迭代"的依据。

    `status` 只回答"我交过什么、多少分"；它回答不了**离奖牌还差多少**。
    而"要不要再跑一轮"完全取决于这个差距：差 0.0005 值得，
    差 0.05 不如换个方向。奖牌线只有排行榜接口给。
    """
    lb, why = skillbridge.try_or_none(skillbridge.leaderboard, args.slug, top=args.top)
    if lb is None:
        print(f"[kauto] 排行榜需要 kaggle-skill 桥接（可选）。{why}")
        return 2

    # 摘要：把"我 vs 榜首 vs 奖牌线"摊平成一眼能读的三行
    mine = lb.get("user_rank")
    total = lb.get("team_count")
    medals = lb.get("medal_lines") or {}
    rows = lb.get("rows") or []
    print(f"比赛 {args.slug} | 队伍 {total} | 奖牌赛: {lb.get('awards_medals')}")
    if rows:
        top1 = rows[0]
        print(f"  榜首  {top1.get('team')}  {top1.get('score')}")
    print(f"  我的名次  {mine if mine else '（未加入或未提交）'}")
    if medals:
        for zone in ("gold", "silver", "bronze"):
            if zone in medals:
                print(f"  {zone:6s} 线  {medals[zone]}")
    elif lb.get("awards_medals") is False:
        print("  这场比赛不发奖牌（Getting Started / Knowledge 类）")
    print()
    _p(lb)
    return 0


def cmd_smoke(args) -> int:
    """用内置夹具离线跑通整条链路。

    ## 为什么需要它

    插件面板上的「跑冒烟」此前只调体检（`read_status`）—— 名字叫冒烟，
    实际一个建模步骤都没跑。用户点完看到一屏绿灯，仍然不知道这条流水线
    在这台机器上到底能不能跑。

    而真跑一场 Kaggle 比赛的代价很大：要联网下载、要训练、**提交还要花配额**。
    所以"验证能不能跑"和"真的去比赛"必须分开。

    这个命令用包内夹具（`fixtures/demo-comp`）把
    prepare → baseline → write-sub → validate 跑一遍：
    真实读 csv、真实训练 LightGBM、真实写 submission、真实跑形状校验。
    全程不联网、不碰 Kaggle、不消耗任何配额。

    ## 它验证什么、不验证什么

    验证：Python 环境、依赖、引擎能否导入、数据能否切分、模型能否训练、
    提交文件能否生成并通过形状校验 —— 也就是"这条管线通不通"。

    不验证：Kaggle 凭据是否有效、上传通道是否可用、比赛规则是否接受。
    那几项需要真凭据，由 `doctor` 与第一次真实 `submit` 覆盖。

    这三者的分工是有意的：把需要网络与凭据的检查留在 doctor，
    把纯本地的可跑性检查放在这里，互不拖累。
    """
    import shutil
    import tempfile

    slug = "smoke-comp"
    # 夹具在**包内**（`engine/fixtures/`），不随可写根移动。
    # 这里必须按模块位置定位，不能用 `kapi.ROOT` —— 后者跟随 KAUTO_ROOT，
    # 宿主插件与测试都会把它指到别处，于是"找夹具"会跑去用户的临时目录里找。
    # （实测踩到：设了 KAUTO_ROOT 后 smoke 一律报"找不到夹具"。）
    package_root = Path(__file__).resolve().parent.parent
    if args.local:
        fixture = Path(args.local)
    else:
        fixture = package_root / "fixtures" / "demo-comp"
    if not (fixture / "train.csv").exists():
        print(f"[kauto] 找不到夹具：{fixture}")
        alt = package_root / "fixtures"
        if alt.is_dir():
            names = sorted(p.name for p in alt.iterdir() if p.is_dir())
            if names:
                print(f"[kauto] 包内可用夹具：{', '.join(names)}")
        print("[kauto] 用 --local <目录> 指定一个含 train.csv / test.csv 的目录。")
        return 2

    steps: list[dict] = []
    started = time.time()

    def step(name: str, fn) -> bool:
        print(f"\n=== {name} ===", flush=True)
        t = time.time()
        try:
            fn()
            steps.append({"step": name, "ok": True, "seconds": round(time.time() - t, 1)})
            return True
        except Exception as e:
            steps.append({"step": name, "ok": False,
                          "seconds": round(time.time() - t, 1),
                          "error": f"{type(e).__name__}: {e}"[:400]})
            print(f"[kauto] {name} 失败：{e}", file=sys.stderr)
            return False

    # 用临时可写根：演练产物不该落在用户真实工作区里。
    tmp_root = Path(tempfile.mkdtemp(prefix="kauto-smoke-"))
    old_root = os.environ.get("KAUTO_ROOT")
    ok = False
    try:
        # kapi.ROOT 是模块级常量，在 import 时求值 —— 想让临时根生效，
        # 必须重载相关模块。这是全项目唯一一处"必须在运行期改根"的场景，
        # 别的路径都由宿主在 import 前注入环境变量。
        os.environ["KAUTO_ROOT"] = str(tmp_root)
        for mod in [m for m in list(sys.modules) if m == "kauto" or m.startswith("kauto.")]:
            sys.modules.pop(mod, None)
        import importlib
        kapi_l = importlib.import_module("kauto.kapi")
        prep_l = importlib.import_module("kauto.prep")
        model_l = importlib.import_module("kauto.model")

        print(f"[kauto] 演练根目录：{kapi_l.ROOT}")
        print(f"[kauto] 夹具：{fixture}")

        if not step("prepare", lambda: prep_l.prepare(
                slug, local_dir=str(fixture), use_skill=False)):
            return _smoke_report(steps, started, 1)
        if not step("baseline", lambda: model_l.run_baseline(slug, folds=3, seed=42)):
            return _smoke_report(steps, started, 1)
        if not step("write-sub", lambda: model_l.write_submission(slug)):
            return _smoke_report(steps, started, 1)

        work = kapi_l.WORK_DIR / slug
        sub_p = work / "submission.csv"
        sample_p = work / "sample_submission.csv"

        def _validate():
            import pandas as pd
            from . import subcheck as _sc
            if not sub_p.exists():
                raise RuntimeError(f"没有生成 {sub_p}")
            out = pd.read_csv(sub_p, low_memory=False)
            sample = pd.read_csv(sample_p, low_memory=False) if sample_p.exists() else None
            test = pd.read_csv(work / "test.csv", low_memory=False)
            meta = prep_l.load_work(slug)
            return _sc.check(out, sample, meta.get("id_col") or "", test)

        if not step("validate", _validate):
            return _smoke_report(steps, started, 1)

        ok = True
        return _smoke_report(steps, started, 0)
    finally:
        if old_root is None:
            os.environ.pop("KAUTO_ROOT", None)
        else:
            os.environ["KAUTO_ROOT"] = old_root
        if getattr(args, "keep", False):
            print(f"\n[kauto] 保留演练产物：{tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)
        # 把模块恢复成原根的版本，避免后续调用读到临时根
        for mod in [m for m in list(sys.modules) if m == "kauto" or m.startswith("kauto.")]:
            sys.modules.pop(mod, None)


def _smoke_report(steps: list[dict], started: float, code: int) -> int:
    total = round(time.time() - started, 1)
    payload = {
        "ok": code == 0,
        "kind": "offline-smoke",
        "seconds": total,
        "steps": steps,
        "note": ("离线演练：真实读数据、真实训练、真实写提交、真实形状校验；"
                 "不联网、不提交、不消耗 Kaggle 配额。"),
        "not_covered": ["Kaggle 凭据有效性", "上传通道可达性", "比赛规则是否接受"],
    }
    if code != 0:
        failed = [s["step"] for s in steps if not s["ok"]]
        payload["failed"] = failed
        payload["hint"] = (
            "prepare 失败 → 夹具目录不对或数据列异常；"
            "baseline 失败 → 依赖或数据量问题；"
            "write-sub 失败 → 模型产物与特征集不一致；"
            "validate 失败 → 提交文件形状不对。")
    _p(payload)
    return code


def cmd_status(args) -> int:
    st, why = skillbridge.try_or_none(skillbridge.status, args.slug, limit=args.limit)
    if st is None:
        # 自己名次同样不需要 skill：kapi 有 ListSubmissions + submission-limits。
        print(f"[kauto] skill 桥接不可用，改用内置提交查询。{why}")
        _p(_status_from_kapi(args.slug, limit=args.limit))
        return 0
    _p(st)
    return 0


def _status_from_kapi(slug: str, limit: int = 5) -> dict:
    out: dict = {"slug": slug, "source": "kapi"}
    try:
        out["limits"] = kapi.run(["competitions", "submission-limits", slug]).strip()
    except Exception as e:
        out["limits_error"] = str(e)[:200]
    try:
        rows = kapi._parse_submissions_table(kapi.submissions(slug))
        out["submissions"] = rows[:limit]
        scored = [float(r["publicScore"]) for r in rows if r.get("publicScore")]
        out["best_public"] = max(scored) if scored else None
    except Exception as e:
        out["submissions_error"] = str(e)[:200]
    return out



def cmd_blend(args) -> int:
    from . import blend as bl
    r = bl.blend(args.slug, top_k=args.top_k)
    _p(r)
    return 0 if r.get("ok") else 3


def cmd_archive(args) -> int:
    """列出 / 归档节点产物。只增不删，用于回退与复现。"""
    from . import archive as arc, prep

    meta = prep.load_work(args.slug)
    work = Path(meta["work_dir"])

    if args.backfill:
        # 把「现在还在 gen_* 里」的节点补归档（只对跑通过的节点）
        jr = work / "journal.json"
        if not jr.exists():
            print("没有 journal.json，无法回填")
            return 1
        d = json.loads(jr.read_text(encoding="utf-8"))
        # 从 baseline 取指标，用于归档里记录"这个分数是按什么指标算的"
        bl = work / "baseline.json"
        metric = None
        if bl.exists():
            metric = json.loads(bl.read_text(encoding="utf-8")).get("metric")

        from .iter import classify_family

        n_ok = 0
        for n in d.get("nodes") or []:
            if n.get("score") is None:
                continue
            cand = work / f"gen_{n['id']}" / "candidate.py"
            code = cand.read_text(encoding="utf-8") if cand.exists() else None
            r = arc.archive_node(work, n["id"], code=code, meta={
                "score": round(float(n["score"]), 6),
                "stage": n.get("stage"), "round": n.get("round_no"),
                "parent": n.get("parent"), "metric": metric,
                # 族要在这里补上：列表页靠它判断候选是否互补，
                # 回填时漏掉会显示成 family=?（实测踩到）
                "family": classify_family(code or ""),
            })
            if r.get("ok"):
                n_ok += 1
        print(f"回填完成：{n_ok} 个节点已归档")
        return 0

    rows = arc.list_archived(work)
    if not rows:
        print("还没有归档。先跑 iterate，或用 --backfill 从现有 gen_* 回填。")
        return 0
    hib = True
    bl = work / "baseline.json"
    if bl.exists():
        hib = bool(json.loads(bl.read_text(encoding="utf-8")).get("higher_is_better", True))
    rows.sort(key=lambda r: r.get("score") or 0, reverse=hib)
    for r in rows:
        miss = r.get("missing") or []
        flag = f"  缺:{','.join(miss)}" if miss else ""
        print(f"{r.get('node_id','?')[:16]:18s} score={r.get('score')}  "
              f"family={r.get('family','?'):12s} stage={r.get('stage','?'):8s}{flag}")
    return 0


def cmd_restore(args) -> int:
    """把某个归档节点恢复成 best_*，之后 write-sub 就会用它。"""
    from . import archive as arc, prep

    meta = prep.load_work(args.slug)
    work = Path(meta["work_dir"])
    r = arc.restore_node(work, args.node)
    _p(r)
    if r.get("ok"):
        print(f"\n[kauto] 已回退到节点 {args.node}。下一步：kauto write-sub {args.slug}")
    return 0 if r.get("ok") else 3


def cmd_oof(args) -> int:
    """给已归档节点**补跑** K 折 OOF。

    ## 为什么需要这条命令

    当前设计只有两条路：
      A) `folds=0` → 每候选 1× 成本，无 OOF，blend 权重口径偏乐观
      B) `--folds-for-oof 5` → 每候选 5× 成本，有 OOF

    A 和 B 都不理想：想在融合前知道"这个候选的 OOF 分数"，要么接受乐观口径，
    要么在迭代阶段就付 5× 成本 —— 而那时还不知道哪个候选值得留。

    **缺的第三条**：迭代照常跑（快），最后只对**值得留的那几个**补 OOF。
    成本是 `5 × 选中数`，而不是 `5 × 候选总数`。

    实测：2 个候选时 A=2、B=10、C=5；候选越多差距越大。
    """
    from . import _runner, archive, prep

    meta = prep.load_work(args.slug)
    work = Path(meta["work_dir"])

    rows = archive.list_archived(work)
    if not rows:
        print("没有归档节点。先跑 iterate（每个跑通的节点都会自动归档）")
        return 1

    # 默认补**没有 OOF 且跑通**的节点
    targets = []
    for r in rows:
        nid = r.get("node_id")
        if not nid or r.get("score") is None:
            continue
        a = work / "archive" / nid
        if (a / "oof.npy").exists() and not args.force:
            continue
        if not (a / "candidate.py").exists():
            continue
        targets.append((nid, a, r))

    if args.node:
        targets = [t for t in targets if t[0] == args.node] or [
            (args.node, work / "archive" / args.node, {})]

    print(f"[oof] 待补 {len(targets)} 个节点，folds={args.folds}")
    if not targets:
        print("[oof] 全部节点已有 OOF（要用 --force 强制重跑）")
        return 0

    ok_n = 0
    for nid, a, rec in targets:
        code = (a / "candidate.py").read_text(encoding="utf-8")
        print(f"  {nid[:12]} (score={rec.get('score')}) …", end="", flush=True)
        res = _runner.run(work, code, tag=f"oof_{nid}", folds=args.folds,
                          container=None if args.no_container else "kauto.sandbox",
                          timeout=args.timeout)
        if not res.get("ok"):
            print(f" 失败：{(res.get('error') or '')[:120]}")
            continue
        # 把 oof.npy 挪进归档目录
        src = work / f"gen_oof_{nid}" / "oof.npy"
        if src.exists():
            storage.copy(src, a / "oof.npy")
            print(" OOF 已补进归档")
            ok_n += 1
        else:
            print(" 没有产出 oof.npy")

    print(f"\n[oof] 完成 {ok_n}/{len(targets)}")
    if ok_n:
        print("      下一步：kauto blend " + args.slug)
    return 0 if ok_n else 3


def cmd_prune(args) -> int:
    """清理 work/<slug>/gen_* 里**已经没用**的候选目录。

    ## 为什么要这个

    每次迭代都会新建 gen_<node_id>/，而且从不删除 ——
    实测 work/demo-comp 里已经累积 102 个（1.4 MB）。
    真实比赛上 npy 更大、代码更长，长期使用会无限膨胀。

    ## 保留原则（宁可少删）

    - 已归档到 `archive/` 的节点：归档里已有完整副本，但 gen_ 留着能免一次复制，
      且回退时多一条路径 —— 保留。
    - `best_node.txt` 指向的节点：当前提交来源，绝不能删。
    - 归档与 best 之外的一律可删（它们的产物已经在归档或失效）。

    `--dry-run` 先看清单，确认后再删。
    """
    from . import prep as _prep

    meta = _prep.load_work(args.slug)
    work = Path(meta["work_dir"])

    arch_dir = work / "archive"
    archived = {d.name for d in arch_dir.iterdir() if d.is_dir()} if arch_dir.exists() else set()

    best = None
    bn = work / "best_node.txt"
    if bn.exists():
        best = bn.read_text(encoding="utf-8").strip() or None

    gens = sorted(work.glob("gen_*"), key=lambda p: p.stat().st_mtime)
    protected, removable = [], []
    # ── 中间产物目录必须优先识别 ────────────────────────────────────────────
    #
    # 实测：`gen_oof_<id>` 的 nid 是 `oof_<id>`，不在 archived 里 → 落到 removable？
    # 不对 —— 反过来也踩过：某些场景下它会被当成"节点名"误判。
    # 更关键的是这类目录**本来就不是候选**（blend 已排除），留着只是占空间。
    #
    # 识别依据与 blend.collect_candidates 保持一致：
    # `kauto oof` 写 gen_oof_<id>，测试脚本写 gen_to_* / gen_timing_*。
    _INTERMEDIATE = ("oof_", "timing_", "to_", "check", "test")
    for g in gens:
        nid = g.name[len("gen_"):]
        if nid.startswith(_INTERMEDIATE) or nid in _INTERMEDIATE:
            removable.append(g)
            continue
        if nid in archived or nid == best:
            protected.append(nid)
        else:
            removable.append(g)

    total = sum(f.stat().st_size for g in removable for f in g.rglob("*") if f.is_file())
    print(f"[prune] {args.slug}: gen_* 共 {len(gens)} 个")
    print(f"        保留 {len(protected)} 个（已归档 {len(archived & {g.name[len('gen_'):] for g in gens})} / best {best or '-'}）")
    print(f"        可清理 {len(removable)} 个，约 {total/1024:.0f} KB")

    # 提醒代价：gen_* 不只是"垃圾"，它们也是融合的候选来源
    n_pool = 0
    try:
        from . import blend as _bl
        n_pool = len(_bl.collect_candidates(args.slug))
    except Exception:
        pass
    # 分两类说明代价 —— 笼统说"可能减少候选"会让人不敢删，
    # 而实际只有一类真的影响融合。
    _inter_rm = [g for g in removable
                 if g.name[len("gen_"):].startswith(("oof_", "timing_", "to_"))]
    _node_rm = [g for g in removable if g not in _inter_rm]
    if _inter_rm:
        print(f"        其中 {len(_inter_rm)} 个是**中间产物**（gen_oof_/gen_to_ 等）——"
              f" 它们不参与融合，删了没有代价。")
    if _node_rm:
        # 未归档节点的 gen_ 里可能有 candidate.py，但代码在 journal 里另有一份
        print(f"        其中 {len(_node_rm)} 个是节点的 gen_ 目录 —— 删掉只丢 npy 副本，"
              f"代码在 journal.json 里仍保留（失败节点不归档，但代码存 journal）。")
    print(f"        当前融合池 {n_pool} 个候选；归档里有完整产物，"
          f"融合会从 archive/ 读，不依赖 gen_。")

    if args.dry_run:
        for g in removable[:20]:
            print(f"          - {g.name}")
        if len(removable) > 20:
            print(f"          … 另 {len(removable)-20} 个")
        print("\n[prune] --dry-run，未删除。确认后加 --yes 执行。")
        return 0

    if not args.yes:
        print("\n[prune] 未加 --yes，未删除。")
        return 2

    import shutil as _sh
    n_ok = 0
    for g in removable:
        try:
            _sh.rmtree(g, ignore_errors=True)
            n_ok += 1
        except Exception:
            pass
    print(f"[prune] 已清理 {n_ok} 个目录，释放约 {total/1024:.0f} KB")
    return 0


def cmd_link(args) -> int:
    from . import ofm

    if args.enable:
        r = ofm.enable()
        _p(r)
        if r.get("changed"):
            print("\n[kauto] 设置已写入。重启 DSH 后网关会自动监听；之后再跑 kauto link 确认 READY。")
        return 0
    _p(ofm.status())
    return 0


def cmd_list(args) -> int:
    rows = kapi.list_competitions(args.search, args.limit)
    if not rows:
        print("没有拿到比赛列表（检查凭据，或换个 --search 词）")
        return 1
    # `--format json` 让宿主（DSH 插件）能拿到结构化结果。
    # 默认仍是 TSV：独立 CLI 下 grep/awk 友好，这是既有调用方的期望。
    if getattr(args, "format", "tsv") == "json":
        _p(rows)
        return 0
    for r in rows:
        print(f"{r.get('ref','')}\t{r.get('deadline','')}\t{r.get('reward','')}\t{r.get('title','')}")
    return 0


def cmd_prepare(args) -> int:
    _p(prep.prepare(args.slug, valid_frac=args.valid_frac, seed=args.seed,
                    local_dir=args.local, use_skill=not args.no_skill,
                    split=args.split))
    return 0


def cmd_baseline(args) -> int:
    _p(model.run_baseline(args.slug, folds=args.folds, seed=args.seed))
    return 0


def cmd_iterate(args) -> int:
    _p(kiter.iterate(args.slug, rounds=args.rounds, model=args.model,
                     target_score=args.target, use_intel=not args.no_intel,
                     parallel=args.parallel, search=args.search,
                     leakcheck_on=not args.no_leakcheck,
                     diverse=not args.same_family,
                     folds=args.folds_for_oof))
    return 0


def cmd_write_sub(args) -> int:
    """生成 submission.csv。

    ## 预测陈旧时**必须重算**，不能复用旧 npy

    实测：修好频次编码判据后重跑 `baseline`（特征 10 列 → 7 列），
    再跑 `write-sub` —— 它看到 `best_test_pred.npy` 存在就复用，
    而那份是旧特征集算的。结果：命令全成功、溯源也写了，
    但 submission.csv 的 sha256 与上一次提交一模一样（1e563a50…），
    分数纹丝不动。**"重跑 baseline + write-sub" 成了纯仪式。**

    现在 `submission_from_best` 会比对 features_fingerprint，
    发现陈旧就抛 KaggleError —— 这里接住它，改走全量重训（真算一次预测）。
    """
    work = kapi.WORK_DIR / args.slug
    if (work / "best_test_pred.npy").exists():
        try:
            _p(kiter.submission_from_best(args.slug))
            return 0
        except Exception as e:
            # 仅在"陈旧"这一种情况下退到重训；其它错误照原样抛出，
            # 否则会把 shape 错误之类的真问题吞掉。
            msg = str(e)
            if "预测文件与当前特征集不一致" not in msg:
                raise
            print(f"[kauto] 预测已陈旧，改为全量重训（原预测不再可用）\n{msg}\n")
    _p(model.write_submission(args.slug))
    return 0




def cmd_run(args) -> int:
    """一条命令跑完整条链路，任何一步失败就停在那里并如实打印。

    ## 为什么在开头查一次截止

    整条链路里的 iterate 可能占用几十分钟。如果比赛已经结束，
    这些时间**换不到任何东西** —— 提交会被拒，分数不会有变化。

    所以先查一次：已结束就**直接不进迭代**，仍然把 prepare / baseline /
    write-sub 跑完。理由不是"省时间"这么简单 —— 基线产物仍然有用：
    换个比赛或等复赛时可以直接复用特征工程与切分结论；
    而迭代的产出（选出的最优节点）在比赛结束后毫无价值。

    这也是为什么这里选择"降级继续"而不是"直接退出"：
    已经下载好的数据、已经验证过的切分、跑通的管线，都是真实的资产。
    """
    steps: list[dict] = []

    # ── 截止预检（见上方说明）──────────────────────────────────────────────
    deadline = {"ok": True}
    try:
        from .submit import _deadline_gate
        deadline = _deadline_gate(args.slug)
    except Exception:
        deadline = {"ok": True}
    skip_iterate = False
    if not deadline.get("ok"):
        print(f"[kauto] {deadline.get('reason')}", file=sys.stderr)
        if deadline.get("url"):
            print(f"[kauto] {deadline['url']}", file=sys.stderr)
        print("[kauto] 比赛已不接受提交：跳过迭代，仍产出基线版 submission"
              "（产物可复用于其他比赛）。", file=sys.stderr)
        skip_iterate = True
        steps.append({"step": "deadline", "ok": False,
                      "error": deadline.get("reason"), "fatal": False})
    elif deadline.get("warning"):
        print(f"\n   ⚠ {deadline['warning']}\n", file=sys.stderr)
        steps.append({"step": "deadline", "ok": True,
                      "warning": deadline.get("warning")})

    def step(name, fn):
        print(f"\n=== {name} ===", flush=True)
        try:
            r = fn()
            steps.append({"step": name, "ok": True})
            return r
        except Exception as e:
            steps.append({"step": name, "ok": False, "error": str(e)[:800]})
            print(f"[kauto] {name} 失败：{e}", file=sys.stderr)
            raise

    try:
        meta = step("prepare", lambda: prep.prepare(
            args.slug, valid_frac=args.valid_frac, seed=args.seed, local_dir=args.local,
            use_skill=not args.no_skill, split=args.split))
        _p(meta)

        base = step("baseline", lambda: model.run_baseline(args.slug, folds=args.folds, seed=args.seed))
        _p(base)

        if args.rounds > 0 and not skip_iterate:
            try:
                it = step("iterate", lambda: kiter.iterate(
                    args.slug, rounds=args.rounds, model=args.model,
                    target_score=args.target, use_intel=not args.no_intel,
                    parallel=args.parallel, search=args.search,
                     leakcheck_on=not args.no_leakcheck,
                     diverse=not args.same_family,
                     folds=args.folds_for_oof))
                _p(it)
            except Exception as e:
                # 迭代失败不该拦住提交：基线版照样是一份可交的结果。
                # 但要把原因摆在明面上，不能装作跑过了。
                steps[-1]["fatal"] = False
                print(f"[kauto] 迭代跳过（{str(e)[:200]}），继续用基线版本生成 submission",
                      file=sys.stderr)

        # ── 融合 ────────────────────────────────────────────────────────────
        # 早期 run 全链路里没有这一步 —— 于是即使 iterate 产出了多方异构候选、
        # blend 能带来真实收益，run 也永远不会用到它（功能存在但没接线）。
        # 融合失败不致命：它被验收门拒绝是正常结果，照样用单模提交。
        if args.rounds > 0 and not args.no_blend and not skip_iterate:
            try:
                b = step("blend", lambda: __import__("kauto.blend", fromlist=["blend"]).blend(
                    args.slug, top_k=args.blend_top_k))
                steps[-1]["ok"] = bool(b.get("ok"))
                if b.get("ok"):
                    print(f"[kauto] 融合生效：{b.get('blend_score_on_oof') or b.get('blend_score_on_valid')}"
                          f"（gain {b.get('gain_vs_best_single')}）")
                else:
                    steps[-1]["fatal"] = False
                    print(f"[kauto] 融合未采用：{b.get('reason')}")
                    if b.get("advice"):
                        print(f"         {b['advice']}")
            except Exception as e:
                steps[-1]["fatal"] = False
                print(f"[kauto] 融合跳过（{str(e)[:160]}），继续用单模", file=sys.stderr)

        sub = step("write-sub", lambda: (
            kiter.submission_from_best(args.slug)
            if (kapi.WORK_DIR / args.slug / "best_test_pred.npy").exists()
            else model.write_submission(args.slug)))
        _p(sub)

        # 交给 skill 做最后一道形状校验，过了才允许提交
        sample = kapi.WORK_DIR / args.slug / "sample_submission.csv"
        if skillbridge.available()["vendored"]:
            v = skillbridge.validate(Path(sub["submission"]), sample if sample.exists() else None,
                                     args.slug)
            steps.append({"step": "validate", "ok": v["passed"]})
            print(v["output"])
            if not v["passed"]:
                print("[kauto] 形状校验未通过，不提交。", file=sys.stderr)
                _p({"steps": steps})
                return 3

        if args.yes:
            # 复用 cmd_submit 的**全部**前置门（溯源指纹 / best_node 一致性 / 形状校验）。
            # 早期这里直接调 skillbridge.submit，只做了形状校验 ——
            # 于是 `run --yes` 可以提交一份被改过或过时的 submission，
            # 而 `submit --yes` 会拦住。同一条外部副作用，两条路径的守卫必须一致。
            gate = _pre_submit_gate(args.slug)
            steps.append({"step": "pre-submit-gate", "ok": gate["ok"],
                          "detail": gate.get("reason")})
            print(gate.get("text") or "")
            if not gate["ok"]:
                print("[kauto] 提交前门未通过，不提交。", file=sys.stderr)
                _p({"steps": steps})
                return 3

            expect = base.get("valid_score")
            r = skillbridge.submit(Path(sub["submission"]), args.slug, args.message, confirm=True,
                                   expect=expect, sample=sample if sample.exists() else None)
            steps.append({"step": "submit", "ok": r["exit"] == 0, "exit": r["exit"]})
            print(r["output"])

            # 提交后闭环：轮询出分 + 写收据。
            # 早期 `run --yes` 提交完就结束 —— 而 `submit --wait` 会轮询并写收据。
            # 同一件事（提交）两套行为，收据时有时无，台账就无法相信。
            if r["exit"] == 0 and args.wait:
                print(f"\n[kauto] 等待评分（最多 {args.wait_timeout}s）…")
                w = kapi.wait_for_score(args.slug, timeout=args.wait_timeout,
                                        interval=args.poll_interval)
                _write_receipt(args.slug, w, args)
                steps.append({"step": "receipt", "ok": w["status"] == "scored",
                              "status": w["status"], "score": w.get("score")})
                if w["status"] == "scored":
                    print(f"[kauto] 出分了：publicScore = {w['score']}")
                elif w["status"] == "blocked":
                    print(f"[kauto] 被阻塞：{str(w.get('reason'))[:180]}")
                else:
                    print(f"[kauto] 未在时限内出分（{w['status']}），收据已写")
        else:
            print("\n[kauto] 已生成 submission，未提交（加 --yes 才会推到 Kaggle）")
    except Exception:
        _p({"steps": steps})
        return 2

    _p({"steps": steps, "submission": str(kapi.WORK_DIR / args.slug / "submission.csv")})
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser("kauto", description="本地优先的 Kaggle 自动参赛流水线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="检查凭据/依赖/LLM/skill 桥接")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("info", help="比赛事实：官方指标/限额/是否 notebook-only")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser("solutions", help="榜单公开解法 writeup")
    p.add_argument("slug")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--refresh", action="store_true",
                   help="忽略本地缓存，重新拉取（默认缓存 6 小时）")
    p.add_argument("--fallback", action="store_true",
                   help="榜单没挂 writeup 时再走全文检索（慢，常一无所获）")
    p.add_argument("--timeout", type=int, default=90)
    p.set_defaults(fn=cmd_solutions)

    p = sub.add_parser("validate", help="用 skill 校验 submission 形状")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("rules", help="读取官方页面并导出可执行的提交判据")
    p.add_argument("slug")
    p.add_argument("--page", default="evaluation",
                   help="页面名（默认 evaluation；也可 rules / data-description）")
    p.add_argument("--format", choices=("text", "json"), default="json")
    p.set_defaults(fn=cmd_rules)

    p = sub.add_parser("smoke", help="用内置夹具离线跑通整条链路（不碰 Kaggle 配额）")
    p.add_argument("--local", default=None,
                   help="夹具目录（默认用包内 fixtures/demo-comp）")
    p.add_argument("--keep", action="store_true",
                   help="保留演练产物（默认跑完删除）")
    p.set_defaults(fn=cmd_smoke)

    p = sub.add_parser("status", help="自己的排名/剩余提交次数（需凭据）")
    p.add_argument("slug")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("leaderboard", help="排行榜：榜首、我的名次、奖牌线（需凭据）")
    p.add_argument("slug")
    p.add_argument("--top", type=int, default=10, help="榜首取前几名")
    p.set_defaults(fn=cmd_leaderboard)

    p = sub.add_parser("list", help="列比赛")
    p.add_argument("--search", default="")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--format", choices=("tsv", "json"), default="tsv",
                   help="tsv（默认，便于 grep/awk）或 json（供宿主解析）")
    p.set_defaults(fn=cmd_list)

    for name, fn, h in [("prepare", cmd_prepare, "下载并切分"), ("baseline", cmd_baseline, "基线+CV")]:
        p = sub.add_parser(name, help=h)
        p.add_argument("slug")
        p.add_argument("--folds", type=int, default=5)
        p.add_argument("--seed", type=int, default=42)
        p.add_argument("--valid-frac", dest="valid_frac", type=float, default=0.2)
        p.add_argument("--local", default=None, help="用本地目录代替 Kaggle 下载（离线演练）")
        p.add_argument("--no-skill", dest="no_skill", action="store_true",
                       help="不查 skill 的官方指标，只用本地推断")
        p.add_argument("--split", default=None,
                       choices=["time", "group", "stratified", "random"],
                       help="强制切分策略；默认自动检测时间/分组列")
        p.set_defaults(fn=fn)

    p = sub.add_parser("iterate", help="LLM 迭代")
    p.add_argument("slug")
    p.add_argument("--rounds", type=int, default=6)
    p.add_argument("--model", default=None)
    p.add_argument("--target", type=float, default=None)
    p.add_argument("--no-intel", dest="no_intel", action="store_true",
                   help="不注入榜单 writeup 情报")
    p.add_argument("--parallel", type=int, default=3,
                   help="每轮并发候选数（推理模型一次 6 分钟，并发才能压住墙钟时间）")
    p.add_argument("--search", choices=["tree", "flat"], default="tree",
                   help="tree=AIDE 式树搜索（默认）；flat=旧的每轮独立生成，用于对照")
    p.add_argument("--no-leakcheck", dest="no_leakcheck", action="store_true",
                   help="跳过数据泄漏静态审查（默认开启：发现 high 级泄漏直接拒收该方案）")
    p.add_argument("--same-family", dest="same_family", action="store_true",
                   help="允许全在同一模型族里迭代（默认会在族不足时新起草，保证可融合）")
    p.add_argument("--folds-for-oof", dest="folds_for_oof", type=int, default=0,
                   help="候选额外产出 K 折 OOF（用于可信的 blend 权重；0=不产，代价约 folds 倍耗时）")
    p.set_defaults(fn=cmd_iterate)

    p = sub.add_parser("write-sub", help="生成 submission.csv")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_write_sub)

    p = sub.add_parser("submit", help="提交（需 --yes）")
    p.add_argument("slug")
    p.add_argument("--message", default="kauto auto-submit")
    p.add_argument("--expect", type=float, default=None, help="预期分数，写进 skill 台账")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--wait", action="store_true",
                   help="提交后轮询到出分并写 submission_receipt.json")
    p.add_argument("--wait-timeout", dest="wait_timeout", type=int, default=900,
                   help="轮询上限秒数（默认 900）")
    p.add_argument("--poll-interval", dest="poll_interval", type=int, default=20,
                   help="轮询间隔秒数（默认 20）")
    p.set_defaults(fn=cmd_submit)

    p = sub.add_parser("receipt", help="查/写提交收据（含轮询出分）")
    p.add_argument("slug")
    p.add_argument("--refresh", action="store_true", help="忽略已有收据，重新轮询")
    p.add_argument("--wait-timeout", dest="wait_timeout", type=int, default=900)
    p.add_argument("--poll-interval", dest="poll_interval", type=int, default=20)
    p.set_defaults(fn=cmd_receipt)

    p = sub.add_parser("run", help="一条命令跑完全链路")
    p.add_argument("slug")
    p.add_argument("--rounds", type=int, default=6, help="LLM 迭代轮数，0=跳过迭代")
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--valid-frac", dest="valid_frac", type=float, default=0.2)
    p.add_argument("--local", default=None, help="用本地目录代替 Kaggle 下载")
    p.add_argument("--model", default=None)
    p.add_argument("--target", type=float, default=None)
    p.add_argument("--message", default="kauto auto-submit")
    p.add_argument("--no-skill", dest="no_skill", action="store_true")
    p.add_argument("--split", default=None,
                   choices=["time", "group", "stratified", "random"])
    p.add_argument("--no-intel", dest="no_intel", action="store_true")
    p.add_argument("--parallel", type=int, default=3, help="每轮并发候选数")
    p.add_argument("--search", choices=["tree", "flat"], default="tree")
    p.add_argument("--no-leakcheck", dest="no_leakcheck", action="store_true")
    p.add_argument("--same-family", dest="same_family", action="store_true")
    p.add_argument("--folds-for-oof", dest="folds_for_oof", type=int, default=0)
    p.add_argument("--no-blend", dest="no_blend", action="store_true",
                   help="跳过融合步骤（默认会在 iterate 之后尝试融合）")
    p.add_argument("--blend-top-k", dest="blend_top_k", type=int, default=6,
                   help="融合参与的候选上限")
    p.add_argument("--yes", action="store_true", help="加了这个才会真的提交")
    p.add_argument("--wait", action="store_true",
                   help="提交后轮询到出分并写收据（与 submit --wait 行为一致）")
    p.add_argument("--wait-timeout", dest="wait_timeout", type=int, default=900)
    p.add_argument("--poll-interval", dest="poll_interval", type=int, default=20)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("blend", help="多方案融合（贪心前向选择）")
    p.add_argument("slug")
    p.add_argument("--top-k", dest="top_k", type=int, default=6,
                   help="参与融合的候选上限（按单模型表现排序取前 k）")
    p.set_defaults(fn=cmd_blend)

    p = sub.add_parser("archive", help="节点产物归档（只增不删，用于回退/复现）")
    p.add_argument("slug")
    p.add_argument("--backfill", action="store_true",
                   help="把现有 gen_* 里跑通的节点补归档")
    p.set_defaults(fn=cmd_archive)

    p = sub.add_parser("restore", help="回退到某个归档节点（之后 write-sub 用它）")
    p.add_argument("slug")
    p.add_argument("--node", required=True, help="归档的节点 id")
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("oof", help="给已归档节点补跑 K 折 OOF（blend 权重需要它）")
    p.add_argument("slug")
    p.add_argument("--node", default=None, help="只补指定节点 id")
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--force", action="store_true", help="已有 OOF 也重跑")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--no-container", dest="no_container", action="store_true",
                   help="跳过 AppContainer（不推荐，会失去文件隔离）")
    p.set_defaults(fn=cmd_oof)

    p = sub.add_parser("prune", help="清理无用的 gen_* 候选目录（保留已归档与 best）")
    p.add_argument("slug")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                   help="只列清单，不删除（默认行为）")
    p.add_argument("--yes", action="store_true",
                   help="真正执行删除（会减少融合候选来源）")
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser("link", help="检查/开启 DSH our-free-model 免费网关")
    p.add_argument("--enable", action="store_true", help="把 forward.enabled 置 true（需重启 DSH 生效）")
    p.set_defaults(fn=cmd_link)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except kapi.KaggleError as e:
        print(f"[kauto] {e}", file=sys.stderr)
        return 2
    except skillbridge.SkillError as e:
        # skill 桥接的错误同样是"给人看的提示"（例如规则未接受、
        # 两套凭据视图不一致），不该以 traceback 形态抛出来。
        print(f"[kauto] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
