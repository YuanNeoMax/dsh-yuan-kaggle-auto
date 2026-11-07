"""`run_real_competition.py --check` 曾经把「不能下载」当「不能提交」。

## 实测缺陷（第 59 轮）

跑 `--check titanic`，输出「✓ 可下载数据 —— 规则已接受 / 可以跑完整链路」，
但它只看 `download` 成功就下结论。公开数据（titanic / digit-recognizer）
历史遗留可以直接下载 —— **"能下载"根本不是"能提交"的判据**。

## 第 61 轮：这个测试自己也变过一次形

它原本还断言「titanic 必须报告未加入 + 退出码非零」。那条断言在
第 61 轮被实测推翻：

```
titanic  GetUser → hasAcceptedRules=false
         list    → userHasEntered=true
         真提交   → 成功，publicScore=0.76076
```

titanic 是**已加入且能提交**的，"必须报未加入"这个断言本身是错的
（它把当时错误的判据固化成了契约）。同类覆盖还有 digit-recognizer：
`userHasEntered=false` 时直接提交也成功（ref=57866099）。

所以这里不再钉死某个具体结论，改为钉**契约**：

1. 判据必须来自 `userHasEntered`，且必须在输出里可见（不能悄悄猜）；
2. 结论必须与判据一致 —— 判为已加入时不能说"提交会 403"；
3. 退出码跟随结论（能跑=0，被真实阻塞时非零）；
4. 未加入的比赛也不阻断（提交动作本身会加入）。
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

results = []


def mark(name, ok):
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}")


print("=== 1) 源码里必须查加入状态，且明确它只是提示 ===")
src = Path("tools/run_real_competition.py").read_text(encoding="utf-8")
mark("查 userHasEntered", "userHasEntered" in src)
mark("不再把加入状态当作提交门槛", "不是提交的门" in src or "仅供参考" in src)
mark("写明提交会自动加入", "自动加入" in src)
mark("不再误称「规则已接受」", "可下载数据 —— 规则已接受" not in src)

print("\n=== 2) 判据必须可观测，且结论与判据一致 ===")
r = subprocess.run([sys.executable, "tools/run_real_competition.py", "titanic", "--check"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
out = r.stdout + r.stderr

shows_judgement = "userHasEntered = " in out
print(f"   输出里带 userHasEntered 实际值: {shows_judgement}")
mark("判据可观测", shows_judgement)

# 结论与判据一致性：判为已加入时，不应再出现"提交会 403"这种硬拦
joined = "userHasEntered = True" in out
says_blocked = "提交会 403" in out
consistent = (not joined) or (not says_blocked)
print(f"   判为已加入={joined}  仍说提交会 403={says_blocked}  → 一致={consistent}")
mark("结论与判据一致", consistent)

print("\n=== 3) 退出码跟随结论 ===")
print(f"   exit code = {r.returncode}")
expect_zero_if_ok = ("结论： 可以跑完整链路" in out) == (r.returncode == 0)
mark("退出码与结论一致", expect_zero_if_ok)

print("\n=== 4) 加入状态这一段本身不得把 ok 置 False ===")
# 注意区分两种"阻断"，它们来源不同：
#   - download 403 → **真实**门槛（这场比赛的数据要先接受规则才给）；
#   - 加入状态（userHasEntered）→ **不应**阻断（提交会自动加入）。
# 所以这里不能拿 s6e2 来验（它会被 download 403 拦住，与加入无关），
# 改为直接检查那段代码里没有 ok = False。
# 切片锚点必须唯一：文件头注释里也出现过 "notebook-only"，
# 所以起点用 "账号加入状态"、终点用它后面第一次出现的 ok = False 判断块。
start = src.find("账号加入状态")
end = src.find("if meta_info.get(\"notebook_only\")")
block = src[start:end] if start != -1 and end != -1 and end > start else ""
print(f"   该段代码长度 {len(block)} 字符")
no_blocking = "ok = False" not in block
print(f"   段内没有 ok = False: {no_blocking}")
mark("加入状态不阻断流程", no_blocking)
has_advisory_hint = "提交会自动加入" in block
mark("段内说明提交会自动加入", has_advisory_hint)

print("\n=== 汇总 ===")
bad = [n for n, ok in results if ok is False]
for n, ok in results:
    if ok is False:
        print(f"  FAIL {n}")
print()
print("全部通过" if not bad else f"{len(bad)} 项未通过")
raise SystemExit(1 if bad else 0)
