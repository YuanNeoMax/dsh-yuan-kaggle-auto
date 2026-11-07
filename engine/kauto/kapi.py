"""Kaggle CLI 封装层。

所有外部动作都集中在这里：列比赛、下载数据、提交。失败一律抛信息量够的异常，
不做静默降级——自动化的最大风险是"看起来跑了但其实没提交"。

## 两条完全不同的 API 通道（第 60 轮实测，之前一直混淆）

| 通道 | 基址 | 认证 | 覆盖 |
|---|---|---|---|
| CLI / 公开 API | `api.kaggle.com/v1/...` | OAuth access token | 列表、下载、提交 |
| 站内 API | `www.kaggle.com/api/i/...` | 同一 token，但**只放行读** | 比赛内用户状态 |

第 60 轮结论：`competitions.UserService/GetUser` 是**只读**探针，能直接给出
`hasAcceptedRules`；而写侧 `competitions.UserService/AcceptRules` 走站内通道，
需要浏览器会话级授权（实测带 XSRF 仍 401）。所以"接受规则"这一动作仍然只能人工，
但**判断它到底有没有做**不再需要靠 list 里的二手字段。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── 可写根目录的解析（插件化改造的唯一改动）─────────────────────────────────
#
# 作为独立 CLI 时，ROOT 由 `__file__` 推导（仓库根）就够了。
# 但装进 DSH 插件包后，`__file__` 位于 `node_modules/dsh-yuan-kaggle-auto/engine/kauto/`，
# 于是 data/ work/ reports/ 全都会写进 node_modules —— npm 升级即丢，
# 只读安装直接失败。所以让 `KAUTO_ROOT` 优先：
#
#   1. KAUTO_ROOT（宿主插件按用户的「工作目录」设置注入）
#   2. 否则退回 `__file__` 推导（保持独立 CLI / 开发时行为不变）
#
# 只改这一处，其余模块都从 kapi 导入 ROOT，不会各自推导。
_ENGINE_ROOT_ENV = os.environ.get("KAUTO_ROOT", "").strip()
ROOT = Path(_ENGINE_ROOT_ENV).expanduser().resolve() if _ENGINE_ROOT_ENV else Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
WORK_DIR = ROOT / "work"
REPORT_DIR = ROOT / "reports"


class KaggleError(RuntimeError):
    pass


def exe() -> str:
    """优先用当前解释器所在 venv 里的 kaggle 入口，避免 PATH 串味。"""
    for name in ("kaggle.exe", "kaggle"):
        cand = Path(sys.executable).parent / name
        if cand.exists():
            return str(cand)
    return shutil.which("kaggle") or "kaggle"


def _oauth_user(home: Path) -> str | None:
    """读 ~/.kaggle/credentials.json 判断 OAuth 是否已登录。

    实测落盘形态（kaggle 2.2.5）：文件名就叫 credentials.json，内容是
    {refresh_token, access_token, access_token_expiration, username, scopes}。
    之前按"文件名含 oauth"猜是错的 —— 只能读内容里的 username 才判得准。
    """
    p = home / "credentials.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if d.get("refresh_token") or d.get("access_token"):
        return str(d.get("username") or "unknown")
    return None


def creds() -> dict:
    """检测可用凭据形态，按 kaggle 2.x 的实际解析顺序：

    1. OAuth（`kaggle auth login` → ~/.kaggle/credentials.json）
    2. access_token 文件（~/.kaggle/access_token 或 .txt）
    3. 旧式 kaggle.json（username + key）
    """
    home = Path.home() / ".kaggle"
    kj = [p for p in (home / "kaggle.json", ROOT / ".kaggle" / "kaggle.json") if p.exists()]
    tok = [p for p in (home / "access_token", home / "access_token.txt") if p.exists()
           and p.stat().st_size > 0]
    env_ok = bool(os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY"))
    token_env = bool(os.environ.get("KAGGLE_API_TOKEN"))
    oauth_user = _oauth_user(home) if home.exists() else None

    ready = bool(env_ok or token_env or tok) or bool(kj) or oauth_user is not None
    method = ("env-kaggle-key" if env_ok else
              "env-api-token" if token_env else
              "oauth" if oauth_user is not None else
              "access_token-file" if tok else
              "kaggle.json" if kj else "")

    hint = ""
    if not ready:
        hint = (
            "缺凭据，两种都可以：\n"
            "  A) 推荐免管理 token：kaggle auth login（浏览器授权，凭据自动缓存）\n"
            "  B) 手动 token：kaggle.com → Settings → API → Generate New Token，\n"
            "     把 token 存到 %USERPROFILE%\\.kaggle\\access_token，"
            "或设置环境变量 KAGGLE_API_TOKEN"
        )
    # `ok` 是所有 provider 的统一可用性别名。
    # 实测教训：llm / winjob / appcontainer 用 ok，这里用 ready，
    # 调用方猜错一个字段名就静默失败（见 ROADMAP 第 27 轮：所有 LLM 调用恒失败）。
    # 保留 `ready` 以免破坏已有调用方，同时给出统一别名。
    return {"ok": ready, "ready": ready, "method": method, "user": oauth_user,
            "env": env_ok,
            "token_files": [str(p) for p in tok], "kaggle_json": [str(p) for p in kj],
            "hint": hint}


SITE_BASE = "https://www.kaggle.com"
API_BASE = "https://api.kaggle.com/v1"

# ── 上传通道：Google 存储域必须走代理，但**代理不是固定的** ────────────────
#
# createUrl 指向 www.googleapis.com。这台机器直连 Google 会永久挂起
# （实测：无超时、无错误，进程就一直躺着 —— 这正是 `competitions submit`
# 卡死 600s 的真因，不是 Kaggle 拒绝，也不是配额问题），必须经代理。
#
# 但把 `http://127.0.0.1:7890` **写死**是一次真实事故：
# 代理软件（mihomo/clash 系）在 2026-11-07 退出后，端口无人监听，
# 于是上传立刻报 `WinError 10061 目标计算机积极拒绝`，而报错信息里
# 只提"代理是否可用"—— 对着一个已经失效的硬编码地址，用户无从下手。
#
# 所以改成**候选列表 + 逐个探测**：环境变量优先，其次常见客户端端口。
# 探测在 `_upload_opener()` 里按需做（连一次 /generate_204），
# 不探测的端口不会被拿来当默认值 —— 宁可明确报"没有可用通道"，
# 也不要拿一个想当然的地址去上传。
UPLOAD_PROXY_CANDIDATES = [
    p for p in (
        os.environ.get("KAUTO_UPLOAD_PROXY", "").strip(),
        "http://127.0.0.1:7890",      # Clash / mihomo 默认
        "http://127.0.0.1:7891",
        "http://127.0.0.1:7897",      # Clash Verge
        "http://127.0.0.1:10809",     # v2rayN (http)
        "http://127.0.0.1:10808",     # v2rayN (socks→http 视配置)
        "http://127.0.0.1:2080",      # nekoray
        "http://127.0.0.1:8080",
    ) if p
]
# 兼容旧名：单值覆盖，留作外部脚本直接改的时候仍可用
UPLOAD_PROXY = UPLOAD_PROXY_CANDIDATES[0]


def access_token(*, force_refresh: bool = False) -> str | None:
    """取一个**当前有效**的 access token，供站内 API 使用。

    ## 为什么不能直接读 credentials.json 里的 access_token

    第 60 轮实测：文件里那个字段会过期（本机实测
    `2026-11-01T01:33:45Z`），过期之后拿它求站内 API 一律
    `401 {"status":"UNAUTHENTICATED"}` —— 而这个 401 看起来
    和"权限不足"一模一样，极易被误判成"这个接口不给用"。

    ## 而且 kagglesdk 的过期判据方向是错的（实测）

    `KaggleCredentials.access_token_has_expired()` 写的是：

        expiration < now - 30min        # 滞后判据

    也就是**过期满 30 分钟之后**它才认为该 refresh。于是在
    「已过期 0~30 分钟」这个窗口里，`get_access_token()` 一直把
    那把死 token 递回来，所有站内请求一律 401。

    本机实测落在这个窗口里：`exp = 01:33:45Z`，`now = 01:59Z`
    → 已过期 25 分钟 → sdk 判「未过期」→ introspect 直接
    `Authentication failed`。

    正确判据应是**提前换**（`expiration < now + slack`），不是滞后换。
    所以这里不信任 sdk 的判断：先看自己算的剩余寿命，不够就显式 refresh。
    """
    try:
        from datetime import datetime, timedelta, timezone

        from kagglesdk import KaggleClient, KaggleCredentials, KaggleEnv
        cl = KaggleClient(env=KaggleEnv.PROD)
        creds_obj = KaggleCredentials.load(cl)
        if creds_obj is not None:
            exp = creds_obj._access_token_expiration
            now = datetime.now(timezone.utc)
            # 提前量 10 分钟：够覆盖一次请求的往返，又不至于频繁换 token
            stale = (exp is None or exp < now + timedelta(minutes=10))
            if force_refresh or stale:
                creds_obj.refresh_access_token()   # 内部会落盘新 token
            tok = creds_obj.get_access_token()
            if tok:
                return tok
    except Exception:
        pass    # sdk 缺失或 refresh 失败，退回裸读

    for p in (Path.home() / ".kaggle" / "credentials.json",
              Path.home() / ".kaggle" / "access_token"):
        if not p.exists():
            continue
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if p.suffix == ".json":
            try:
                raw = str(json.loads(raw).get("access_token") or "")
            except json.JSONDecodeError:
                raw = ""
        if raw:
            return raw
    return None


def site_api(path: str, *, timeout: int = 25, raw: bool = False,
             accept: str = "application/json") -> dict:
    """读站内 API（`/api/i/...`）。只做 GET —— 写侧不走这里。

    `raw=True` 时不解析 JSON，把响应正文原样带回 `text` 字段 ——
    网页路由（如 `/competitions/<slug>/rules`）返回的是 HTML，这里只做
    "页面里有没有某句话"这种判据，不需要 DOM 解析。

    ## 为什么用 urllib + ProxyHandler({})

    与 `run()` 里 NO_PROXY 的做法同源：本机系统代理会破坏 Kaggle 的 TLS 握手。
    `ProxyHandler({})` 是根治，比依赖环境变量更可靠（requests 还会读注册表，
    urllib 不会）。

    ## 返回形态

    统一返回 {ok, status, data, error}，**不抛异常** —— 这是个探针，
    它的失败本身就是信息（比如 401 说明 token 过期、403 说明没权限）。
    """
    tok = access_token()
    if not tok:
        return {"ok": False, "status": None, "data": None,
                "error": "无可用 access token（先 kaggle auth login）"}

    url = path if path.startswith("http") else SITE_BASE + path
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Accept", "text/html" if raw else accept)
    req.add_header("User-Agent",
                   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:                      # 网络层失败
        return {"ok": False, "status": None, "data": None,
                "error": f"{type(e).__name__}: {str(e)[:140]}"}

    if raw:
        # 网页路由：正文就是判据来源，不解析 JSON。
        return {"ok": status == 200, "status": status, "text": body,
                "data": None,
                "error": "" if status == 200 else f"HTTP {status}"}

    try:
        data = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        data = {"_raw": body[:300]}

    if status != 200:
        msg = ""
        if isinstance(data, dict):
            msg = str((data.get("error") or {}).get("message") or data.get("message") or "")
        return {"ok": False, "status": status, "data": data,
                "error": msg or f"HTTP {status}"}
    return {"ok": True, "status": status, "data": data, "error": ""}


def competition_user_state(slug: str) -> dict:
    """比赛内**用户自己的**状态：有没有接受规则。

    ## 这补的是哪个洞

    `_check_entered` 一直读 `list` 里的 `userHasEntered` —— 那是**公开列表**
    的二手字段，粒度粗。第 60 轮找到更准的一手探针：

    ```
    GET /api/i/competitions.UserService/GetUser?competitionName=<slug>
    → 200 {"hasAcceptedRules": false, "canUnregister": false}
    ```

    这是"我和这场比赛的关系"，不是"这场比赛存在吗"。

    ## 参数校验（实测，别乱传）

    | 传参 | 结果 |
    |---|---|
    | 不带任何比赛 | 400 `No competition specified.` |
    | `competitionName=不存在的` | 404 `Not found` |
    | `competitionId=999999` | 403 `competitions.get was denied`（越权探测会被拦） |
    | `competitionName=<真 slug>` | 200 + 真实状态 |

    所以：**用 competitionName 传 slug**，不要用猜的 id。

    返回 {ok, accepted, status, error}。
    `accepted` 为 None 表示没探到（不是"未接受"）。

    ## 第 61 轮修正：`hasAcceptedRules=false` **不等于**「没加入比赛」

    实测（titanic，账号 `yuanneo`）：

    ```
    GetUser            → {"hasAcceptedRules": false, "canUnregister": false}
    list.userHasEntered → True          ← 已经真的加入了
    submit（浏览器/CLI） → 成功，publicScore=0.76076
    ```

    而 titanic 的 rules 页标题写着 **"Rules acceptance is not required for
    this competition"**。也就是说 `hasAcceptedRules` 对**不需要接受规则**的
    比赛永远返回 false —— 它不是"未加入"的判据，只是"没点过 Accept"的判据。
    旧代码拿它当"未加入"的硬拦截，于是这类比赛永远提交不了，
    而 `--yes` 会在这里被卡住（这条路径此前就卡过一次，被记成"只能人工"）。

    正确判据分层：

    1. `accepted == True` → 一定已加入；
    2. `accepted == False` → **不能直接判未加入**，要看比赛本身是否要求接受规则；
    3. 要求情况未知时退回 `list.userHasEntered`（加入这件事它一次就问清了）。

    `eventual_accepted` 是给调用方的"最终判断"：`True` 表示已加入（规则要么已接受、
    要么这场比赛不要求），`False` 表示确实没加入，`None` 表示探不出来。
    """
    r = site_api("/api/i/competitions.UserService/GetUser"
                 f"?competitionName={urllib.request.quote(slug)}")
    if not r["ok"]:
        return {"ok": False, "accepted": None, "eventual_accepted": None,
                "status": r["status"], "error": r["error"]}
    data = r["data"] or {}
    accepted = data.get("hasAcceptedRules")
    return {"ok": True,
            "accepted": bool(accepted) if accepted is not None else None,
            "eventual_accepted": None,
            "status": r["status"], "error": "",
            "raw": {"hasAcceptedRules": accepted,
                    "canUnregister": data.get("canUnregister")}}


def rules_required(slug: str) -> dict:
    """这场比赛**是否需要**接受规则 —— **已废弃，恒返回 None**。

    ## 为什么留着而不删

    这个探针按 rules 页里的文案判"是否需要接受规则"，实测**读不到**：
    `/competitions/<slug>/rules` 返回的是 5.6 KB 的 SPA 外壳（`#root` +
    内联脚本），`Rules acceptance is not required for this competition` 这条
    横幅是前端渲染出来的，纯 GET 抓不到（titanic / digit-recognizer 两场
    实测都是 `required=None`，而浏览器里明明有那行字）。

    保留函数是为了让旧调用点不会 AttributeError，同时明确它不提供信息 ——
    "是否加入"的判据只能靠 `entered_state()`（它用 `list.userHasEntered`）。
    """
    return {"ok": True, "required": None, "status": 200,
            "error": "rules 页是 SPA 外壳，文案由前端渲染，纯 GET 读不到；已废弃"}


def entered_state(slug: str) -> dict:
    """账号**是否已加入**这场比赛 —— 一手 → 二手 的分层判断。

    这是 `submit._check_entered` 与 `tools/run_real_competition.py --check`
    共用的唯一判据，避免两条路径各自解释字段（此前就因此出现过一次
    "check 说能跑、submit 却拦下"的分歧）。

    ## 一个实测把整套分层都推翻了：`hasAcceptedRules` 根本不是加入判据

    对 titanic / digit-recognizer / playground-series-s6e2 / spaceship-titanic
    四场比赛逐个查 `GetUser`，结果**全是** `hasAcceptedRules: false`：

    ```
    titanic                   → false   （但已加入、提交成功 publicScore=0.76076）
    digit-recognizer          → false   （提交成功后才 userHasEntered=true）
    playground-series-s6e2    → false
    spaceship-titanic         → false
    ```

    也就是说这个字段在当前账号/当前 API 上恒为 false，它**不携带"是否加入"
    的信息**，只可能携带"点过 Accept 按钮"这一件事 —— 而大量比赛根本不要求
    点那个按钮（rules 页写着 *Rules acceptance is not required*）。

    所以判据只保留能真正回答问题的那一个：**`list` 的 `userHasEntered`**。
    `hasAcceptedRules` 仍然回传（供诊断），但不再参与"是否已加入"的判断 ——
    拿恒为 false 的字段当门槛，是这一轮两个拦截误判的共同根源。

    返回 {entered, accepted, source, note, reason}；
    `entered` 为 None 表示拿不到（不是"未加入"）。
    """
    note = ""
    st = competition_user_state(slug)
    if not st.get("ok"):
        note = (f"[一手探针不可用：HTTP {st.get('status')} "
                f"{str(st.get('error',''))[:60]}；用公开列表判加入]")

    try:
        rows = list_competitions(slug, limit=10)
    except Exception as e:
        return {"entered": None, "accepted": st.get("accepted"), "source": "none",
                "note": note, "reason": f"查询失败：{str(e)[:120]}"}
    hit = [r for r in rows if slug in (r.get("ref") or "")]
    if not hit:
        return {"entered": None, "accepted": st.get("accepted"), "source": "none",
                "note": note, "reason": f"未在列表里找到 {slug}"}
    joined = bool(hit[0].get("userHasEntered"))
    if st.get("accepted") is False and joined:
        note = (note + " " if note else "") + (
            "[hasAcceptedRules=false 但 userHasEntered=true：该字段不反映加入状态"
            "（实测四场比赛恒为 false），已忽略")
    return {"entered": joined, "accepted": st.get("accepted"),
            "source": "userHasEntered", "note": note,
            "reason": "" if joined else f"userHasEntered=false（{hit[0].get('ref')}）"}


def run(args: list[str], timeout: int = 1800) -> str:
    cmd = [exe(), *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8",
                 # ── 绕过系统代理访问 Kaggle ────────────────────────────────
                 #
                 # 实测（本机 Windows + 系统代理 127.0.0.1:7890 已启用）：
                 #   TCP 到 api.kaggle.com:443 能连上（Test-NetConnection = True）
                 #   Python socket+ssl 直连 TLS 握手也成功（TLSv1.3）
                 #   但走 `requests`（读系统代理注册表）时：
                 #     SSL: UNEXPECTED_EOF_WHILE_READING
                 #   用 urllib 的 ProxyHandler({}) 绕过代理 → 正常
                 #
                 # 结论：该代理会破坏 Kaggle 的 TLS 握手。
                 # 清空 HTTP_PROXY/HTTPS_PROXY 环境变量**无效** ——
                 # requests 还会去读 Windows 注册表的 ProxyEnable/ProxyServer。
                 # 只有 NO_PROXY 能精确绕开它（实测：设置后 kaggle CLI 立刻返回 JSON）。
                 #
                 # 只对 kaggle 域名生效，不影响其它走代理的流量。
                 "NO_PROXY": "api.kaggle.com,www.kaggle.com,kaggle.com",
                 "no_proxy": "api.kaggle.com,www.kaggle.com,kaggle.com"},
        )
        # 注意 NO_PROXY 的 merge 行为：environ 里已有的 NO_PROXY 会被上面的键覆盖，
        # 这是有意的 —— 我们要保证 kaggle 域名一定在排除列表里。
        # 若用户原本有 NO_PROXY 且需要保留，应在此合并（当前场景没有）。
    except FileNotFoundError:
        raise KaggleError("找不到 kaggle 命令，先执行：pip install kaggle") from None
    except subprocess.TimeoutExpired:
        raise KaggleError(f"kaggle 调用超时 {timeout}s: {' '.join(args)}") from None
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise KaggleError(f"kaggle {' '.join(args)} 失败 (exit {proc.returncode})\n{out.strip()[:2000]}")
    return out


def list_competitions(search: str = "", limit: int = 20) -> list[dict]:
    # 注意：`-v` 与 `--format` 互斥。
    # 实测真实 API 才暴露：早期写成 `["--format","json","-v"]`，
    # Kaggle CLI 直接报 "argument -v/--csv: not allowed with argument --format"（exit 2）。
    # 只读探针测不到这个 —— 必须真的调一次外部命令。
    args = ["competitions", "list", "--format", "json"]
    if search:
        args += ["--search", search]
    if limit:
        args += ["--page-size", str(int(limit))]
    raw = run(args, timeout=120)

    # 输出形态实测有**两种**：
    #   1) 单个 JSON 数组（`--format json` 时就是这种）
    #   2) 每行一个 JSON 对象（某些版本/某些子命令如此）
    # 早期只处理了第 2 种（`line.startswith("{")`），于是 `--format json`
    # 的输出第一行是 `[`，整段被跳过 → 报"没有拿到比赛列表"。这只有真调一次 API 才发现。
    text = (raw or "").strip()
    rows: list[dict] = []
    if not text:
        return rows
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)][:limit]
        if isinstance(parsed, dict):
            return [parsed][:limit]
    except json.JSONDecodeError:
        pass    # 落到逐行解析

    for line in text.splitlines():
        line = line.strip().rstrip(",")     # 数组元素行尾会带逗号
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[:limit]


def download(slug: str) -> Path:
    """下载并解压到 data/<slug>/，返回目录。"""
    dest = DATA_DIR / slug
    dest.mkdir(parents=True, exist_ok=True)
    has_csv = len(list(dest.rglob("*.csv"))) > 0
    if not has_csv:
        try:
            run(["competitions", "download", "-c", slug, "-p", str(dest)], timeout=3600)
        except KaggleError as e:
            # 实测的真实错误：未接受规则时 Kaggle 返回
            #   403 Client Error: Forbidden for .../DownloadDataFiles
            #   You don't have access to this resource. ... accept its rules first
            #
            # 这不是程序错误，而是需要人去网页点一下的动作。
            # 把它翻译成「你能做什么」，而不是把 403 原文丢给用户。
            msg = str(e)
            if "403" in msg or "Forbidden" in msg or "accept its rules" in msg.lower():
                raise KaggleError(
                    f"这场比赛的规则还没接受，Kaggle 拒绝下载数据。\n"
                    f"  请打开 https://www.kaggle.com/competitions/{slug}/rules\n"
                    f"  点页面上的「I Understand and Accept」后重跑本条命令。\n"
                    f"  （这一步只能人工完成，API 无法代签）"
                ) from e
            raise
    for z in sorted(dest.rglob("*.zip")):
        with zipfile.ZipFile(z) as zf:
            zf.extractall(dest)
        z.unlink()
    csvs = sorted(dest.rglob("*.csv"))
    if not csvs:
        raise KaggleError(f"{dest} 里没有 csv，检查比赛是否需要先接受规则（Rules/Accept）")
    return dest


def submit(slug: str, file: Path, message: str, confirm: bool) -> str:
    """提交就是外部状态变更，没有 confirm 一律不执行。"""
    if not confirm:
        raise KaggleError("提交未确认：加 --yes 才会真正提交到 Kaggle。")
    if not file.exists():
        raise KaggleError(f"提交文件不存在: {file}")
    msg = message or "kauto auto-submit"
    return run(["competitions", "submit", "-c", slug, "-f", str(file), "-m", msg], timeout=600)


def _kaggle_opener():
    """Kaggle 域：**必须禁代理**。

    本机系统代理（127.0.0.1:7890）会破坏 api.kaggle.com 的 TLS 握手
    （SSL: UNEXPECTED_EOF_WHILE_READING）。清环境变量不够 —— requests/urllib
    会读注册表；`ProxyHandler({})` 是根治。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe_upload_proxy(candidates: list[str] | None = None,
                       *, timeout: float = 3.0) -> str | None:
    """找出一个**当前真的能用**的上传代理；都没有就返回 None。

    ## 判据是"能通 Google"，不是"端口在监听"

    端口在监听但出口规则没开（代理进程起来了却没走代理规则）同样不可用，
    所以必须先做一次 TCP 连通、再做一次真实的 HTTPS 请求。

    ## 为什么并行而不是串行

    实测：代理全关时串行探测 8 个候选要 **5.7 秒**（每次连接尝试都等到
    超时才进下一个），而这只是"报个错"的代价，用户会以为程序卡住。
    并行之后最坏耗时 = 单个候选的时间上限，不再随候选数线性增长。

    探测目标用 `generate_204`（极轻，无正文）。
    """
    cands = candidates if candidates is not None else UPLOAD_PROXY_CANDIDATES
    if not cands:
        return None

    def _try(cand: str) -> str | None:
        # ── 第一关：TCP 端口是否真的有人监听 ────────────────────────────
        try:
            u = urllib.parse.urlsplit(cand)
            host = u.hostname or "127.0.0.1"
            port = u.port or (443 if u.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=0.6):
                pass
        except Exception:
            return None                   # 死端口：立刻跳过，不付 TLS 成本
        # ── 第二关：通过它能否真的访问 Google 域 ─────────────────────────
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": cand, "https": cand}))
            req = urllib.request.Request("https://www.googleapis.com/generate_204")
            with opener.open(req, timeout=timeout) as r:
                if r.status in (204, 200):
                    return cand
        except Exception:
            return None
        return None

    # as_completed：谁先成功用谁；全部失败则整体耗时≈单个上限。
    with ThreadPoolExecutor(max_workers=min(len(cands), 8)) as pool:
        futures = {pool.submit(_try, c): c for c in cands}
        for fut in as_completed(futures):
            if fut.result() is not None:
                return fut.result()
    return None


