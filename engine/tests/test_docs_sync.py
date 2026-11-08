"""文档与代码一致性：README 提到的工具必须真的存在，反之亦然。

## 为什么要这个测试

这类漂移只有两种发现方式：有人读文档时发现不对，或者没人发现。
本项目已经踩过一次：`kaggle_rules` / `kaggle_leaderboard` 加进代码之后
README 没有同步，导致"文档没写的能力等于不存在"——用户不会去用一个
他没读到的工具。

## 判据是双向的

- **代码有、文档没写** → FAIL（能力被藏起来了）
- **文档写了、代码没有** → FAIL（承诺兑现不了）

双向检查比单向更值得做，因为后者是更糟的一种失败：用户按文档调用，
拿到的是"没有这个工具"。

## 只查 README 提到的 `kaggle_*` 名字

不解析表格结构、不依赖排版 —— 名字出现在 README 的任何位置都算"写到了"。
这样文档可以自由调整格式，测试不会因为改排版而误报。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

results = []


def mark(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail}  )" if detail else ""))


PKG = ROOT.parent          # 包根（engine/ 的上一级）
TOOLS_TS = PKG / "src" / "tools.ts"
README = PKG / "README.md"
ARCH = PKG / "docs" / "ARCH.md"
CHANGELOG = PKG / "CHANGELOG.md"

print("=== 1) 从源码取实际注册的工具名 ===")
tools_src = TOOLS_TS.read_text(encoding="utf-8")
registered = sorted(set(re.findall(r"name: '(kaggle_[a-z_]+)'", tools_src)))
print(f"   {len(registered)} 个: {', '.join(registered)}")
mark("解析到工具名", len(registered) >= 20)
mark("每个名字都是 kaggle_ 前缀", all(n.startswith("kaggle_") for n in registered))

print("\n=== 2) README 必须提到每一个 ===")
readme_text = README.read_text(encoding="utf-8") if README.exists() else ""
missing_in_readme = [n for n in registered if n not in readme_text]
print(f"   缺失: {missing_in_readme or '无'}")
mark("README 覆盖全部工具", not missing_in_readme, ", ".join(missing_in_readme))

print("\n=== 3) README 不能提到不存在的工具 ===")
claimed = sorted(set(re.findall(r"kaggle_[a-z_]+", readme_text)))
phantom = [n for n in claimed if n not in registered]
print(f"   文档声称: {len(claimed)} 个；不存在的: {phantom or '无'}")
mark("README 无幽灵工具", not phantom, ", ".join(phantom))

print("\n=== 4) helpPayload 的索引必须覆盖核心工具 ===")
# kaggle_help 是模型发现自己有哪些能力的入口；索引漏项 = 能力不可发现。
help_index = tools_src[tools_src.find("export function helpPayload"):]
CORE = [
    "kaggle_doctor", "kaggle_setup", "kaggle_info", "kaggle_prepare",
    "kaggle_baseline", "kaggle_iterate", "kaggle_write_sub", "kaggle_validate",
    "kaggle_submit", "kaggle_receipt", "kaggle_run", "kaggle_rules",
    "kaggle_leaderboard",
]
absent = [name for name in CORE if name not in help_index]
mark("helpPayload 覆盖核心工具", not absent, ", ".join(absent))

print("\n=== 5) CHANGELOG 提到本轮新增的能力 ===")
changelog = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else ""
for feature in ("kaggle_rules", "kaggle_leaderboard"):
    mark(f"CHANGELOG 记录 {feature}", feature in changelog)

print("\n=== 6) ARCH.md 记录了引擎侧改动清单 ===")
arch = ARCH.read_text(encoding="utf-8") if ARCH.exists() else ""
mark("ARCH.md 存在", bool(arch))
if arch:
    mark("有引擎侧改动清单", "引擎侧改动清单" in arch)
    # 每一处引擎改动都要有，否则下一个人会重新踩。
    # 这份清单此前只记到 6 处，实际已有 11 处 —— 文档落后于代码正是
    # 这个测试要抓的东西，所以把"该出现的标记"补全。
    for key in ("KAUTO_ROOT", "KAUTO_SKILL_ROOT", "try_or_none", "format json",
                "work_dir", "leaderboard", "smoke", "缓存", "deadline"):
        mark(f"ARCH 记录 {key}", key in arch)

print("\n=== 7) ARCH.md 记录了最贵的教训（四类静默失败）===")
if arch:
    # 这几轮修掉的缺陷大多同类，不写下来下一个人会重犯。
    mark("有静默失败章节", "静默失败" in arch)
    for key in ("rounds.requested", "cached_age_seconds", "work_dir_relocated_from"):
        mark(f"ARCH 举了 {key} 作例子", key in arch)
    # 四类失效机制必须都在：认错了类，就会用错检测方法。
    for kind in ("配置读不到", "返回值少说了什么", "一套信息两套视图", "环境差异"):
        mark(f"ARCH 记录失效机制：{kind}", kind in arch)
    # 每类的检测手段都要点名，否则清单只是"知道有坑"而非"知道怎么查"。
    for detector in ("test_tool_cli_parity", "install-shape", "shell-quoting", "pack-contents"):
        mark(f"ARCH 指出检测手段：{detector}", detector in arch)

print("\n=== 8) ARCH.md 记录了新增的坑 ===")
if arch:
    for key in ("meta.json", "面板与工具的代码路径是两条", "here-string"):
        mark(f"ARCH 记录坑：{key}", key in arch)

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
for n, ok, _ in results:
    if not ok:
        print(f"  FAIL {n}")
print("全部通过" if not bad else f"{len(bad)} 项未通过")
raise SystemExit(1 if bad else 0)

