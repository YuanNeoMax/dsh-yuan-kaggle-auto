"""超时估算把规模因子算了两次。

## 实测

`digit-recognizer` 的超时被算成 **7200s（顶到 MAX_TIMEOUT）**：

```
baseline 实测 307.2s × 8 = 2457s
× 规模因子 ×17.75（42000 行 / 784 列）
= 43600s → 被封顶到 7200s（2 小时）
```

**但 baseline 的 307s 已经包含了"42000 行 / 784 列"的成本** ——
它就是在这份数据上跑出来的。再乘规模因子等于**同一因素算两次**。

后果：超时形同虚设。死循环候选会白等 2 小时而不是 41 分钟。

## 修法

规模因子**只在没有 baseline 实测时**才用（那时确实只能靠规模估计）。
有实测时只做温和钳制，不再叠加规模外推。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import timebudget

results = []

print("=== 1) 有 baseline 实测时不再叠加规模因子 ===")
w = Path("work/digit-recognizer")
if (w / "baseline.json").exists():
    r = timebudget.estimate(w)
    print(f"   timeout={r['timeout']}s")
    print(f"   reason={r['reason']}")
    ok1 = "不再叠加规模因子" in r["reason"]
    print(f"   → 未叠加: {ok1}")
    results.append(("不叠加规模因子", ok1))
else:
    print("   (缺 digit baseline，跳过)")
    results.append(("不叠加规模因子", None))

print("\n=== 2) digit 的超时应在合理区间（不该顶到 7200）===")
if (w / "baseline.json").exists():
    b = json.loads((w / "baseline.json").read_text(encoding="utf-8"))
    base_s = b.get("seconds") or 0
    r = timebudget.estimate(w)
    expect = base_s * timebudget.CANDIDATE_FACTOR
    print(f"   baseline={base_s}s  期望≈{expect:.0f}s  实际={r['timeout']}s")
    ok2 = r["timeout"] < timebudget.MAX_TIMEOUT and abs(r["timeout"] - expect) < 200
    print(f"   → 接近实测×8 且未封顶: {ok2}")
    results.append(("超时合理", ok2))
else:
    results.append(("超时合理", None))

print("\n=== 3) 小数据的超时不受影响 ===")
tw = Path("work/titanic")
if (tw / "baseline.json").exists():
    r2 = timebudget.estimate(tw)
    print(f"   titanic timeout={r2['timeout']}s（应为 120）")
    ok3 = r2["timeout"] == timebudget.MIN_TIMEOUT
    print(f"   → 仍为下限: {ok3}")
    results.append(("小数据不受影响", ok3))
else:
    results.append(("小数据不受影响", None))

print("\n=== 4) 没有 baseline 时才用规模因子（逻辑仍在）===")
import tempfile, shutil
tmp = Path(tempfile.mkdtemp())
try:
    slug = tmp / "nobase"
    slug.mkdir()
    (slug / "meta.json").write_text(
        json.dumps({"n_rows": 50000, "n_features": 500}), encoding="utf-8")
    r3 = timebudget.estimate(slug)
    print(f"   无 baseline: timeout={r3['timeout']}s, reason={r3['reason']}")
    ok4 = "规模因子" in r3["reason"]
    print(f"   → 用了规模因子: {ok4}")
    results.append(("无实测时用规模因子", ok4))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n=== 5) 边界钳制仍有效 ===")
print(f"   MIN={timebudget.MIN_TIMEOUT}  MAX={timebudget.MAX_TIMEOUT}")
if (w / "baseline.json").exists():
    r = timebudget.estimate(w)
    ok5 = timebudget.MIN_TIMEOUT <= r["timeout"] <= timebudget.MAX_TIMEOUT
    print(f"   digit {r['timeout']}s 在区间内: {ok5}")
    results.append(("边界钳制有效", ok5))
else:
    results.append(("边界钳制有效", None))

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
