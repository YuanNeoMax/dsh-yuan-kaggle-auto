"""验证提交闭环：轮询出分 + 收据。

参照 agentic-kaggle-skill 的 submission-endgame.md：
「Continue working until scored / blocked」，并且
「Save the latest submission table to submission_receipt.json」。

早期实现提交完就返回，拿不到分数 —— "提交成功"和"提交被拒"在本地看起来完全一样。

本测试用**离线样本**验证解析与状态机，不需要凭据、不联网。
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import kapi

results = []

print("=== 1) 解析 submissions 表格 ===")
SAMPLE = """ref,fileName,date,description,status,publicScore,privateScore
playground-series-s5e3,submission.csv,2025-03-31 10:00:00,kauto auto-submit,SubmissionStatus.COMPLETE,0.90123,
"""
rows = kapi._parse_submissions_table(SAMPLE)
print(f"   解析出 {len(rows)} 行")
if rows:
    print(f"   status={rows[0].get('status')} publicScore={rows[0].get('publicScore')}")
ok1 = len(rows) == 1 and rows[0].get("publicScore") == "0.90123"
print(f"   → 字段可用: {ok1}")
results.append(("解析 submissions 表格", ok1))

print("\n=== 2) 有分数时 wait_for_score 返回 scored ===")
with mock.patch.object(kapi, "submissions", return_value=SAMPLE):
    w = kapi.wait_for_score("x", timeout=10, interval=1)
print(f"   status={w['status']} score={w.get('score')} polls={w['poll_count']}")
ok2 = w["status"] == "scored" and isinstance(w.get("score"), float)
print(f"   → scored 且分数是浮点: {ok2}")
results.append(("有分数返回 scored", ok2))

print("\n=== 3) 分数为空时应继续轮询，最终 timeout ===")
EMPTY_SCORE = """ref,fileName,date,description,status,publicScore,privateScore
x,submission.csv,2025-03-31 10:00:00,msg,SubmissionStatus.PROCESSING,,
"""
with mock.patch.object(kapi, "submissions", return_value=EMPTY_SCORE):
    w2 = kapi.wait_for_score("x", timeout=3, interval=1)
print(f"   status={w2['status']} polls={w2['poll_count']} waited={w2['waited_seconds']}s")
ok3 = w2["status"] == "timeout"
print(f"   → 未出分时是 timeout（不是假装成 scored）: {ok3}")
results.append(("未出分返回 timeout", ok3))

print("\n=== 4) 403 应立刻返回 blocked，不做无意义重试 ===")
def raise_403(*a, **kw):
    raise kapi.KaggleError("403 Client Error: Forbidden ... accept its rules first")

with mock.patch.object(kapi, "submissions", side_effect=raise_403):
    w3 = kapi.wait_for_score("x", timeout=30, interval=5)
print(f"   status={w3['status']} polls={w3['poll_count']}")
print(f"   reason={str(w3.get('reason'))[:90]}")
ok4 = w3["status"] == "blocked" and w3["poll_count"] == 1
print(f"   → blocked 且只轮询 1 次: {ok4}")
results.append(("403 立刻 blocked", ok4))

print("\n=== 5) blocked 原因必须可操作（不是英文原文）===")
reason = str(w3.get("reason") or "")
actionable = ("rules" in reason) and ("Accept" in reason or "接受" in reason)
print(f"   含 rules 链接与接受指引: {actionable}")
print(f"   原始错误另存 raw_error: {bool(w3.get('raw_error'))}")
results.append(("blocked 原因可操作", actionable))
results.append(("保留原始错误", bool(w3.get("raw_error"))))

print("\n=== 6) receipt 写入与历史追加 ===")
from kauto import cli, storage
import json
with mock.patch.object(kapi, "submissions", return_value=SAMPLE):
    ww = kapi.wait_for_score("rcpt-test", timeout=10, interval=1)
p = cli._write_receipt("rcpt-test", ww)
rec = json.loads(Path(p).read_text(encoding="utf-8"))
print(f"   status={rec['status']} public_score={rec['public_score']}")
ok6 = rec["status"] == "scored" and rec["public_score"] is not None
print(f"   → 收据含分数: {ok6}")
results.append(("收据含分数", ok6))

# 再写一次，历史应增长
cli._write_receipt("rcpt-test", ww)
hp = Path(p).parent / "submission_receipt_history.json"
hist = json.loads(hp.read_text(encoding="utf-8"))
print(f"   历史条数: {len(hist)}")
ok7 = len(hist) >= 2
print(f"   → 历史追加不覆盖: {ok7}")
results.append(("历史追加不覆盖", ok7))

# 清理测试数据
import shutil
shutil.rmtree(Path(p).parent, ignore_errors=True)

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