def _upload_opener():
    """Google 存储域：**必须走代理**，且代理要现探测。

    与 Kaggle 域相反：上传的 createUrl 指向 www.googleapis.com，这台机器
    直连 Google 会永久挂起（实测：无超时、无错误，进程就一直躺在那里 ——
    这正是 `competitions submit` 卡死 600s 的真因，不是 Kaggle 拒绝，
    也不是配额问题）。

    ## 为什么不能写死一个地址

    曾经写死 `127.0.0.1:7890`。代理软件退出后端口无人监听，
    上传直接 `WinError 10061 目标计算机积极拒绝`；而错误文案只说
    "代理是否可用"，对着一个失效的硬编码地址，用户无从下手（真实事故）。

    现在由 `probe_upload_proxy()` 现场探测候选列表，取第一个能通 Google 的；
    一个都没有就明确抛 KaggleError，说明"这条链路上没有可用代理"，
    而不是让调用方去猜某个端口。可通过 KAUTO_UPLOAD_PROXY 指定首选。
    """
    cand = probe_upload_proxy()
    if cand is None:
        raise KaggleError(
            "上传需要经代理访问 Google 存储（www.googleapis.com），"
            "但当前没有任何可用代理通道。\n"
            f"  已探测候选：{', '.join(UPLOAD_PROXY_CANDIDATES)}\n"
            "  处理：启动你的代理软件（Clash/mihomo/v2ray 等），"
            "或用环境变量指定：KAUTO_UPLOAD_PROXY=http://127.0.0.1:<port>\n"
            "  （Kaggle 域本身仍直连，不受此影响；比赛下载/查询可正常进行）")
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": cand, "https": cand}))


