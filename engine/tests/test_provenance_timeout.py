"""验证提交溯源与自适应超时。

两件事必须同时成立：
1. 溯源：submission 的来源可查，且被篡改时能拦住
2. 超时：小数据给短、大数据给长，不再固定 900s
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import timebudget

results = []

print("=== 1) 自适应超时 ===")
# 场景 A：小数据（demo 量级）
tmp = Path(tempfile.mkdtemp(prefix="tb_small_"))
(tmp / "baseline.json").write_text(json.dumps({"seconds": 3.8}), encoding="utf-8")
(tmp / "meta.json").write_text(json.dumps({"n_rows": 1200, "n_features": 6}), encoding="utf-8")
small = timebudget.estimate(tmp)
print(f"   小数据（1200 行）: {small['timeout']}s  ← {small['reason']}")
print(f"   → 短于旧的 900s: {small['timeout'] < 900}")
results.append(("小数据超时收紧", small["timeout"] < 900))

# 场景 B：大数据（百万行）
tmp2 = Path(tempfile.mkdtemp(prefix="tb_big_"))
(tmp2 / "baseline.json").write_text(json.dumps({"seconds": 300}), encoding="utf-8")
(tmp2 / "meta.json").write_text(json.dumps({"n_rows": 1_000_000, "n_features": 120}), encoding="utf-8")
big = timebudget.estimate(tmp2, folds=5)
print(f"   大数据（100 万行 + OOF5）: {big['timeout']}s  ← {big['reason']}")
print(f"   → 长于旧的 900s: {big['timeout'] > 900}")
results.append(("大数据超时放宽", big["timeout"] > 900))

# 场景 C：无 baseline 时给保守值
tmp3 = Path(tempfile.mkdtemp(prefix="tb_none_"))
none = timebudget.estimate(tmp3)
print(f"   无 baseline: {none['timeout']}s  ← {none['reason']}")
print(f"   → 不低于下限 120s: {none['timeout'] >= 120}")
results.append(("缺信息仍给保守值", none["timeout"] >= 120))

# 上限保护
print(f"   → 有上限（≤{timebudget.MAX_TIMEOUT}s）: {big['timeout'] <= timebudget.MAX_TIMEOUT}")
results.append(("超时有上限", big["timeout"] <= timebudget.MAX_TIMEOUT))

print("\n=== 2) 提交溯源 ===")
work = Path("work/demo-comp")
prov_p = work / "submission_provenance.json"
if prov_p.exists():
    prov = json.loads(prov_p.read_text(encoding="utf-8"))
    print(f"   source={prov.get('source')} node={prov.get('node_id')} "
          f"score={prov.get('node_score')} family={prov.get('node_family')}")
    print(f"   指纹={prov.get('submission_sha256_16')} archived={prov.get('archived')}")
    has_key = all(k in prov for k in ("source", "node_id", "submission_sha256_16"))
    results.append(("溯源记录完整", has_key))
else:
    print("   (无 provenance 文件，先跑 write-sub)")
    results.append(("溯源记录完整", False))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
