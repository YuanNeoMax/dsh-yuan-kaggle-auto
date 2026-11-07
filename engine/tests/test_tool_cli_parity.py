"""工具层与引擎 CLI 的参数一致性。

## 为什么需要这个测试

工具层做的是"把模型的参数翻译成引擎 argv"。这个翻译有两类静默失败：

**多传**（工具传了引擎不认的选项）→ argparse 直接报 `unrecognized arguments`，
好在会立刻失败，用户能看到。

**少传**（引擎支持、工具没暴露）→ **完全没有提示**。用户看到工具描述里没有
那个参数，就以为引擎不支持，于是永远走那条更贵的路径。

本轮实测就抓到两例：

- `kaggle_oof` 没有 `--node`。引擎的 `oof` 不带 `--node` 时**遍历所有归档
  节点**，每个都是一次完整 K 折重训。而"只补某一个节点"才是常见需求。
- `kaggle_blend` 没有 `--top-k`。引擎一直支持，默认 6，工具从没传过。

这类缺陷无法靠运行错误发现，只能靠静态核对 —— 所以把它变成测试。
"""

import re
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
PKG = ENGINE.parent
CLI = ENGINE / "kauto" / "cli.py"
TOOLS = PKG / "src" / "tools.ts"

results = []


def mark(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail})" if detail else ""))


if not TOOLS.exists():
    print("SKIP  src/tools.ts 不在（只有插件包装时有）")
    raise SystemExit(77)

cli_src = CLI.read_text(encoding="utf-8")
tools_src = TOOLS.read_text(encoding="utf-8")

# ── 1. 收集引擎每个子命令支持的选项 ───────────────────────────────────────
print("=== 1) 解析引擎子命令与它们的选项 ===")
# argparse 的结构：`p = sub.add_parser("name" ...)` 之后紧跟若干 add_argument。
# 循环注册（prepare/baseline）单独处理。
engine: dict[str, set[str]] = {}

lines = cli_src.splitlines()
i = 0
while i < len(lines):
    line = lines[i]
    m = re.search(r'sub\.add_parser\(\s*"([a-z-]+)"', line)
    if m:
        name = m.group(1)
        opts: set[str] = set()
        i += 1
        # 一直吃到下一个 add_parser / set_defaults
        while i < len(lines) and "add_parser" not in lines[i]:
            for opt in re.findall(r'add_argument\(\s*"(--[a-z-]+)"', lines[i]):
                opts.add(opt)
            if "set_defaults" in lines[i]:
                i += 1
                break
            i += 1
        engine[name] = opts
        continue
    i += 1

# 循环注册：for name, fn, h in [("prepare", ...), ("baseline", ...)]
for block in re.finditer(
        r'for name, fn, h in \[(.*?)\]:\s*\n(.*?)(?=\n    \w|\Z)',
        cli_src, re.S):
    names = re.findall(r'\("([a-z-]+)",', block.group(1))
    body = block.group(2)
    opts = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', body))
    for n in names:
        engine[n] = opts

print(f"   引擎命令 {len(engine)} 个: {', '.join(sorted(engine))}")
mark("解析到引擎命令", len(engine) >= 18, str(len(engine)))

# ── 2. 收集工具层对每个命令传的选项 ───────────────────────────────────────
print("\n=== 2) 解析工具层实际会传的选项 ===")
# 形态：const argv = ['cmd', ...]  或  invoke(deps, ['cmd', ...])
# 然后同函数体里若干 argv.push('--opt', ...)
tool_opts: dict[str, set[str]] = {}
for m in re.finditer(r"const argv\s*=\s*\[\s*'([a-z-]+)'", tools_src):
    cmd = m.group(1)
    # 取该命令所在 tool 定义到下一个 make({ 之间的片段
    start = m.start()
    nxt = tools_src.find("  make({", start)
    body = tools_src[start:] if nxt < 0 else tools_src[start:nxt]
    opts = set(re.findall(r"argv\.push\('(--[a-z-]+)'", body))
    tool_opts.setdefault(cmd, set()).update(opts)

# 单行形态：invoke(deps, ['cmd', ...], ...) 且没有 argv 变量
for m in re.finditer(r"invoke\(deps,\s*\['([a-z-]+)'", tools_src):
    tool_opts.setdefault(m.group(1), set())

print(f"   工具调用 {len(tool_opts)} 个命令: {', '.join(sorted(tool_opts))}")
mark("工具调用覆盖 20+ 命令", len(tool_opts) >= 20, str(len(tool_opts)))

# ── 3. 多传：工具传了引擎不认的选项 ───────────────────────────────────────
print("\n=== 3) 工具不能传引擎不认的选项（会 unrecognized arguments）===")
extra_found = []
for cmd, opts in sorted(tool_opts.items()):
    if cmd not in engine:
        extra_found.append(f"{cmd}(整个命令不存在)")
        continue
    unknown = sorted(opts - engine[cmd])
    if unknown:
        extra_found.append(f"{cmd}: {unknown}")
print(f"   多传：{extra_found or '无'}")
mark("没有多传的选项", not extra_found, "; ".join(extra_found))

# ── 4. 少传：引擎支持但工具没暴露的关键选项 ───────────────────────────────
print("\n=== 4) 引擎支持、且值得暴露的选项不能漏 ===")
# 只检查"漏了会导致走更贵路径或无法控制行为"的选项。
# 不追求全暴露：--timeout / --poll-interval 这类有合理默认值的可以不给。
CRITICAL = {
    "oof": ["--node"],          # 不带它 → 遍历所有归档节点，每个都是完整重训
    "blend": ["--top-k"],       # 不带它 → 候选上限固定 6，无法调
    "archive": ["--backfill"],  # 引擎归档靠 --backfill 遍历 gen_*，没有 --node
    "prune": ["--dry-run"],     # 不带它 → 直接删
}
for cmd, must in CRITICAL.items():
    have = tool_opts.get(cmd, set())
    missing = [opt for opt in must if opt not in have]
    mark(f"{cmd} 暴露了关键选项", not missing,
         f"缺 {missing}" if missing else ", ".join(must))

# ── 5. 危险选项的默认方向 ─────────────────────────────────────────────────
print("\n=== 5) 破坏性/花钱的操作默认必须是安全侧 ===")
# prune：dry-run 是默认，只有显式 dryRun=false 才真删
prune_body = tools_src[tools_src.find("name: 'kaggle_prune'"):]
prune_body = prune_body[:prune_body.find("make({")] if "make({" in prune_body else prune_body
mark("prune 默认 dry-run",
     "args.dryRun !== false" in prune_body or "dryRun !== false" in prune_body,
     "默认逻辑")
# submit：必须经过 submitGate
submit_body = tools_src[tools_src.find("name: 'kaggle_submit'"):]
submit_body = submit_body[:submit_body.find("make({")] if "make({" in submit_body else submit_body
mark("submit 经过 submitGate", "submitGate(" in submit_body)

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
for n, ok, _ in results:
    if not ok:
        print(f"  FAIL {n}")
print("全部通过" if not bad else f"{len(bad)} 项未通过")
raise SystemExit(1 if bad else 0)
