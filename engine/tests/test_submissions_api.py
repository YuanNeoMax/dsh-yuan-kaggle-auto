"""`kaggle competitions submissions` 的两个错法。

## 错法一：用 `-c` 传比赛名（参数错位）

```python
run(["competitions", "submissions", "-c", slug, "-v"])   # → 400 Bad Request
```

`submissions` 子命令的 competition 是**位置参数**，没有 `-c` 选项：

```
usage: kaggle competitions submissions [-h] [-v | --format OUTPUT_FORMAT] ...
                                       [competition]
```

传 `-c` 后 argparse 把 slug 当成位置参数、`-c` 成了非法选项，请求参数整个错位。

## 错法二：把服务端的 400 当成参数问题去修

改成 `submissions <slug> --format json` 后**仍是 400**。对照实验：

| 命令 | 结果 |
|---|---|
| `submission-limits titanic` | ✓ 正常（Remaining today: 10）|
| `list -s titanic` | ✓ 正常（表格输出）|
| `submissions titanic` | ✗ 400 |

同账号下两个命令正常、一个 400 —— 说明是服务端行为：
`ListSubmissions` 对**未加入比赛的账号**用 400 表达"没有提交记录可列"
（而不是 403 或空列表）。

调用方修不了，只能：
1. 用正确参数（位置参数而非 `-c`）
2. 把 400 转成**指向可能原因**的错误信息，而不是原样抛出
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

results = []

print("=== 1) 必须用位置参数，不能用 -c ===")
src = Path("kauto/kapi.py").read_text(encoding="utf-8")
import inspect
from kauto import kapi
body = inspect.getsource(kapi.submissions)
uses_positional = '"competitions", "submissions", slug' in body
uses_dash_c = '"submissions", "-c"' in body
print(f"   用位置参数: {uses_positional}")
print(f"   仍在用 -c: {uses_dash_c}")
results.append(("用位置参数", uses_positional and not uses_dash_c))

print("\n=== 2) 400 必须被转成可读信息 ===")
has_hint = "账号尚未加入该比赛" in body or "未加入" in body
print(f"   错误信息指向原因: {has_hint}")
results.append(("400 转可读", has_hint))

print("\n=== 3) 实测：错误信息确实可读 ===")
try:
    kapi.submissions("titanic")
    print("   竟然成功了（账号可能已加入）")
    results.append(("实测可读", None))
except Exception as e:
    msg = str(e)
    readable = "未加入" in msg or "Accept" in msg
    print(f"   信息含指引: {readable}")
    print(f"   前 120 字: {msg[:120]}")
    results.append(("实测可读", readable))

print("\n=== 4) 对照：正常命令仍可用（证明不是全局网络问题）===")
try:
    lim = kapi.run(["competitions", "submission-limits", "titanic"], timeout=60)
    ok4 = "Remaining" in lim
    print(f"   submission-limits 可用: {ok4}")
except Exception as e:
    ok4 = False
    print(f"   submission-limits 失败：{str(e)[:100]}")
results.append(("对照命令可用", ok4))

print("\n=== 5) receipt 命令不得因 submissions 失败而崩 ===")
import json
rp = Path("work/titanic/submission_receipt.json")
if rp.exists():
    d = json.loads(rp.read_text(encoding="utf-8"))
    print(f"   receipt status={d.get('status')}（应为 blocked，不是崩溃）")
    ok5 = d.get("status") in ("blocked", "scored", "timeout")
    print(f"   → 优雅降级: {ok5}")
    results.append(("receipt 优雅降级", ok5))
else:
    print("   (无 receipt 文件，跳过)")
    results.append(("receipt 优雅降级", None))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    mark = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    print(f"  {mark:5s} {n}")
    if ok is False:
        bad += 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
