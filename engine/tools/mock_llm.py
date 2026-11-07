"""假 LLM：按 prompt 类型返回不同候选，用来验证树搜索的编排逻辑。

三种响应：
- draft   → 基线等价代码
- improve → 加了交互特征的改进版（应得分更高）
- debug   → 修掉报错的版本
第一次 debug 故意返回坏代码，验证「失败节点会被记住、后续会去修它」。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {"n": 0, "kinds": {"draft": 0, "improve": 0, "debug": 0}}

DRAFT = """
```python
def build_features(train, valid, test):
    def enc(df, ref):
        out = df[['x1','x2','x3','x4']].copy()
        out['segment'] = df['segment'].map(ref['segment'].value_counts(normalize=True)).fillna(0.0)
        return out
    return (enc(train, train), enc(valid, train), enc(test, train),
            None, None)

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=400, learning_rate=0.05, num_leaves=63,
                           random_state=42, verbose=-1)
    m.fit(X_train, y_train)
    return m.predict_proba(X_valid)[:, 1], m.predict_proba(X_test)[:, 1]
```
"""

IMPROVE = """
```python
import numpy as np
def build_features(train, valid, test):
    def enc(df, ref):
        out = df[['x1','x2','x3','x4']].copy()
        out['segment'] = df['segment'].map(ref['segment'].value_counts(normalize=True)).fillna(0.0)
        out['x2_gt04'] = (df['x2'] > 0.4).astype(int)
        out['x4_gt062'] = (df['x4'] > 0.62).astype(int)
        out['seg_b'] = (df['segment'] == 'b').astype(int)
        out['x1_x4'] = df['x1'] * df['x4']
        out['x1_x2'] = df['x1'] * df['x2']
        return out
    return (enc(train, train), enc(valid, train), enc(test, train),
            None, None)

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=600, learning_rate=0.03, num_leaves=31,
                           subsample=0.9, colsample_bytree=0.85,
                           random_state=42, verbose=-1)
    m.fit(X_train, y_train)
    return m.predict_proba(X_valid)[:, 1], m.predict_proba(X_test)[:, 1]
```
"""

BROKEN = """
```python
def build_features(train, valid, test):
    return train[['x9']], valid[['x9']], test[['x9']], None, None

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    return [0.5] * len(X_valid), [0.5] * len(X_test)
```
"""

# 带数据泄漏的方案：在验证集上 fit scaler。
# 它跑得通、CV 通常还虚高，所以能骗过"只看分数"的流程 ——
# 正是泄漏审查该拦下的那种。
LEAKY = """
```python
def build_features(train, valid, test):
    from sklearn.preprocessing import StandardScaler
    sc = StandardScaler()
    Xv = sc.fit_transform(valid[['x1','x2']])
    Xt = sc.transform(test[['x1','x2']])
    Xtr = sc.transform(train[['x1','x2']])
    return Xtr, Xv, Xt, None, None

def fit_predict(X_train, y_train, X_valid, y_valid, X_test):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=400, random_state=42, verbose=-1)
    m.fit(X_train, y_train)
    return m.predict_proba(X_valid)[:, 1], m.predict_proba(X_test)[:, 1]
```
"""


def classify(prompt: str) -> str:
    if "跑失败了" in prompt and "连续调试" in prompt:
        return "debug"
    if "已经跑通" in prompt and "原子改进" in prompt:
        return "improve"
    return "draft"


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        prompt = body["messages"][0]["content"]
        kind = classify(prompt)
        STATE["n"] += 1
        STATE["kinds"][kind] += 1
        cur = STATE["kinds"][kind]

        if kind == "draft" and cur == 1:
            # 第一次 draft 故意崩：制造一个 buggy 叶子，
            # 这样 search_policy 才有对象可 debug（否则 debug 分支永远走不到）
            code = BROKEN
        elif kind == "draft" and cur == 2:
            # 第二次 draft 给一个带泄漏的方案，验证泄漏审查会拒收它
            code = LEAKY
        elif kind == "debug" and cur == 1:
            code = DRAFT            # 第一次调试成功修好它
        elif kind == "improve":
            code = IMPROVE
        else:
            code = DRAFT

        # 支持流式（正式 llm_call 用 stream=true）
        if body.get("stream"):
            out = []
            out.append("data: " + json.dumps({
                "choices": [{"index": 0, "delta": {"content": code},
                             "finish_reason": None}]}, ensure_ascii=False) + "\n\n")
            out.append("data: " + json.dumps({
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                ensure_ascii=False) + "\n\n")
            out.append("data: [DONE]\n\n")
            payload = "".join(out).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        resp = json.dumps({
            "id": "mock",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": code},
                         "finish_reason": "stop"}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass


def serve(port: int = 8899) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    print("mock LLM on http://127.0.0.1:8899/v1", flush=True)
    serve().serve_forever()
