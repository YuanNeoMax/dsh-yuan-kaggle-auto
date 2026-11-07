"""journal.json 里重复存了一份已归档节点的代码。

## 实测

| 项 | 值 |
|---|---|
| 每节点 code 平均 | **6612 字节** |
| titanic 的 4 节点 journal | 25.8 KB，其中 **21.9 KB 是代码** |
| 100 节点预估 | **646 KB** |
| 200 节点预估 | 1.3 MB |

而 `save()` 每次都全量重写 —— 文件越大，每次迭代的写盘开销越大。

**关键发现**：跑通节点的代码在 `archive/<id>/candidate.py` 里**已经有一份**，
实测 `fa7eba79563a` 在两处的 7702 字符**逐字相同**。

## 但不能一刀切

失败节点**不归档**（只留在 journal 里），它们的代码必须保留：
- `debug` 需要父节点代码来生成修复 prompt
- `one_child` 的"重复代码检测"需要比对父节点代码

所以规则是：**已归档且有同源代码 → 只留引用；否则保留正文**。

## 实测效果

| 项 | 修复前 | 修复后 |
|---|---|---|
| titanic journal | 25.8 KB | **18.1 KB**（↓30%）|
| 重载后代码可读回 | — | **7702 字符，完整** |
| debug 链路 | 正常 | **正常**（draft→debug→debug）|
| nochange 检测 | 正常 | **正常**（命中 2 个）|
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import journal as J

results = []

print("=== 1) 已归档且代码相同的节点，journal 里不留正文 ===")


def _roundtrip(tmp):
    slug = "dedup"
    work = tmp / slug
    work.mkdir(parents=True)
    code = "def build_features(train, valid, test):\n" + "    x = 1\n" * 100
    j = J.Journal(slug, work)
    n = J.Node(id="arch0001", code=code, stage="draft", score=0.8)
    j.nodes = [n]
    # 建一份同源代码的归档
    ad = work / "archive" / n.id
    ad.mkdir(parents=True)
    (ad / "candidate.py").write_text(code, encoding="utf-8")
    j.save()
    raw = json.loads((work / "journal.json").read_text(encoding="utf-8"))
    node_raw = raw["nodes"][0]
    return code, node_raw, work


code, node_raw, work = _roundtrip(Path(tempfile.mkdtemp()))
print(f"   journal 里 code 长度: {len(node_raw.get('code') or '')}（应为 0）")
print(f"   code_in_archive 标记: {node_raw.get('code_in_archive')}")
ok1 = (node_raw.get("code") or "") == "" and node_raw.get("code_in_archive") is True
print(f"   → 不重复存正文: {ok1}")
results.append(("归档节点不重复存", ok1))

print("\n=== 2) 重载后代码必须能读回（否则 debug 失效）===")
j2 = J.Journal("dedup", work)
j2.load()
got = j2.nodes[0].code if j2.nodes else ""
print(f"   重载后 code 长度: {len(got)}（应为 {len(code)}）")
ok2 = got.strip() == code.strip()
print(f"   → 完整读回: {ok2}")
results.append(("重载可读回", ok2))

print("\n=== 3) 未归档节点（失败节点）代码必须保留 ===")


def _unarchived(tmp):
    slug = "keep"
    work = tmp / slug
    work.mkdir(parents=True)
    code = "def broken(): pass\n" * 50
    j = J.Journal(slug, work)
    n = J.Node(id="bug0001", code=code, stage="debug", error="boom")
    j.nodes = [n]
    j.save()          # 故意不建 archive
    raw = json.loads((work / "journal.json").read_text(encoding="utf-8"))
    return code, raw["nodes"][0]


code3, node3 = _unarchived(Path(tempfile.mkdtemp()))
print(f"   失败节点 code 长度: {len(node3.get('code') or '')}（应为 {len(code3)}）")
ok3 = (node3.get("code") or "").strip() == code3.strip()
print(f"   → 保留正文: {ok3}")
results.append(("失败节点保留代码", ok3))

print("\n=== 4) 真实 titanic journal 已瘦身 ===")
w = Path("work/titanic")
jp = w / "journal.json"
if jp.exists():
    d = json.loads(jp.read_text(encoding="utf-8"))
    n_nodes = len(d.get("nodes") or [])
    code_kb = sum(len(n.get("code") or "") for n in d["nodes"]) / 1024
    refs = [n for n in d["nodes"] if n.get("code_in_archive")]
    print(f"   {n_nodes} 节点, 文件 {jp.stat().st_size/1024:.1f} KB, 正文代码 {code_kb:.1f} KB")
    print(f"   用引用的节点: {len(refs)}")
    # 不要断言「至少一个引用」—— journal 内容随每次运行变化，
    # 最新一轮可能刚好没有已归档+代码相同的节点。
    # 真正的判据是：机制本身可用（前 3 项已用构造数据验证），
    # 这里只记录现状，并验证「已归档节点确实没重复存正文」这个一致性。
    import json as _j
    raw = _j.loads(jp.read_text(encoding="utf-8"))
    arch = {p.name for p in (w / "archive").iterdir() if p.is_dir()} if (w / "archive").exists() else set()
    dup = []
    for n in raw.get("nodes") or []:
        if n["id"] in arch and (n.get("code") or "").strip() and not n.get("code_in_archive"):
            dup.append(n["id"])
    print(f"   已归档但仍存正文（不一致）: {len(dup)}")
    ok4 = len(dup) == 0
    print(f"   → 无重复存储: {ok4}")
    results.append(("真实 journal 无重复", ok4))
else:
    print("   (缺 journal，跳过)")
    results.append(("真实 journal 瘦身", None))

print("\n=== 5) 实测：debug 链路不受影响 ===")
jd = Path("work/dirty-comp/journal.json")
if jd.exists():
    d2 = json.loads(jd.read_text(encoding="utf-8"))
    stages = [n.get("stage") for n in d2["nodes"]]
    has_debug = "debug" in stages
    nochange = [n for n in d2["nodes"] if "完全相同" in (n.get("error") or "")]
    print(f"   阶段: {stages}")
    print(f"   nochange 命中: {len(nochange)}")
    ok5 = has_debug and len(nochange) >= 1
    print(f"   → 链路正常: {ok5}")
    results.append(("debug 链路正常", ok5))
else:
    print("   (缺 dirty-comp journal，跳过)")
    results.append(("debug 链路正常", None))

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
