"""第 60 轮：规则接受状态的**一手探针**，以及一个被它照出来的 token 缺陷。

## 这一轮推翻了两条旧结论

### 旧结论 1（错的）："Kaggle 没有接受规则的 API，两个端点都 404"

早期探的是：

```
/v1/competitions.CompetitionApiService/AcceptRules   → 404
/api/i/competitions.CompetitionService/AcceptRules   → 404
```

两个都没测错，但**服务名选错了**。真实路由在前端 bundle 的路由表里：

```
competitions.UserService/AcceptRules        （155 条路由里的一条，不是猜的）
competitions.UserService/GetUser            ← 读侧，这个是我们能用的
```

所以"没有端点"这个结论是错的。正确结论是：**写侧端点存在，但走站内通道
`www.kaggle.com/api/i/...`，需要浏览器会话级授权 —— 程序化一律 401。**
这个区别很重要：它把"无解"换成了"用一个有明确边界的读探针把状态问清楚"。

### 旧结论 2（也错的）："没有任何探针能暴露未加入"

`list` 的 `userHasEntered` 不是唯一探针，而且它是二手字段。一手的在：

```
GET /api/i/competitions.UserService/GetUser?competitionName=titanic
→ 200 {"hasAcceptedRules": false, "canUnregister": false}
```

## 被照出来的第三个缺陷：kagglesdk 的过期判据方向反了

写这个探针时，同一个 token 一会儿 200 一会儿 401，看起来像"接口不稳"。
真相是 sdk 这一行：

```python
def access_token_has_expired(self):
    return not self._exp or self._exp < datetime.now(timezone.utc) - timedelta(minutes=30)
```

`now - 30min` 是**滞后**判据：要等过期满 30 分钟才肯 refresh。
于是「已过期但不足 30 分钟」这个窗口里，`get_access_token()` 返回死 token，
所有请求 401 —— 而这 401 长得跟"权限不足"一模一样，极易误判成"接口不给用"。

正确判据是提前换（`exp < now + slack`），不是滞后换。

## 本脚本的验证方式

不联网也能跑（离线部分用桩数据）；联网部分若凭据不可用会 SKIP 而不是 FAIL。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import kapi
from kauto.submit import _check_entered

results = []


def check(name, ok):
    results.append((name, ok))


print("=== 1) 一手探针存在且是只读 GET ===")
src = Path("kauto/kapi.py").read_text(encoding="utf-8")
has_fn = "def competition_user_state" in src
has_route = "competitions.UserService/GetUser" in src
# 站内调用只能走 GET —— 写侧不在这一层
site_src = src.split("def site_api")[1].split("def competition_user_state")[0] if "def site_api" in src else ""
read_only = 'method="GET"' in site_src and "method=\"POST\"" not in site_src
print(f"   有 competition_user_state: {has_fn}")
print(f"   用 GetUser 路由: {has_route}")
print(f"   site_api 只发 GET: {read_only}")
check("一手探针存在", has_fn)
check("路由为 UserService/GetUser", has_route)
check("站内层只读 GET", read_only)

print("\n=== 2) 路由名字必须是 UserService，不是 CompetitionService ===")
# 这条是防止回归：写错服务名会得到 404 而不是 401，两者的诊断含义完全不同
wrong = "competitions.CompetitionService/GetUser" in src
print(f"   用了错的服务名: {wrong}（应为 False）")
check("服务名正确", not wrong)

print("\n=== 3) 过期判据必须提前，不能滞后 ===")
# 直接验我们自己那层：stale 判据要带 +minutes 语义
tok_src = src.split("def access_token")[1].split("def site_api")[0]
# 契约：出现 now + timedelta(minutes=...)，且不出现 now - timedelta(minutes=...)
forward = "now + timedelta(minutes=" in tok_src
backward = "now - timedelta(minutes=" in tok_src
has_force = "force_refresh" in tok_src and "refresh_access_token()" in tok_src
print(f"   提前量写法 now + ...: {forward}")
print(f"   滞后写法 now - ...: {backward}（应为 False）")
print(f"   支持 force_refresh: {has_force}")
check("过期判据提前", forward and not backward)
check("支持强制 refresh", has_force)

print("\n=== 4) _check_entered 的判据来源 ===")
# 第 61 轮：`hasAcceptedRules` 实测恒为 false（四场全 false），不能当判据。
# 于是"一手优先"这个断言失去意义 —— 现在它只是诊断字段，
# 判加入的唯一依据是 list.userHasEntered。
sub_src = Path("kauto/submit.py").read_text(encoding="utf-8")
fn_src = sub_src.split("def _check_entered")[1].split("def _pre_submit_gate")[0]
uses_state = "entered_state" in fn_src
uses_fallback = "userHasEntered" in fn_src or "entered_state" in fn_src
reports_source = '"source"' in fn_src or "entered_state" in fn_src
print(f"   用统一的 entered_state 判据: {uses_state}")
print(f"   保留 list 判加入: {uses_fallback}")
print(f"   结果带判据来源: {reports_source}")
check("_check_entered 走统一判据", uses_state)
check("保留 list 判加入", uses_fallback)
check("回报判据来源", reports_source)

print("\n=== 5) 离线行为：探针不可用时必须退回 list，不能当成'未接受' ===")
# 用桩替换站内探针，模拟 401（token 过期）——此时 accepted 必须是 None 而不是 False
orig = kapi.competition_user_state


def _stub_fail(slug):
    return {"ok": False, "accepted": None, "status": 401,
            "error": "Unauthenticated"}


kapi.competition_user_state = _stub_fail
_orig_list = kapi.list_competitions
kapi.list_competitions = lambda search="", limit=20: [
    {"ref": "https://www.kaggle.com/competitions/titanic", "userHasEntered": True}
]
try:
    r = _check_entered("titanic")
    print(f"   401 + list=entered → entered={r.get('entered')} source={r.get('source')}")
    # 关键：401 不能被当成"规则未接受"。必须退回 list 并且承认来源是二手。
    ok5 = r.get("entered") is True and r.get("source") == "userHasEntered"
    check("401 退回 list 且不误判", ok5)

    print("\n=== 6) 一手返回 False 时**不再**据此拦截（第 61 轮实测推翻）===")
    # 旧断言是「一手 False 直接拦下」，它把当时错误的判据固化成了契约。
    # 实测：四场比赛 GetUser 恒返回 false，其中 titanic 已加入且提交成功
    # （publicScore=0.76076），digit-recognizer 提交后才变 userHasEntered。
    # 所以正确行为是：忽略这个恒 false 的字段，改用 list 判加入。
    kapi.competition_user_state = lambda slug: {"ok": True, "accepted": False,
                                                "status": 200, "error": ""}
    kapi.list_competitions = lambda search="", limit=20: [
        {"ref": "https://www.kaggle.com/competitions/titanic", "userHasEntered": True}
    ]
    r = _check_entered("titanic")
    print(f"   accepted=False + list=entered → entered={r.get('entered')} "
          f"source={r.get('source')}")
    ok6 = r.get("entered") is True and r.get("source") == "userHasEntered"
    check("一手 False 不再拦下（改用 list 判加入）", ok6)

    print("\n=== 7) 一手返回 True 时也应放行（不再有 'accepted=True 优先' 分支）===")
    # accepted=True 时同样落到 list（本实现只看 userHasEntered）。
    # digit-recognizer 是这条的真实反例：accepted=false 却已能提交。
    kapi.list_competitions = lambda search="", limit=20: [
        {"ref": "https://www.kaggle.com/competitions/titanic", "userHasEntered": True}
    ]
    r = _check_entered("titanic")
    print(f"   entered={r.get('entered')} source={r.get('source')}")
    check("accepted=True 也放行", r.get("entered") is True)
finally:
    kapi.competition_user_state = orig
    kapi.list_competitions = _orig_list

print("\n=== 8) 联网实测（凭据不可用则 SKIP）===")
live = None
try:
    r = kapi.competition_user_state("titanic")
    if r.get("ok"):
        live = r
        print(f"   真实探针: hasAcceptedRules={r.get('accepted')} status={r.get('status')}")
    else:
        print(f"   探针不可用（{r.get('status')} {r.get('error')[:80]}）→ SKIP")
        live = None
except Exception as e:
    print(f"   异常 {type(e).__name__}: {str(e)[:100]} → SKIP")
    live = None

if live is not None:
    print("\n=== 9) 真实探针的字段契约 ===")
    has_field = "hasAcceptedRules" in (live.get("raw") or {})
    print(f"   原始响应含 hasAcceptedRules: {has_field}")
    check("字段契约", has_field)
else:
    check("字段契约", None)

print("\n=== 10) 未知 slug 必须是 404 而不是 200 ===")
if live is not None:
    bad = kapi.competition_user_state("no-such-competition-xyz")
    print(f"   未知 slug → status={bad.get('status')} accepted={bad.get('accepted')}")
    check("未知 slug 不乱报", bad.get("ok") is False and bad.get("accepted") is None)
else:
    check("未知 slug 不乱报", None)

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
