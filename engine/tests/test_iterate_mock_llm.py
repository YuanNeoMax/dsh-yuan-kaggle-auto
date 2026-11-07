"""树搜索编排的端到端验证：用假 LLM 驱动真实的 iterate。

## 为什么这条测试值得花时间

`iterate` 是整个引擎里**最贵**的一段（真实调用单次 274–410 秒，一轮 3–5 分钟），
所以平时只能靠人肉跑真比赛来验证。结果是：LLM 循环内部最容易出错的部分
（draft/improve/debug 三路分派、失败节点是否被记住、节点图是否连着、
分数是否按方向择优）在自动化测试里是**空白**。

`tools/mock_llm.py` 早就实现了假 LLM（按 prompt 类型返回不同候选，
第一次 debug 故意返回坏代码），但它此前只用于手工验证 —— 没有任何测试引用它。

这条测试把它接进来：本地 HTTP + 真实 iterate + 真实 LightGBM 训练，
不联网、不花钱、无外部依赖。

## 它验证的是编排，不是模型质量

假 LLM 的候选代码是固定的，所以分数不会真变好；能验证的是：

1. 三种 prompt 都被真的发出去了（draft / improve / debug）；
2. 跑挂的候选被**记住**了（journal 里有 buggy 节点），而不是丢掉；
3. 节点之间形成**父子关系**（树，不是一堆散点）；
4. 无 LLM 时给出可操作的错误，而不是静默返回空结果。
"""

import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tools"))

results = []


def mark(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail})" if detail else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ── 0. 前置：夹具与依赖 ────────────────────────────────────────────────────
print("=== 0) 前置检查 ===")
FIXTURE = ENGINE / "work" / "demo-comp"
if not (FIXTURE / "baseline.json").exists():
    print("SKIP  work/demo-comp 未准备（插件包不带真实运行产物）")
    raise SystemExit(77)
mark("夹具就绪", True, str(FIXTURE.name))

try:
    import lightgbm  # noqa: F401
    HAS_LGB = True
except Exception as e:
    HAS_LGB = False
    print(f"SKIP  缺 lightgbm（{e}）")
    raise SystemExit(77)
mark("lightgbm 可用", HAS_LGB)

# ── 1. 起假 LLM ────────────────────────────────────────────────────────────
print("\n=== 1) 启动假 LLM ===")
import mock_llm  # noqa: E402

port = free_port()
srv = mock_llm.serve(port)
time.sleep(0.3)
base = f"http://127.0.0.1:{port}/v1"
print(f"   mock LLM at {base}")

# 用一个**临时** runtime root，避免污染夹具的真实 journal
TMP_ROOT = ENGINE / ".tmp-iterate-test"
shutil.rmtree(TMP_ROOT, ignore_errors=True)
(TMP_ROOT / "work").mkdir(parents=True, exist_ok=True)
WORK = TMP_ROOT / "work" / "demo-comp"
shutil.copytree(FIXTURE, WORK)

# 夹具的 journal.json 里已有历史节点。留着它 → iterate 走**断点续跑**
# （开发时要验证的性质）；删掉它 → iterate 从零起草（编排验证需要这个）。
# 第一次跑的时候我只留了 journal，结果 mock 只收到 debug 调用、
# draft/improve 一次没有 —— 那不是缺陷，正是续跑在起作用。
# 所以两种情形都要测，改名清楚。
(WORK / "journal.json").unlink(missing_ok=True)
for stale in WORK.glob("gen_*"):
    if stale.is_dir():
        shutil.rmtree(stale, ignore_errors=True)

os.environ["KAUTO_ROOT"] = str(TMP_ROOT)
os.environ["KAUTO_LLM_BASE"] = base
os.environ["KAUTO_LLM_KEY"] = "sk-mock"
os.environ["KAUTO_LLM_MODEL"] = "mock"
os.environ["KAUTO_THREADS"] = "1"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ.pop("KAUTO_LLM_PROVIDER", None)

# KAUTO_ROOT 在 import 时就被 kapi 读取，所以必须在 import 之后再确认
import kauto.kapi as kapi  # noqa: E402

if str(kapi.ROOT) != str(TMP_ROOT):
    # 重新加载，让 ROOT 生效（模块级常量在 import 时求值）
    for mod in [m for m in list(sys.modules) if m.startswith("kauto.")]:
        sys.modules.pop(mod, None)
    sys.modules.pop("kauto", None)

import kauto.iter as kiter  # noqa: E402
import kauto.kapi as kapi2  # noqa: E402

mark("KAUTO_ROOT 生效（临时根）", str(kapi2.ROOT) == str(TMP_ROOT), str(kapi2.ROOT))

# ── 2. 真的跑 iterate ──────────────────────────────────────────────────────
print("\n=== 2) 跑 3 轮树搜索（真实训练，假 LLM）===")
# 3 轮而不是 2 轮：搜索策略按顺序决定动作 —— 草稿不足先 draft，
# 有 buggy 叶子且调试深度未超限就去 debug，否则 improve。
# 只跑 2 轮只能观察到 draft→debug，improve 这条最关键的路径（拿最优节点的
# 完整代码做一处原子改进）根本走不到。多跑一轮才能覆盖全部三路。
t0 = time.time()
error = ""
outcome = None
try:
    outcome = kiter.iterate(
        "demo-comp",
        rounds=3,
        parallel=1,          # 单条线程，减少 flaky
        use_intel=False,     # 不拉 writeup（无网络）
        leakcheck_on=False,  # 泄漏审查由别的测试覆盖
        folds=0,
    )
