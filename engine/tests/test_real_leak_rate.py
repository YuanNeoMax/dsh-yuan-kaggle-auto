"""记录真实 LLM 的泄漏率，并验证审查层确实拦得住。

## 实测：真实 LLM 写法里泄漏率 25%

用 `trae/deepseek-v4.1-flash` 跑 `--folds-for-oof 5` 时，生成的代码被拒：

```
数据泄漏（静态审查拒收）：
[high] 在全量数据（含验证集）上 fit —— 典型的先 transform 再切分
[medium] 用验证集早停，随后又用同一验证集报分
```

**而且是三重叠加**：`concat` → 全量 `StandardScaler.fit` → 全量
`PolynomialFeatures.fit` → 再用 valid 早停 → 最后拿全量模型预测。

统计：有代码的 4 个节点里 1 个因泄漏被拒（**25%**）。

## 这条数据的意义

1. **审查层不是可选项**。25% 的泄漏率意味着"每 4 个候选就有 1 个在骗你"，
   而这些代码的 CV 会很好看、LB 会崩。
2. **真实模型比 mock 更容易泄漏**。mock 返回固定代码（不泄漏），
   所以前 30 多轮从没暴露过这个比例 —— 只有真实 LLM 才会写这种"实战风格"的代码：
   用 valid 早停（很多人觉得这是正常做法）、在全量上做标准化（教科书式写法）。

## 附带确认：OOF 路径本身没问题

用归档里的合法代码跑 `folds=5` → `ok=True`、`oof=True`、长度 1200。
上一轮没产 OOF，是因为**泄漏代码被拒收、根本没走到执行阶段** —— 这是正确行为。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from kauto import _runner, leakcheck, storage

results = []

print("=== 1) 真实 LLM 写过的泄漏代码，审查必须拒收 ===")
LEAKY = '''
X_full = pd.concat([X_train, X_valid], axis=0, ignore_index=True)
y_full = np.concatenate([y_train, y_valid])
scaler = StandardScaler().fit(X_full)
X_full_s = scaler.transform(X_full)
poly = PolynomialFeatures(degree=2).fit(X_full_s)
model.fit(X_train, y_train, eval_set=[(X_valid, y_valid)])
'''
r = leakcheck.scan(LEAKY)
print(f"   ok={r['ok']} high={r['n_high']} medium={r['n_medium']}")
for f in r.get("findings") or []:
    print(f"      {f['rule']} ({f['severity']})")
ok1 = not r["ok"] and r["n_high"] >= 1
print(f"   → 拒收: {ok1}")
results.append(("泄漏代码被拒", ok1))

print("\n=== 2) 三重叠加都应命中（不是只抓一条）===")
rules_hit = {f["rule"] for f in r.get("findings") or []}
print(f"   命中规则: {sorted(rules_hit)}")
ok2 = len(rules_hit) >= 2
print(f"   → 至少两条规则命中: {ok2}")
results.append(("多重泄漏被识别", ok2))

print("\n=== 3) OOF 路径在合法代码上正常 ===")
w = Path("work/demo-comp")
code_p = w / "archive" / "3984f15ea28b" / "candidate.py"
if code_p.exists():
    code = code_p.read_text(encoding="utf-8")
    rr = _runner.run(w, code, tag="oof_check", folds=5, container=None, timeout=300)
    print(f"   ok={rr.get('ok')} oof={rr.get('oof')} folds={rr.get('folds')}")
    o = w / "gen_oof_check" / "oof.npy"
    ln = len(np.asarray(storage.load_npy(o)).ravel()) if o.exists() else 0
    print(f"   oof.npy 长度: {ln}")
    ok3 = bool(rr.get("ok")) and bool(rr.get("oof")) and ln > 0
    print(f"   → OOF 路径可用: {ok3}")
    results.append(("OOF 路径可用", ok3))
else:
    print("   (缺归档代码，跳过)")
    results.append(("OOF 路径可用", None))

print("\n=== 4) 泄漏率统计（记录用）===")
import json
total = leaked = 0
for jp in Path("work").glob("*/journal.json"):
    try:
        j = json.loads(jp.read_text(encoding="utf-8"))
    except Exception:
        continue
    for n in j.get("nodes") or []:
        if n.get("code"):
            total += 1
            if "数据泄漏" in (n.get("error") or ""):
                leaked += 1
rate = leaked / total if total else 0
print(f"   有代码的节点 {total} 个，泄漏被拒 {leaked} 个 → {rate:.0%}")
print("   （样本很小，只作记录：真实模型确实会写泄漏代码）")
results.append((f"泄漏率记录 {rate:.0%}", None))

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
