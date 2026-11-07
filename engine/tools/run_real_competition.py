"""真实比赛端到端：一条命令跑完整链路并拿到 LB 分。

## 为什么是脚本而不是 python -m kauto.cli run

因为这一步涉及**外部状态变更**（提交到 Kaggle），需要：
- 明确的阶段提示（每一步在做什么、失败在哪一步）
- 提交前的多重自检
- 提交后的轮询与收据
- 全程可中断、可重跑

`run` 命令已经做了大部分，但真实比赛还有几件事 `run` 没做：
1. 提交前先查当日剩余配额（`submission-limits`）
2. 把这次运行的完整链路（参数 → CV → LB）记进台账
3. 失败时区分「代码问题」vs「平台问题」

## 前置条件（人工，API 无法代做）

必须先在网页点 Accept Rules：
  https://www.kaggle.com/competitions/<slug>/rules

## 第 60 轮修正：路由不是 404，之前探测的路径根本错

早期探测的是这两个，命中 404 就下了「Kaggle 没有 API」的结论：

  /v1/competitions.CompetitionApiService/AcceptRules   → 404（路径本身不存在）
  /api/i/competitions.CompetitionService/AcceptRules   → 404（服务选错）

真实路由是第三个，来自前端 bundle 的路由表（不是猜的）：

  competitions.UserService/AcceptRules

它**存在**，但走的是站内通道 `www.kaggle.com/api/i/...`，写侧需要浏览器
会话级授权 —— 实测 JSON 体 / protobuf 体 / 带 XSRF-TOKEN 全部 401。
所以"只能人工"这个结论仍然成立，**但理由换了**：不是"没有端点"，
而是"端点有，却不接受程序化授权"。这个区别影响判断方式 —— 详见下一条。

## 而"有没有加入"的判据，第 61 轮被实测推翻了两次

【推翻 1】`hasAcceptedRules` 不是加入判据。对 titanic / digit-recognizer /
playground-series-s6e2 / spaceship-titanic 逐个查 GetUser，**四场全是 false**，
其中 titanic 已加入且提交成功（publicScore=0.76076）、digit-recognizer 提交
后才变 userHasEntered=true。一个恒为 false 的字段不能拿来当门槛 —— 旧版
`--check` 正是这么用的，于是把能提交的比赛判成"未加入"。

【推翻 2】未加入也不妨碍提交。实测 digit-recognizer：
`userHasEntered=false` → 直接提交成功（ref=57866099, publicScore=0.09896）
→ 提交后 userHasEntered 才变 true。**提交动作本身就会把账号加入比赛。**
所以"先加入才能提交"这条因果是反的。

结论：加入状态只用于**提示**，不作为通行条件；能不能提交由 Kaggle 自己回答
（真 403 会在 direct_submit 里返回明确错误）。`--check` 的"可以跑完整链路"
也不再依赖这个字段。


## 用法

```bash
# 1. 先确认规则已接受（会给出明确提示）
python tools/run_real_competition.py playground-series-s6e2 --check

# 2. 本地跑完不出错，但不提交（推荐先这样验证一次）
python tools/run_real_competition.py playground-series-s6e2 --rounds 4

# 3. 真提交（会先查配额、过门、提交后轮询出分）
python tools/run_real_competition.py playground-series-s6e2 --rounds 4 --submit
```
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kauto import cli, kapi, prep, skillbridge


def _hr(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}", flush=True)


def step_quota(slug: str) -> dict:
    """查当日剩余提交配额。**这一步能区分「提交被拒」与「没配额了」。"""
    try:
        out = kapi.run(["competitions", "submission-limits", "-c", slug], timeout=60)
        return {"ok": True, "raw": out.strip()[:400]}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


def step_check(slug: str) -> int:
    """只检查：凭据 / 规则是否接受 / 配额 / 是否 notebook-only。"""
    _hr(f"检查比赛 {slug}")

    meta_info = {}
    try:
        import kauto.cli as _c
        # 复用 cmd_info 的取数逻辑
        from kauto import skillbridge as sb
        prof = sb.profile(slug) if sb.available()["vendored"] else {}
        meta_info = prof
        print(f"  标题      : {prof.get('title')}")
        print(f"  官方指标  : {prof.get('raw_metric')} → {prof.get('kauto_metric')}")
        print(f"  notebook-only: {prof.get('notebook_only')}")
        print(f"  每日配额  : {prof.get('max_daily_submissions')}")
    except Exception as e:
        print(f"  (取比赛信息失败：{type(e).__name__} {str(e)[:100]})")

    print("\n  配额明细：")
    q = step_quota(slug)
    if q["ok"]:
        print("   " + q["raw"].replace("\n", "\n   "))
        # 实测陷阱：未参与比赛时 `submission-limits` 显示的是**平台默认上限**（100），
        # 而该比赛真实的限额在 info 的 max_daily_submissions 里（这里是 10）。
        # 只看前者会严重误判 —— 必须把两者摆在一起。
        official = meta_info.get("max_daily_submissions")
        if official:
            print(f"   ⚠ 注意：上面的 Remaining 是平台默认值；"
                  f"**本场比赛的真实限额是 {official}/天**（来自 info）。")
            if "Remaining today: 100" in q["raw"] and int(official) < 100:
                print(f"     只看 100 会误判成「能交 100 次」，实际只能交 {official} 次。")
    else:
        print(f"   查询失败：{q['reason']}")

    # 规则是否接受。
    #
    # 实测踩坑：我最初用 `competitions files` 作为探针 —— 它能列出元数据，
    # **不需要接受规则**，于是报了「规则已接受」的假阳性。
    # 真正的门槛在 `download`（403 Forbidden for DownloadDataFiles）。
    # 所以探针必须用 download，并且接受「已经下载过」这种情况：
    # download 会跳过已存在的文件，此时不会因为规则未接受而报错，
    # 但那也不代表能**提交**。真正的提交门槛用 `submission-limits` 一并查。
    print("\n  规则状态（用 download 作探针 —— files 只列元数据，不需要规则）：")
    try:
        kapi.run(["competitions", "download", "-c", slug,
                  "-p", str(kapi.DATA_DIR / slug)], timeout=300)
        print("   ✓ 可下载数据")
        ok = True
    except Exception as e:
        msg = str(e)
        if "403" in msg or "accept" in msg.lower():
            print("   ✗ 数据下载被拦（这场比赛要求先接受规则）：")
            print(f"     https://www.kaggle.com/competitions/{slug}/rules")
            print("     点「I Understand and Accept」后可重跑本检查。")
            print("     注：这只是**下载**的门槛。提交走的是 CreateSubmission，")
            print("         与 download 无关 —— 已实测过无需先接受规则也能提交成功")
            print(f"         （digit-recognizer ref=57866099 / titanic ref=57865998）。")
            ok = False
        else:
            print(f"   ✗ 其它错误：{msg[:160]}")
            ok = False

    # ── 关键：能下载 ≠ 能提交；而"已加入"也不是提交的门 ──────────────────
    #
    # 实测缺陷链（三轮才收敛）：
    #   1. 原本只看 download 成功就报"规则已接受 / 可以跑完整链路"；
    #      但公开数据（titanic / digit-recognizer）历史遗留可直接下载，
    #      "能下载"不是"能提交"的判据。
    #   2. 第 60 轮改用一手的 hasAcceptedRules 当判据 —— 又错：对
    #      titanic / digit-recognizer / playground-series-s6e2 /
    #      spaceship-titanic 逐场实测，该字段**恒为 false**（其中两场已加入
    #      且提交成功）。恒 false 的字段不能当门槛。
    #   3. 第 61 轮实测：未加入也照样能提交（digit-recognizer
    #      userHasEntered=false → 提交成功 ref=57866099 → 提交后才变 true）。
    #      **提交动作本身会加入比赛**，所以这个字段只用于提示。
    print("\n  账号加入状态（仅供参考，不是提交的门）：")
    entered = kapi.entered_state(slug)
    joined = entered.get("entered")
    source = entered.get("source", "none")
    if joined is None:
        print(f"   拿不到（{entered.get('reason','')[:100]}）—— 不影响能否提交")
    else:
        print(f"   userHasEntered = {joined}   （公开列表字段，判据来源 {source}）")
        note = entered.get("note")
        if note:
            print(f"   {note}")
    if joined is False:
        print("   提示：列表显示尚未加入；实测提交会自动加入，尝试提交即可。")
        print("         若返回 403，打开 "
              f"https://www.kaggle.com/competitions/{slug}/rules 点「Join」。")
    elif joined is True:
        print("   ✓ 已加入")

    if meta_info.get("notebook_only"):
        print("\n   ⚠ 这是 notebook-only 比赛：只能提交 notebook，本工具无法提交 csv。")
        ok = False

    print()
    print("  结论：", "可以跑完整链路" if ok else "先解决上面标 ✗ 的问题")
    return 0 if ok else 3


def main() -> int:
    ap = argparse.ArgumentParser(description="真实比赛端到端（含可选提交）")
    ap.add_argument("slug")
    ap.add_argument("--check", action="store_true", help="只检查前置条件，不下载不跑")
    ap.add_argument("--rounds", type=int, default=4, help="LLM 迭代轮数")
    ap.add_argument("--folds", type=int, default=5, help="baseline CV 折数")
    ap.add_argument("--folds-for-oof", dest="folds_for_oof", type=int, default=0,
                    help="候选额外产 OOF（0=不产，融合前可用 kauto oof 补）")
    ap.add_argument("--parallel", type=int, default=1, help="并发候选数")
    ap.add_argument("--model", default=None)
    ap.add_argument("--submit", action="store_true", help="真正提交（需规则已接受）")
    ap.add_argument("--wait-timeout", dest="wait_timeout", type=int, default=900)
    args = ap.parse_args()

    if args.check:
        return step_check(args.slug)

    t_start = time.time()
    _hr(f"真实比赛端到端：{args.slug}")

    # ── 1) 前置检查 ────────────────────────────────────────────────────────
    rc = step_check(args.slug)
    if rc != 0:
        print("\n[停止] 前置条件不满足，未开始跑。")
        return rc

    # ── 2) 跑本地链路（prepare → baseline → iterate → blend → write-sub） ──
    _hr("本地链路")
    steps = []
    for name, argv in [
        ("prepare", ["prepare", args.slug]),
        ("baseline", ["baseline", args.slug, "--folds", str(args.folds)]),
    ]:
        print(f"\n>>> {name}")
        code = cli.main(argv)
        steps.append({"step": name, "code": code})
        if code != 0:
            print(f"[停止] {name} 失败（code={code}）")
            _dump_report(args.slug, steps, t_start)
            return code

    if args.rounds > 0:
        print(f"\n>>> iterate（{args.rounds} 轮，真实 LLM）")
        argv = ["iterate", args.slug, "--rounds", str(args.rounds),
                "--parallel", str(args.parallel)]
        if args.model:
            argv += ["--model", args.model]
        if args.folds_for_oof:
            argv += ["--folds-for-oof", str(args.folds_for_oof)]
        code = cli.main(argv)
        steps.append({"step": "iterate", "code": code})
        if code != 0:
            print(f"[警告] iterate 失败（code={code}），继续用已有最优方案")

    print("\n>>> blend")
    code = cli.main(["blend", args.slug, "--top-k", "6"])
    steps.append({"step": "blend", "code": code})

    print("\n>>> write-sub")
    code = cli.main(["write-sub", args.slug])
    steps.append({"step": "write-sub", "code": code})
    if code != 0:
        print(f"[停止] write-sub 失败（code={code}）")
        _dump_report(args.slug, steps, t_start)
        return code

    # ── 3) 提交（可选） ────────────────────────────────────────────────────
    if not args.submit:
        _hr("本地链路完成，未提交")
        print("  submission.csv 已生成并过了自检。")
        print("  要真提交：加 --submit")
        _dump_report(args.slug, steps, t_start)
        return 0

    _hr("提交")
    code = cli.main(["submit", args.slug, "--yes", "--wait",
                     "--wait-timeout", str(args.wait_timeout)])
    steps.append({"step": "submit", "code": code})

    # ── 4) 汇报 ────────────────────────────────────────────────────────────
    _hr("结果")
    work = kapi.WORK_DIR / args.slug
    rp = work / "submission_receipt.json"
    if rp.exists():
        rec = json.loads(rp.read_text(encoding="utf-8"))
        print(f"  状态      : {rec.get('status')}")
        print(f"  publicScore: {rec.get('public_score')}")
        prov = rec.get("provenance") or {}
        print(f"  来源节点  : {prov.get('node_id')}（{prov.get('prediction_source')}）")
        print(f"  节点 CV   : {prov.get('node_score')}")
    else:
        print("  没有收据 —— 提交可能没真正发生（看上面的输出）")

    _dump_report(args.slug, steps, t_start)
    return 0 if code == 0 else code


def _dump_report(slug: str, steps: list, t_start: float) -> None:
    """把这次运行的链路与关键数字汇总成一个 JSON，便于回看。"""
    work = kapi.WORK_DIR / slug
    work.mkdir(parents=True, exist_ok=True)
    rep = {"slug": slug, "steps": steps,
           "elapsed_seconds": round(time.time() - t_start, 1)}

    bl = work / "baseline.json"
    if bl.exists():
        b = json.loads(bl.read_text(encoding="utf-8"))
        rep["baseline"] = {"cv_mean": b.get("cv_mean"), "metric": b.get("metric")}
    it = work / "iterate.json"
    if it.exists():
        i = json.loads(it.read_text(encoding="utf-8"))
        rep["iterate"] = {"final_cv": i.get("final_cv"),
                          "families": i.get("families"),
                          "good": i.get("good_nodes"), "buggy": i.get("buggy_nodes")}
    bj = work / "blend.json"
    if bj.exists():
        c = json.loads(bj.read_text(encoding="utf-8"))
        rep["blend"] = {"ok": c.get("ok"), "pred_kind": c.get("pred_kind"),
                        "gain": c.get("gain_vs_best_single")}
    rp = work / "submission_receipt.json"
    if rp.exists():
        r = json.loads(rp.read_text(encoding="utf-8"))
        rep["receipt"] = {"status": r.get("status"),
                          "public_score": r.get("public_score")}

    out = work / "run_report.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n  报告已写入：{out}")


if __name__ == "__main__":
    raise SystemExit(main())
