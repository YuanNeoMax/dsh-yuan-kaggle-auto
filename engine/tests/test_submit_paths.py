"""真实提交链路暴露的三个路径缺陷。

## 三个缺陷都是「路径/环境」类，且都只在真实提交时暴露

| # | 缺陷 | 症状 | 根因 |
|---|---|---|---|
| 1 | 提交文件用了相对路径 | `error: the submission file was not found` | `_run` 把 cwd 设成 `SKILL_ROOT`，相对路径在那边不存在 |
| 2 | skill 脚本找不到 kaggle CLI | `exit 127` / `missing_package: kaggle` | `shutil.which("kaggle")` 在 SKILL_ROOT 下返回 None（venv 的 Scripts 不在 PATH） |
| 3 | 中间产物被当成候选 | `oof_<id>` 与 `<id>` 同时进融合池 | `kauto oof` 写的 `gen_oof_<id>` 被当作独立候选 |

## 为什么前 40 多轮没发现

因为它们全都发生在**真实提交/真实 skill 调用**的路径上：

- `submit` 需要 `--yes`，而 `--yes` 之前只跑过 dry-run
- dry-run 不经过 skill 脚本（直接在 cli 里做自检）
- 中间产物那一条只在补过 OOF 且真跑 blend 时才会显现

## 缺陷 2 最值得记

`exit 127` 的输出**看起来像提交成功了**（前面所有自检都是 PASS，
最后一行是「Confirmed with --yes」），只是退出码不对。
若不是刻意看 exit code，很容易误以为提交完成。
"""
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

print("=== 1) 提交文件必须转绝对路径 ===")
src = Path("kauto/skillbridge.py").read_text(encoding="utf-8")
resolves = "sub_file = Path(sub_file).resolve()" in src
print(f"   对 sub_file 做 resolve: {resolves}")
results.append(("sub_file 转绝对路径", resolves))

print("\n=== 2) sample 也必须转绝对路径 ===")
sample_resolves = 'Path(sample).resolve()' in src
print(f"   sample 也 resolve: {sample_resolves}")
results.append(("sample 转绝对路径", sample_resolves))

print("\n=== 3) skill 子进程必须能找到 kaggle CLI ===")
has_path = "PATH" in src and "_bin = Path(sys.executable).parent" in src
print(f"   注入 venv Scripts 到 PATH: {has_path}")
results.append(("PATH 注入", has_path))

print("\n=== 4) 实测：注入后 which kaggle 能找到 ===")

import os
import subprocess
from kauto import skillbridge

# 插件包不携带 vendor/kaggle-skill：这一段依赖它存在，缺失时 SKIP 而非 FAIL。
if not skillbridge.SKILL_ROOT.exists():
    print(f"SKIP  skill 桥接未安装（{skillbridge.SKILL_ROOT}）")
    raise SystemExit(77)
_bin = Path(sys.executable).parent
env = {**os.environ, "PATH": f"{_bin}{os.pathsep}{os.environ.get('PATH','')}"}
r = subprocess.run([sys.executable, "-c", "import shutil;print(shutil.which('kaggle') or 'NONE')"],
                   cwd=str(skillbridge.SKILL_ROOT), capture_output=True, text=True, env=env)
found = "kaggle" in (r.stdout or "").lower()
print(f"   which kaggle: {r.stdout.strip()}")
results.append(("能找到 kaggle CLI", found))

print("\n=== 5) 中间产物目录不得进融合池 ===")
bsrc = Path("kauto/blend.py").read_text(encoding="utf-8")
excludes = ('nid.startswith("oof_")' in bsrc)
print(f"   排除 gen_oof_*: {excludes}")
results.append(("排除中间产物", excludes))

print("\n=== 6) 实测：titanic 融合池不含 oof_ 前缀 ===")
import numpy as np
from kauto import blend as bl
c = bl.collect_candidates("titanic")
names = [x["id"] for x in c]
bad = [n for n in names if n.startswith("oof_")]
print(f"   池: {names}")
print(f"   含 oof_ 前缀: {bad}")
ok6 = len(bad) == 0
results.append(("池内无重复", ok6))

print("\n=== 7) OOF 与 valid 各自配对应标签长度 ===")
# 修复后：不再要求全池长度一致，而是按 kind 配对标签
kind_aware = "_labels_for" in Path("kauto/blend.py").read_text(encoding="utf-8")
print(f"   按 kind 取标签: {kind_aware}")
results.append(("kind 感知标签", kind_aware))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
