"""验证泄漏扫描：真泄漏代码必须被抓到，干净代码不能误报。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kauto.leakcheck import scan, describe

CASES = {
    "干净方案": """
def build_features(train, valid, test):
    freq = train['segment'].value_counts(normalize=True).to_dict()
    out_tr = train[['x1','x2']].copy()
    out_va = valid[['x1','x2']].copy()
    out_te = test[['x1','x2']].copy()
    out_tr['segment_freq'] = train['segment'].map(freq).fillna(0)
    out_va['segment_freq'] = valid['segment'].map(freq).fillna(0)
    out_te['segment_freq'] = test['segment'].map(freq).fillna(0)
    return out_tr, out_va, out_te, train['target'], valid['target']

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=400, random_state=42, verbose=-1)
    m.fit(X_train, y_train)
    return m.predict_proba(X_valid)[:, 1], m.predict_proba(X_test)[:, 1]
""",

    "在 valid 上 fit scaler": """
def build_features(train, valid, test):
    from sklearn.preprocessing import StandardScaler
    sc = StandardScaler()
    Xv = sc.fit_transform(valid[['x1','x2']])
    Xt = sc.transform(test[['x1','x2']])
    Xtr = sc.transform(train[['x1','x2']])
    return Xtr, Xv, Xt, train['target'], valid['target']
""",

    "全量 fit 后切": """
def build_features(train, valid, test):
    import pandas as pd
    from sklearn.preprocessing import TargetEncoder
    all_df = pd.concat([train, valid, test])
    enc = TargetEncoder()
    all_df['enc'] = enc.fit_transform(all_df['segment'], all_df['target'])
    return all_df.iloc[:len(train)][['enc']], all_df.iloc[len(train):len(train)+len(valid)][['enc']], all_df.iloc[len(train)+len(valid):][['enc']], train['target'], valid['target']
""",

    "验证集早停又报分": """
def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=1000, verbose=-1)
    m.fit(X_train, y_train, eval_set=[(X_valid, y_valid)],
          callbacks=[lgb.early_stopping(50)])
    return m.predict_proba(X_valid)[:, 1], m.predict_proba(X_test)[:, 1]
""",
}

for name, code in CASES.items():
    r = scan(code)
    print(f"\n=== {name} ===")
    print(f"  ok={r['ok']}  high={r['n_high']}  medium={r['n_medium']}")
    for f in r["findings"]:
        print(f"    [{f['severity']}] {f['rule']}: {f['why'][:60]}")
