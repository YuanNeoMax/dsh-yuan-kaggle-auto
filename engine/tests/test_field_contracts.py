"""系统性守门：provider 返回的字段名不能被调用方猜错。

## 背景

第 27 轮发现 `llmio.py` 把 `llm.resolve()` 的可用性字段写成 `cfg.get("ready")`，
而实际键叫 `ok` —— 导致每一次 LLM 调用都立刻抛「LLM 不可用：None」，
**真实端点一次都没碰到**，而且隐藏了很多轮。

根因不是"某个人写错了"，而是**这套代码里字段命名不统一**：

| provider | 可用性字段 |
|---|---|
| `llm.resolve()` | **ok** |
| `winjob.available()` | **ok** |
| `appcontainer.available()` | **ok** |
| `kapi.creds()` | **ready** |
| `skillbridge.available()` | **vendored**（连可用性字段都不是 ok/ready） |

命名不一致 + 全用 `dict.get()`（取不到就是 None 不报错）= 静默失败。
所以这里做**契约测试**：把真实的键写进表里，逐个校验调用方能读到。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import appcontainer, kapi, llm, skillbridge, winjob

results = []

print("=== 1) 记录各 provider 的实际键（作为契约基线）===")
providers = {
    "llm.resolve()": (llm.resolve(), "ok"),
    "winjob.available()": (winjob.available(), "ok"),
    "appcontainer.available()": (appcontainer.available(), "ok"),
    "kapi.creds()": (kapi.creds(), "ok"),
    "skillbridge.available()": (skillbridge.available(), "vendored"),
}
for name, (d, key) in providers.items():
    has = key in d
    print(f"   {'OK  ' if has else 'FAIL'} {name:28s} 可用性字段='{key}'  实际键={sorted(d.keys())}")
    results.append((f"{name} 有 {key}", has))

print("\n=== 2) 代码里对 llm.resolve 只能读 ok（或兼容写 ok or ready）===")
src_llmio = Path("kauto/llmio.py").read_text(encoding="utf-8")
# 去掉注释行后检查
code_lines = [l for l in src_llmio.splitlines()
              if not l.strip().startswith("#")]
code = "\n".join(code_lines)
reads_ok = 'cfg.get("ok")' in code
ok2 = reads_ok
print(f"   llmio 代码里读 cfg.get('ok'): {reads_ok}")
results.append(("llmio 读 ok", ok2))

print("\n=== 3) 所有 provider 都能用统一的 ok 判定 ===")
# 修复后：creds 也加了 ok 别名，所有 provider 至少都能读 ok
ok_providers = [n for n, (d, _k) in providers.items() if "ok" in d]
print(f"   能用 ok 判定的: {ok_providers}")
all_have_ok = len(ok_providers) >= 4   # skillbridge 用 vendored，单独计
print(f"   → 统一别名覆盖 {len(ok_providers)}/5（skillbridge 用 vendored）")
results.append(("统一 ok 别名", all_have_ok))
# 向后兼容：ready 仍在
back_compat = "ready" in kapi.creds()
print(f"   creds 仍保留 ready（向后兼容）: {back_compat}")
results.append(("creds 保留 ready", back_compat))

print("\n=== 4) 关键：不可用时的错误信息必须带原因 ===")
# 用替身验证错误信息结构
orig = llm.resolve
llm.resolve = lambda model=None: {"ok": False, "reason": "模拟原因", "source": "s", "base": "b", "hint": ""}
try:
    from kauto import llmio
    try:
        llmio.llm_call("x", retries=0, timeout=3)
        msg = ""
    except RuntimeError as e:
        msg = str(e)
    has_reason = "模拟原因" in msg
    has_source = "source=" in msg
    print(f"   错误信息: {msg[:110]}")
    print(f"   → 含原因: {has_reason}  含来源: {has_source}")
    results.append(("错误信息带原因", has_reason))
    results.append(("错误信息带来源", has_source))
finally:
    llm.resolve = orig

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
