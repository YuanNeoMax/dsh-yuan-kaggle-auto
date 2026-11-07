"""LLM 调用与代码抽取：从 `iter.py` 拆出来。

## 为什么拆

`iter.py` 涨到 862 行，里面混了三类东西：
1. **prompt 模板**（给人读的文字）
2. **LLM 通信**（SSE 流式解析、重试、代码抽取）
3. **搜索循环**（树搜索、动作选择、并发调度）

这三类改动频率完全不同：模板经常调、通信偶调、搜索循环很稳定。
混在一个文件里，每次改模板都要读一整屏无关代码。

这里拆出第 1、2 类，`iter.py` 只留搜索循环。对外接口保持不变
（`iter` 仍 re-export 这些名字），以免破坏已有调用方与工具脚本。

## 保留的实测经验（不要简化掉）

- 推理模型必须 `stream=True`：非流式会返回 `content=None` 或 `RemoteDisconnected`
- `max_tokens` 给 8000 最好；16000 反而更差
- `reasoning_effort=disabled` 会 400，只能用 low/medium/high
- 实测耗时 274~479s，240s 超时必失败
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import http.client

from . import llm


def pred_spec_for(metric: str, kind: str) -> str:
    """告诉 LLM：valid_pred 该交什么形态。"""
    if metric in {"auc", "logloss", "map"}:
        return ("每个样本的**正类概率**（0~1 的浮点，不是硬标签）。"
                "二分类用 predict_proba(X)[:,1]；多分类交 (n, K) 概率矩阵")
    if metric in {"accuracy", "f1"}:
        return "每个样本的**类别标签**（0/1 或类别名），不是概率"
    return "每个样本的**预测数值**（回归目标）"


def _consume_sse(resp, on_delta=None, with_diag=False):
    """消费 OpenAI 兼容的 SSE 流，返回累积文本（可选返回流诊断）。

    推理模型有时先吐 reasoning 再吐 content；两者都要接，
    否则会把"思考"当输出丢掉（实测出现过 content 只有 555 字符的假短回复）。

    `with_diag=True` 时返回 `(text, diag)`，diag 描述流里究竟收到了什么 ——
    空响应时这是唯一的排查线索（"收到 0 行 data:" vs "收到 20 行但 choices 为空"
    指向完全不同的原因）。
    """
    parts = []
    n_data = 0
    n_choices = 0
    saw_done = False
    n_reasoning = 0
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line or not line.startswith("data:"):
            continue
        n_data += 1
        payload = line[5:].strip()
        if payload == "[DONE]":
            saw_done = True
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = obj.get("choices") or []
        if not choices:
            continue
        n_choices += 1
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if delta.get("reasoning_content"):
            n_reasoning += 1
        if piece:
            parts.append(piece)
            if on_delta:
                on_delta(piece)
    text = "".join(parts)
    if with_diag:
        diag = (f"data行={n_data} choices={n_choices} "
                f"content片段={len(parts)} reasoning片段={n_reasoning} "
                f"[DONE]={saw_done}")
        return text, diag
    return text


def llm_call(prompt: str, model: str | None = None, timeout: int | None = None,
             retries: int = 2, effort: str | None = None) -> str:
    """调一次推理模型，强制流式。

    实测：非流式返回 `content=None` / `RemoteDisconnected`，
    流式稳定拿到 7550~11807 字符。所以这里硬编码 stream=True。
    """
    import os

    cfg = llm.resolve(model)
    # `llm.resolve()` 返回的可用性字段叫 **ok**，不是 ready。
    # 实测踩到：第 8 轮把这段搬进 llmio.py 时写成了 cfg.get("ready")，
    # 而那个键根本不存在 -> 永远 None -> `if not None` 恒真 ->
    # **每一次 LLM 调用都直接抛「LLM 不可用：None」**。
    # 当时用 mock 端点跑迭代所以没暴露（mock 走的是另一条路径）。
    if not (cfg.get("ok") or cfg.get("ready")):
        raise RuntimeError(
            f"LLM 不可用：{cfg.get('reason') or cfg.get('hint') or '未找到可用端点'}"
            f"（source={cfg.get('source')}  base={cfg.get('base')}）")

    if timeout is None:
        try:
            timeout = int(os.environ.get("KAUTO_LLM_TIMEOUT", "900"))
        except ValueError:
            timeout = 900

    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 8000,
        "stream": True,
    }
    eff = effort if effort is not None else os.environ.get("KAUTO_LLM_EFFORT", "low")
    if eff:
        body["reasoning_effort"] = eff

    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg.get('key', '')}"},
        method="POST",
    )

    last_err = None
    last_diag = ""
    for attempt in range(max(1, retries + 1)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text, diag = _consume_sse(resp, with_diag=True)
            if text.strip():
                return text
            # 空响应不是"模型没内容"，而是流里什么都没收到 ——
            # 实测过：网关偶发 SSLEOF / 提前关流都会走到这里。
            # 只说"空响应"无从排查，必须带上流诊断（收到了几行 data:、
            # 有没有 [DONE]、有没有 choices）。
            last_err = f"空响应（{diag}）"
            last_diag = diag
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            # 空响应多半是流被提前关掉，重试间隔不必等太久；
            # 网络类错误才需要退避。
            delay = 2 if "空响应" in (last_err or "") else min(30, 5 * (attempt + 1))
            time.sleep(delay)
    raise RuntimeError(
        f"LLM 调用失败（重试 {retries} 次）：{last_err}"
        + (f"　流诊断：{last_diag}" if last_diag else ""))


_CODE_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def extract_code(raw: str) -> str:
    """从 LLM 回复里抽出可执行的 Python 代码。

    优先取 ``` 围栏内的最长段；没有围栏就整段当代码。
    去掉可能混入的说明文字，保留两个必需函数。
    """
    if not raw:
        return ""
    blocks = _CODE_FENCE.findall(raw)
    code = max(blocks, key=len) if blocks else raw

    # 去掉行首的说明性文字（如 "Here is the solution:"）
    lines = code.splitlines()
    start = 0
    for i, ln in enumerate(lines):
        if ln.startswith(("import ", "from ", "def ", "#")):
            start = i
            break
    code = "\n".join(lines[start:])
    return code.strip()
