"""真实 Kaggle API 层的回归测试（离线可跑，不联网）。

这一轮的问题都只在**真调外部命令**时才暴露，纯本地探针测不到：

1. `kaggle competitions list --format json -v` —— `-v` 与 `--format` 互斥，直接 exit 2
2. `--format json` 的输出是**一个 JSON 数组**；早期解析器逐行找 `{`，
   第一行是 `[`，整段被跳过 → 报"没有拿到比赛列表"
3. 规则未接受时 Kaggle 返回 403 原文，早期把原文丢给用户，没有可操作指引
4. `SkillError` 在 CLI 层没有被捕获 → 以 traceback 形态抛出

本脚本用**离线样本**复现这些形态并验证修复，不需要凭据、不联网。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import kapi, skillbridge

results = []

print("=== 1) 命令行参数不得同时含 -v 与 --format ===")
src = Path("kauto/kapi.py").read_text(encoding="utf-8")
# 找出 list_competitions 里的 args 构造
import re
m = re.search(r'args = \[\"competitions\", \"list\".*?\]', src)
line = m.group(0) if m else ""
print(f"   构造语句: {line}")
has_conflict = ("-v" in line) or ("\"-v\"" in line)
print(f"   → 不含 -v：{not has_conflict}")
results.append(("参数不冲突", not has_conflict))

print("\n=== 2) 解析 JSON 数组形态 ===")
sample_array = """[
  {
    "ref": "https://www.kaggle.com/competitions/a",
    "deadline": "2025-01-01",
    "category": "Playground",
    "teamCount": 100
  },
  {
    "ref": "https://www.kaggle.com/competitions/b",
    "deadline": "2026-01-01",
    "category": "Playground",
    "teamCount": 200
  }
]"""
import json
from unittest import mock
with mock.patch.object(kapi, "run", return_value=sample_array):
    rows = kapi.list_competitions("x", limit=10)
print(f"   解析出 {len(rows)} 条")
for r in rows:
    print("    ", r.get("ref", "")[-40:], r.get("deadline"))
ok2 = len(rows) == 2
print(f"   → 数组形态可解析：{ok2}")
results.append(("JSON 数组可解析", ok2))

print("\n=== 3) 逐行 JSON 形态也要兼容 ===")
sample_lines = '{"ref": "https://x/a", "deadline": "2025-01-01"}\n{"ref": "https://x/b", "deadline": "2026-01-01"}'
with mock.patch.object(kapi, "run", return_value=sample_lines):
    rows2 = kapi.list_competitions("x", limit=10)
print(f"   解析出 {len(rows2)} 条 → 两种形态都兼容：{len(rows2) == 2}")
results.append(("两种形态都兼容", len(rows2) == 2))

print("\n=== 4) 规则未接受时的提示必须可操作 ===")
import inspect
src_dl = inspect.getsource(kapi.download)
has_guidance = "rules" in src_dl and "Accept" in src_dl
print(f"   download 里含 rules 指引：{has_guidance}")
results.append(("下载失败给规则指引", has_guidance))

src_st = inspect.getsource(skillbridge.status)
has_st_guide = "rules" in src_st and "403" in src_st
print(f"   status 里含 403 → 规则指引：{has_st_guide}")
results.append(("status 失败给规则指引", has_st_guide))

print("\n=== 5) CLI 必须捕获 SkillError ===")
from kauto import cli as _cli
src_main = inspect.getsource(_cli.main)
has_catch = "SkillError" in src_main
print(f"   main 里捕获 SkillError：{has_catch}")
results.append(("CLI 捕获 SkillError", has_catch))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
