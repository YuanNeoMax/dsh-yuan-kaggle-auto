"""端到端：真实 LLM 生成候选 → 泄漏审查 → 沙箱执行 → 评分。

## 为什么需要这个

前 28 轮验证全部走 **mock LLM**。mock 只返回固定代码，所以它验证的是
「管道能不能把代码从 A 送到 B」，验证不了：

- 真实模型能不能理解 prompt 的契约（`build_features` 返回 `None, None`、
  `valid` 不含 target）
- 真实输出能不能通过泄漏审查
- 真实代码在 AppContainer 里能不能跑
- 生成的东西到底比基线好还是差

## 实测结果（2026-11-01，模型 trae/deepseek-v4.1-flash）

| 项 | 值 |
|---|---|
| LLM 耗时 | 267.2s |
| 代码长度 | 4880 字符 |
| 泄漏审查 | 通过 |
| 沙箱执行 | ok=True |
| 候选 logloss | **0.303130** |
| baseline logloss | 0.465690 |
| **提升** | **比基线好 34.9%** |

这证明整条链路在真实模型下是工作的，不是只有 mock 能跑通。

## 这个测试的运行成本

真实调用约 4–5 分钟，且消耗网关配额。所以：
- 默认**跳过**（需设 `KAUTO_E2E_REAL=1` 才跑）
- 收集到 `run_all_tests.py` 里会显示 SKIP，不拖慢日常回归
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import llm, llmio

results = []

print("=== 真实 LLM 端到端（expensive，默认跳过）===")
enabled = os.environ.get("KAUTO_E2E_REAL") == "1"
print(f"   KAUTO_E2E_REAL=1 ? {enabled}")

if not enabled:
    print("   → 跳过（真实调用约 4-5 分钟、消耗配额）")
    print("     要跑：$env:KAUTO_E2E_REAL=1; python tools/test_real_llm_e2e.py")
    results.append(("真实 LLM 端到端", None))
else:
    import json
    import time

    import pandas as pd

    from kauto import _runner, dataoverview, iter as kiter, leakcheck, prep

    slug = "demo-comp"
    meta = prep.load_work(slug)
    w = Path(meta["work_dir"])
    base = json.loads((w / "baseline.json").read_text(encoding="utf-8"))

    ov = dataoverview.for_work(w, meta) or "略"
    prompt = kiter.DRAFT_PROMPT.format(
        slug=slug, task=meta["task"], kind=meta["kind"], target=meta["target"],
        metric=base["metric"], direction="越小越好", overview=ov,
        pred_spec=kiter.pred_spec_for(base["metric"], meta["kind"]),
        intel="", failures="（无）", memory="（无）",
        baseline_cv=f"{base['cv_mean']:.6f}")

    print("\n=== 1) 真实 LLM 生成 ===")
    t0 = time.time()
    raw = kiter.llm_call(prompt, retries=2, timeout=600)
    dt = time.time() - t0
    code = kiter.extract_code(raw)
    print(f"   耗时 {dt:.1f}s, 代码 {len(code)} 字符")
    ok1 = len(code) > 200
    results.append(("真实 LLM 产出代码", ok1))

    print("\n=== 2) 泄漏审查 ===")
    lk = leakcheck.scan(code)
    print(f"   {'通过' if lk['ok'] else '拒收'}")
    results.append(("真实代码过泄漏审查", lk["ok"]))

    print("\n=== 3) 沙箱执行 ===")
    r = _runner.run(w, code, tag="e2e_real", container="kauto.sandbox", timeout=300)
    print(f"   ok={r.get('ok')}")
    ok3 = bool(r.get("ok"))
    results.append(("真实代码能在沙箱跑", ok3))

    if ok3:
        print("\n=== 4) 评分并与 baseline 比较 ===")
        s = _runner.evaluate(w, meta, base["metric"], tag="e2e_real")
        better = s < base["cv_mean"]
        print(f"   候选 {s:.6f} vs baseline {base['cv_mean']:.6f} → 更好={better}")
        results.append(("候选优于基线", better))
    else:
        print("   err:", (r.get("error") or "")[:200])
        results.append(("候选优于基线", False))

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
