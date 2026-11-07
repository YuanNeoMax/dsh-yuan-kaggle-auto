"""LLM 凭据自动发现。

按优先级尝试，先用谁就用谁，全程不打印明文：

1. 显式参数 / KAUTO_LLM_* 环境变量
2. **DSH 渠道包网关**（127.0.0.1:8326）—— 本项目实际使用的主路径：
   它把 14 个渠道统一成 OpenAI 端点，本项目的目标是其中 TRAE 的
   DeepSeek V4.1 Flash（`trae/deepseek-v4.1-flash`，x0.06 倍率，1M 上下文）。
   key 是插件生成的随机串，存在 ~/.dsh/openai-gateway/api-key。
3. DSH our-free-model 的 forward 监听（127.0.0.1:18899，需手动开启）
4. DSH 已配置的外部 provider key（~/.dsh/.credentials.yaml 里的 *_API_KEY）
5. 本地 ollama
"""
from __future__ import annotations

import json
import os
import re
import socket
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

HOME = Path(os.environ.get("DSH_HOME") or (Path(os.environ.get("USERPROFILE") or Path.home()) / ".dsh"))
OFM_SETTINGS = HOME / "our-free-model" / "settings.json"
OFM_CATALOG = HOME / "our-free-model" / "catalog.json"
DSH_CRED = HOME / ".credentials.yaml"

# provider 名 → (base url, 默认模型)
KNOWN = {
    "ofm": ("http://127.0.0.1:18899/v1", "kilo-auto/free"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "siliconflow": ("https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct"),
    "openrouter": ("https://openrouter.ai/api/v1", "deepseek/deepseek-chat"),
    "ollama": ("http://127.0.0.1:11434/v1", "qwen2.5-coder"),
}

_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_]+_API_KEY)\s*:\s*(.+?)\s*$")


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def _provider_channel(name: str, model_override: str | None) -> dict | None:
    """把面板上选的 provider 名字解析成一条通道。

    返回 None 表示"这个名字不认识，调用方应退回 auto"。
    认识但不可用的通道返回 `ok: False` 并带可操作 hint ——
    用户显式选了它，就不能悄悄换成别的。
    """
    # 名字 → (默认 base, 默认 model, 需要 key 的环境变量名)
    channels: dict[str, tuple[str, str, str]] = {
        "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", "DEEPSEEK_API_KEY"),
        "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
        "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1",
                      "qwen2.5-coder-32b-instruct", "DASHSCOPE_API_KEY"),
        "siliconflow": ("https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-Coder-32B-Instruct",
                        "SILICONFLOW_API_KEY"),
        "openrouter": ("https://openrouter.ai/api/v1", "deepseek/deepseek-chat",
                       "OPENROUTER_API_KEY"),
    }

    if name == "ofm":
        fw = ofm_forward()
        if fw["listening"] and fw["has_key"]:
            return {"ok": True, "base": fw["base"], "key": fw["key"],
                    "model": model_override or (ofm_best_model() or PREFERRED_MODEL),
                    "source": "ofm-forward", "hint": ""}
        return {"ok": False, "reason": "ofm 通道未监听",
                "hint": "面板里选了 ofm：需要先在 DSH 里启用 our-free-model 的 forward 监听，"
                        "或把 LLM 通道改回 auto。"}

    if name == "trae":
        gw = gateway_available()
        if gw["listening"] and gw["has_key"]:
            return {"ok": True, "base": gw["base"], "key": gateway_key(),
                    "model": model_override or PREFERRED_MODEL,
                    "source": "trae-via-gateway", "hint": ""}
        return {"ok": False, "reason": "trae 需要 DSH 渠道包网关",
                "hint": "trae 通过 DSH 渠道包网关（127.0.0.1:8326）访问；"
                        "该网关未运行。把通道改回 auto，或直接配 deepseek/openai 的 key。"}

    if name not in channels:
        # 不认识的取值：退回 auto，但要说明，否则会变成又一个静默失效的配置。
        return {"ok": False, "reason": f"未知 provider: {name}",
                "hint": f"不认识的通道名 `{name}`，已退回自动探测。"
                        f"可用：auto / ofm / trae / {', '.join(sorted(channels))}"}

    base, default_model, key_env = channels[name]
    key = _env(key_env)
    if not key:
        return {"ok": False, "reason": f"{name} 缺少 API key（{key_env} 为空）",
                "hint": f"面板里选了 {name}，但没有可用的 key。"
                        f"在设置里填「LLM API Key」，或设置环境变量 {key_env}；"
                        f"也可以把通道改回 auto 复用宿主已配的凭据。"}
    return {"ok": True, "base": base, "key": key,
            "model": model_override or default_model,
            "source": f"provider:{name}", "hint": ""}


def _port_open(url_base: str, timeout: float = 1.5) -> bool:
    try:
        u = urlparse(url_base)
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def ofm_settings() -> dict:
    return _read_json(OFM_SETTINGS)


