"""kauto link —— 检查并修复与 DSH our-free-model 网关的对接。

这个子命令存在的理由：网关是插件在 DSH 进程里起的监听，它的 key 由插件
首次启动时生成。所以"能不能用"分成三种状态，必须分别对待：

  READY     网关在监听且有 key —— 直接可用
  NEED_RESTART  设置已开但进程没重启 —— 提示重启，不假装失败
  NEED_ENABLE   设置还没开 —— 说明怎么开

第三种才是唯一需要动配置的，而且只改 forward 这一个字段，其余原样保留。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from . import llm as llmcfg


def status() -> dict:
    fw = llmcfg.ofm_forward()
    models = llmcfg.ofm_models()
    best = llmcfg.ofm_best_model()

    if fw["listening"] and fw["has_key"]:
        state = "READY"
        action = "无需操作，直接用：kauto run <slug>"
    elif fw["configured"]:
        state = "NEED_RESTART"
        action = ("重启 DSH 让网关监听（设置已开）：重启后 18899 会自动起来，"
                  "forwardKey 也会由插件生成")
    else:
        state = "NEED_ENABLE"
        action = "执行：kauto link --enable（只改 forward 字段，并在重启 DSH 后生效）"

    return {
        "state": state,
        "gateway": {"base": fw["base"], "port": fw["port"],
                    "configured": fw["configured"], "listening": fw["listening"],
                    "has_key": fw["has_key"]},
        "models_available": len(models),
        "suggested_model": best,
        "action": action,
    }


def enable() -> dict:
    """把 forward.enabled 置 true。只碰这一个字段，先备份再写，写完回读比对。"""
    path = llmcfg.OFM_SETTINGS
    if not path.exists():
        raise FileNotFoundError(f"找不到 our-free-model 设置：{path}")

    raw = path.read_text(encoding="utf-8")
    before = json.loads(raw)
    fwd_before = dict(before.get("forward") or {})

    if fwd_before.get("enabled") is True:
        return {"changed": False, "reason": "已经是开启状态", "forward": fwd_before}

    backup = path.with_suffix(f".json.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)

    after = json.loads(raw)
    after["forward"] = {
        **fwd_before,
        "enabled": True,
        "host": fwd_before.get("host") or "127.0.0.1",
        "port": int(fwd_before.get("port") or 18899),
        "lan": fwd_before.get("lan") or {"enabled": False, "port": 0},
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

    written = json.loads(path.read_text(encoding="utf-8"))
    if written.get("forward", {}).get("enabled") is not True:
        shutil.copy2(backup, path)
        raise RuntimeError(f"写入后回读不一致，已还原备份 {backup.name}")

    other_keys = [k for k in before if k != "forward" and before[k] != written.get(k)]
    return {
        "changed": True,
        "forward": written["forward"],
        "backup": str(backup),
        "other_keys_touched": other_keys,
        "next": "重启 DSH，网关会在 127.0.0.1:18899 上监听",
    }
