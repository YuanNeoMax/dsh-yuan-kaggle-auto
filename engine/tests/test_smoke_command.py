"""离线演练（smoke）：把"能不能跑"与"去不去比赛"分开。

## 补的是什么

插件面板上那个「跑冒烟」按钮，此前只调体检函数 —— 名字叫冒烟，
实际一个建模步骤都没跑。用户点完看到一屏绿灯，仍然不知道这条流水线
在这台机器上到底能不能跑。

而真跑一场 Kaggle 比赛的代价很大：要联网下载、要训练、**提交还要花配额**。
所以"验证能不能跑"和"真的去比赛"必须分开。

`kauto smoke` 用包内夹具把
`prepare → baseline → write-sub → validate` 跑一遍：
真实读 csv、真实训练 LightGBM、真实写 submission、真实跑形状校验。
全程不联网、不碰 Kaggle、不消耗配额。

## 三条必须钉住的性质

1. **不碰真实工作区**：用临时根，跑完删掉；用户的工作目录里不该多出
   一个 `smoke-comp`。
2. **失败要能定位**：夹具目录不对时报可操作错误（退出码 2），
   而不是一个 traceback。
3. **报告要说清边界**：明确列出"未覆盖"的三项（凭据 / 上传通道 / 规则接受），
   否则一次全绿会被误读成"连 Kaggle 都通了"。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

results = []


def mark(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail})" if detail else ""))


def run_cli(*argv: str, root: str | None = None) -> tuple[int, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8",
           "NO_PROXY": "api.kaggle.com,www.kaggle.com,kaggle.com,127.0.0.1,localhost"}
    if root:
        env["KAUTO_ROOT"] = root
    p = subprocess.run([sys.executable, "-m", "kauto.cli", *argv],
                       cwd=str(ENGINE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=600)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def parse_json(text: str) -> dict | None:
    """从可能带进度行的输出里取那段 JSON。"""
    start = text.find("{")
    if start < 0:
        return None
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
    return None


# ── 1. 正常路径 ───────────────────────────────────────────────────────────
print("=== 1) 演练能跑通整条链路 ===")
fixture = ENGINE / "fixtures" / "demo-comp"
if not (fixture / "train.csv").exists():
    print("SKIP  fixtures/demo-comp 不在包里")
    raise SystemExit(77)

TMP_ROOT = Path(tempfile.mkdtemp(prefix="kauto-smoke-test-"))
t0 = __import__("time").time()
code, out = run_cli("smoke", root=str(TMP_ROOT))
dt = round(__import__("time").time() - t0, 1)

payload = parse_json(out)
print(f"   exit={code}  用时 {dt}s")
mark("退出码 0", code == 0, str(code))
mark("返回可解析 JSON", payload is not None)
if payload:
    steps = payload.get("steps") or []
    names = [s.get("step") for s in steps]
    print(f"   steps: {[(s.get('step'), s.get('ok'), s.get('seconds')) for s in steps]}")
    mark("四个步骤都跑了", names == ["prepare", "baseline", "write-sub", "validate"], str(names))
    mark("四步全通过", all(s.get("ok") for s in steps))
    mark("报告标注为离线演练", payload.get("kind") == "offline-smoke", str(payload.get("kind")))
    mark("给出总耗时", isinstance(payload.get("seconds"), (int, float)))
    nc = payload.get("not_covered") or []
    print(f"   not_covered: {nc}")
    mark("明确列出未覆盖项", len(nc) == 3, f"{len(nc)} 项")
    mark("未覆盖项含凭据", any("凭据" in str(x) for x in nc))
    mark("未覆盖项含上传通道", any("上传" in str(x) for x in nc))
    mark("未覆盖项含规则接受", any("规则" in str(x) for x in nc))

# ── 2. 不碰真实工作区 ─────────────────────────────────────────────────────
print("\n=== 2) 用临时根，不污染真实工作区 ===")
mark("临时根里没留下产物（默认跑完删除）",
     not (TMP_ROOT / "work" / "smoke-comp").exists(),
     str(TMP_ROOT / "work" / "smoke-comp"))
mark("真实工作区没有 smoke-comp", not (ENGINE / "work" / "smoke-comp").exists())

# --keep 时才保留。
# 注意：smoke 有自己的临时根（`tempfile.mkdtemp`），**不是** KAUTO_ROOT ——
# 它刻意不写进用户指定的工作根，因为演练产物没有保留价值，混进去只会让人
# 以为那是真实比赛的数据。所以断言要看它自己报出来的那个路径。
TMP2 = Path(tempfile.mkdtemp(prefix="kauto-smoke-keep-"))
code2, out2 = run_cli("smoke", "--keep", root=str(TMP2))
print(f"   --keep exit={code2}")
kept_line = next((ln for ln in out2.splitlines() if "保留演练产物" in ln), "")
print(f"   {kept_line}")
kept_root = Path(kept_line.split("：")[-1].strip()) if "：" in kept_line else None
mark("--keep 报出了保留路径", kept_root is not None, kept_line[:70])
if kept_root is not None:
    mark("保留路径存在", kept_root.exists(), str(kept_root))
    mark("保留了 smoke-comp 工作目录",
         (kept_root / "work" / "smoke-comp").exists(),
         str(kept_root / "work" / "smoke-comp"))
    kept = kept_root / "work" / "smoke-comp"
    if kept.exists():
        mark("保留了 submission.csv", (kept / "submission.csv").exists())
        mark("保留了 baseline.json", (kept / "baseline.json").exists())
    mark("没有写进 KAUTO_ROOT（演练产物不混入工作根）",
         not (TMP2 / "work" / "smoke-comp").exists(),
         str(TMP2 / "work" / "smoke-comp"))
    shutil.rmtree(kept_root, ignore_errors=True)
mark("保留时退出码 0", code2 == 0)

# ── 3. 失败路径可定位 ─────────────────────────────────────────────────────
print("\n=== 3) 夹具不存在时给可操作错误 ===")
code3, out3 = run_cli("smoke", "--local", "C:/definitely-not-here", root=str(TMP_ROOT))
print(f"   exit={code3}  输出前两行: {out3.strip().splitlines()[:2]}")
mark("退出码 2（参数/夹具问题）", code3 == 2, str(code3))
mark("指出找不到夹具", "找不到夹具" in out3)
mark("给出 --local 用法", "--local" in out3)
mark("不是 traceback", "Traceback" not in out3)

# ── 4. 自定义夹具目录 ─────────────────────────────────────────────────────
print("\n=== 4) --local 指向别的夹具也能跑 ===")
alt = ENGINE / "fixtures" / "dirty-comp"
if (alt / "train.csv").exists():
    TMP3 = Path(tempfile.mkdtemp(prefix="kauto-smoke-alt-"))
    code4, out4 = run_cli("smoke", "--local", str(alt), root=str(TMP3))
    p4 = parse_json(out4)
    print(f"   dirty-comp exit={code4}")
    # dirty-comp 的列名有脏数据，prepare 可能被它自己的清洗逻辑处理掉；
    # 两种结局都能接受，关键是不能崩成 traceback。
    mark("不崩（未出现 traceback）", "Traceback" not in out4)
    if code4 == 0 and p4:
        mark("dirty-comp 也能走完四步",
             len(p4.get("steps") or []) == 4, str([s.get("step") for s in (p4.get("steps") or [])]))
    shutil.rmtree(TMP3, ignore_errors=True)
else:
    print("   （跳过：dirty-comp 不在包里）")

# ── 5. 命令已注册 ─────────────────────────────────────────────────────────
print("\n=== 5) 命令注册与 help ===")
code5, out5 = run_cli("--help")
mark("--help 里有 smoke", "smoke" in out5, "")
mark("--help 退出码 0", code5 == 0)
mark("help 文案说明不碰配额", "配额" in out5 or "离线" in out5)

# 清理
shutil.rmtree(TMP_ROOT, ignore_errors=True)
shutil.rmtree(TMP2, ignore_errors=True)

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
for n, ok, _ in results:
    if not ok:
        print(f"  FAIL {n}")
print("全部通过" if not bad else f"{len(bad)} 项未通过")
raise SystemExit(1 if bad else 0)
