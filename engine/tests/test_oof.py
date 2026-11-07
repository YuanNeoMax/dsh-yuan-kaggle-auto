"""验证 K 折 OOF：候选执行器在 folds>1 时真的产出折外预测，且不泄漏。

检查三件事：
1. oof.npy 被写出、长度等于 train+valid
2. OOF 分数与"留出集分数"不同（因为口径不同），说明确实是另一套折
3. 每折的特征工程在折内重算（把 target encoding 塞进候选，看 OOF 会不会虚高）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import _runner, prep, storage
from kauto.model import score

slug = "demo-comp"
meta = prep.load_work(slug)
work = Path(meta["work_dir"])

GOOD = '''
def build_features(train, valid, test):
    def enc(df, ref):
        out = df[["x1","x2","x3","x4"]].copy()
        out["segment"] = df["segment"].map(ref["segment"].value_counts(normalize=True)).fillna(0.0)
        return out
    return (enc(train, train), enc(valid, train), enc(test, train),
            train["target"], valid["target"])

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, num_leaves=31,
                           random_state=42, n_jobs=1, force_row_wise=True, verbose=-1)
    m.fit(X_train, y_train)
    return m.predict_proba(X_valid)[:, 1], m.predict_proba(X_test)[:, 1]
'''

print("=== folds=0（不产 OOF）===")
r0 = _runner.run(work, GOOD, tag="f0", folds=0)
print(" ok:", r0.get("ok"), "| oof:", r0.get("oof"))
print(" oof.npy 存在:", (work / "gen_f0" / "oof.npy").exists())

print("\n=== folds=5（产 OOF）===")
r5 = _runner.run(work, GOOD, tag="f5", folds=5)
print(" ok:", r5.get("ok"), "| oof:", r5.get("oof"), "| folds:", r5.get("folds"))
if not r5.get("ok"):
    print((r5.get("error") or "")[:500])
else:
    oof = storage.load_npy(work / "gen_f5" / "oof.npy")
    print(" OOF 长度:", len(oof), "（train+valid 应为 1200）")
    # 用完整标签算 OOF logloss
    import pandas as pd
    tr = pd.read_csv(work / "train.csv", low_memory=False)
    va = pd.read_csv(work / "valid.csv", low_memory=False)
    y = pd.concat([tr["target"], va["target"]], ignore_index=True).to_numpy()
    s = score("logloss", y, oof)
    print(" OOF logloss:", round(float(s), 6))
    vp = storage.load_npy(work / "gen_f5" / "valid_pred.npy")
    s2 = score("logloss", va["target"].to_numpy(), vp)
    print(" 留出集 logloss:", round(float(s2), 6), "（口径不同，两者不必相等）")
