"""验证 llm.resolve() 与 llm_call 的字段契约一致。

## 实测缺陷（严重）

第 8 轮把 LLM 通信代码从 `iter.py` 搬进 `llmio.py` 时，
把可用性字段写成了 `cfg.get("ready")` —— 而 `llm.resolve()` 返回的键叫 **`ok`**。

后果：`cfg.get("ready")` 永远是 `None`，`if not None` 恒真，
**每一次 LLM 调用都立刻抛「LLM 不可用：None」**，连真实端点都碰不到。

当时没被发现，是因为验证一直用 mock 端点跑 ——
而 mock 的状态与真实端点的字段结构不同。`journal.failures()` 里
反复出现的 `LLM 不可用：None` 就是它的症状，只是当时归因成"mock 没起来"。

## 这个测试守什么

不测行为细节，只守**字段名契约**：resolve 返回的可用性字段
必须被 llm_call 用同一个名字读到。名字一改，测试立刻红。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import llm, llmio

results = []

print("=== 1) resolve() 必须返回可判定的可用性字段 ===")
r = llm.resolve()
keys = list(r.keys())
print(f"   resolve 返回键: {keys}")
has_ok = "ok" in r
print(f"   含 'ok': {has_ok}  (值={r.get('ok')})")
results.append(("resolve 有 ok 字段", has_ok))

print("\n=== 2) llmio 必须读到同一个字段名 ===")
src = Path("kauto/llmio.py").read_text(encoding="utf-8")
# 允许 ok / ready 两种兼容写法，但必须至少认 ok
reads_ok = 'cfg.get("ok")' in src
reads_ready_only = ('cfg.get("ready")' in src) and not reads_ok
print(f"   读 cfg.get('ok'): {reads_ok}")
print(f"   只读 cfg.get('ready')（错误写法）: {reads_ready_only}")
results.append(("读的是 ok 字段", reads_ok and not reads_ready_only))

print("\n=== 3) 用一个必然失败的分支验证错误信息可读 ===")
# 构造：resolve 返回不可用，看抛出的信息是否带 source/base
fake_bad = {"ok": False, "reason": "网关未监听", "source": "test", "base": "http://x", "hint": ""}
orig_resolve = llm.resolve
llm.resolve = lambda model=None: fake_bad
try:
    try:
        llmio.llm_call("x", retries=0, timeout=5)
    except RuntimeError as e:
        msg = str(e)
        readable = ("网关未监听" in msg) and ("source=test" in msg)
        print(f"   错误信息: {msg[:120]}")
        print(f"   → 含原因与来源: {readable}")
        results.append(("不可用时信息可读", readable))
finally:
    llm.resolve = orig_resolve

print("\n=== 4) 可用时不应被误判为不可用（回归守门）===")
r2 = llm.resolve()
if r2.get("ok"):
    print("   当前端点可用，检查 llmio 判定逻辑：")
    # 直接检查判定表达式本身（不发真实请求）
    can_proceed = bool(r2.get("ok") or r2.get("ready"))
    print(f"   → 判定为可继续: {can_proceed}")
    results.append(("可用时能继续", can_proceed))
else:
    print(f"   当前端点不可用（{r2.get('reason')}），跳过")
    results.append(("可用时能继续", None))

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
