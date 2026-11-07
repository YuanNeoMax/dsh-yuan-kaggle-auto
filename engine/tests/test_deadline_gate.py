"""截止门：把"提交被拒"的错误信息指向正确方向。

## 补的是什么

比赛结束（或提交入口关闭）后调提交接口，Kaggle 会拒绝，但本地拿到的
通常只是一个泛泛的错误。人看到它的第一反应是"我的文件有问题"，
于是反复改文件、重训、重交 —— 而真正的原因只有一个：**这个比赛已经不收了**。

## 三条设计约束（都在测试里钉住）

1. **`submissionsDisabled=true` 是硬拦截**（服务端明确说不收）；
2. **仅截止时间已过只给 warning，不硬拦** —— 有的比赛接受迟到提交，
   硬拦会把本来可用的机会拦掉；
3. **读不到就不拦**，并说明"查不到不等于已结束" ——
   把"读不到"当"已结束"会造成假拦截。

这一组不依赖网络：`_parse_deadline` 是纯函数；
`_deadline_gate` 的硬拦截分支用桩替换 `site_api`。
"""

import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import kapi, submit  # noqa: E402

results = []


def mark(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail})" if detail else ""))


# ── 1. 时间解析 ────────────────────────────────────────────────────────────
print("=== 1) 截止时间解析 ===")
cases = [
    ("2030-01-01T00:00:00", False),
    ("2030-01-01T00:00:00Z", False),
    ("2020-01-01T00:00:00Z", True),
    ("2020-06-01", True),
    ("2000-01-01 00:00:00", True),
]
for text, should_expire in cases:
    expired, parsed = submit._parse_deadline(text)
    mark(f"{text} → 已过={should_expire}", expired == should_expire and parsed is not None)

print("\n=== 2) 无法解析时不猜 ===")
for text in ("not-a-date", "", "   ", "deadline TBD"):
    expired, parsed = submit._parse_deadline(text)
    mark(f"解析不了：{text!r} → (False, None)", expired is False and parsed is None)

print("\n=== 3) 时区：无 tz 的时间按 UTC 解释，不按本地时区 ===")
# 这是个真实陷阱：把 naive 时间当本地时间会让边界上的比赛判错几小时。
naive = "2030-01-01T00:00:00"
expired, parsed = submit._parse_deadline(naive)
mark("naive 时间被标为 UTC", parsed is not None and parsed.tzinfo is not None
     and parsed.utcoffset() == _dt.timedelta(0), str(parsed))

print("\n=== 4) 硬拦截：服务端明确关闭提交 ===")
real_site_api = kapi.site_api
try:
    kapi.site_api = lambda *a, **k: {
        "ok": True,
        "data": {"deadline": "2030-01-01T00:00:00Z", "submissionsDisabled": True},
    }
    gate = submit._deadline_gate("some-comp")
    print(f"   {gate}")
    mark("submissionsDisabled=true 时硬拦", gate["ok"] is False)
    mark("拦截理由说明是服务端关闭", "关闭" in gate.get("reason", ""))
    mark("给出可点的比赛页", "kaggle.com/competitions" in gate.get("url", ""))
finally:
    kapi.site_api = real_site_api

print("\n=== 5) 仅截止已过：warning 而非硬拦 ===")
try:
    kapi.site_api = lambda *a, **k: {
        "ok": True,
        "data": {"deadline": "2020-01-01T00:00:00Z", "submissionsDisabled": False},
    }
    gate = submit._deadline_gate("old-comp")
    print(f"   {gate}")
    mark("截止已过仍放行（可能有迟到提交）", gate["ok"] is True)
    mark("但给出明确 warning", "已过" in gate.get("warning", ""))
    mark("warning 提示别改文件重试", "别再改文件" in gate.get("warning", ""))
finally:
    kapi.site_api = real_site_api

print("\n=== 6) 读不到就不拦（读不到 ≠ 已结束）===")
try:
    kapi.site_api = lambda *a, **k: {"ok": False, "error": "offline"}
    gate = submit._deadline_gate("unknown-comp")
    print(f"   {gate}")
    mark("API 不可用时不拦截", gate["ok"] is True)
    mark("并说明查不到不等于已结束", "无法确认" in gate.get("warning", ""))
    mark("提示怀疑方向是比赛结束而非文件格式",
         "已结束" in gate.get("warning", "") and "文件格式" in gate.get("warning", ""))
finally:
    kapi.site_api = real_site_api

print("\n=== 7) 仍在收的比赛：放行且回传截止时间 ===")
try:
    kapi.site_api = lambda *a, **k: {
        "ok": True,
        "data": {"deadline": "2030-01-01T00:00:00Z", "submissionsDisabled": False},
    }
    gate = submit._deadline_gate("live-comp")
    print(f"   {gate}")
    mark("仍在收时放行", gate["ok"] is True)
    mark("回传截止时间供调用方展示", gate.get("deadline") == "2030-01-01T00:00:00Z")
    mark("无 warning", gate.get("warning") is None)
finally:
    kapi.site_api = real_site_api

print("\n=== 8) run 链路：已结束的比赛跳过迭代但保留基线 ===")
# 这是"别白花几十分钟"的落点。用源码结构断言而不是真跑 run
# （真跑要下载数据 + 训练，不适合放进快速测试）。
import re as _re

_src = (Path(__file__).resolve().parent.parent / "kauto" / "cli.py").read_text(encoding="utf-8")
_run_block = _src[_src.find("def cmd_run("):]
_run_block = _run_block[:_run_block.find("\ndef ", 1)]
mark("run 里查了截止", "_deadline_gate" in _run_block)
mark("已结束时不进迭代", "skip_iterate" in _run_block)
mark("迭代受 skip_iterate 控制",
     bool(_re.search(r"if args\.rounds > 0 and not skip_iterate", _run_block)))
mark("融合同样受控",
     bool(_re.search(r"not args\.no_blend and not skip_iterate", _run_block)))
mark("说明为什么保留基线（产物可复用）",
     "复用" in _run_block or "资产" in _run_block)

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
for n, ok, _ in results:
    if not ok:
        print(f"  FAIL {n}")
print("全部通过" if not bad else f"{len(bad)} 项未通过")
raise SystemExit(1 if bad else 0)
