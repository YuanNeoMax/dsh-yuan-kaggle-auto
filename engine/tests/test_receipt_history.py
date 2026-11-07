"""收据历史会无限膨胀 —— 长期使用必然恶化。

## 实测

`_write_receipt` 每次提交都把完整记录追加到 `submission_receipt_history.json`，
**没有保留上限，也不压缩字段**。实测规模：

| 提交次数 | 文件大小 | 说明 |
|---|---|---|
| 100 | 238 KB | 每条约 2439 字节 |
| 1000 | 2.3 MB | 而且**每次写都要全量重读+重写** |

膨胀的主因是每条都存 10 行 `rows`（Kaggle 提交表的原样记录），
而 `rows` 在 `submission_receipt.json`（最近一次）里已经有了。

Kaggle 每天限额 10 次，长期用下去这个问题只会恶化。

## 修法

1. 历史条目只留关键字段（去掉 `rows`，保留溯源 `node_id` / `node_score`）
2. 只保留最近 `MAX_HISTORY = 50` 条
3. 主文件 `submission_receipt.json` **仍保留完整 rows** —— 那是"最近一次"的详情

分工：主文件 = 最近一次的完整现场；历史 = 历次的关键数字对比。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import kapi, submit

results = []

BIG_ROWS = [{"fileName": "submission.csv", "date": "d", "status": "complete",
             "publicScore": "0.8", "privateScore": "", "description": "kauto",
             "ref": "123"}] * 10


def _with_tmp(fn):
    tmp = Path(tempfile.mkdtemp())
    orig = kapi.WORK_DIR
    kapi.WORK_DIR = tmp
    try:
        return fn(tmp)
    finally:
        kapi.WORK_DIR = orig
        shutil.rmtree(tmp, ignore_errors=True)


print("=== 1) 100 次提交后文件不应失控 ===")


def _grow(tmp):
    slug = "grow"
    (tmp / slug).mkdir(parents=True)
    for i in range(100):
        submit._write_receipt(slug, {
            "status": "scored", "score": 0.8 + i * 0.001,
            "waited_seconds": 30, "poll_count": 3,
            "submitted_at": f"t{i}", "rows": BIG_ROWS, "reason": None})
    hp = tmp / slug / "submission_receipt_history.json"
    return len(json.loads(hp.read_text(encoding="utf-8"))), hp.stat().st_size


n_hist, size = _with_tmp(_grow)
print(f"   历史条数={n_hist}  大小={size} 字节 ({size/1024:.1f} KB)")
ok1 = n_hist <= 50 and size < 60 * 1024
print(f"   → 条数 ≤50 且 <60KB: {ok1}")
results.append(("历史有上限", ok1))

print("\n=== 2) 保留的是最近 N 条（不是最早的）===")


def _tail(tmp):
    slug = "tail"
    (tmp / slug).mkdir(parents=True)
    for i in range(60):
        submit._write_receipt(slug, {
            "status": "scored", "score": i, "waited_seconds": 1,
            "poll_count": 1, "submitted_at": f"t{i:03d}",
            "rows": BIG_ROWS, "reason": None})
    hp = tmp / slug / "submission_receipt_history.json"
    h = json.loads(hp.read_text(encoding="utf-8"))
    return [e.get("submitted_at") for e in h]


times = _with_tmp(_tail)
print(f"   共 {len(times)} 条，首={times[0]}  末={times[-1]}")
ok2 = times[-1] == "t059" and times[0] != "t000"
print(f"   → 保留最近（末条是最新）: {ok2}")
results.append(("保留最近 N 条", ok2))

print("\n=== 3) 历史条目不含 rows（去冗余）===")


def _slim(tmp):
    slug = "slim"
    (tmp / slug).mkdir(parents=True)
    submit._write_receipt(slug, {
        "status": "scored", "score": 0.9, "waited_seconds": 1,
        "poll_count": 1, "submitted_at": "t", "rows": BIG_ROWS, "reason": None})
    hp = tmp / slug / "submission_receipt_history.json"
    return json.loads(hp.read_text(encoding="utf-8"))[-1]


entry = _with_tmp(_slim)
print(f"   字段: {sorted(entry.keys())}")
ok3 = "rows" not in entry
print(f"   → 历史无 rows: {ok3}")
results.append(("历史去 rows", ok3))

print("\n=== 4) 主文件仍保留完整 rows（最近一次详情）===")


def _main(tmp):
    slug = "main"
    (tmp / slug).mkdir(parents=True)
    submit._write_receipt(slug, {
        "status": "scored", "score": 0.9, "waited_seconds": 1,
        "poll_count": 1, "submitted_at": "t", "rows": BIG_ROWS, "reason": None})
    return json.loads((tmp / slug / "submission_receipt.json").read_text(encoding="utf-8"))


main_rec = _with_tmp(_main)
n_rows = len(main_rec.get("rows") or [])
print(f"   主文件 rows 行数: {n_rows}（期望 10）")
ok4 = n_rows == 10
print(f"   → 主文件保完整: {ok4}")
results.append(("主文件保完整", ok4))

print("\n=== 5) 历史条目保留溯源字段 ===")
ok5 = "node_id" in entry and "node_score" in entry
print(f"   含 node_id / node_score: {ok5}")
results.append(("历史留溯源", ok5))

print("\n=== 汇总 ===")
bad = 0
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    bad += 0 if ok else 1
print()
print("全部通过" if bad == 0 else f"{bad} 项未通过")
raise SystemExit(1 if bad else 0)
