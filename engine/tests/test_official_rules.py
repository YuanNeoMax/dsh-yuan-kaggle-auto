"""官方评测页约束 → 本地可执行判据（合并自 kaggle-skill 的能力）。

## 这一轮补的是什么

`subcheck` 原来只查结构（列名 / 行数 / id 对齐 / 重复 id / 空值 / 取值分布）。
它读不到**官方评测页**，而评测页常写着本地看不出来的硬约束，例如 titanic 的：

    You should submit a csv file with exactly 418 entries plus a header row.
    Your submission will show an error if you have extra columns
    (beyond PassengerId and Survived) or rows.

为什么这件事值得做：kaggle-skill 的 platform notes 明确记录
**一次报错的提交也可能照样占用当日配额**。所以能在本地读到官方口径，
就把"会不会白烧一次提交"从猜测变成可判定。

评测页是自然语言，但会导致提交被判废的约束几乎只有固定几种形态：
`exactly N entries/rows`、`exactly N columns`、`beyond <col> and <col>`、
取值 `between a and b`。所以不做整页理解，只把这几类抽成结构化判据；
抽不出来就什么都不加 —— 宁可少查，不可误判。
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import subcheck  # noqa: E402
from kauto.skillbridge import rules_from_evaluation  # noqa: E402

results = []


def mark(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail})" if detail else ""))


# ── 1. 真实评测页文本的抽取 ────────────────────────────────────────────────
print("=== 1) 从 titanic 评测页原文抽出判据 ===")
TITANIC_EVAL = """
## Goal

It is your job to predict if a passenger survived the sinking of the Titanic.

## Metric

Your score is the percentage of passengers you correctly predict.

## Submission File Format

You should submit a csv file with exactly 418 entries **plus** a header row.
Your submission will show an error if you have extra columns (beyond
PassengerId and Survived) or rows.

The file should have exactly 2 columns:

- PassengerId (sorted in any order)
- Survived (contains your binary predictions: 1 for survived, 0 for deceased)
"""
rules = rules_from_evaluation(TITANIC_EVAL)
print(f"   {rules}")
mark("抽出 rows=418", rules.get("rows") == 418)
mark("抽出 columns=2", rules.get("columns") == 2)
mark("抽出 allowlist 两列", rules.get("allowed_columns") == ["PassengerId", "Survived"])

print("\n=== 2) 值域约束 ===")
rng_text = "Your predictions must be between 0 and 1, inclusive."
rng = rules_from_evaluation(rng_text)
print(f"   {rng}")
mark("抽出 range=[0,1]", rng.get("range") == [0.0, 1.0])

print("\n=== 3) 认不出的文本不瞎下结论 ===")
for text in (
    "",
    "Predict the probability of each class.",
    "See the rules page for details.",
    "The metric is RMSE over the test set.",
):
    got = rules_from_evaluation(text)
    mark(f"不误抽：{text[:34] or '(空)'}", got == {}, str(got))

print("\n=== 4) subcheck 第二层：官方约束真的会拦 ===")


def frame(rows: int, cols: list[str], values=None) -> pd.DataFrame:
    data = {cols[0]: list(range(rows))}
    if len(cols) > 1:
        data[cols[1]] = values if values is not None else [0] * rows
    return pd.DataFrame(data)


def run_check(out, sample, id_col, test, rules_):
    try:
        info = subcheck.check(out, sample, id_col, test, rules=rules_)
        return True, info
    except subcheck.SubmissionInvalid as error:
        return False, str(error)


# 正确文件：418 行 / 2 列 / 值在 0..1
good = frame(418, ["PassengerId", "Survived"], [0, 1] * 209)
ok, info = run_check(good, good.copy(), "PassengerId", good, rules)
print(f"   正确文件 → ok={ok} applied={info.get('official_rules_applied') if ok else info}")
mark("正确文件通过", ok)
mark("报告里写明应用了哪些官方规则",
     ok and bool(info.get("official_rules_applied")))

# 行数不符
bad_rows = frame(417, ["PassengerId", "Survived"], [0, 1] * 208 + [0])
ok, msg = run_check(bad_rows, bad_rows.copy(), "PassengerId", bad_rows, rules)
print(f"   417 行 → ok={ok}  {str(msg)[:110]}")
mark("行数不符被拦", not ok and "418" in str(msg))

# 多出列
extra = frame(418, ["PassengerId", "Survived"], [0, 1] * 209)
extra["Extra"] = 0
ok, msg = run_check(extra, good.copy(), "PassengerId", extra, rules)
print(f"   多一列 → ok={ok}  {str(msg)[:110]}")
mark("多出列被拦", not ok and ("Extra" in str(msg) or "列" in str(msg)))

# 值越界
out_of_range = frame(418, ["PassengerId", "Survived"], [0, 5] * 209)
ok, msg = run_check(out_of_range, good.copy(), "PassengerId", out_of_range,
                    {"range": [0.0, 1.0]})
print(f"   值=5 → ok={ok}  {str(msg)[:110]}")
mark("值越界被拦", not ok and "0" in str(msg))

print("\n=== 5) 不传官方规则时行为与以前一致（新增能力不改变旧路径）===")
ok_old, _ = run_check(good, good.copy(), "PassengerId", good, None)
mark("rules=None 时正确文件仍通过", ok_old)
bad_old = frame(417, ["PassengerId", "Survived"], [0, 1] * 208 + [0])
ok_bad, msg_bad = run_check(bad_old, good.copy(), "PassengerId", bad_old, None)
print(f"   417 行 + 有 sample → ok={ok_bad}  {str(msg_bad)[:80]}")
mark("rules=None 时行数不符仍由第一层拦住", not ok_bad)

print("\n=== 6) 第一层优先：结构问题先报，不等人格化 ===")
mismatched_cols = frame(418, ["Wrong", "Survived"], [0, 1] * 209)
ok, msg = run_check(mismatched_cols, good.copy(), "Wrong", mismatched_cols, rules)
print(f"   列名错 → ok={ok}  {str(msg)[:90]}")
mark("列名问题由第一层报出", not ok and "列名" in str(msg))

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
for n, ok, _ in results:
    if not ok:
        print(f"  FAIL {n}")
print("全部通过" if not bad else f"{len(bad)} 项未通过")
raise SystemExit(1 if bad else 0)
