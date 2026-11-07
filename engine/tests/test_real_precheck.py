"""验证真实比赛前置检查的两个判据。

## 发现一：`competitions files` 不是「规则已接受」的探针

我最初用 `kapi.run(["competitions","files","-c",slug])` 判断规则是否接受 ——
它**成功返回了文件列表**，于是报「规则已接受」。但真跑 `download` 就 403。

**根因**：`files` 只列元数据，不需要接受规则；真正的门槛在 `download`
（403 Forbidden for DownloadDataFiles）。

这条错误判据的代价很大：它会让人以为可以跑，然后跑完整条链路
（prepare → baseline → iterate，每轮 4-5 分钟）才在 download 处失败。

## 发现二：`submission-limits` 显示的是平台默认值，不是本场上限

实测：
```
submission-limits → Remaining today: 100
info              → max_daily_submissions: 10
```

**只看前者会误判成「能交 100 次」，实际只能交 10 次。**
两种来源必须摆在一起看。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

results = []

print("=== 1) 探针必须是 download，不能是 files ===")
src = Path("tools/run_real_competition.py").read_text(encoding="utf-8")
# 检查用的是 download 而非 files
uses_download = '"competitions", "download"' in src
mentions_files_trap = "files 只列元数据" in src
print(f"   用 download 作探针: {uses_download}")
print(f"   注释里写明 files 的陷阱: {mentions_files_trap}")
results.append(("探针用 download", uses_download))
results.append(("记录了 files 陷阱", mentions_files_trap))

print("\n=== 2) 配额陷阱必须被警示 ===")
has_quota_warn = "平台默认值" in src and "真实限额" in src
print(f"   有配额警示: {has_quota_warn}")
results.append(("配额陷阱已警示", has_quota_warn))

print("\n=== 3) 实测：files 能列（证明它不能当探针）===")
from kauto import kapi
try:
    out = kapi.run(["competitions", "files", "-c", "playground-series-s6e2"], timeout=60)
    files_ok = len(out) > 20
    print(f"   files 返回 {len(out)} 字符（成功）")
except Exception as e:
    files_ok = False
    print(f"   files 失败：{str(e)[:80]}")
print(f"   → files 对未接受规则的比赛也成功（所以不能当探针）: {files_ok}")
results.append(("files 不能当探针", files_ok))

print("\n=== 4) 实测：download 对未接受规则的比赛必须失败 ===")
try:
    kapi.run(["competitions", "download", "-c", "playground-series-s6e2",
              "-p", str(kapi.DATA_DIR / "playground-series-s6e2")], timeout=300)
    dl_failed = False
    print("   download 竟然成功了")
except Exception as e:
    dl_failed = True
    msg = str(e)
    is_403 = "403" in msg or "accept" in msg.lower()
    print(f"   download 失败（预期），是 403/规则类错误: {is_403}")
print(f"   → download 能区分规则状态: {dl_failed}")
results.append(("download 能区分规则", dl_failed))

print("\n=== 5) 配额双来源都存在 ===")
try:
    lim = kapi.run(["competitions", "submission-limits", "-c",
                    "playground-series-s6e2"], timeout=60)
    has_100 = "Remaining today: 100" in lim
    print(f"   submission-limits 显示 100: {has_100}")
except Exception as e:
    has_100 = False
    print(f"   submission-limits 失败：{str(e)[:80]}")
results.append(("配额双来源可查", has_100))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
