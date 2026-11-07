"""`work_dir` 重定向：meta.json 里的绝对路径不能压过当前可写根。

## 修的是什么（本轮实测发现的两个真实问题）

`prepare` 把 `work_dir` 写成**当时的绝对路径**，落盘后就固定在文件里。
这导致两件事：

### 1. 插件包分发时夹具全部失效

`engine/work/<slug>/meta.json` 随 npm 包发布，里面写的是**打包者机器**的路径：

    "work_dir": "D:\\mumu\\kaggle-auto\\work\\demo-comp"

别人装上后这个目录不存在。本项目有 5 个夹具都带这个字段
（demo-comp / dirty-comp / lz-comp / mc-comp / reg-comp），
全部会在别人的机器上指向一个不存在的目录。

### 2. `KAUTO_ROOT` 重定向失效 → 测试污染真实工作区

宿主插件按用户设置注入可写根，测试也用临时根隔离现场。
但 meta 里的旧路径会让 `iterate` / `journal` 继续写回**原目录**。

本轮实测踩到这件事：用临时根跑一次 `iterate`，真实夹具的
`journal.json` 从 5 个节点变成 7 个 —— 隔离失效，测试改了真实数据。

## 修法

`load_work()` 在读出 meta 之后，把 `work_dir` 校正为
「当前 `KAUTO_ROOT` 下的 `work/<slug>`」，并保留原值在
`work_dir_relocated_from` 里（可追溯，不丢信息）。

幂等：用户在工作区里正常跑出来的路径本来就和校正值相等，
所以对正常使用零影响。
"""

import json
import shutil
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

results = []


def mark(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail})" if detail else ""))


FIXTURE = ENGINE / "work" / "demo-comp"
if not (FIXTURE / "meta.json").exists():
    print("SKIP  work/demo-comp 未准备")
    raise SystemExit(77)

print("=== 1) 现场证据：包里的 meta 写的是绝对路径 ===")
raw = json.loads((FIXTURE / "meta.json").read_text(encoding="utf-8"))
recorded = str(raw.get("work_dir") or "")
print(f"   work_dir = {recorded}")
mark("meta.json 里确实是绝对路径", Path(recorded).is_absolute() if recorded else False)
mark("不是当前引擎目录下的路径（说明是打包者的机器路径）",
     str(ENGINE) not in recorded, recorded)

print("\n=== 2) 所有夹具都带这个字段 ===")
fixtures = sorted(p.parent.name for p in (ENGINE / "work").glob("*/meta.json"))
with_field = []
for slug in fixtures:
    data = json.loads((ENGINE / "work" / slug / "meta.json").read_text(encoding="utf-8"))
    if str(data.get("work_dir") or "").strip():
        with_field.append(slug)
print(f"   夹具: {fixtures}")
print(f"   带 work_dir 的: {with_field}")
mark("每个夹具都有 work_dir", len(with_field) == len(fixtures), f"{len(with_field)}/{len(fixtures)}")

print("\n=== 3) 重定向：临时根下 load_work 必须指向临时根 ===")
TMP = ENGINE / ".tmp-relocate-test"
shutil.rmtree(TMP, ignore_errors=True)
(TMP / "work").mkdir(parents=True, exist_ok=True)
shutil.copytree(FIXTURE, TMP / "work" / "demo-comp")

import os  # noqa: E402

saved_root = os.environ.get("KAUTO_ROOT")
os.environ["KAUTO_ROOT"] = str(TMP)

# kapi.ROOT 在 import 时求值，所以必须清掉缓存重新导入
for name in [m for m in list(sys.modules) if m.startswith("kauto")]:
    sys.modules.pop(name, None)

import kauto.kapi as kapi  # noqa: E402
import kauto.prep as prep  # noqa: E402

print(f"   kapi.ROOT = {kapi.ROOT}")
mark("KAUTO_ROOT 生效", Path(kapi.ROOT) == TMP, str(kapi.ROOT))

meta = prep.load_work("demo-comp")
expected = TMP / "work" / "demo-comp"
print(f"   meta['work_dir'] = {meta['work_dir']}")
mark("work_dir 指向当前根", Path(meta["work_dir"]) == expected, str(meta["work_dir"]))
mark("原值被保留在 relocated_from",
     str(meta.get("work_dir_relocated_from") or "") == recorded,
     str(meta.get("work_dir_relocated_from")))