def ofm_forward() -> dict:
    """返回网关状态：{configured, listening, key, port, base}。key 只用于内部拼接，不外显。"""
    st = ofm_settings()
    fwd = st.get("forward") or {}
    port = int(fwd.get("port") or 18899)
    host = fwd.get("host") or "127.0.0.1"
    base = f"http://{host}:{port}/v1"
    key = st.get("forwardKey") or ""
    return {
        "configured": fwd.get("enabled") is True,
        "listening": _port_open(base),
        "base": base,
        "port": port,
        "key": key,
        "has_key": bool(key),
    }


def ofm_models() -> list[dict]:
    """免费通道能用的模型清单（来自插件自己的 catalog）。"""
    cat = _read_json(OFM_CATALOG)
    rows: list[dict] = []
    for r in cat.get("kiloRows") or []:
        rows.append({"id": r.get("id"), "name": r.get("name"), "channel": r.get("channel"),
                     "context": r.get("contextWindow"), "reasoning": r.get("reasoning")})
    return [r for r in rows if r["id"]]


def ofm_best_model() -> str | None:
    """挑一个适合写特征工程代码的默认模型。

    排序依据是"能不能稳定吐出一个完整的 Python 文件"，不是参数量：
    视觉模型（vl）和内容安全模型不选——它们要么对代码输出有额外格式约束，
    要么根本不是通用的。优先选带 code 的，其次 auto，再次任意 free 文本模型。
    """
    rows = ofm_models()
    if not rows:
        return None

    def score(r: dict) -> tuple[int, int]:
        mid = (r.get("id") or "").lower()
        name = (r.get("name") or "").lower()
        s = 0
        if "code" in mid or "code" in name:
            s += 100
        if "auto" in mid:
            s += 80
        if "vl" in mid or "vision" in name or "safety" in mid or "safety" in name:
            s -= 200
        if "instruct" in mid or "chat" in mid:
            s += 10
        # 上下文越长越能塞下完整 prompt（数据 schema + 历史）
        ctx = r.get("context") or 0
        return (s, int(ctx))

    best = max(rows, key=score)
    return best.get("id")


# ---------------------------------------------------------------- channel-pack 网关

# 渠道包的 OpenAI 网关（不是 our-free-model 的 forward 监听，是另一条路）。
# 它把 14 个渠道（TRAE / CodeBuddy / Qoder / Cline / MiniMax ...）统一成
# `provider/model` 形式的 OpenAI 端点，默认 127.0.0.1:8326。
GW_APIKEY_FILE = HOME / "openai-gateway" / "api-key"
GW_DEFAULT_PORT = 8326

# 本项目的首选模型：TRAE 的 DeepSeek V4.1 Flash（x0.06 倍率，1M 上下文）。
PREFERRED_MODEL = "trae/deepseek-v4.1-flash"


def gateway_key() -> str:
    try:
        return GW_APIKEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def gateway_base(port: int = GW_DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}/v1"


