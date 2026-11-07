"""排行榜缓存：15.7 秒的只读查询不该每次重复等。

## 为什么缓存是必需的而非可选

面板上「查排行」是只读、可重复、无副作用的操作，用户会反复点。
实测单次 **15.7 秒**（走 skill 脚本那条路：一次跨网往返 + 快照落盘）。
反复点就是反复等十几秒 —— 而榜单在几分钟内不可能变。

实测缓存后：**17.59 秒 → 1.81 秒**（约 10 倍）。

## 三条必须钉住的性质

1. **缓存命中要说明**：`cached` + `cached_age_seconds` 随结果返回，
   否则用户会把几分钟前的名次当成刚刚的实时值。
2. **`refresh` 能绕过**：刚提交完必须能看新名次。
3. **缓存坏了不是故障**：读失败一律走网络，缓存只是加速手段。

本测试用桩替换网络路径，不发起真实请求。
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

results = []


def mark(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail})" if detail else ""))


# 临时可写根，避免写进真实工作区
TMP = ENGINE / ".tmp-lb-cache"
shutil.rmtree(TMP, ignore_errors=True)
(TMP / "work").mkdir(parents=True, exist_ok=True)
saved_root = os.environ.get("KAUTO_ROOT")
os.environ["KAUTO_ROOT"] = str(TMP)
os.environ["NO_PROXY"] = "api.kaggle.com,www.kaggle.com,kaggle.com,127.0.0.1,localhost"

for name in [m for m in list(sys.modules) if m == "kauto" or m.startswith("kauto.")]:
    sys.modules.pop(name, None)

import kauto.kapi as kapi  # noqa: E402
import kauto.skillbridge as sb  # noqa: E402

mark("KAUTO_ROOT 生效", Path(kapi.ROOT) == TMP, str(kapi.ROOT))

SLUG = "cache-probe"
CALLS = {"n": 0}


def fake_leaderboard_rows(*a, **k):
    """模拟 CLI 路径成功（返回 CSV）。"""
    CALLS["n"] += 1
    return "Rank,TeamName,Score\n1,alpha,0.99\n2,beta,0.98\n"


def fake_site_api(*a, **k):
    return {"ok": True, "data": {"userRank": 42, "teamCount": 999}}


# 用桩替换：CLI 路径（kapi.run）与站内 API（kapi.site_api）
real_run = kapi.run
real_site = kapi.site_api
kapi.run = fake_leaderboard_rows
kapi.site_api = fake_site_api

try:
    # ── 1. 首次调用走网络，第二次命中缓存 ─────────────────────────────────
    print("\n=== 1) 首次取网络，二次命中缓存 ===")
    first = sb.leaderboard(SLUG, top=5)
    calls_after_first = CALLS["n"]
    print(f"   首次：source={first.get('source')} calls={calls_after_first}")
    mark("首次真的调了网络", calls_after_first == 1)
    mark("首次不标 cached", not first.get("cached"))

    second = sb.leaderboard(SLUG, top=5)
    calls_after_second = CALLS["n"]
    print(f"   二次：cached={second.get('cached')} age={second.get('cached_age_seconds')} calls={calls_after_second}")
    mark("二次没再调网络", calls_after_second == 1, f"calls 仍为 {calls_after_second}")
    mark("二次标了 cached", second.get("cached") is True)
    mark("给出了缓存年龄", isinstance(second.get("cached_age_seconds"), (int, float)))

    # ── 2. refresh 绕过缓存 ───────────────────────────────────────────────
    print("\n=== 2) refresh 能绕过缓存 ===")
    third = sb.leaderboard(SLUG, top=5, refresh=True)
    print(f"   refresh 后 calls={CALLS['n']}")
    mark("refresh 重新取了网络", CALLS["n"] == 2, f"calls={CALLS['n']}")
    mark("refresh 结果不标 cached", not third.get("cached"))

    # ── 3. 缓存文件确实落盘 ───────────────────────────────────────────────
    print("\n=== 3) 缓存文件落盘 ===")
    cache_p = kapi.WORK_DIR / SLUG / "leaderboard_cache.json"
    mark("缓存文件存在", cache_p.exists(), str(cache_p))
    if cache_p.exists():
        record = json.loads(cache_p.read_text(encoding="utf-8"))
        mark("含 fetched_at", isinstance(record.get("fetched_at"), (int, float)))
        mark("含 payload", isinstance(record.get("payload"), dict))

    # ── 4. TTL 过期后重新取 ───────────────────────────────────────────────
    print("\n=== 4) TTL 过期后重新取 ===")
    os.environ["KAUTO_LEADERBOARD_TTL"] = "0"   # 立即过期
    time.sleep(0.05)
    fourth = sb.leaderboard(SLUG, top=5)
    print(f"   TTL=0 时 calls={CALLS['n']}")
    mark("TTL 过期后重新取网络", CALLS["n"] == 3, f"calls={CALLS['n']}")
    mark("过期结果不标 cached", not fourth.get("cached"))
    os.environ.pop("KAUTO_LEADERBOARD_TTL", None)

    # ── 5. 默认 TTL 是 3 分钟（榜单会变，不能像 writeup 那样 6 小时）────
    print("\n=== 5) 默认 TTL ===")
    ttl = sb._leaderboard_ttl()
    print(f"   默认 TTL = {ttl}s")
    mark("默认 TTL 在合理区间（30s–15min）", 30 <= ttl <= 900, f"{ttl}s")
    os.environ["KAUTO_LEADERBOARD_TTL"] = "abc"
    mark("非法 TTL 回落到默认", sb._leaderboard_ttl() == 180.0, str(sb._leaderboard_ttl()))
    os.environ.pop("KAUTO_LEADERBOARD_TTL", None)
    os.environ["KAUTO_LEADERBOARD_TTL"] = "42"
    mark("TTL 可用环境变量覆盖", sb._leaderboard_ttl() == 42.0, str(sb._leaderboard_ttl()))
    os.environ.pop("KAUTO_LEADERBOARD_TTL", None)

    # ── 6. 缓存坏了不是故障 ───────────────────────────────────────────────
    print("\n=== 6) 缓存损坏时走网络而不是报错 ===")
    cache_p.write_text("{ this is not json", encoding="utf-8")
    before = CALLS["n"]
    try:
        recovered = sb.leaderboard(SLUG, top=5)
        mark("损坏缓存不抛异常", True)
        mark("损坏后重新取网络", CALLS["n"] == before + 1, f"calls={CALLS['n']}")
        mark("结果仍可用", bool(recovered.get("rows") or recovered.get("source")))
    except Exception as e:
        mark("损坏缓存不抛异常", False, f"{type(e).__name__}: {e}")

    print("\n=== 7) 缓存写入失败不致命 ===")
    # 把缓存目标目录变成一个文件，让写盘必然失败
    bad_slug = "cache-bad"
    bad_dir = kapi.WORK_DIR / bad_slug
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "leaderboard_cache.json").mkdir(exist_ok=True)   # 目录占位 → 写入失败
    try:
        got = sb.leaderboard(bad_slug, top=3)
        mark("写缓存失败仍返回结果", bool(got.get("source")), str(got.get("source")))
    except Exception as e:
        mark("写缓存失败仍返回结果", False, f"{type(e).__name__}: {e}")

finally:
    kapi.run = real_run
    kapi.site_api = real_site
    shutil.rmtree(TMP, ignore_errors=True)
    if saved_root is None:
        os.environ.pop("KAUTO_ROOT", None)
    else:
        os.environ["KAUTO_ROOT"] = saved_root

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
for n, ok, _ in results:
    if not ok:
        print(f"  FAIL {n}")
print("全部通过" if not bad else f"{len(bad)} 项未通过")
raise SystemExit(1 if bad else 0)
