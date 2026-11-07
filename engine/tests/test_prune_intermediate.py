"""`prune` 的中间产物识别 + 提示信息准确性。

## 缺陷一：中间产物目录没有被区分出来

`kauto oof` 写 `gen_oof_<id>`，测试脚本写 `gen_to_*` / `gen_timing_*`。
这些目录**不是候选**（`blend.collect_candidates` 已明确排除），
但它们和普通节点的 `gen_<id>` 混在一起，清理时按同一套规则判定。

实测 titanic：`gen_oof_fa7eba79563a`（31 KB）被归在 removable 里是对的，
但提示语笼统说"清理后可能显著减少候选"，让人不敢删 ——
而实际上中间产物删了**没有任何代价**。

## 缺陷二：提示信息没有说清"代码会不会丢"

失败节点不归档，它们的 `gen_<id>/` 里有 `candidate.py`。
若只笼统说"可清理"，用户会担心代码丢失。

实测结论：**代码不会丢** —— 失败节点的代码保留在 `journal.json` 里
（这正是第 50 轮设计的那条例外）。
"""
import json
import sys
from pathlib import Path

# [插件包适配] 目标缺失时按 SKIP 退出（布局差异，不是引擎缺陷）
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _layout_guard import needs_file, needs_dir  # type: ignore  # noqa: E402
    needs_dir("work/demo-comp", "需要夹具的已准备产物")  # noqa: E402
except SystemExit:
    raise
except Exception:
    pass  # guard 不可用时不阻断原测试

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

results = []

print("=== 1) prune 代码里识别中间产物 ===")
src = Path("kauto/cli.py").read_text(encoding="utf-8")
has_inter = '_INTERMEDIATE = ("oof_", "timing_", "to_", "check", "test")' in src
print(f"   含中间产物前缀定义: {has_inter}")
results.append(("识别中间产物", has_inter))

print("\n=== 2) 提示语分两类说明（不再笼统吓人）===")
has_split = "中间产物" in src and "不参与融合" in src
print(f"   分两类说明: {has_split}")
results.append(("提示分两类", has_split))

print("\n=== 3) 提示语说明代码不丢 ===")
has_keep = "代码在 journal.json 里仍保留" in src
print(f"   说明代码保留: {has_keep}")
results.append(("说明代码保留", has_keep))

print("\n=== 4) 实测：titanic 的 gen_oof_ 被归入可清理 ===")
# 需要真实比赛工作区的中间产物；插件包不带真实运行产物。
if not (Path("work") / "titanic").is_dir():
    print("SKIP  work/titanic 不存在 —— 需要真实比赛的运行产物")
    raise SystemExit(77)

import subprocess
r = subprocess.run([sys.executable, "-m", "kauto.cli", "prune", "titanic"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
out = r.stdout + r.stderr
has_oof = "gen_oof_fa7eba79563a" in out and "可清理" in out
print(f"   gen_oof_ 出现在可清理列表: {has_oof}")
print(f"   输出片段: {[l.strip() for l in out.splitlines() if '中间产物' in l][:1]}")
results.append(("gen_oof 可清理", has_oof))

print("\n=== 5) 失败节点代码确实在 journal 里（不会因 prune 丢失）===")
w = Path("work/titanic")
jp = w / "journal.json"
arch = {p.name for p in (w / "archive").iterdir() if p.is_dir()} if (w / "archive").exists() else set()
if jp.exists():
    d = json.loads(jp.read_text(encoding="utf-8"))
    unarch = [n for n in d["nodes"] if n["id"] not in arch and (n.get("code") or "").strip()]
    print(f"   未归档但有代码的节点: {[(n['id'][:10], len(n['code'])) for n in unarch]}")
    ok5 = all(len(n["code"]) > 100 for n in unarch) if unarch else True
    print(f"   → 代码完整保存在 journal: {ok5}")
    results.append(("失败节点代码不丢", ok5))
else:
    print("   (缺 journal，跳过)")
    results.append(("失败节点代码不丢", None))

print("\n=== 6) dry-run 是默认行为（不删） ===")
has_dry = "args.dry_run" in src and "dry-run" in src
print(f"   默认 dry-run: {has_dry}")
results.append(("默认 dry-run", has_dry))

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
