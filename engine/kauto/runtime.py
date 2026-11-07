"""本机环境探测与运行时约束。

踩到的坑（实测记录，2026-11）：
- pandas 冷启动导入 ~5s，正常
- LightGBM 在本机 n_jobs>1 时会无限卡住不返回（12 核，200 棵树，>120s 无响应），
  n_jobs=1 时同任务 1.35s 完成 → 全项目统一 THREADS=1，可用 KAUTO_THREADS 覆盖
- 系统代理 127.0.0.1:7890 不定时导致 PyPI 握手超时，pip 需 --proxy ""
"""
from __future__ import annotations

import os

THREADS = max(1, int(os.environ.get("KAUTO_THREADS", "1"))
)
os.environ["OMP_NUM_THREADS"] = str(THREADS)
os.environ.setdefault("KMP_DEVICE_THREAD_LIMIT", str(THREADS))
