"""提交成功后能否读出分数 —— 上一轮改格式时漏改了解析器。

## 缺陷

第 47 轮我把 `submissions()` 从 `-v`（CSV）改成 `--format json`，
但 `_parse_submissions_table()` **还是按 CSV 写的**。

喂 JSON 进去会切出 3 行乱码：

```
[{'{"ref": "12345"': '"description": "kauto"',
  '"fileName": "submission.csv"': '"status": "complete"', ...}]
```

`publicScore` 永远读不到 → `wait_for_score` 一直 pending 直到超时。

## 为什么前几轮没发现

这个缺陷只在**提交成功且拿到 200 响应**时才显现。而账号一直 `userHasEntered=False`，
`submissions()` 每次都返回 400，`wait_for_score` 走的是 `blocked` 分支 ——
**根本到不了解析这一步**。

## 修法

解析器改成「JSON 优先 + CSV 兜底」，字段名统一成 `wait_for_score` 期望的那几个。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import kapi

results = []

JSON_OK = """[{"fileName":"submission.csv","date":"2026-11-01 05:00:00",
"status":"complete","publicScore":"0.81564","privateScore":"",
"description":"kauto","ref":"12345"}]"""

CSV_OK = """fileName,date,description,status,publicScore,privateScore
submission.csv,2026-11-01 05:00:00,kauto,complete,0.81564,"""

print("=== 1) JSON 格式能解析出 publicScore ===")
rows = kapi._parse_submissions_table(JSON_OK)
print(f"   {len(rows)} 行")
ok1 = bool(rows) and rows[0].get("publicScore") == "0.81564"
print(f"   → publicScore 正确: {ok1}")
results.append(("JSON 解析 publicScore", ok1))

print("\n=== 2) 字段名必须与 wait_for_score 期望的一致 ===")
need = {"fileName", "date", "status", "publicScore"}
got = set(rows[0].keys()) if rows else set()
missing = need - got
print(f"   期望: {sorted(need)}")
print(f"   缺失: {sorted(missing) or '无'}")
ok2 = not missing
results.append(("字段名对齐", ok2))

print("\n=== 3) CSV 兜底仍可用 ===")
rows2 = kapi._parse_submissions_table(CSV_OK)
ok3 = bool(rows2) and rows2[0].get("publicScore") == "0.81564"
print(f"   CSV 行数={len(rows2)} publicScore={rows2[0].get('publicScore') if rows2 else None}")
print(f"   → CSV 可用: {ok3}")
results.append(("CSV 兜底", ok3))

print("\n=== 4) 空输入不崩 ===")
ok4 = kapi._parse_submissions_table("") == []
print(f"   空串 → 空列表: {ok4}")
results.append(("空输入", ok4))

print("\n=== 5) wait_for_score 能读出分数（端到端替身）===")
orig = kapi.submissions
kapi.submissions = lambda slug: JSON_OK
try:
    r = kapi.wait_for_score("titanic", timeout=20, interval=2)
finally:
    kapi.submissions = orig
print(f"   status={r.get('status')} score={r.get('score')}")
ok5 = r.get("status") == "scored" and abs(float(r.get("score") or 0) - 0.81564) < 1e-9
print(f"   → 读出分数: {ok5}")
results.append(("wait_for_score 读分数", ok5))

print("\n=== 6) 处理中（无分）应保持 pending 而非误判 scored ===")
PENDING = """[{"fileName":"submission.csv","date":"2026-11-01 05:00:00",
"status":"pending","publicScore":""}]"""
orig = kapi.submissions
kapi.submissions = lambda slug: PENDING
try:
    r2 = kapi.wait_for_score("titanic", timeout=6, interval=2)
finally:
    kapi.submissions = orig
print(f"   status={r2.get('status')}")
ok6 = r2.get("status") in ("pending", "timeout")
print(f"   → 未误判 scored: {ok6}")
results.append(("pending 不误判", ok6))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