def _post_json(url: str, payload: dict, token: str, opener, timeout: int = 40) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "Accept": "application/json"})
    with opener.open(req, timeout=timeout) as r:
        return {"status": r.status, "data": json.loads(r.read().decode("utf-8", "replace"))}


def direct_submit(slug: str, file: Path, message: str = "",
                  *, timeout: int = 40) -> dict:
    """提交文件到比赛 —— **三步直连 API**，不走 `kaggle competitions submit`。

    ## 为什么不能用 CLI

    实测（titanic，Windows）：`kaggle competitions submit` 会永久挂起，
    skill 桥接 600s 超时报"skill 调用超时"——看着像 Kaggle 侧拒绝或配额问题，
    其实是卡在上传那一步。逐层剥出来的证据：

    ```
    StartSubmissionUpload（kaggle 域）      → 200，1.1s，返回 createUrl
    PUT createUrl（禁用代理）               → 永久挂起（无超时、无错误）
    PUT createUrl（走 127.0.0.1:7890）      → 200，1.2s
    ```

    原因是两个域对网络的**要求相反**：Kaggle 域被系统代理破坏 TLS（坑 #1），
    而上传的 createUrl 落在 www.googleapis.com，本机只有走代理才通。
    一条 NO_PROXY 规则覆盖不了两者。

    ## 三步

    1. `StartSubmissionUpload`（api.kaggle.com，**禁代理**）→ 拿 `token`/`createUrl`；
    2. `PUT createUrl`（googleapis，**走代理**）→ 上传字节；
    3. `CreateSubmission`（api.kaggle.com，**禁代理**，带 `blobFileTokens`）→ 建提交。

    返回 {ok, ref, message, status, error}；ref 是 Kaggle 的提交号，用来对回执。
    """
    if not file.exists():
        raise KaggleError(f"提交文件不存在: {file}")
    tok = access_token()
    if not tok:
        raise KaggleError("无可用 access token（先 kaggle auth login）")

    data = file.read_bytes()
    stat = file.stat()
    try:
        start = _post_json(
            f"{API_BASE}/competitions.CompetitionApiService/StartSubmissionUpload",
            {"competitionName": slug, "fileName": file.name,
             "contentLength": len(data),
             "lastModifiedEpochSeconds": int(stat.st_mtime)},
            tok, _kaggle_opener(), timeout)
        created_url = start["data"]["createUrl"]
        blob_token = start["data"]["token"]
    except urllib.error.HTTPError as e:
        return {"ok": False, "ref": None, "status": e.code,
                "error": f"申请上传位失败：{e.read(300).decode('utf-8','replace')[:200]}"}
    except Exception as e:
        return {"ok": False, "ref": None, "status": None,
                "error": f"申请上传位失败：{type(e).__name__}: {str(e)[:160]}"}

    # ── 上传：一次重试，且**每次重试都重新探代理** ───────────────────────
    #
    # 重试的必要：这是唯一跨出 Kaggle 域的步骤（走 Google 存储），
    # 也是最容易被本机网络抖动击中的一步。实测代理重启期间第一次 PUT
    # 会 `WinError 10061`，而重新探测后立刻就能用。
    #
    # **重探而不复用 opener**：代理进程重启后端口可能换（7890→7897），
    # 拿着旧 opener 重试只会重复同一个失败。每次重试重新选通道，
    # 才和"代理可能已经恢复"这个真实情形对齐。
    last_err = ""
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                created_url, data=data, method="PUT",
                headers={"Content-Type": "text/csv", "Content-Length": str(len(data))})
            with _upload_opener().open(req, timeout=timeout) as r:
                if r.status not in (200, 201):
                    return {"ok": False, "ref": None, "status": r.status,
                            "error": f"上传返回 HTTP {r.status}"}
            last_err = ""
            break
        except KaggleError as e:
            # 没通道（probe 全失败）：重试也没用，直接给出可操作说明
            return {"ok": False, "ref": None, "status": None, "error": str(e)}
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:160]}"
            if attempt == 0:
                import time as _t
                _t.sleep(1.0)     # 给代理一点恢复时间，再探一次
                continue
    if last_err:
        return {"ok": False, "ref": None, "status": None,
                "error": f"上传失败（已重试 1 次）：{last_err}"}

    try:
        payload = {"competitionName": slug, "blobFileTokens": blob_token}
        if message:
            payload["submissionDescription"] = message
        done = _post_json(
            f"{API_BASE}/competitions.CompetitionApiService/CreateSubmission",
            payload, tok, _kaggle_opener(), timeout)
    except urllib.error.HTTPError as e:
        body = e.read(300).decode("utf-8", "replace")
        return {"ok": False, "ref": None, "status": e.code,
                "error": f"建提交失败：{body[:200]}"}
    except Exception as e:
        return {"ok": False, "ref": None, "status": None,
                "error": f"建提交失败：{type(e).__name__}: {str(e)[:160]}"}

    d = done["data"]
    return {"ok": True, "ref": d.get("ref"), "status": done["status"],
            "message": d.get("message", ""), "error": ""}


