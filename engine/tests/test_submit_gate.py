"""验证提交守卫在两条路径上一致。

实测缺陷：`kauto run --yes` 直接调 skillbridge.submit，
**绕过** `kauto submit --yes` 里的溯源指纹校验与 best_node 一致性校验。
同一条外部副作用（推进 Kaggle），两条路径的守卫不一致 ——
于是 `run --yes` 能提交被改过或过时的 submission，而 `submit --yes` 会拦住。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import cli

results = []

print("=== 1) 两条提交路径都走同一道公共门 ===")
src_run = inspect.getsource(cli.cmd_run)
src_sub = inspect.getsource(cli.cmd_submit)
use_in_run = "_pre_submit_gate" in src_run
use_in_submit = "_pre_submit_gate" in src_sub
print(f"   run --yes 调用公共门: {use_in_run}")
print(f"   submit --yes 调用公共门: {use_in_submit}")
results.append(("run 走公共门", use_in_run))
results.append(("submit 走公共门", use_in_submit))

print("\n=== 2) 公共门覆盖的检查项 ===")
src_gate = inspect.getsource(cli._pre_submit_gate)
checks = {
    "溯源指纹 sha256": "sha256" in src_gate,
    "best_node 一致性": "best_node.txt" in src_gate,
    "provenance 存在性": "submission_provenance" in src_gate,
}
for k, v in checks.items():
    print(f"   {k:24s} {v}")
results.append(("三道检查齐全", all(checks.values())))

print("\n=== 3) 实测：篡改后必须被拒 ===")
sub = cli.kapi.WORK_DIR / "demo-comp" / "submission.csv"
import shutil
backup = sub.with_suffix(".csv.bak")
shutil.copy(sub, backup)
try:
    with open(sub, "a", encoding="utf-8") as f:
        f.write("9999,0.5\n")
    g = cli._pre_submit_gate("demo-comp")
    print(f"   ok={g['ok']}  reason={str(g.get('reason'))[:80]}")
    blocked = not g["ok"]
    print(f"   → 篡改被拒: {blocked}")
    results.append(("篡改被拒", blocked))
finally:
    shutil.move(str(backup), str(sub))

print("\n=== 4) 恢复正常后必须放行 ===")
g2 = cli._pre_submit_gate("demo-comp")
print(f"   ok={g2['ok']}  text={str(g2.get('text'))[:70]}")
results.append(("正常放行", bool(g2["ok"])))

print("\n=== 5) best_node 不一致时必须被拒 ===")
# 临时把 best_node.txt 改成别的 id，模拟"restore 后没重跑 write-sub"
bn = cli.kapi.WORK_DIR / "demo-comp" / "best_node.txt"
bn_backup = bn.read_text(encoding="utf-8")
try:
    bn.write_text("deadbeefdead", encoding="utf-8")
    g3 = cli._pre_submit_gate("demo-comp")
    print(f"   ok={g3['ok']}  reason={str(g3.get('reason'))[:90]}")
    blocked2 = not g3["ok"]
    print(f"   → 不一致被拒: {blocked2}")
    results.append(("best_node 不一致被拒", blocked2))
finally:
    bn.write_text(bn_backup, encoding="utf-8")

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
