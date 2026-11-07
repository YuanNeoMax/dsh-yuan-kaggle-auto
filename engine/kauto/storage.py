"""落盘工具：写 → flush+fsync → 立刻回读校验，不一致就重试，最终失败则抛错。

为什么需要这个：本机实测出现过"文件大小正确但内容全是 0x00"的情况
（work/demo-comp 下 baseline.json / iterate.json / best_fe.py / *.npy 都被清零，
而同一目录里 pandas 写出的 csv、meta.json 和另一批 write_text 写出的 json 完好）。
这类静默损坏如果不管，流水线会拿着一个空预测跑到底，最终 submission 行数正常、
表头正常，但数值全错——比直接崩溃更难发现。所以每个关键产物写完都要回读比对。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class CorruptWrite(RuntimeError):
    pass


def _fsync_dir(path: Path) -> None:
    """尽力而为地刷目录项；Windows 上对只读句柄 fsync 会 EBADF，忽略即可。"""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def write_bytes(path: Path, data: bytes, retries: int = 3) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for attempt in range(retries):
        tmp = p.with_suffix(p.suffix + f".tmp{attempt}")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())   # 数据先真正落盘，再原子替换
        os.replace(tmp, p)
        back = p.read_bytes()
        if back == data:
            return p
        last = CorruptWrite(
            f"回读不一致：{p} 期望 {len(data)} 字节，实际 {len(back)} 字节，"
            f"非零字节 {sum(1 for b in back if b)}"
        )
        time.sleep(0.05)
    raise last or CorruptWrite(f"写入失败：{p}")


def write_text(path: Path, text: str) -> Path:
    return write_bytes(path, text.encode("utf-8"))


def write_json(path: Path, obj) -> Path:
    return write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def read_json(path: Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise CorruptWrite(f"缺文件：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def copy(src: Path, dst: Path) -> Path:
    src, dst = Path(src), Path(dst)
    if not src.exists():
        raise CorruptWrite(f"复制源不存在：{src}")
    return write_bytes(dst, src.read_bytes())


def save_npy(path: Path, arr, expect_varied: bool = True) -> Path:
    """保存 npy 并做两级校验：写盘一致性 + **内容是否退化**。

    第二级是实测踩出来的：曾经出现过 OOF 预测全为 0.0000 的产物，
    而且 save_npy 的回读校验全部通过 —— 因为它只比对"写进去的和读出来的相同"，
    没有检查"内容本身是不是废的"。这种坏产物会一路流到 blend 去拟合权重，
    算出一个看起来正常但毫无意义的分数（实测单模 logloss 7.28 = 全押错）。

    所以这里加一道语义检查：预测数组不该是常数。允许关闭（expect_varied=False），
    用于确实只有单一取值的场景。
    """
    import io

    import numpy as np
    a = np.asarray(arr)
    buf = io.BytesIO()
    np.save(buf, a)
    raw = buf.getvalue()
    for _ in range(3):
        p = write_bytes(path, raw)
        back = np.load(p)
        if back.shape != a.shape:
            raise CorruptWrite(f"npy 形状回读不一致：{p} {back.shape} != {a.shape}")
        if a.size and not np.allclose(back.astype(float), a.astype(float),
                                      equal_nan=True, rtol=0, atol=1e-12):
            raise CorruptWrite(f"npy 内容回读不一致（疑似被清零）：{p}")
        if expect_varied and a.size > 1:
            finite = a.astype(float)
            finite = finite[np.isfinite(finite)]
            if finite.size > 1 and float(np.ptp(finite)) < 1e-9:
                raise CorruptWrite(
                    f"npy 内容退化（全为常数 {float(finite.flat[0])}）：{p} —— "
                    f"预测失效，不应写盘")
        return p
    raise CorruptWrite(f"npy 写入失败：{path}")


def load_npy(path: Path):
    import numpy as np
    p = Path(path)
    if not p.exists():
        raise CorruptWrite(f"缺文件：{p}")
    return np.load(p)