def submissions(slug: str) -> str:
    """列出自己的提交记录。

    实测踩到：写成 `["competitions","submissions","-c",slug,"-v"]` 会返回
    **400 Bad Request**（ListSubmissions）。根因是本子命令的 competition 是
    **位置参数**，没有 `-c` 选项 —— 传 `-c` 后 argparse 把 slug 当成了
    位置参数、`-c` 又成了非法选项，请求参数整个错位。

    正确写法：`competitions submissions <competition> --format json`

    ## 400 的第二种原因（服务端，不是参数）
    实测对照：同账号下 `submission-limits titanic` 和 `list -s titanic` 都正常，
    只有 `submissions` 返回 400。说明 `ListSubmissions` 对**未加入比赛的账号**
    用 400（而不是 403 或空列表）表达"你没有提交记录可列"。
    这不是调用方能修的，只能优雅降级：调用方据 `ok=False` + `reason` 判断。
    """
    try:
        return run(["competitions", "submissions", slug, "--format", "json"], timeout=120)
    except KaggleError as e:
        # 400 且原因指向 ListSubmissions → 大概率是"账号未加入/无提交记录"
        msg = str(e)
        if "400" in msg and "ListSubmissions" in msg:
            raise KaggleError(
                f"查不到 {slug} 的提交记录（Kaggle 返回 400 —— 通常是**账号尚未加入该比赛**，"
                f"或确实从未提交过）。先去 {slug} 的 rules 页点 Accept。\n原始错误：{msg[:300]}"
            ) from None
        raise


