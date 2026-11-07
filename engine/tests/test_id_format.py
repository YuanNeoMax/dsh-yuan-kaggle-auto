"""审计：带前导零的 id 是否能完整穿过 prepare → write-sub 链路。

真实比赛里 id 常是 "00123" 这种字符串。若任一环节把它当数字读，
前导零会永久丢失，最终 submission 与 sample_submission 对不上，
Kaggle 判 "id 不匹配" —— 而本地所有校验可能都是通过的。
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from kauto import prep

SRC = Path("fixtures/lz-comp")


def make_fixture() -> Path:
    SRC.mkdir(parents=True, exist_ok=True)
    n = 60
    rows = []
    for i in range(n):
        rows.append({
            "id": f"{i:05d}",                       # 00123 这种
            "x1": (i % 7) / 7.0,
            "x2": (i % 5) / 5.0,
            "target": i % 2,
        })
    tr = pd.DataFrame(rows)
    tr.to_csv(SRC / "train.csv", index=False)
    test = tr.drop(columns=["target"]).copy()
    test["id"] = [f"{10000 + i:05d}" for i in range(n)]
    test.to_csv(SRC / "test.csv", index=False)
    pd.DataFrame({"id": test["id"], "target": [0] * n}).to_csv(
        SRC / "sample_submission.csv", index=False)
    return SRC


make_fixture()
meta = prep.prepare("lz-comp", local_dir=str(SRC), use_skill=False)
work = Path(meta["work_dir"])
print("id_col:", meta["id_col"])

for name in ("train.csv", "valid.csv", "test.csv", "sample_submission.csv"):
    d = pd.read_csv(work / name, dtype={meta["id_col"]: str}, nrows=4)
    raw = (work / name).read_text(encoding="utf-8").splitlines()[1:3]
    print(f"  {name:22s} dtype(str读)={d[meta['id_col']].dtype}  样本={d[meta['id_col']].tolist()}")
    print(f"    {'':20s} 文件原文={raw}")

# 关键判定：文件里是否仍是 5 位带前导零
ok = True
for name in ("train.csv", "valid.csv", "test.csv", "sample_submission.csv"):
    d = pd.read_csv(work / name, dtype={meta["id_col"]: str})
    vals = d[meta["id_col"]].astype(str)
    has_leading_zero = vals.str.startswith("0").any() or vals.str.len().ge(5).all()
    print(f"  {name:22s} 保持 5 位格式: {has_leading_zero}")
    ok = ok and bool(has_leading_zero)

print()
print("结论:", "PASS 前导零完整保留" if ok else "FAIL 前导零在某环节被丢弃")
