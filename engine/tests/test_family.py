"""验证模型族识别与族多样性约束。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kauto.iter import classify_family
from kauto.journal import Journal, Node

CASES = {
    "lightgbm": 'import lightgbm as lgb\nm = lgb.LGBMClassifier(n_estimators=400)',
    "xgboost": 'import xgboost as xgb\nm = xgb.XGBClassifier(n_estimators=300)',
    "random-forest": 'from sklearn.ensemble import RandomForestClassifier\nm = RandomForestClassifier(n_estimators=100)',
    "sklearn-hgb": 'from sklearn.ensemble import HistGradientBoostingClassifier\nm = HistGradientBoostingClassifier()',
    "linear": 'from sklearn.linear_model import LogisticRegression\nm = LogisticRegression()',
    "kernel": 'from sklearn.svm import SVC\nm = SVC(probability=True)',
    "neural": 'from sklearn.neural_network import MLPClassifier\nm = MLPClassifier()',
    "catboost": 'from catboost import CatBoostClassifier\nm = CatBoostClassifier()',
}

bad = 0
print("=== 族识别 ===")
for expect, code in CASES.items():
    got = classify_family(code)
    ok = got == expect
    bad += 0 if ok else 1
    print(f"  {'OK ' if ok else 'MISMATCH'} {expect:14s} -> {got}")

print("\n=== 族多样性约束 ===")
j = Journal("t", Path("."))
j.metric_maximize = True
j.append(Node(id="base", code="", score=0.90, stage="draft", round_no=0))
j.append(Node(id="a", code=CASES["lightgbm"], parent="base", score=0.93, stage="improve", round_no=1))
j.append(Node(id="b", code=CASES["lightgbm"], parent="a", score=0.94, stage="improve", round_no=2))
fams1 = {classify_family(n.code) for n in j.good_nodes if n.code}
print(f"  同族 good nodes -> 族={fams1} 族数={len(fams1)} 应触发 draft：{len(fams1) < 2}")

j.append(Node(id="c", code=CASES["random-forest"], parent="base", score=0.91,
              stage="improve", round_no=3))
fams2 = {classify_family(n.code) for n in j.good_nodes if n.code}
print(f"  加入异构后 -> 族={fams2} 族数={len(fams2)} 不再强制 draft：{len(fams2) >= 2}")

if len(fams1) >= 2:
    bad += 1
if len(fams2) < 2:
    bad += 1

print(f"\n{'全部通过' if bad == 0 else f'失败 {bad} 项'}")
raise SystemExit(1 if bad else 0)
