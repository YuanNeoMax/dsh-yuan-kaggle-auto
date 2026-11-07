"""Provider 固定：面板上选了通道就必须用它，不能悄悄换成别的。

## 补的是什么缺陷

宿主插件的面板让用户选 LLM 通道（auto / ofm / trae / deepseek / openai …），
并把它作为 `KAUTO_LLM_PROVIDER` 传给引擎。**这个变量此前根本没有被读取** ——
面板上选了 provider 却毫无作用，是一个静默失效的配置项。

## 三条语义（都在测试里钉住）

1. **未设置 / `auto`** → 走完整探测链（保持旧行为，不改变既有安装）；
2. **已知名字** → 只用该通道；它不可用时 `ok: False` + 可操作 hint，
   **不退回其它通道**；
3. **未知名字** → 退回 auto（但 hint 说明不认识），因为那个值不承载
   任何可以遵守的意图。

第 2 条为什么不能兜底：用户选 deepseek，可能正是不想用宿主网关
（计费、隐私、限流）。悄悄换通道会让他以为在用 A、实际在用 B。

本测试只动环境变量与已注册通道表，不发起网络请求。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import llm  # noqa: E402

results = []


def mark(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail})" if detail else ""))


# 保存并隔离所有相关环境变量，测试结束恢复
_KEYS = ("KAUTO_LLM_PROVIDER", "KAUTO_LLM_BASE", "KAUTO_LLM_KEY", "KAUTO_LLM_MODEL",
         "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY",
         "SILICONFLOW_API_KEY", "OPENROUTER_API_KEY")
_saved = {k: os.environ.get(k) for k in _KEYS}


def reset_env(**overrides):
    for k in _KEYS:
        os.environ.pop(k, None)
    for k, v in overrides.items():
        if v:
            os.environ[k] = v


def visible(result):
    """去掉 key 值再打印 —— 绝不把凭据写进日志。"""
    return {k: v for k, v in result.items() if k != "key"}


try:
    # ── 1. auto：行为不变 ──────────────────────────────────────────────────
    print("=== 1) 未设置 provider 时保持旧行为 ===")
    for name in (None, "auto", "AUTO", "  auto  "):
        reset_env(KAUTO_LLM_PROVIDER=name)
        r = llm.resolve()
        # 本机可能没有任何通道可用；两种结局都合法，但不能报"未知 provider"
        mark(f"provider={name!r} 不当作未知", "未知 provider" not in str(r.get("reason", "")),
             r.get("source") or r.get("reason", "")[:40])

    # ── 2. 显式 base+key 仍最优先 ─────────────────────────────────────────
    print("\n=== 2) 显式 base+key 优先于 provider ===")
    reset_env(KAUTO_LLM_PROVIDER="deepseek", KAUTO_LLM_BASE="http://localhost:9/v1",
              KAUTO_LLM_KEY="sk-x")
    r = llm.resolve()
    print(f"   {visible(r)}")
    mark("env 路径胜出", r.get("source") == "env", r.get("source"))
    mark("base 被采用", r.get("base") == "http://localhost:9/v1")

    # ── 3. 已知 provider 无 key：硬失败，不兜底 ────────────────────────────
    print("\n=== 3) 选了 deepseek 但没有 key ===")
    reset_env(KAUTO_LLM_PROVIDER="deepseek")
    r = llm.resolve()
    print(f"   {visible(r)}")
    mark("明确失败", r.get("ok") is False)
    mark("理由点名缺少的环境变量", "DEEPSEEK_API_KEY" in str(r.get("reason", "")))
    mark("hint 给出三种处理方式",
         all(k in str(r.get("hint", "")) for k in ("LLM API Key", "auto")))
    mark("没有悄悄换通道", "source" not in r or r.get("source") != "dsh-channel-pack-gateway")

    # ── 4. 已知 provider 有 key：只用它 ───────────────────────────────────
    print("\n=== 4) 选了 deepseek 且有 key ===")
    reset_env(KAUTO_LLM_PROVIDER="deepseek", DEEPSEEK_API_KEY="sk-test")
    r = llm.resolve()
    print(f"   {visible(r)}")
    mark("走该 provider", r.get("ok") is True and r.get("source") == "provider:deepseek")
    mark("base 是 deepseek 官方端点", "api.deepseek.com" in str(r.get("base", "")))
    mark("有默认模型", r.get("model") == "deepseek-chat")
    mark("key 已带上（不打印）", bool(r.get("key")))

    # ── 5. model 覆盖 ─────────────────────────────────────────────────────
    print("\n=== 5) 面板填了 model 时应覆盖默认 ===")
    reset_env(KAUTO_LLM_PROVIDER="deepseek", DEEPSEEK_API_KEY="sk-test",
              KAUTO_LLM_MODEL="deepseek-reasoner")
    r = llm.resolve()
    mark("model 被覆盖", r.get("model") == "deepseek-reasoner", str(r.get("model")))

    # ── 6. 未知名字：退回 auto，但要说明 ─────────────────────────────────
    print("\n=== 6) 不认识的 provider 名 ===")
    reset_env(KAUTO_LLM_PROVIDER="nonsense")
    r = llm.resolve()
    print(f"   {visible(r)}")
    mark("不因为未知名字而硬失败", r.get("ok") is True or "未知" not in str(r.get("reason", "")),
         r.get("source") or r.get("reason", "")[:40])

    # ── 7. 每个名字都必须能给出确定回答 ───────────────────────────────────
    print("\n=== 7) 所有可选名字都有确定语义 ===")
    for name in ("ofm", "trae", "deepseek", "openai", "dashscope", "siliconflow", "openrouter"):
        reset_env(KAUTO_LLM_PROVIDER=name)
        got = llm._provider_channel(name, None)
        mark(f"{name} 返回确定结果（None 之外）", got is not None)
        mark(f"{name} 含 ok 字段", isinstance(got, dict) and "ok" in got)
        if got and not got.get("ok"):
            mark(f"{name} 失败时给了 hint", bool(got.get("hint")), str(got.get("hint", ""))[:52])
finally:
    for k, v in _saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
for n, ok, _ in results:
    if not ok:
        print(f"  FAIL {n}")
print("全部通过" if not bad else f"{len(bad)} 项未通过")
raise SystemExit(1 if bad else 0)
