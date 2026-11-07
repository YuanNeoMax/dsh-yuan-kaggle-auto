"""验证三种 prompt 都带数据实况。

实测缺陷：只有 `DRAFT_PROMPT` 用了 `{overview}`，`IMPROVE` 与 `DEBUG` 都没有。
后果很具体：

- `DEBUG` 的要求 2 写着「按真实数据修正」，却**不给**真实数据 ——
  KeyError 就是因为列名假设错了，而不给列名等于让它继续猜。
- `IMPROVE` 要加特征，却不给列信息 —— 只能按猜测的列名写代码。

`common` 里其实一直带着 `overview_text`（`iterate` 里算一次），
只是这两个模板没用它。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import iter as kiter

results = []

print("=== 1) 三个模板都必须有 {overview} 占位 ===")
for name, tmpl in (("DRAFT", kiter.DRAFT_PROMPT),
                   ("IMPROVE", kiter.IMPROVE_PROMPT),
                   ("DEBUG", kiter.DEBUG_PROMPT)):
    ok = "{overview}" in tmpl
    print(f"   {'OK  ' if ok else 'FAIL'} {name}")
    results.append((f"{name} 含 overview", ok))

print("\n=== 2) 三个模板都能渲染出实际特征 ===")
OV = "特征列概览：\n  - `x1` (float64) | 960 种取值\n  - `segment` (str) | 4 种取值"
common = dict(slug="demo", metric="logloss", direction="越小越好",
              pred_spec="正类概率", memory="无", failures="无", intel="",
              overview=OV)

try:
    d = kiter.DRAFT_PROMPT.format(**common, task="classification",
                                  kind="binary", target="t", baseline_cv="0.5")
    ok = "x1" in d
    print(f"   DRAFT 渲染含 x1: {ok}")
    results.append(("DRAFT 渲染真实列", ok))
except KeyError as e:
    print(f"   DRAFT 缺占位: {e}")
    results.append(("DRAFT 渲染真实列", False))

try:
    i = kiter.IMPROVE_PROMPT.format(**common, parent_code="...", parent_score="0.5")
    ok = "x1" in i
    print(f"   IMPROVE 渲染含 x1: {ok}")
    results.append(("IMPROVE 渲染真实列", ok))
except KeyError as e:
    print(f"   IMPROVE 缺占位: {e}")
    results.append(("IMPROVE 渲染真实列", False))

try:
    g = kiter.DEBUG_PROMPT.format(**common, parent_code="...",
                                  error="KeyError: 'x9'", debug_depth=1)
    ok = "x1" in g
    print(f"   DEBUG 渲染含 x1: {ok}")
    results.append(("DEBUG 渲染真实列", ok))
except KeyError as e:
    print(f"   DEBUG 缺占位: {e}")
    results.append(("DEBUG 渲染真实列", False))

print("\n=== 3) iterate 里的 common 确实带 overview ===")
import inspect
src = inspect.getsource(kiter.iterate)
ok3 = "overview=overview_text" in src
print(f"   common 含 overview_text: {ok3}")
results.append(("common 带 overview", ok3))

print("\n=== 4) DEBUG 的说明要指出「KeyError 多半是列名假设错」===")
hint_ok = "列名假设错" in kiter.DEBUG_PROMPT
print(f"   有列名提示: {hint_ok}")
results.append(("debug 有列名提示", hint_ok))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
