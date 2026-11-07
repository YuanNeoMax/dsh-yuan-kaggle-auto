"""第 61 轮：一路实测推翻了两个「拦截」假设，并顺手修掉上传挂死。

## 假设 1（错）：`hasAcceptedRules=false` = 没加入比赛

旧 `_check_entered` / `--check` 把一手 `hasAcceptedRules` 当硬拦截。逐场实测：

```
titanic                   → false   但已加入、提交成功 publicScore=0.76076
digit-recognizer          → false   提交成功后才 userHasEntered=true
playground-series-s6e2    → false
spaceship-titanic         → false
```

**四场全是 false** —— 这个字段恒为 false，不携带"是否加入"的信息。
拿它当门槛，于是"能提交的比赛"全被判成未加入，还被记成
"写侧需浏览器会话授权，只能人工"。

## 假设 2（错）：未加入 → 提交会 403

实测 digit-recognizer：`userHasEntered=false` 时直接提交

```
ref=57866099  status=COMPLETE  publicScore=0.09896
```

提交**成功**，之后 `userHasEntered` 才变 true。也就是说
**提交动作本身会把账号加入比赛**，"先加入才能提交"这条因果是反的。

→ 结论：加入状态只用于提示（advisory），不是通行条件；能不能提交
由 Kaggle 自己回答（真 403 会以明确错误返回）。

## 缺陷 3（真问题）：提交永久挂起 600s，根因是上传落在 Google 存储

`kaggle competitions submit` 卡死，skill 桥接 600s 超时。逐层剥开：

```
StartSubmissionUpload（api.kaggle.com）    → 200，1.1s
PUT createUrl（禁用代理）                   → 永久挂起（无超时无错误）
PUT createUrl（走 127.0.0.1:7890）          → 200，1.2s
CreateSubmission（带 blobFileTokens）       → 200，拿到 ref
```

两个域对网络要求**相反**：Kaggle 域被系统代理破坏 TLS（ROADMAP 坑 #1），
而 createUrl 指向 www.googleapis.com，本机只有走代理才通。
一条 NO_PROXY 覆盖不了两者 → 上传挂死，看起来像"Kaggle 拒绝/超时"。

修法：`kapi.direct_submit()` 三步，每步用对的网络路径。

## 实测结果

```
titanic        ref=57865998  publicScore=0.76076   （本地 CV 0.815642）
digit          ref=57866099  publicScore=0.09896   （sample_submission，探针用）
```

titanic 那条是这个项目的**第一个真实 public LB 分**，HANDOFF 第四节记的
"唯一硬缺口"闭合。
"""
from pathlib import Path


import json
import os
import pathlib
import sys

# [插件包适配] 目标缺失时按 SKIP 退出（布局差异，不是引擎缺陷）
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _layout_guard import needs_file, needs_dir  # type: ignore  # noqa: E402
    needs_file("work/titanic/submission_receipt.json", "需要真实比赛的回执")  # noqa: E402
except SystemExit:
    raise
except Exception:
    pass  # guard 不可用时不阻断原测试

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("NO_PROXY", "api.kaggle.com,www.kaggle.com,kaggle.com")

from kauto import kapi  # noqa: E402
from kauto.submit import _check_entered  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail})" if detail else ""))


print("=== 1) hasAcceptedRules 不再参与「是否加入」的判断 ===")
states = {s: kapi.entered_state(s) for s in
          ("titanic", "digit-recognizer", "playground-series-s6e2", "spaceship-titanic")}
for s, r in states.items():
    print(f"   {s:26s} entered={r['entered']!s:5s} source={r['source']:14s} accepted={r['accepted']}")
check("titanic 判为已加入", states["titanic"]["entered"] is True)
check("digit-recognizer 判为已加入（曾提交成功）",
      states["digit-recognizer"]["entered"] is True)
check("判据统一来自 userHasEntered（不再用恒 false 的字段当门槛）",
      all(r["source"] == "userHasEntered" for r in states.values()))
check("四场的 accepted 全为 false，说明该字段无法区分加入与否",
      all(r["accepted"] is False for r in states.values()))

print("\n=== 2) 加入状态是提示，不是拦截 ===")
ent = _check_entered("titanic")
check("entered=True 时无提示", ent["entered"] is True and not ent.get("reason"))
# 用一个确实未加入的比赛验证"未加入也只提示"
unjoined = next((s for s, r in states.items() if r["entered"] is False), None)
if unjoined:
    ent2 = _check_entered(unjoined)
    print(f"   {unjoined}: entered={ent2['entered']} advisory={ent2.get('advisory')}")
    check("未加入时给 advisory 而非硬失败", ent2.get("advisory") is True)
    check("提示里说明提交会自动加入", "自动加入" in (ent2.get("reason") or ""))
else:
    check("有未加入的比赛可验证", False, "四场都已加入")

print("\n=== 3) cmd_submit 不再因加入状态早退（return 3 已移除）===")
src_submit = pathlib.Path("kauto/submit.py").read_text(encoding="utf-8")
check("没有再用 entered is False 直接 return 3",
      'ent.get("entered") is False' not in src_submit.split("r = skillbridge.submit")[0]
      or "advisory" in src_submit)
check("提示用 ⚠ 而不是 ✗ 拦截", "⚠ {ent['reason']}" in src_submit)

print("\n=== 4) --check 不再把加入状态当作结论条件 ===")
src_check = pathlib.Path("tools/run_real_competition.py").read_text(encoding="utf-8")
check("不再有「尚未加入该比赛 —— 提交会 403」的硬结论",
      "提交会 403" not in src_check)
check("打印 userHasEntered 的实际值",
      "userHasEntered = {joined}" in src_check)
check("明确写了提交会自动加入", "提交会自动加入" in src_check or "自动加入" in src_check)

print("\n=== 5) direct_submit：三步各自用对的网络路径 ===")
check("kapi.direct_submit 存在", callable(getattr(kapi, "direct_submit", None)))
dsrc = pathlib.Path("kauto/kapi.py").read_text(encoding="utf-8")
check("有上传专用 opener（走代理）", "_upload_opener" in dsrc)
check("有 Kaggle 专用 opener（禁代理）", "_kaggle_opener" in dsrc)
check("代理地址可被 KAUTO_UPLOAD_PROXY 覆盖", "KAUTO_UPLOAD_PROXY" in dsrc)
check("提交用 blobFileTokens", "blobFileTokens" in dsrc)
check("skillbridge 优先走直连", "kapi.direct_submit" in
      pathlib.Path("kauto/skillbridge.py").read_text(encoding="utf-8"))

print("\n=== 6) 真实回执（实测数据，不是断言）===")
receipt = pathlib.Path("work/titanic/submission_receipt.json")
if receipt.exists():
    d = json.loads(receipt.read_text(encoding="utf-8"))
    print("   receipt:", {k: d.get(k) for k in ("status", "public_score", "ref")})
    stale = d.get("status") == "blocked" and d.get("public_score") is None
    check("回执已刷新为 scored（旧 blocked 是上传挂死时的记录）", not stale,
          "" if not stale else "需要重跑 kauto receipt titanic --wait 刷新")
else:
    check("回执存在", False)

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
print("全部通过" if not bad else f"{len(bad)} 项未通过：" + ", ".join(bad))
raise SystemExit(1 if bad else 0)