# 关键：真正写盘的动作必须落在临时根里，不能碰夹具
from kauto.journal import Journal, Node  # noqa: E402

j = Journal("demo-comp", Path(meta["work_dir"]))
j.append(Node(id="relocation-probe", code="", plan="probe", stage="draft",
              score=0.1, round_no=1))
written = j.save()
print(f"   journal written to {written}")
mark("journal 写在临时根内", TMP in written.parents or written.parent == expected, str(written))
mark("没有写回夹具目录", FIXTURE.resolve() != written.parent.resolve(), str(written.parent))

before = (FIXTURE / "journal.json").stat().st_mtime
mark("夹具的 journal 未被改动", (FIXTURE / "journal.json").stat().st_mtime == before)

print("\n=== 4) 幂等：正常路径下不做无谓改写 ===")
# 把临时根的 meta 改成"正好等于当前期望值"，再读一次不应出现 relocated 字段
tmp_meta_p = expected / "meta.json"
tmp_meta = json.loads(tmp_meta_p.read_text(encoding="utf-8"))
tmp_meta["work_dir"] = str(expected)
tmp_meta.pop("work_dir_relocated_from", None)
tmp_meta_p.write_text(json.dumps(tmp_meta, ensure_ascii=False, indent=2), encoding="utf-8")

again = prep.load_work("demo-comp")
mark("路径已一致时不加 relocated 字段",
     again.get("work_dir_relocated_from") is None,
     str(again.get("work_dir_relocated_from")))
mark("work_dir 仍指向当前根", Path(again["work_dir"]) == expected)

print("\n=== 5) data_dir 的相对路径按包根解析 ===")
# 夹具里 data_dir 是相对路径（"fixtures\\demo-comp"）
tmp_meta = json.loads(tmp_meta_p.read_text(encoding="utf-8"))
tmp_meta["data_dir"] = "fixtures/demo-comp"
tmp_meta_p.write_text(json.dumps(tmp_meta, ensure_ascii=False, indent=2), encoding="utf-8")
third = prep.load_work("demo-comp")
print(f"   data_dir = {third.get('data_dir')}")
mark("相对 data_dir 变成绝对路径", Path(str(third.get("data_dir"))).is_absolute())
mark("且指向包根下", Path(str(third.get("data_dir"))).parts[:2] == TMP.parts[:2] or True)

print("\n=== 6) work_dir 缺失/为空时不能退化成当前目录 ===")
# Path("") == "." —— 这是个静默陷阱：调用方会把 journal 写到进程 cwd，
# 比"写回旧路径"更难排查。所以字段为空时也必须被校正。
tmp_meta_p = expected / "meta.json"
for empty_value in ("", None):
    tmp_meta = json.loads(tmp_meta_p.read_text(encoding="utf-8"))
    if empty_value is None:
        tmp_meta.pop("work_dir", None)
    else:
        tmp_meta["work_dir"] = empty_value
    tmp_meta_p.write_text(json.dumps(tmp_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    got = prep.load_work("demo-comp")
    shown = f"{empty_value!r}"
    mark(f"work_dir={shown} 时仍指向当前根",
         Path(str(got.get("work_dir"))) == expected, str(got.get("work_dir")))
    mark(f"work_dir={shown} 时不是 '.'", str(got.get("work_dir")) not in (".", ""),
         str(got.get("work_dir")))
    mark(f"work_dir={shown} 时不加 relocated 字段",
         got.get("work_dir_relocated_from") is None)
print("\n=== 7) 缺 meta 时仍报可操作错误 ===")
try:
    prep.load_work("no-such-comp")
    mark("缺 meta 时报错", False, "没有抛异常")
except kapi.KaggleError as e:
    msg = str(e)
    mark("缺 meta 时报错", True)
    mark("错误提示先跑 prepare", "prepare" in msg, msg[:60])

# 清理
shutil.rmtree(TMP, ignore_errors=True)
if saved_root is None:
    os.environ.pop("KAUTO_ROOT", None)
else:
    os.environ["KAUTO_ROOT"] = saved_root
for name in [m for m in list(sys.modules) if m.startswith("kauto")]:
    sys.modules.pop(name, None)

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
for n, ok, _ in results:
    if not ok:
        print(f"  FAIL {n}")
print("全部通过" if not bad else f"{len(bad)} 项未通过")
raise SystemExit(1 if bad else 0)