def wait_for_score(slug: str, *, timeout: int = 900, interval: int = 20,
                   expect_status: str | None = None) -> dict:
    """提交后轮询，直到出现分数或状态明确变化。

    ## 为什么需要

    参照 agentic-kaggle-skill 的 submission-endgame.md：
    「Continue working until one of these states is reached: scored / blocked」，
    并且「Poll or re-run `kaggle competitions submissions` until the submission is
    processed or a failure is visible」。

    早期实现提交完就返回，**拿不到分数**——于是"提交成功"和"提交被拒"在本地看
    起来完全一样。这是 endgame 里明确要求闭环的一步。

    返回 {status, score, rows, waited_seconds, poll_count}。
    status ∈ {"scored", "pending", "failed", "timeout"}
    """
    import time as _t

    t0 = _t.time()
    polls = 0
    last = None
    while _t.time() - t0 < timeout:
        polls += 1
        try:
            raw = submissions(slug)
        except KaggleError as e:
            # 403 之类是"外部阻塞"，不是暂时性失败，立刻返回。
            # 与 status() 保持一致：把原始英文 403 翻译成可操作动作，
            # 否则收据里存一段英文原文，事后看还是不知道该做什么。
            msg = str(e)
            if "403" in msg or "accept its rules" in msg.lower():
                reason = (f"规则未接受：请打开 "
                          f"https://www.kaggle.com/competitions/{slug}/rules "
                          f"点「I Understand and Accept」后重试（API 无法代签）")
            else:
                reason = msg[:400]
            return {"status": "blocked", "reason": reason,
                    "raw_error": msg[:600],
                    "poll_count": polls,
                    "waited_seconds": round(_t.time() - t0, 1)}
        rows = _parse_submissions_table(raw)
        last = rows
        if rows:
            top = rows[0]
            score_raw = top.get("publicScore") or top.get("public_score") or ""
            status_raw = (top.get("status") or "").strip()
            # Kaggle 处理中常见状态：pending / processing / 空
            if score_raw and score_raw not in {"", "-", "nan", "NaN"}:
                try:
                    score = float(score_raw)
                except ValueError:
                    score = None
                if score is not None:
                    return {"status": "scored", "score": score,
                            "submitted_at": top.get("date") or top.get("submittedAt"),
                            "file": top.get("fileName") or top.get("fileName"),
                            "rows": rows, "poll_count": polls,
                            "waited_seconds": round(_t.time() - t0, 1)}
            if expect_status and status_raw.lower() == expect_status.lower():
                return {"status": "matched", "desired": status_raw, "rows": rows,
                        "poll_count": polls, "waited_seconds": round(_t.time() - t0, 1)}
        _t.sleep(interval)

    return {"status": "timeout", "rows": last or [], "poll_count": polls,
            "waited_seconds": round(_t.time() - t0, 1),
            "hint": "Kaggle 仍在处理；稍后重跑 kauto status 或 kauto receipt 可再查"}


