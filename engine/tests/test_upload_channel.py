"""第 62 轮：把「上传通道」的三次真实事故钉成测试。

## 事故 1：上传禁用代理 → 永久挂起

`kaggle competitions submit` 卡死，skill 桥接 600s 超时报"skill 调用超时"，
看起来像 Kaggle 拒绝或配额问题。逐层剥出来：

```
StartSubmissionUpload（api.kaggle.com）    → 200，1.1s，返回 createUrl
PUT createUrl（禁用代理）                   → 永久挂起（无超时、无错误）
PUT createUrl（走 127.0.0.1:7890）          → 200，1.2s
```

两个域对网络的要求**相反**：Kaggle 域被系统代理破坏 TLS（ROADMAP 坑 #1），
上传的 createUrl 落在 www.googleapis.com，本机只有走代理才通。
一条 NO_PROXY 覆盖不了两者 → 上传挂死。

修法：`kapi.direct_submit()` 三步，每步用对的网络路径。

## 事故 2：把代理地址写死

曾经写死 `127.0.0.1:7890`。代理软件退出后端口无人监听，上传立刻
`WinError 10061 目标计算机积极拒绝` —— 而文案只说"代理是否可用"，
对着一个失效的硬编码地址，用户无从下手。

修法：候选列表 + 现场探测，取第一个"真能通 Google"的。

## 事故 3：探测串行，报错要等 5.7 秒

代理全关时 8 个候选逐个等到超时，报个错要 5.7s（用户会以为卡住）。
修法：TCP 预筛（死端口立刻跳过）+ 并行探测 → 0.62s，
且成功/失败同速。
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import kapi  # noqa: E402

results = []


def mark(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f"  ({detail})" if detail else ""))


src = Path("kauto/kapi.py").read_text(encoding="utf-8")

print("=== 1) 两个域用不同的 opener（不能一条 NO_PROXY 走天下）===")
mark("有 _kaggle_opener（禁代理）", "_kaggle_opener" in src)
mark("有 _upload_opener（走代理）", "_upload_opener" in src)
mark("Kaggle opener 用空 ProxyHandler", "ProxyHandler({})" in src)

print("\n=== 2) 三步提交的次序与字段 ===")
mark("有 StartSubmissionUpload", "StartSubmissionUpload" in src)
mark("有 CreateSubmission", "CreateSubmission" in src)
mark("提交用 blobFileTokens", "blobFileTokens" in src)
i_start = src.find("StartSubmissionUpload")
i_create = src.find("CreateSubmission")
mark("先申请上传位，再建提交", i_start != -1 and i_create != -1 and i_start < i_create)

print("\n=== 3) 代理地址不写死一个 ===")
mark("有候选列表", "UPLOAD_PROXY_CANDIDATES" in src)
cands = kapi.UPLOAD_PROXY_CANDIDATES
print(f"   候选数: {len(cands)}  首个: {cands[0] if cands else '(none)'}")
mark("候选多于 1 个（有回退余地）", len(cands) > 1)
mark("环境变量优先", "KAUTO_UPLOAD_PROXY" in src)

print("\n=== 4) 探测必须 TCP 预筛 + 并行 ===")
mark("TCP 预筛（create_connection）", "create_connection" in src)
mark("并行探测（ThreadPoolExecutor）", "ThreadPoolExecutor" in src)

print("\n=== 5) 探测速度：写死端口（死）要秒级返回 ===")
# 断言用相对比较，不用绝对秒数。
#
# 教训：第一版写 `elapsed < 3.0`，本机 100% CPU 负载下实测 3.12s 就 FAIL ——
# 而实现本身没问题，是并行线程的调度在满载机器上被拖慢。
# 绝对时间断言在"负载可变"的机器上是脆的；这里改成与**串行基线**对照：
# 串行是"候选数 × 每次超时"，并行是"单次上限"，两者相差一个量级。
dead = [f"http://127.0.0.1:{p}" for p in (9999, 9998, 9997, 9996, 9995, 9994, 9993, 9992)]
t0 = time.time()
r = kapi.probe_upload_proxy(dead, timeout=2)
elapsed = time.time() - t0
serial_bound = len(dead) * 0.6        # 串行最坏：每个候选都吃满 TCP 预筛超时
print(f"   8 个死端口 → {r}，耗时 {elapsed:.2f}s"
      f"（串行下界约 {serial_bound:.1f}s，实测串行实现 5.7s）")
mark("全死时返回 None", r is None)
mark("明显快于串行（< 串行下界的 80%）", elapsed < serial_bound * 0.8,
     f"{elapsed:.2f}s vs {serial_bound:.1f}s")

print("\n=== 6) 没有可用通道时给出可操作报错，不是让用户猜端口 ===")
try:
    # 强制候选全死，触发 _upload_opener 的报错
    orig = kapi.UPLOAD_PROXY_CANDIDATES
    kapi.UPLOAD_PROXY_CANDIDATES = dead
    try:
        kapi._upload_opener()
        raised, msg = False, ""
    except kapi.KaggleError as e:
        raised, msg = True, str(e)
    finally:
        kapi.UPLOAD_PROXY_CANDIDATES = orig
    print(f"   抛出 KaggleError: {raised}")
    print(f"   文案含处理建议: {'KAUTO_UPLOAD_PROXY' in msg}")
    mark("无通道时明确报错", raised)
    mark("报错给出环境变量/启动代理的指引",
         "KAUTO_UPLOAD_PROXY" in msg or "启动" in msg)
    mark("报错说明 Kaggle 域不受影响", "不受此影响" in msg or "仍直连" in msg)
except Exception as e:
    mark("无通道时明确报错", False, str(e)[:80])

print("\n=== 7) 上传失败会重试，且每次重探通道 ===")
mark("有重试循环", "for attempt in range(2)" in src or "range(2)" in src)
mark("重试前重新探通道（不复用旧 opener）",
     "_upload_opener().open" in src)

print("\n=== 汇总 ===")
bad = [n for n, ok, _ in results if not ok]
print("全部通过" if not bad else f"{len(bad)} 项未通过：" + ", ".join(bad))
raise SystemExit(1 if bad else 0)
