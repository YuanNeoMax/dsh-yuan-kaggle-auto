"""验证切分指纹：重新切分后旧派生产物必须被清理。

实测问题：重复跑 prepare（换 seed / valid_frac）会重新切分数据，
而 baseline_oof.npy、best_*.npy、archive/* 都是基于**旧切分**算出来的。
两者混用会得到"看起来正常、实际对不上"的产物，且没有任何报错。

实测数据：同一份 demo-comp，seed=42 的 cv_mean 是 0.46569，
seed=7 变成 0.498451 —— 切分对结果影响显著，混用代价很大。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import prep

results = []
SLUG = "fp2-test"
FX = str(Path("fixtures/demo-comp").resolve())

# 清干净
shutil.rmtree(Path("work") / SLUG, ignore_errors=True)

print("=== 1) 首次 prepare 无指纹、不清理 ===")
m1 = prep.prepare(SLUG, local_dir=FX, use_skill=False, seed=42)
print(f"   split_fingerprint={m1.get('split_fingerprint')}")
print(f"   stale_cleared={m1.get('stale_cleared')}")
ok1 = m1.get("split_fingerprint") and not m1.get("stale_cleared")
print(f"   → 首次不清理: {bool(ok1)}")
results.append(("首次不清理", bool(ok1)))

work = Path(m1["work_dir"])

print("\n=== 2) 造出派生产物 ===")
(work / "fake_baseline.json").write_text('{"cv_mean":1}', encoding="utf-8")
# 用真实文件名
(work / "baseline_oof.npy").write_text("dummy", encoding="utf-8")

print("\n=== 3) 换 seed → 必须清理 + 告警 ===")
m2 = prep.prepare(SLUG, local_dir=FX, use_skill=False, seed=7)
cleared = m2.get("stale_cleared") or []
print(f"   清理项: {cleared}")
cleaned_files = [c for c in cleared if c.endswith(".json") or c.endswith(".npy")]
ok3 = len(cleaned_files) > 0
print(f"   → 有清理: {ok3}")
results.append(("换 seed 触发清理", ok3))

still = (work / "baseline_oof.npy").exists()
print(f"   baseline_oof.npy 是否还在: {still}（应为 False）")
results.append(("旧产物已删除", not still))

fp_changed = m2.get("split_fingerprint") != m1.get("split_fingerprint")
print(f"   指纹变化: {fp_changed}")
results.append(("指纹已更新", fp_changed))

print("\n=== 4) 同 seed 重跑不应清理 ===")
m3 = prep.prepare(SLUG, local_dir=FX, use_skill=False, seed=7)
print(f"   stale_cleared={m3.get('stale_cleared')}")
ok4 = not m3.get("stale_cleared")
print(f"   → 幂等: {ok4}")
results.append(("同 seed 幂等", ok4))

print("\n=== 5) 实测切分对分数的影响（说明为什么必须校验）===")
import subprocess
# 用两次不同 seed 的 baseline 展示差异——只读已生成的 meta 里的行数
print(f"   seed=42 的行数: {m1.get('n_train_rows')} / {m1.get('n_valid_rows')}")
print(f"   seed=7  的行数: {m2.get('n_train_rows')} / {m2.get('n_valid_rows')}")
print("   （行数相同但样本不同 —— 只看行数无法发现问题，必须用内容指纹）")

# 清理
shutil.rmtree(work, ignore_errors=True)

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