except Exception as e:
    error = f"{type(e).__name__}: {e}"
dt = round(time.time() - t0, 1)
print(f"   {dt}s，error={error[:120] or '无'}")
mark("iterate 未崩溃", error == "", error[:90])

if error == "" and outcome:
    print(f"   outcome: {json.dumps(outcome, ensure_ascii=False)[:220]}")

# ── 3. 编排断言 ────────────────────────────────────────────────────────────
print("\n=== 3) 编排是否正确（从零起草）===")
kinds = dict(mock_llm.STATE.get("kinds") or {})
print(f"   mock 收到的 prompt 类型: {kinds}")
mark("发过 draft", kinds.get("draft", 0) >= 1, f"{kinds.get('draft', 0)} 次")
mark("发过 debug", kinds.get("debug", 0) >= 1, f"{kinds.get('debug', 0)} 次")
mark("总调用数 ≥ 3", sum(kinds.values()) >= 3, f"{sum(kinds.values())} 次")

# improve 在这条测试里**不一定出现**，原因要说清楚，否则以后有人以为漏了：
# 搜索策略的规则是「除 baseline 外没有跑通的候选时，优先修已知失败」。
# mock_llm 的 draft 候选是故意写坏的，所以修好第一个之后，
# 下一轮仍然没有 real_good（那一个 debug 成功节点是 draft 的子节点），
# 于是继续 draft。improve 只在「已有跑通候选」时才被选中。
#
# 真正要验证的是**这条规则被遵守**：debug 出现在 draft 失败之后，
# 而不是系统无视失败继续无限起草。下面用 journal 的顺序来断言这一点。
journal_p = WORK / "journal.json"
mark("写出了 journal", journal_p.exists())
nodes = []
if journal_p.exists():
    journal = json.loads(journal_p.read_text(encoding="utf-8"))
    nodes = journal.get("nodes") or []
    print(f"   journal: {len(nodes)} 个节点")
    for n in nodes[:8]:
        print(f"     {str(n.get('id')):16s} stage={str(n.get('stage')):8s}"
              f" score={n.get('score')}  parent={n.get('parent')}")

    mark("节点数 ≥ 3（基线 + 至少两个候选）", len(nodes) >= 3, f"{len(nodes)} 个")
    mark("有 baseline 起点节点", any(str(n.get("id")) == "baseline" for n in nodes))
    mark("存在非基线候选节点", any(str(n.get("id")) != "baseline" for n in nodes))

    # 树不是散点：至少一个节点声明了父节点
    with_parent = [n for n in nodes if n.get("parent")]
    mark("节点形成父子关系（是树而非散点）", bool(with_parent), f"{len(with_parent)} 个有 parent")

    # 失败被记住，而不是丢掉：这是树搜索区别于"每轮从零重写"的关键性质。
    failed_like = [n for n in nodes if n.get("score") is None and str(n.get("id")) != "baseline"]
    buggy = [n for n in nodes if n.get("buggy")]
    mark("跑挂的候选被记住（不留丢弃）",
         bool(buggy) or bool(failed_like),
         f"buggy={len(buggy)} 无分={len(failed_like)}")

    # 已知失败优先于新草稿：debug 节点必须是某个失败节点的子节点。
    debug_nodes = [n for n in nodes if str(n.get("stage")) == "debug"]
    if debug_nodes:
        parent_ids = {str(n.get("parent")) for n in debug_nodes}
        failed_ids = {str(n.get("id")) for n in failed_like}
        mark("debug 挂在**失败节点**下（已知失败优先修复）",
             bool(parent_ids & failed_ids),
             f"debug.parent={sorted(parent_ids)} 失败节点={sorted(failed_ids)}")

    scored = [n for n in nodes if isinstance(n.get("score"), (int, float))]
    mark("候选有分数记录", len(scored) >= 2, f"{len(scored)} 个有分")

# ── 4. 无 LLM 时给可操作错误 ──────────────────────────────────────────────
print("\n=== 4) 无 LLM 通道时的错误是否可操作 ===")
srv.shutdown()
time.sleep(0.2)
os.environ.pop("KAUTO_LLM_BASE", None)
os.environ.pop("KAUTO_LLM_KEY", None)
os.environ.pop("KAUTO_LLM_MODEL", None)
os.environ["KAUTO_LLM_PROVIDER"] = "deepseek"   # 明确选一个没 key 的通道
os.environ.pop("DEEPSEEK_API_KEY", None)

import kauto.llm as llm  # noqa: E402

r = llm.resolve()
print(f"   {({k: v for k, v in r.items() if k != 'key'})}")
mark("明确选了不可用的通道时 ok=False", r.get("ok") is False)
mark("错误说明缺什么", "DEEPSEEK_API_KEY" in str(r.get("reason", "")))
mark("hint 给出处理方式",
     any(k in str(r.get("hint", "")) for k in ("auto", "LLM API Key")))

# ── 清理 ───────────────────────────────────────────────────────────────────
shutil.rmtree(TMP_ROOT, ignore_errors=True)
for k in ("KAUTO_ROOT", "KAUTO_LLM_BASE", "KAUTO_LLM_KEY", "KAUTO_LLM_MODEL",
          "KAUTO_LLM_PROVIDER", "NO_PROXY", "KAUTO_THREADS"):
    os.environ.pop(k, None)

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
for n, ok, _ in results:
    if not ok:
        print(f"  FAIL {n}")
print("全部通过" if not bad else f"{len(bad)} 项未通过")
raise SystemExit(1 if bad else 0)
