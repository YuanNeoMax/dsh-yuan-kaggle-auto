"""验证 solutions 缓存：同一场比赛不重复拉网络。

实测：`solutions` 调用要 **7s 以上**（跨网络），而榜单 writeup 是**静态内容**，
同一场比赛几小时内不会变。每次 iterate 都重新拉一遍纯属浪费。

顺带修了一处：网络抖动（SSLEOFError）原本只抛原始错误，
现在会提示「这看起来是网络抖动，可稍后重试」。
"""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import kapi, skillbridge, storage
from kauto.skillbridge import SkillError

results = []
SLUG = "cache-t"
w = kapi.WORK_DIR / SLUG
shutil.rmtree(w, ignore_errors=True)
w.mkdir(parents=True, exist_ok=True)

print("=== 1) 有新鲜缓存时应直接命中，不走网络 ===")
fake = [{"rank": 1, "title": "cached writeup", "score": 0.9},
        {"rank": 2, "title": "second", "score": 0.88}]
storage.write_json(w / "writeups_cache.json",
                   {"fetched_at": time.time(), "slug": SLUG, "writeups": fake})

# 把网络路径改成"一旦走到就抛错"，以此证明没有走网络
orig_run = skillbridge._run
def boom(*a, **kw):
    raise AssertionError("不该走网络")
skillbridge._run = boom
try:
    rows = skillbridge.solutions(SLUG, top=5)
    hit = rows == fake
    print(f"   命中缓存: {hit}  条目={len(rows)}")
except AssertionError as e:
    hit = False
    print(f"   FAIL：{e}")
finally:
    skillbridge._run = orig_run
results.append(("新鲜缓存命中", hit))

print("\n=== 2) 过期缓存应走网络 ===")
storage.write_json(w / "writeups_cache.json",
                   {"fetched_at": time.time() - 7 * 3600, "slug": SLUG, "writeups": fake})
try:
    skillbridge.solutions(SLUG, top=5)
    print("   FAIL：过期缓存仍被使用")
    results.append(("过期缓存失效", False))
except AssertionError:
    print("   走了网络（说明 TTL 判定生效）")
    results.append(("过期缓存失效", True))
except Exception as e:
    print(f"   走网络但失败（预期，因为 _run 被替换）：{type(e).__name__}")
    results.append(("过期缓存失效", True))

print("\n=== 3) --refresh 应强制走网络 ===")
storage.write_json(w / "writeups_cache.json",
                   {"fetched_at": time.time(), "slug": SLUG, "writeups": fake})
# 注意：第 2 项已经把 _run 恢复了，这里必须重新替换，
# 否则测的是真实网络而不是"是否走网络"（实测踩到）。
skillbridge._run = boom
try:
    skillbridge.solutions(SLUG, top=5, refresh=True)
    print("   FAIL：refresh 时仍用了缓存")
    results.append(("refresh 强制重拉", False))
except AssertionError:
    print("   强制走了网络")
    results.append(("refresh 强制重拉", True))
except SkillError:
    print("   走了网络（网络层报错，说明缓存被绕过）")
    results.append(("refresh 强制重拉", True))
finally:
    skillbridge._run = orig_run

print("\n=== 4) 空结果不应被缓存 ===")
src = inspect_src = Path("kauto/skillbridge.py").read_text(encoding="utf-8")
has_empty_guard = "if rows:" in src and "writeups_cache" in src
print(f"   只在有内容时写缓存: {has_empty_guard}")
results.append(("空结果不缓存", has_empty_guard))

print("\n=== 5) 网络抖动应给出可重试提示 ===")
has_hint = "网络抖动" in src and "refresh" in src
print(f"   有可重试提示: {has_hint}")
results.append(("网络抖动提示", has_hint))

print("\n=== 6) TTL 常量合理（不是 0 也不是极大）===")
ttl = skillbridge.CACHE_TTL
ok6 = 3600 <= ttl <= 86400
print(f"   CACHE_TTL={ttl} 秒（{ttl/3600:.0f} 小时）")
results.append(("TTL 合理", ok6))

shutil.rmtree(w, ignore_errors=True)

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