def gateway_available(port: int = GW_DEFAULT_PORT) -> dict:
    """渠道包网关是否在跑，是否认得目标模型。

    光看端口不够：端口在监听但模型不在 /v1/models 名单里（比如没登录该渠道、
    或模型被 disabledModels 关掉），请求会 404 —— 那也算不可用。
    """
    base = gateway_base(port)
    key = gateway_key()
    info = {"base": base, "listening": _port_open(base), "has_key": bool(key),
            "preferred_model": PREFERRED_MODEL, "model_listed": None}
    if info["listening"] and key:
        try:
            import json as _json
            req = urllib.request.Request(
                base + "/models", headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = _json.loads(r.read().decode("utf-8"))
            ids = [m.get("id") for m in (data.get("data") or [])]
            info["model_listed"] = PREFERRED_MODEL in ids
            info["model_count"] = len(ids)
        except Exception as e:
            info["probe_error"] = str(e)[:200]
    return info


def _dsh_keys() -> dict[str, str]:
    """从 DSH 凭据文件里抽 *_API_KEY 行。只收 key 名与值，不碰其他内容。"""
    if not DSH_CRED.exists():
        return {}
    found: dict[str, str] = {}
    try:
        for line in DSH_CRED.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _KEY_RE.match(line)
            if m:
                name, val = m.group(1), m.group(2).strip().strip('"').strip("'")
                if val:
                    found[name] = val
    except OSError:
        return {}
    return found


def _provider_from_keyname(name: str) -> str | None:
    low = name.lower()
    for p in KNOWN:
        if p == "ofm":
            continue
        if low.startswith(p.upper()) or p in low:
            return p
    return None


def resolve(explicit_base: str | None = None, explicit_key: str | None = None,
            explicit_model: str | None = None, prefer: str | None = None) -> dict:
    """返回 {ok, base, key, model, source, hint}。绝不返回明文 key 到日志。

    优先级：
      显式参数 → KAUTO_LLM_* / OPENAI_API_KEY → KAUTO_LLM_PROVIDER 指定的通道
      → channel-pack 网关（TRAE DeepSeek V4.1 Flash）→ our-free-model forward
      → DSH 外部 provider key → ollama

    ## `KAUTO_LLM_PROVIDER` 是插件带进来的

    宿主插件面板上让用户选 provider（auto / ofm / deepseek / trae / openai …），
    并把它作为 `KAUTO_LLM_PROVIDER` 传给引擎。**这个变量此前没有被读取** ——
    面板上选了 provider 却毫无作用，是个静默失效的配置项。

    语义：
      · 未设置 / `auto`  → 走完整探测链（保持旧行为）
      · 已知名字          → 只用该通道；它不通就**明确报错**，
                            而不是悄悄退回别的通道
      · 未知名字          → 当作 auto，并在 hint 里说明不认识的取值

    为什么显式选择要"失败即报错"而不是"自动兜底"：用户选了 deepseek，
    可能正是因为不想用宿主的网关（计费、隐私、限流）。悄悄换通道会让
    他以为在用 A，实际在用 B。
    """
    base = explicit_base or _env("KAUTO_LLM_BASE")
    key = explicit_key or _env("KAUTO_LLM_KEY") or _env("OPENAI_API_KEY")
    model = explicit_model or _env("KAUTO_LLM_MODEL")

    if base and key:
        return {"ok": True, "base": base.rstrip("/"), "key": key,
                "model": model or PREFERRED_MODEL, "source": "env", "hint": ""}

    chosen = (_env("KAUTO_LLM_PROVIDER") or "").strip().lower()
    if chosen and chosen != "auto":
        pinned = _provider_channel(chosen, model)
        if pinned is not None:
            if pinned.get("ok"):
                return pinned
            # A known provider that cannot be reached is a hard failure:
            # the user picked it on purpose (billing, privacy, rate limits),
            # so silently switching channels would mean they run on something
            # other than what they asked for.
            # An UNKNOWN name is different — that falls through to auto below,
            # because the value carries no intent we could honour.
            if "未知 provider" not in str(pinned.get("reason", "")):
                return pinned

    if prefer != "external":
        gw = gateway_available()
        if gw["listening"] and gw["has_key"]:
            return {"ok": True, "base": gw["base"], "key": gateway_key(),
                    "model": model or PREFERRED_MODEL,
                    "source": "dsh-channel-pack-gateway", "hint": ""}

        fw = ofm_forward()
        if fw["listening"] and fw["has_key"]:
            return {"ok": True, "base": fw["base"], "key": fw["key"],
                    "model": model or ofm_best_model() or KNOWN["ofm"][1],
                    "source": "dsh-our-free-model-gateway", "hint": ""}

    for name, val in _dsh_keys().items():
        prov = _provider_from_keyname(name)
        if prov:
            b, m = KNOWN[prov]
            return {"ok": True, "base": b, "key": val, "model": model or m,
                    "source": f"dsh-credentials:{name}", "hint": ""}

    b, m = KNOWN["ollama"]
    if _port_open(b):
        return {"ok": True, "base": b, "key": "ollama", "model": model or m,
                "source": "local-ollama", "hint": ""}

    reason = ""
    if not gw["listening"]:
        reason = (f"DSH 渠道包网关（{gw['base']}）未监听：DSH 需要处于运行状态。")
    return {
        "ok": False, "base": "", "key": "", "model": model or PREFERRED_MODEL,
        "source": "", "hint":
            f"{reason}\n没找到可用的 LLM。任选一种：\n"
            "  1) 启动 DSH（渠道包网关 127.0.0.1:8326 会自动监听），kauto 直接接入 "
            f"{PREFERRED_MODEL}\n"
            "  2) 环境变量：KAUTO_LLM_BASE / KAUTO_LLM_KEY / KAUTO_LLM_MODEL\n"
            "  3) 在 DSH 里配好 DEEPSEEK_API_KEY 等（kauto 自动读取 ~/.dsh/.credentials.yaml）\n"
            "  4) 本地 ollama：ollama serve 后自动识别 http://127.0.0.1:11434/v1",
    }


def apply(explicit_base: str | None = None, explicit_key: str | None = None,
          explicit_model: str | None = None, prefer: str | None = None) -> dict:
    """把发现的配置写进 os.environ，供 iter.py 的 llm_call 直接使用。"""
    r = resolve(explicit_base, explicit_key, explicit_model, prefer)
    if r["ok"]:
        os.environ["KAUTO_LLM_BASE"] = r["base"]
        os.environ["KAUTO_LLM_KEY"] = r["key"]
        os.environ["KAUTO_LLM_MODEL"] = r["model"]
    return r
