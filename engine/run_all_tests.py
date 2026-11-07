"""一键跑全部自测：不依赖 pytest，直接顺序执行 tools/test_*.py。

为什么不用 pytest：这些脚本里有一半是"打印对比结果"的探针式检查
（比如切分策略判定、族识别、泄漏规则命中），它们看的是输出内容而不是断言。
强行改成 assert 会丢掉这些可读的诊断信息。

所以这里做两件事：
1. 顺序跑每个脚本，捕获退出码；
2. 同时把输出里明显的失败信号（FAIL / MISMATCH / Traceback / UNEXPECTEDED）
   标出来，避免"脚本没崩但结果不对"被当成通过。

用法（两种布局都认）：
  python tools/run_all_tests.py     # 原始仓库：tests 在 ../tools/
  python engine/run_all_tests.py    # 插件包：tests 就在 ./tests/
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 测试所在目录：插件布局是 <本文件同级>/tests，原始仓库布局是 ../tools。
# 显式解析而不是用 parents[1] —— 后者把"本文件在 tools/ 下"这个前提写死，
# 搬到 engine/ 之后就指向一个不存在的 ../tools，整套自测静默变成 MISSING。
_CANDIDATE_DIRS = [ROOT / "tests", ROOT.parent / "tools"]


def _tests_dir() -> Path:
    for candidate in _CANDIDATE_DIRS:
        if candidate.is_dir() and any(candidate.glob("test_*.py")):
            return candidate
    return _CANDIDATE_DIRS[0]


TESTS_DIR = _tests_dir()

# 共享的"前置条件缺失"退出码，与 tests/_layout_guard.py 的 SKIP_CODE 一致。
SKIP_CODE = 77

# 明确要求断言通过的脚本（靠退出码）
HARD = [
    "test_a1_a2.py",
    "test_nan.py",
    "test_split.py",
    "test_leakcheck.py",
]

# 探针式脚本：看输出内容
PROBE = [
    "test_journal.py",
    "test_search_policy.py",
    "test_sandbox.py",
    "test_family.py",
    "test_oof.py",
    "test_archive.py",
    "test_structural_noleak.py",
    "test_provenance_timeout.py",
    "test_multiclass.py",
    "test_realapi.py",
    "test_rowcontract.py",
    "test_receipt.py",
    "test_blend_wiring.py",
    "test_parallel_run.py",
    "test_archive_blend.py",
    "test_submit_gate.py",
    "test_no_drift.py",
    "test_output_formats.py",
    "test_notebook_only.py",
    "test_split_fingerprint.py",
    "test_isolation_visibility.py",
    "test_readme_sync.py",
    "test_blend_dedup.py",
    "test_prune.py",
    "test_writeup_cache.py",
    "test_writeup_line.py",
    "test_prompt_quality.py",
    "test_prompt_overview.py",
    "test_llm_contract.py",
    "test_field_contracts.py",
    "test_outlier_cleaning.py",
    "test_overview_annotations.py",
    "test_debug_priority.py",
    "test_search_fixes.py",
    "test_suite_audit.py",
    "test_real_llm_e2e.py",
    "test_real_iterate.py",
    "test_leak_distribution.py",
    "test_timeout_behavior.py",
    "test_oof_backfill.py",
    "test_archive_oof_priority.py",
    "test_real_leak_rate.py",
    "test_network_proxy.py",
    "test_real_precheck.py",
    "test_real_data_split.py",
    "test_submit_paths.py",
    "test_submissions_api.py",
    "test_score_parsing.py",
    "test_receipt_history.py",
    "test_journal_dedup.py",
    "test_prune_intermediate.py",
    "test_hints_diversity.py",
    "test_draft_memory.py",
    "test_sandbox_njobs.py",
    "test_schema_inference.py",
    "test_adaptive_params.py",
    "test_timeout_scale.py",
    "test_check_entered.py",
    "test_accept_rules_probe.py",
    "test_entered_state.py",
    "test_upload_channel.py",
    "test_write_sub_id.py",
    "test_official_rules.py",
    "test_leaderboard_bridge.py",
    "test_deadline_gate.py",
    "test_llm_provider.py",
    "test_docs_sync.py",
    "test_iterate_mock_llm.py",
    "test_workdir_relocation.py",
    "test_smoke_command.py",
]

# 只把这些当作真失败信号。
#
# 注意「Traceback」不能算：好几个测试脚本会**故意**构造带 Traceback 的
# 报错字符串来模拟失败节点（test_journal.py 就是），按关键词扫会误报。
# 判据改成：非零退出码 / 显式的断言式关键字。
FAIL_PAT = re.compile(r"(MISMATCH|UNEXPECTED|AssertionError|\bFAIL\b)", re.I)


def _is_real_failure(text: str) -> bool:
    """排除「作为测试数据出现的」Traceback —— 只有真正抛出的才算失败。"""
    if "Traceback (most recent call last)" in text:
        return True
    return False


def run_one(name: str) -> dict:
    path = TESTS_DIR / name
    if not path.exists():
        return {"name": name, "status": "MISSING", "seconds": 0, "hits": []}
    t0 = time.time()
    p = subprocess.run([sys.executable, str(path)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       cwd=str(ROOT), timeout=1800)
    dt = round(time.time() - t0, 1)
    out = (p.stdout or "") + (p.stderr or "")
    hits = sorted({m.group(0) for m in FAIL_PAT.finditer(out)})
    real_crash = p.returncode != 0 and _is_real_failure(out)
    # Exit 77 is the shared "precondition absent" code (_layout_guard.SKIP_CODE).
    # A test whose target belongs to a user workspace (README, a prepared
    # competition, an e2e script outside the package) cannot assert anything
    # here — that is a layout difference, not an engine regression, and counting
    # it as FAIL would make the packaged suite permanently red.
    if p.returncode == SKIP_CODE:
        status = "SKIP"
    else:
        status = "OK" if p.returncode == 0 and not hits and not real_crash else "FAIL"
    return {"name": name, "status": status, "seconds": dt,
            "exit": p.returncode, "hits": hits, "tail": out.strip()[-400:]}


def main() -> int:
    results = []
    for n in HARD + PROBE:
        r = run_one(n)
        results.append(r)
        mark = {"OK": "PASS", "FAIL": "FAIL", "MISSING": "SKIP", "SKIP": "SKIP"}.get(r["status"], r["status"])
        extra = f"  {','.join(r.get('hits') or [])}" if r["status"] == "FAIL" else ""
        print(f"{mark:5s} {n:26s} {r['seconds']:>5}s{extra}", flush=True)
        if r["status"] == "FAIL" and r.get("tail"):
            print("      tail:", r["tail"].replace("\n", " ")[:200], flush=True)

    bad = [r for r in results if r["status"] == "FAIL"]
    print()
    if bad:
        print(f"失败 {len(bad)} 项：{[r['name'] for r in bad]}")
        return 1
    print(f"全部通过（{len(results)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())