def _parse_submissions_table(raw: str) -> list[dict]:
    """解析 `kaggle competitions submissions` 的输出（JSON 优先，CSV 兜底）。

    ## 为什么两种格式都要认

    实测缺陷：这个函数最初是按 `-v`（CSV）写的，后来 `submissions()` 改成了
    `--format json`，**解析器没跟着改** —— 于是把 JSON 喂进去会切出 3 行乱码，
    `publicScore` 永远读不到，`wait_for_score` 会一直 pending 到超时。
    这个缺陷在 `blocked` 状态下看不出来（那时根本拿不到 200 响应）。

    JSON 是主路径（结构化、字段名稳定）；CSV 保留是因为 `-v` 在旧版 CLI 上是唯一出口。
    """
    import csv
    import io

    text = (raw or "").strip()
    if not text:
        return []

    # ── JSON 优先 ────────────────────────────────────────────────────────────
    # Kaggle 的 json 输出可能是一整个数组，也可能是逐行 JSON（line-JSON），
    # 两种都试。字段名统一成 wait_for_score 期望的那几个。
    try:
        import json as _json
        if text.startswith("["):
            data = _json.loads(text)
            rows = data if isinstance(data, list) else [data]
        else:
            rows = []
            for ln in text.splitlines():
                ln = ln.strip()
                if ln.startswith("{"):
                    rows.append(_json.loads(ln))
        if rows:
            out = []
            for r in rows:
                out.append({
                    "fileName": str(r.get("fileName") or r.get("file_name") or ""),
                    "date": str(r.get("date") or r.get("submittedAt") or ""),
                    "description": str(r.get("description") or ""),
                    "status": str(r.get("status") or ""),
                    "publicScore": str(r.get("publicScore") or r.get("public_score") or ""),
                    "privateScore": str(r.get("privateScore") or r.get("private_score") or ""),
                    "ref": str(r.get("ref") or ""),
                })
            return out
    except Exception:
        pass     # 不是 JSON，落到下面的 CSV 分支

    # ── CSV 兜底 ────────────────────────────────────────────────────────────
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines[:8]):
        low = ln.lower()
        if "," in ln and ("fileName".lower() in low or "publicscore" in low
                          or "date" in low or "status" in low):
            start = i
            break
    if start is None:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    out = []
    for r in reader:
        out.append({(k or "").strip(): (v or "").strip()
                    for k, v in r.items() if k})
    return out
