"""README 必须跟上代码：关键能力不能只在代码里存在。

## 为什么需要这个测试

前 19 轮审计往代码里加了一堆能力，但 README 一直停在旧版本 ——
实测时 8 项关键能力一个都没写进去（结构禁写 / AppContainer / Job Object /
提交溯源 / 收据 / 自适应超时 / 数据概览 / 切分指纹）。

对一个工具来说，**用户看到的门面文档过期，比代码有 bug 更影响可用性**：
他会按文档去用，然后发现行为对不上。

所以把"README 必须提到这些能力"变成可执行检查：
每加一个关键模块，就在 KEY_CAPABILITIES 里加一条。
"""
import sys
from pathlib import Path

# [插件包适配] 目标缺失时按 SKIP 退出（布局差异，不是引擎缺陷）
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _layout_guard import needs_file, needs_dir  # type: ignore  # noqa: E402
    needs_file("README.md", "插件包不带用户工作区的 README")  # noqa: E402
except SystemExit:
    raise
except Exception:
    pass  # guard 不可用时不阻断原测试

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

readme = Path("README.md").read_text(encoding="utf-8")

results = []

# 每项： (能力名, README 里必须出现的关键词, 对应模块文件)
KEY_CAPABILITIES = [
    ("结构禁写（valid 无 target）", "结构禁写", "kauto/_runner.py"),
    ("AppContainer 文件隔离", "AppContainer", "kauto/appcontainer.py"),
    ("Job Object 资源限制", "Job Object", "kauto/winjob.py"),
    ("提交溯源", "provenance", "kauto/iter.py"),
    ("提交收据", "receipt", "kauto/cli.py"),
    ("自适应超时", "timebudget", "kauto/timebudget.py"),
    ("数据概览", "dataoverview", "kauto/dataoverview.py"),
    ("切分指纹", "切分指纹", "kauto/prep.py"),
    ("多列输出型提交", "多列输出", "kauto/prep.py"),
    ("notebook-only 早拦", "notebook-only", "kauto/prep.py"),
    ("融合相关性过滤 + 验收门", "相关性", "kauto/blend.py"),
    ("节点归档", "archive", "kauto/archive.py"),
    ("行数契约", "行数契约", "kauto/_runner.py"),
]

print("=== README 覆盖检查 ===")
missing = 0
for name, kw, module in KEY_CAPABILITIES:
    mod_exists = Path(module).exists()
    in_readme = kw.lower() in readme.lower()
    if mod_exists and not in_readme:
        missing += 1
        print(f"  缺   {name:28s}（模块 {module} 存在，但 README 没提「{kw}」）")
    elif in_readme:
        print(f"  OK   {name}")
    else:
        print(f"  --   {name}（模块不存在，跳过）")

print()
print(f"缺失 {missing} 项" if missing else "全部覆盖")
results.append(("README 覆盖关键能力", missing == 0))

print("\n=== 目录清单里的模块数 ===")
import re
listed = re.findall(r"^  ([a-z_]+\.py)\s", readme, re.M)
actual = sorted(p.name for p in Path("kauto").glob("*.py"))
print(f"   README 列出: {len(listed)} 个")
print(f"   实际模块:   {len(actual)} 个")
# README 不一定全列（回想 __init__ 这类），但主要模块应在
main_mods = [m for m in actual if m not in {"__init__.py"}]
not_listed = [m for m in main_mods if m not in listed]
print(f"   未列出的主模块: {not_listed}")
ok2 = len(not_listed) == 0
print(f"   → 主模块全列出: {ok2}")
results.append(("主模块全列出", ok2))

print("\n=== 自测数量声明与实际一致 ===")
m = re.search(r"当前 (\d+) 项", readme)
claimed = int(m.group(1)) if m else None
test_files = len([p for p in Path("tools").glob("test_*.py")])
print(f"   README 声明: {claimed} 项   实际测试文件: {test_files} 个")
if claimed is not None:
    # run_all_tests 里的清单可能多于文件数（有些文件含多项），这里只检查量级不差太多
    ok3 = abs(claimed - test_files) <= 6
    print(f"   → 量级一致: {ok3}")
    results.append(("自测数量声明合理", ok3))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
