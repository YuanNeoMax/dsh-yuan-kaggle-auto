"""验证 writeup 压行：摘要不能被丢。

实测缺陷：`_writeup_line` 里 `title` 有顶层回退（`it.get("title")`），
但 `excerpt` 只有 `it.get("snippet")`，**漏了顶层的 `excerpt`**。

后果：`{"rank":2,"title":"T","excerpt":"I blend GBDT and NN"}` 压出来只有标题，
摘要整个丢掉。而摘要是"用什么方法"的真正信息 —— 标题通常只是模型族罗列
（"GBDT + NN + SVR"），对 LLM 的指导价值远低于摘要。

这条信息最终会进 prompt 的情报段，丢了等于每轮都在喂空话。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import iter as kiter

results = []

CASES = {
    "顶层 excerpt": ({"rank": 2, "title": "T", "excerpt": "I blend GBDT and NN"},
                    "I blend GBDT and NN"),
    "顶层 snippet": ({"rank": 3, "title": "S", "snippet": "two XGB with 0.3 weight"},
                    "two XGB with 0.3 weight"),
    "preview dict": ({"rank": 4, "preview": {"title": "P", "excerpt": "target encoding per fold"}},
                    "target encoding per fold"),
    "preview str": ({"rank": 5, "preview": "single lgbm with custom loss"},
                    "single lgbm with custom loss"),
    "两种都有(优先 preview)": (
        {"rank": 6, "excerpt": "top-level", "preview": {"title": "P", "excerpt": "from preview"}},
        "from preview"),
}

print("=== 每种数据形态都必须保留摘要 ===")
for name, (item, expect) in CASES.items():
    line = kiter._writeup_line(item) or ""
    ok = expect in line
    print(f"  {'OK  ' if ok else 'FAIL'} {name:22s} -> {line[:70]}")
    results.append((f"保留摘要：{name}", ok))

print("\n=== 只有标题时不应崩 ===")
line = kiter._writeup_line({"rank": 7, "title": "only title"})
ok2 = line is not None and "only title" in line
print(f"   {line}")
results.append(("只有标题可用", ok2))

print("\n=== 完全空的记录返回 None ===")
empty = kiter._writeup_line({"rank": 8})
print(f"   返回: {empty}")
results.append(("空记录返回 None", empty is None))

print("\n=== 摘要被截断（不无限长）===")
long_excerpt = "x" * 500
line = kiter._writeup_line({"rank": 9, "title": "T", "excerpt": long_excerpt})
n_chars = len(line) if line else 0
ok4 = n_chars < 400
print(f"   总长 {n_chars} 字符（应 < 400）")
results.append(("摘要被截断", ok4))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
