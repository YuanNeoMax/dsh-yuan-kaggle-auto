"""提交闭环：加入检查 → 提交前守卫 → 提交 → 写收据 → 回看收据。

## 为什么单独成模块

这五个函数围绕同一件事，且是**外部状态变更**（提交到 Kaggle）的唯一通道。
它们原本散在 `cli.py` 里（976 行），和 doctor/prune/archive 这些本地命令混在一起，
读代码时要跨 200 多行才能看清一次提交发生了什么。

## 设计要点

**提交前有四道门，顺序不能乱**：
1. `_check_entered` —— 账号是否已加入比赛（未加入会 403，`--wait` 会空转）
2. 文件自检 —— 列名/行数/id 完全匹配 sample_submission
3. `_pre_submit_gate` —— 溯源一致性（同一份 csv 不能对应不同的 node_score）
4. `--yes` 明确确认

这份代码里每一条实测教训都写成了注释，因为它们都是"只有真跑才会暴露"的类型。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import pandas as pd

from . import blend as bl
from . import kapi, skillbridge, storage, subcheck


def _p(obj) -> None:
    """带缩进的 JSON 打印 —— 与 cli.py 里的同名辅助保持一致。"""
    print(json.dumps(obj, ensure_ascii=False, indent=1))

def _deadline_gate(slug: str) -> dict:
    """比赛是否还收提交。

    ## 这个门解决的是"错误信息指向错误方向"的问题

    比赛结束（或提交入口关闭）后调提交接口，Kaggle 会拒绝，但本地拿到的
    通常只是一个泛泛的错误。人看到它的第一反应是"我的文件有问题"，
    于是反复改文件、重训、重交 —— 而真正的原因只有一个：这个比赛已经不收了。

    所以在这里先把"收不收"查清楚，能早一步拦下就拦下，并把截止时间与
    入口状态原样告诉用户，避免整轮瞎试。

    ## 判断依据是两处独立证据

    · `GetCompetition` 的 `deadline` / `submissionsDisabled`（站内 API）
    · `brief` 的 `deadline` / `submissions_disabled`（skill 桥接，如果有）

    两者都拿不到时**不拦**（返回 ok=True 并附 warning）——
    查不到不等于已结束，因为"读不到"与"已结束"是两件不同的事，
    把后者当结论会造成假拦截。

    信任级别：`submissionsDisabled=true` 是硬证据（服务端明确说不收）；
    仅 `deadline` 已过则降级为 warning 而不是硬拦 ——
    有的比赛截止后仍接受 late submission，硬拦会把可用的机会拦掉。
    """
    from . import kapi

    facts: dict = {}
    try:
        raw = kapi.site_api(
            "/api/i/competitions.CompetitionService/GetCompetition"
            f"?competitionName={kapi.urllib.request.quote(slug)}"
        )
        if raw.get("ok"):
            data = raw.get("data") or {}
            facts["deadline"] = data.get("deadline")
            facts["submissions_disabled"] = data.get("submissionsDisabled")
            facts["has_leaderboard"] = data.get("hasLeaderboard")
    except Exception:
        pass

    if "deadline" not in facts:
        return {"ok": True,
                "warning": f"无法确认 {slug} 的截止时间（站内 API 不可用）；"
                           f"提交失败时先怀疑比赛已结束，而不是文件格式。"}

    if facts.get("submissions_disabled") is True:
        return {"ok": False,
                "reason": f"{slug} 的提交入口已在服务端关闭（submissionsDisabled=true）。",
                "url": f"https://www.kaggle.com/competitions/{slug}/leaderboard"}

    deadline_text = str(facts.get("deadline") or "")
    if not deadline_text:
        return {"ok": True, "warning": f"{slug} 未给出截止时间，无法预判。"}

    expired, parsed = _parse_deadline(deadline_text)
    if parsed is None:
        return {"ok": True, "warning": f"截止时间格式无法解析：{deadline_text}"}
    if expired:
        return {"ok": True,
                "warning": f"截止时间已过（{deadline_text}）。"
                           f"提交仍会尝试 —— 有的比赛接受迟到提交；"
                           f"若被拒，别再改文件重试，先去看比赛页是否已结束。"}
    return {"ok": True, "deadline": deadline_text}


def _parse_deadline(text: str) -> tuple[bool, object]:
    """返回 (是否已过, 解析出的时间)。解析不了时时间为 None。"""
    import datetime as _dt

    raw = text.strip().replace("Z", "+00:00")
    try:
        moment = _dt.datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                moment = _dt.datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return False, None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    now = _dt.datetime.now(_dt.timezone.utc)
    return moment <= now, moment


def _check_entered(slug: str) -> dict:
    """提交前必须确认账号已加入该比赛。

    实测踩到：titanic 的**数据能下载**（公开数据，历史遗留），但
    `userHasEntered = False` —— 提交会 403。而 `submission-limits` 返回
    `Remaining today: 10`（看起来正常），`files` 也能列 ——
    **没有任何一个探针能暴露"未加入"这件事**，只有 list 里的 userHasEntered 字段。

    于是 `submit --yes --wait` 会一直轮询一个永远不会出分的提交。

    ## 第 60 轮修正：上面那句"没有任何探针"是错的

    找到了更准的一手探针（`kapi.competition_user_state`）：

        GET /api/i/competitions.UserService/GetUser?competitionName=<slug>
        → {"hasAcceptedRules": false, "canUnregister": false}

    它比 `list().userHasEntered` 高一层 —— 那是公开列表里的二手字段，
    这是"我本人和这场比赛的关系"。策略是先问一手、再退回二手：

    1. `hasAcceptedRules == True` → 一定已加入，直接放行；
    2. `hasAcceptedRules is False` → 明确未接受规则，拦下；
    3. 探针不可用（401 过期 / 断网 / 未知 slug）→ 退回 `list` 的
       `userHasEntered`，并**在 reason 里注明判据来源**，避免事后
       分不清"结论来自一手还是二手"。

    注意 signal 的强弱：一手返回 False 足够拦，但一手返回 True 也不能
    完全断定能提交（它不带提交权限信息）—— 所以 True 之后仍走原有流程，
    真提交时若 403 上层会翻译成可操作指引。
    """
    # ## 第 61 轮修正：这个门**根本不是提交的通行证**
    #
    # 两个被实测推翻的旧假设：
    #
    # (a) `hasAcceptedRules=false` ≠ 没加入 —— titanic 的 rules 页写着
    #     "Rules acceptance is not required"，对该类比赛这个字段恒为 false，
    #     旧代码拿它当硬拦截，于是永远提交不了（被误记成"只能人工"）。
    #
    # (b) **未加入也不会被 403** —— 实测 digit-recognizer：提交前
    #     userHasEntered=False，直接提交却成功（ref=57866099，
    #     publicScore=0.09896），提交后 userHasEntered 才变成 True。
    #     也就是说提交动作本身就会把账号加入比赛，"先加入才能提交"是错的。
    #
    # 所以这里的语义是**提示而非拦截**：判不出、或判成"未加入"时，
    # 打印建议并放行，让 Kaggle 自己回答（真 403 会在 direct_submit 里
    # 以明确错误返回）。继续硬拦只会把"能提交的比赛"挡在门外。
    st = kapi.entered_state(slug)
    note = st.get("note") or ""
    if st.get("entered") is True:
        return {"entered": True, "source": st.get("source", "none"), "reason": ""}
    if st.get("entered") is False:
        if st.get("source") == "hasAcceptedRules":
            hint = (f"{slug} 要求先接受规则（hasAcceptedRules=false）。"
                    f"若提交返回 403，打开 "
                    f"https://www.kaggle.com/competitions/{slug}/rules 点 "
                    f"「I Understand and Accept」后重试")
        else:
            hint = (f"{note}列表显示尚未加入 {slug}（userHasEntered=false），"
                    f"但实测提交本身会自动加入（digit-recognizer 已验证），"
                    f"因此不拦。若返回 403，打开 "
                    f"https://www.kaggle.com/competitions/{slug}/rules 点「Join」")
        # 放行：entered 仍如实记录 False，但 reason 只作为提示。
        return {"entered": False, "source": st.get("source", "none"),
                "reason": hint, "advisory": True}
    return {"entered": None, "source": st.get("source", "none"),
            "reason": f"{note}{st.get('reason', '')}", "advisory": True}



def _pre_submit_gate(slug: str) -> dict:
    """提交前的全部门：溯源指纹一致性 + best_node 一致性。

    ## 为什么要抽成公共函数

    实测缺陷：`kauto run --yes` 直接调 skillbridge.submit，
    **绕过了** `kauto submit --yes` 里的溯源与一致性检查 ——
    同一条外部副作用（推进 Kaggle），两条路径的守卫不一致。
    那意味着 `run --yes` 能提交一份被改过或过时的 submission，
    而 `submit --yes` 会拦住。

    统一到这里，两条路径共用，不会再 drift。
    返回 {ok, reason, text}。
    """
    import hashlib

    work = kapi.WORK_DIR / slug
    sub = work / "submission.csv"
    if not sub.exists():
        return {"ok": False, "reason": f"缺 {sub}，先跑 write-sub", "text": ""}

    prov_path = work / "submission_provenance.json"
    prov = None
    if prov_path.exists():
        try:
            prov = json.loads(prov_path.read_text(encoding="utf-8"))
        except Exception:
            prov = None

    if not prov:
        return {"ok": True, "text": "[kauto] 注意：没有 provenance，无法确认来源",
                "reason": ""}

    actual = hashlib.sha256(sub.read_bytes()).hexdigest()[:16]
    recorded = prov.get("submission_sha256_16")
    node = prov.get("node_id")
    text = (f"来源：{prov.get('source')}　节点：{node or '-'}　"
            f"节点分数：{prov.get('node_score')}　族：{prov.get('node_family') or '-'}")

    if recorded and actual != recorded:
        return {"ok": False, "text": text,
                "reason": (f"submission 已被改过（指纹 {actual} ≠ 记录 {recorded}）。"
                           "这份文件与 provenance 记录的不是同一次产出，请重跑 write-sub。")}

    if node:
        best_now = work / "best_node.txt"
        if best_now.exists():
            cur = best_now.read_text(encoding="utf-8").strip()
            if cur and cur != node:
                return {"ok": False, "text": text,
                        "reason": (f"best_node 已是 {cur}，但 submission 来自 {node}。"
                                   "先 restore 后没重跑 write-sub —— 提交的会是旧方案。")}

    return {"ok": True, "text": text, "reason": ""}


def cmd_submit(args) -> int:
    sub = kapi.WORK_DIR / args.slug / "submission.csv"
    if not sub.exists():
        print(f"缺 {sub}，先跑 write-sub")
        return 1
    import hashlib

    import pandas as pd
    df = pd.read_csv(sub)
    print(f"提交前自检：{sub}  行数={len(df)}  列={list(df.columns)}")

    # notebook-only 比赛交不了 csv —— 早于其它所有检查就拦住，
    # 免得跑一堆验证最后发现根本提交不了。
    meta_p = kapi.WORK_DIR / args.slug / "meta.json"
    meta_now = {}
    if meta_p.exists():
        try:
            meta_now = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            meta_now = {}
    if meta_now.get("notebook_only"):
        print("[kauto] 这场比赛是 notebook-only：只能提交 notebook，不能提交 csv。",
              file=sys.stderr)
        print("        本工具产出的是 submission.csv，无法提交。", file=sys.stderr)
        return 3

    gate = _pre_submit_gate(args.slug)
    print(gate.get("text") or "")
    if not gate["ok"]:
        print(f"[kauto] {gate.get('reason')}", file=sys.stderr)
        return 3

    # 真提交前先过 skill 的形状校验：用错列/缺行会被 Kaggle 直接判废，白烧一次配额
    sample = kapi.WORK_DIR / args.slug / "sample_submission.csv"
    if skillbridge.available()["vendored"]:
        v = skillbridge.validate(sub, sample if sample.exists() else None, args.slug)
        print(v["output"])
        if not v["passed"] and args.yes:
            print("[kauto] 形状校验未通过，已拒绝提交。")
            return 3

    if not args.yes:
        print("\n[kauto] 未提交：加 --yes 才会真正推到 Kaggle。")
        return 2

    # ── 截止检查：放在 --yes 之后、真正提交之前 ────────────────────────────
    #
    # 为什么必须有这一步：比赛结束（或提交入口关闭）后调用提交接口，Kaggle 会
    # 拒绝，但**本地拿不到"为什么"** —— 表现为一个很泛的错误，而人很容易
    # 把它读成"文件有问题"，于是反复改文件重试。真正的原因只有一个：这个
    # 比赛已经不收了。所以先把这件事查清楚并直接拦下来，省掉整轮瞎试。
    #
    # 放在 `--yes` 之后是刻意的：`--dry-run`/预检路径不该因此失败 ——
    # 那些调用本来就不提交，检查截止反而会掩盖真正的形状问题。
    deadline = _deadline_gate(args.slug)
    if not deadline["ok"]:
        print(f"[kauto] {deadline['reason']}", file=sys.stderr)
        if deadline.get("url"):
            print(f"[kauto] {deadline['url']}", file=sys.stderr)
        return 4
    if deadline.get("warning"):
        print(f"\n   ⚠ {deadline['warning']}\n")

    expect = None
    bl = kapi.WORK_DIR / args.slug / "baseline.json"
    if bl.exists():
        try:
            expect = json.loads(bl.read_text(encoding="utf-8")).get("valid_score")
        except Exception:
            expect = None
    # 参赛状态**只提示不拦截**（理由见 _check_entered 的第 61 轮修正：
    # 实测未加入的 digit-recognizer 直接提交就成功了，提交动作本身会加入）。
    ent = _check_entered(args.slug)
    if ent.get("reason"):
        print(f"\n   ⚠ {ent['reason']}\n")

    r = skillbridge.submit(sub, args.slug, args.message, confirm=True,
                           expect=args.expect if args.expect is not None else expect,
                           sample=sample if sample.exists() else None)
    print(r["output"])
    if r["exit"] != 0:
        return r["exit"]

    # 提交后闭环：轮询到出分，写 submission_receipt.json。
    # 参照 submission-endgame.md：必须到达 scored 或 blocked 才停。
    if not args.wait:
        print("\n[kauto] 已提交。加 --wait 会轮询到出分并写收据。")
        return 0

    print(f"\n[kauto] 等待评分（最多 {args.wait_timeout}s）…")
    w = kapi.wait_for_score(args.slug, timeout=args.wait_timeout,
                            interval=args.poll_interval)
    _write_receipt(args.slug, w, args)
    if w["status"] == "scored":
        print(f"[kauto] 出分了：publicScore = {w['score']}")
    elif w["status"] == "blocked":
        print(f"[kauto] 被外部条件阻塞：{w.get('reason','')[:200]}")
    else:
        print(f"[kauto] 未在时限内出分（{w['status']}），收据已写，稍后可重跑 kauto receipt")
    return 0


def _write_receipt(slug: str, w: dict, args=None) -> Path:
    """把一次提交的结果固化成 submission_receipt.json。

    endgame 文档要求「Save the latest submission table or parsed record to
    submission_receipt.json」并把分数记入实验台账。
    与 provenance 的区别：provenance 记「这份文件从哪来」，receipt 记「提交后 Kaggle 怎么说」。
    两者合起来才能回答"这个分数是哪个方案、用什么文件、什么时候拿到的"。
    """
    import time as _t

    work = kapi.WORK_DIR / slug
    work.mkdir(parents=True, exist_ok=True)

    prov = {}
    pp = work / "submission_provenance.json"
    if pp.exists():
        try:
            prov = json.loads(pp.read_text(encoding="utf-8"))
        except Exception:
            prov = {}

    rec = {
        "slug": slug,
        "status": w.get("status"),
        "public_score": w.get("score"),
        "waited_seconds": w.get("waited_seconds"),
        "poll_count": w.get("poll_count"),
        "submitted_at": w.get("submitted_at"),
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        # 把 provenance 并进来，避免两份文件各自独立、事后对不上
        "provenance": prov or None,
        "rows": (w.get("rows") or [])[:10],
        "reason": w.get("reason"),
    }

    # 追加历史，不覆盖：同一场比赛会提交多次，每次都要能回看
    hist_p = work / "submission_receipt_history.json"
    hist = []
    if hist_p.exists():
        try:
            hist = json.loads(hist_p.read_text(encoding="utf-8"))
            if not isinstance(hist, list):
                hist = []
        except Exception:
            hist = []
    # ── 历史压缩：不做的话会无限膨胀 ─────────────────────────────────────
    #
    # 实测：每次带 10 行 rows 的记录约 2439 字节，100 次提交 = 238 KB，
    # 1000 次 = 2.3 MB，而且**每次写都要全量重读+重写**。
    # Kaggle 每天限额 10 次，长期用下去这个问题会一直恶化。
    #
    # 三件事：
    #   1. 历史条目只留关键字段（去掉 rows —— 它在 receipt 主文件里已有）
    #   2. 只保留最近 MAX_HISTORY 条（早期记录对排查没用，当前状态更重要）
    #   3. 主文件 submission_receipt.json 仍保留完整 rows（那是"最近一次"的详情）
    MAX_HISTORY = 50
    slim = {k: rec.get(k) for k in
            ("slug", "status", "public_score", "submitted_at",
             "written_at", "poll_count", "waited_seconds")}
    prov_s = rec.get("provenance") or {}
    slim["node_id"] = prov_s.get("node_id")
    slim["prediction_source"] = prov_s.get("prediction_source")
    slim["node_score"] = prov_s.get("node_score")

    hist.append(slim)
    if len(hist) > MAX_HISTORY:
        hist = hist[-MAX_HISTORY:]

    storage.write_json(work / "submission_receipt.json", rec)
    storage.write_json(hist_p, hist)
    return work / "submission_receipt.json"


def cmd_receipt(args) -> int:
    """查收据：已有则打印，没有则重新轮询一次。"""
    work = kapi.WORK_DIR / args.slug
    p = work / "submission_receipt.json"
    if p.exists() and not args.refresh:
        _p(json.loads(p.read_text(encoding="utf-8")))
        return 0
    w = kapi.wait_for_score(args.slug, timeout=args.wait_timeout,
                            interval=args.poll_interval)
    _write_receipt(args.slug, w)
    _p(w)
    return 0 if w.get("status") == "scored" else 3
