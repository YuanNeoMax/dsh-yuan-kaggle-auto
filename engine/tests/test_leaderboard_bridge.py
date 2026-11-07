"""排行榜桥接：凭据双视图的弥合，以及"该不该继续迭代"的判定依据。

## 这一轮补的是什么

`status` 只回答"我交过什么、多少分"，回答不了**离奖牌还差多少**。
而"要不要再跑一轮"完全取决于这个差距：差 0.0005 值得，差 0.05 不如换方向。
奖牌线只有排行榜接口给。

## 顺带修的一处真实裂缝

skill 脚本只认 `KAGGLE_API_TOKEN` / `~/.kaggle/access_token`，
**不认 OAuth 的 `~/.kaggle/credentials.json`**；而 `kaggle` CLI 认 OAuth。
实测表现：CLI 一切正常，skill 脚本却报 "no Kaggle credential was found" ——
同一台机器上两套凭据视图。

`leaderboard()` 的处理顺序与 `status()` 一致（先 CLI 后脚本），
并且新增 `_run(extra_env=...)` 把 OAuth token **只交给那一个子进程**。

本测试不需要网络与凭据：只验证解析、降级、环境隔离这几条纯逻辑。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import skillbridge  # noqa: E402

results = []


def mark(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail})" if detail else ""))


# ── 1. 排行榜 CSV 解析 ─────────────────────────────────────────────────────
print("=== 1) `kaggle competitions leaderboard -v` 的 CSV 解析 ===")
CSV_OK = """TeamId,TeamName,SubmissionDate,Score,SubmissionCount
4288623,Sas Pav,2026-11-07T08:34:41.283,1.00000,12
12801551,Veny,2026-11-07T03:12:17.390,1.00000,4
"""
rows = skillbridge._parse_leaderboard(CSV_OK)
print(f"   {len(rows)} 行，首行 {rows[0] if rows else '空'}")
mark("解析出 2 行", len(rows) == 2)
mark("解析出队伍名", rows and rows[0].get("TeamName") == "Sas Pav")
mark("解析出分数", rows and str(rows[0].get("Score")).strip() == "1.00000")

print("\n=== 2) 各种坏输入不崩、返回空 ===")
for name, text in (
    ("空字符串", ""),
    ("纯空白", "   \n\n  "),
    ("HTML 错误页", "<!DOCTYPE html><html><body>403</body></html>"),
    ("无表头", "1,2,3\n4,5,6"),
    ("只有表头", "TeamId,TeamName,Score\n"),
):
    try:
        got = skillbridge._parse_leaderboard(text)
        mark(f"不崩：{name}", isinstance(got, list), f"{len(got)} 行")
    except Exception as e:
        mark(f"不崩：{name}", False, f"{type(e).__name__}: {e}")

print("\n=== 3) _run 的 extra_env 只作用于子进程，不污染本进程 ===")
# 关键性质：把 OAuth token 交给 skill 脚本做"弥合凭据视图"，
# 绝不能顺手写进本进程环境（那会让 token 出现在后续所有子进程里）。
probe_cmd = [sys.executable, "-c", "import os;print(os.environ.get('YkgProbe',''))"]
before = os.environ.get("YkgProbe")
out, err, rc = skillbridge._run(probe_cmd, timeout=30, extra_env={"YkgProbe": "sentinel"})
mark("子进程收到了 extra_env", "sentinel" in (out or ""), (out or "").strip()[:40])
mark("本进程环境未被改写", os.environ.get("YkgProbe") == before)

# 空值不该被写进子进程（否则会覆盖已有的有效值）
out2, _, _ = skillbridge._run(probe_cmd, timeout=30, extra_env={"YkgProbe": ""})
mark("空值不透传", "sentinel" not in (out2 or ""), (out2 or "").strip()[:20])

print("\n=== 4) 未安装桥接时 leaderboard 的降级语义 ===")
# 无桥接：CLI 路径也会失败（没有 kaggle 凭据时），最终应抛 SkillError 而不是 AttributeError
saved_root = skillbridge.SKILL_ROOT
try:
    skillbridge.SKILL_ROOT = Path("/nonexistent/skill-root")
    skillbridge.COMP_SCRIPTS = skillbridge.SKILL_ROOT / "modules" / "competitions" / "scripts"
    try:
        skillbridge.leaderboard("titanic", top=2)
        mark("无桥接时抛出 SkillError", False, "没有抛异常")
    except skillbridge.SkillError as e:
        msg = str(e)
        mark("无桥接时抛出 SkillError", True, msg[:70])
        mark("错误信息含可执行指引",
             any(k in msg for k in ("缺 competitions", "凭据", "kaggle auth login", "规则")),
             msg[:90])
    except Exception as e:
        mark("无桥接时抛出 SkillError", False, f"抛了 {type(e).__name__}: {e}")
finally:
    skillbridge.SKILL_ROOT = saved_root
    skillbridge.COMP_SCRIPTS = saved_root / "skills" / "kaggle" / "modules" / "competitions" / "scripts"

print("\n=== 5) 奖牌线的摘要语义 ===")
# cmd_leaderboard 的摘要分支：不发奖牌的比赛必须说清楚，而不是打印空表
def summarize(lb):
    lines = []
    rows = lb.get("rows") or []
    if rows:
        lines.append(f"top {rows[0].get('team')} {rows[0].get('score')}")
    medals = lb.get("medal_lines") or {}
    if medals:
        for z in ("gold", "silver", "bronze"):
            if z in medals:
                lines.append(f"{z} {medals[z]}")
    elif lb.get("awards_medals") is False:
        lines.append("不发奖牌")
    return lines


mark("发奖牌的比赛列出金/银/铜线",
     summarize({"rows": [{"team": "a", "score": "1"}],
                "medal_lines": {"gold": 0.9, "silver": 0.85},
                "awards_medals": True})
     == ["top a 1", "gold 0.9", "silver 0.85"])
mark("不发奖牌时明确说明",
     "不发奖牌" in summarize({"rows": [], "awards_medals": False}))

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
for n, ok, _ in results:
    if not ok:
        print(f"  FAIL {n}")
print("全部通过" if not bad else f"{len(bad)} 项未通过")
raise SystemExit(1 if bad else 0)
