"""验证 notebook-only 比赛在早期就被拦住。

实测缺陷：`notebook_only` 只是被记进 meta，**没有任何拦截动作**。
于是这类比赛会正常下载数据、跑完 baseline/iterate/blend/write-sub，
直到 submit 那一刻才发现根本交不上去 —— 前面全白跑。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import cli, kapi

results = []

print("=== 1) prepare 阶段应标记 submittable=False ===")
import inspect
from kauto import prep
src = inspect.getsource(prep.prepare)
has_flag = 'meta["submittable"] = False' in src
has_warn = "notebook_only" in src
has_print = "只能提交 notebook" in src
print(f"   设置 submittable=False: {has_flag}")
print(f"   有显式告警文案: {has_print}")
print(f"   代码里出现 notebook_only: {has_warn}")
results.append(("prepare 标记不可提交", has_flag))
results.append(("prepare 有告警", has_print))

print("\n=== 2) submit 必须拦住 notebook-only ===")
src_submit = inspect.getsource(cli.cmd_submit)
has_block = "notebook_only" in src_submit and "return 3" in src_submit
print(f"   cmd_submit 检查 notebook_only: {has_block}")
results.append(("submit 拦截", has_block))

print("\n=== 3) 实测拦截行为 ===")
# 造一个 notebook-only 的 work 目录
w = kapi.WORK_DIR / "nbo-t"
w.mkdir(parents=True, exist_ok=True)
src_dir = kapi.WORK_DIR / "demo-comp"
for f in ("submission.csv", "sample_submission.csv", "baseline.json",
          "meta.json", "submission_provenance.json"):
    if (src_dir / f).exists():
        shutil.copy(src_dir / f, w / f)
m = json.loads((w / "meta.json").read_text(encoding="utf-8"))
m["notebook_only"] = True
(w / "meta.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

# 直接调 cmd_submit 的等价逻辑：用 argparse 命名空间
class Args:
    slug = "nbo-t"
    yes = True
    message = "t"
    expect = None
    wait = False
    wait_timeout = 60
    poll_interval = 5

rc = cli.cmd_submit(Args())
print(f"   返回码: {rc}（3 表示被拦）")
ok3 = rc == 3
print(f"   → 被拦: {ok3}")
results.append(("实测被拦", ok3))

print("\n=== 4) 普通比赛不应被误拦 ===")
class Args2:
    slug = "demo-comp"
    yes = False      # 不加 --yes，走 dry-run
    message = "t"
    expect = None
    wait = False
    wait_timeout = 60
    poll_interval = 5

rc2 = cli.cmd_submit(Args2())
print(f"   返回码: {rc2}（2 表示 dry-run，未提交）")
ok4 = rc2 == 2
print(f"   → 未被误拦: {ok4}")
results.append(("普通比赛不误拦", ok4))

# 清理
shutil.rmtree(w, ignore_errors=True)

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
