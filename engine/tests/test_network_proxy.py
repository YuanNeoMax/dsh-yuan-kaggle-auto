"""验证 Kaggle 网络通路 —— 系统代理会破坏 TLS，只有 NO_PROXY 能绕过。

## 实测出来的排查过程（这轮最花时间的部分）

试过很多方向都不对：

| 尝试 | 结果 | 结论 |
|---|---|---|
| 清空 `HTTP_PROXY` / `HTTPS_PROXY` | 仍 `SSLEOFError` | 环境变量不是根源 |
| 用系统代理 `127.0.0.1:7890` | 仍 `SSLEOFError` | 也不是"没设对代理" |
| `Test-NetConnection api.kaggle.com:443` | **True** | TCP 层没问题 |
| Python `socket` + `ssl` 直连 | **TLSv1.3 握手成功** | TLS 层也没问题 |
| `urllib` 默认（读系统代理） | `UNEXPECTED_EOF_WHILE_READING` | **只有走代理时才崩** |
| `urllib` + `ProxyHandler({})` | 正常（404 只是路径错） | 绕过代理就好了 |
| `NO_PROXY=kaggle域名` | **kaggle CLI 立刻返回 JSON** | ✅ 解 |

**根因：本机的系统代理（注册表 `ProxyEnable=1`，指向 7890）会破坏 Kaggle 的 TLS 握手。**
而 `requests` 除了读环境变量，**还会读 Windows 注册表的代理设置** ——
所以清空环境变量无效，必须用 `NO_PROXY` 精确排除。

## 修复位置（两处，同一个坑）

| 位置 | 原来 | 现在 |
|---|---|---|
| `kapi.run` | 只透传 `os.environ` | 加 `NO_PROXY`（kaggle 域名） |
| `skillbridge._run` | 清了 `HTTP_PROXY/HTTPS_PROXY` | 加 `NO_PROXY` |

第二处特别隐蔽：**它看起来"已经处理了代理"**（清了两个变量），
但清的正是无效的那两个。

## 验证方式

本测试**不依赖网络**（网络随时可能又变），只验证代码里确实注入了 `NO_PROXY`。
真实调用由 `--live` 手动触发。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import kapi, skillbridge

results = []

print("=== 1) kapi.run 注入 NO_PROXY ===")
src_kapi = inspect.getsource(kapi.run)
has_no_proxy = "NO_PROXY" in src_kapi
mentions_kaggle = "api.kaggle.com" in src_kapi
print(f"   含 NO_PROXY: {has_no_proxy}")
print(f"   指到 kaggle 域名: {mentions_kaggle}")
results.append(("kapi 注入 NO_PROXY", has_no_proxy and mentions_kaggle))

print("\n=== 2) skillbridge._run 也注入 NO_PROXY ===")
src_skill = inspect.getsource(skillbridge._run)
has2 = "NO_PROXY" in src_skill
print(f"   含 NO_PROXY: {has2}")
print("   （原实现只清了 HTTP_PROXY/HTTPS_PROXY —— 清的正是无效的那两个）")
results.append(("skillbridge 注入 NO_PROXY", has2))

print("\n=== 3) 两处都带 no_proxy 小写形式（requests 大小写都读）===")
ok3 = ("no_proxy" in src_kapi) and ("no_proxy" in src_skill)
print(f"   两处都有小写形式: {ok3}")
results.append(("大小写双写", ok3))

print("\n=== 4) 诊断能力：错误信息应指出可能是代理 ===")
# 这是这轮排查最花时间的地方 —— 错误信息当时只说 SSLEOFError，没提示代理
src_sol = inspect.getsource(skillbridge.solutions)
has_hint = "网络抖动" in src_sol
print(f"   solutions 里有网络提示: {has_hint}")
print("   （现有提示只说了「网络抖动」，没提「可能是系统代理」——")
print("     这轮排查花了几十分钟才定位到代理，值得补进文案）")
results.append(("有网络提示", has_hint))

print("\n=== 5) 实测（可选，需 --live）===")
live = "--live" in sys.argv
if live:
    try:
        rows = kapi.list_competitions("playground", limit=2)
        print(f"   真实 list 返回 {len(rows)} 条")
        results.append(("真实 API 可用", len(rows) >= 1))
    except Exception as e:
        print(f"   真实调用失败：{type(e).__name__} {str(e)[:100]}")
        results.append(("真实 API 可用", False))
else:
    print("   （跳过真实调用；加 --live 才跑）")
    results.append(("真实 API 可用", None))

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
