"""验证 prompt 里必须有关键上下文 —— 否则 LLM 在盲写。

实测缺陷：`DRAFT_PROMPT` **完全没提当前基线分数**。
LLM 无法判断"要超越什么"，写出来的方案可能连基线都不如，而它自己并不知道。

同时修了一处措辞矛盾：方向写着「越小越好」，目标却写「CV 尽可能高」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import iter as kiter

results = []

print("=== 1) DRAFT_PROMPT 必须含基线分数 ===")
has_placeholder = "{baseline_cv}" in kiter.DRAFT_PROMPT
print(f"   模板含 {{baseline_cv}} 占位: {has_placeholder}")
results.append(("模板含基线占位", has_placeholder))

p = kiter.DRAFT_PROMPT.format(
    slug="demo", task="classification", kind="binary", target="target",
    metric="logloss", direction="越小越好", overview="特征略",
    pred_spec="正类概率", intel="", failures="无", memory="无",
    baseline_cv="0.465690")
print(f"   渲染后含具体分数: {'0.465690' in p}")
results.append(("渲染含分数", "0.465690" in p))

print("\n=== 2) 方向措辞不能自相矛盾 ===")
bad_wording = "CV 尽可能高" in kiter.DRAFT_PROMPT
good_wording = "注意上面的好坏方向" in kiter.DRAFT_PROMPT
print(f"   仍有矛盾措辞「CV 尽可能高」: {bad_wording}（应为 False）")
print(f"   有方向提醒: {good_wording}")
results.append(("措辞无矛盾", not bad_wording and good_wording))

print("\n=== 3) 两种方向都要能渲染 ===")
for d in ("越小越好", "越大越好"):
    try:
        out = kiter.DRAFT_PROMPT.format(
            slug="d", task="classification", kind="binary", target="t",
            metric="logloss", direction=d, overview="略", pred_spec="p",
            intel="", failures="无", memory="无", baseline_cv="0.5")
        ok = d in out
        print(f"   {d}: 渲染成功={ok}")
        results.append((f"渲染 {d}", ok))
    except KeyError as e:
        print(f"   {d}: 缺占位 {e}")
        results.append((f"渲染 {d}", False))

print("\n=== 4) 契约说明必须在（防 LLM 写错接口）===")
has_nolabel = "都不含标签列" in p
has_return = "返回 (X_train, X_valid, X_test, None, None)" in p
print(f"   说明 valid 不含 target: {has_nolabel}")
print(f"   说明返回值形态: {has_return}")
results.append(("契约说明齐全", has_nolabel and has_return))

print("\n=== 5) IMPROVE_PROMPT 也要有当前分数 ===")
has_improve_score = "{parent_score}" in kiter.IMPROVE_PROMPT
print(f"   含 {{parent_score}}: {has_improve_score}")
results.append(("improve 含当前分", has_improve_score))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
