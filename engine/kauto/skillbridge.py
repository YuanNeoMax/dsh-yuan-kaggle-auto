"""把 shepsci/kaggle-skill 的脚本桥接进来，作为"读情报 / 校验 / 提交"这一层的执行者。

分工（组合路线的核心）：
- kauto 自己负责：下载、切分、编码、CV、迭代、生成 submission
- skill 负责：比赛事实（指标/限额/是否 notebook-only）、榜单 writeup、提交前形状校验、真提交+台账

关键价值：**官方指标**。kauto 原本靠标签分布猜指标（二分类不平衡就猜 AUC），
一旦猜错，CV 把 logloss 优化到很低、榜单却按 accuracy 排，模型越训越亏。
skill 的 `brief --json` 直接给出 metric 字段，改成以它为准。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import storage

CACHE_TTL = 6 * 3600

ROOT = Path(__file__).resolve().parents[1]

# kaggle-skill 的可选桥接：目录由 `KAUTO_SKILL_ROOT` 指定（宿主插件把它映射到
# 用户选择的 checkout，通常是 <DSH_HOME>/kaggle-auto/kaggle-skill）。
#
# 插件包**不携带** vendor/kaggle-skill：那是 5 MB 的上游克隆，自带 .git，
# 塞进 npm 包既臃肿又会让嵌套仓库被当成子模块。缺失时 `available()` 返回
# `vendored: False`，六条调用路径各自降级（见 cli.py / submit.py 的 try_or_none
# 用法），核心流程（下载/建模/提交）完全不依赖它。
_SKILL_ROOT_ENV = os.environ.get("KAUTO_SKILL_ROOT", "").strip()
SKILL_ROOT = Path(_SKILL_ROOT_ENV).expanduser().resolve() if _SKILL_ROOT_ENV else ROOT / "vendor" / "kaggle-skill"
SKILL_ENTRY = SKILL_ROOT / "skills" / "kaggle" / "scripts" / "kaggle_skill.py"
COMP_SCRIPTS = SKILL_ROOT / "skills" / "kaggle" / "modules" / "competitions" / "scripts"
DISC_SCRIPTS = SKILL_ROOT / "skills" / "kaggle" / "modules" / "discussions" / "scripts"

_UNTRUSTED = re.compile(r"<untrusted-content[^>]*>|</untrusted-content[^>]*>")


class SkillError(RuntimeError):
    pass


def available() -> dict:
    ok = SKILL_ENTRY.exists()
    return {
        "vendored": ok,
        "entry": str(SKILL_ENTRY) if ok else "",
        "hint": "" if ok else (
            f"未安装 kaggle-skill 桥接（可选）。核心流程不依赖它；"
            f"需要时执行：git clone --depth 1 https://github.com/shepsci/kaggle-skill "
            f"\"{SKILL_ROOT}\""
        ),
    }


def try_or_none(fn, *args, **kwargs):
    """调用桥接函数；未安装或执行失败时返回 (None, 原因) 而不是抛异常。

    ## 这是插件化的必要一层

    作为独立 CLI 时，`skillbridge` 缺失属于"环境没配好"，让它抛 SkillError
    由 cli 顶层捕获、打印安装指引是合理的。但装进 DSH 插件后，vendor
    目录**默认就不在**（5 MB 上游克隆，不随 npm 包分发），此时六条调用路径
    （profile / solutions / status / validate / submit / run）如果继续抛异常，
    用户看到的是「插件坏了」而不是「这个可选组件没装」。

    所以统一收敛到这里：
      - 桥接不可用 → 立刻返回 (None, 一行可操作说明)，由调用方降级；
      - 桥接可用但执行失败 → 同样返回 (None, 错误摘要)，调用方决定是否致命。

    调用方一律按 `value is None` 判断，不要用 `available()` 预判 ——
    预判和实际执行之间存在竞态（用户可能中途删掉目录）。
    """
    if not SKILL_ENTRY.exists():
        return None, (
            "未安装 kaggle-skill 桥接（可选，核心流程不需要它）。"
            f"需要时把仓库克隆到：{SKILL_ROOT}"
        )
    try:
        return fn(*args, **kwargs), ""
    except SkillError as error:
        return None, f"{error}"[:400]
    except Exception as error:                     # 桥接脚本自身的任何故障
        return None, f"{type(error).__name__}: {str(error)[:360]}"



def _run(cmd: list[str], timeout: int = 300) -> tuple[str, str, int]:
    # 与 kapi.run 同一个坑：清空 HTTP(S)_PROXY **不够** ——
    # requests 还会读 Windows 注册表的 ProxyEnable/ProxyServer，
    # 而本机那个系统代理（7890）会破坏 Kaggle 的 TLS 握手
    # （SSL: UNEXPECTED_EOF_WHILE_READING）。
    # 只有 NO_PROXY 能精确绕过它（实测：设置后 skill 脚本立刻能拉到 writeup）。
    _no_proxy = "api.kaggle.com,www.kaggle.com,kaggle.com"
    # skill 脚本内部用 `kaggle_cli.installed()` → `shutil.which("kaggle")` 检测 CLI。
    # 而它在 `SKILL_ROOT` 目录下运行，且**继承的 PATH 里没有 venv 的 Scripts** ——
    # 实测 `shutil.which("kaggle")` 返回 None，于是脚本报 "missing_package: kaggle"
    # 并以 exit 127 退出（提交根本没发生，但错误信息看起来像已经提交了）。
    # 修法：把当前解释器所在的 Scripts 目录前置到 PATH。
    _bin = Path(sys.executable).parent
    _path = os.environ.get("PATH", "")
    _new_path = f"{_bin}{os.pathsep}{_path}" if str(_bin) not in _path else _path
    env = {**os.environ, "PYTHONIOENCODING": "utf-8",
           "HTTP_PROXY": "", "HTTPS_PROXY": "",
           "NO_PROXY": _no_proxy, "no_proxy": _no_proxy,
           "PATH": _new_path}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", env=env, cwd=str(SKILL_ROOT))
    except FileNotFoundError as e:
        raise SkillError(f"无法执行：{e}") from None
    except subprocess.TimeoutExpired:
        raise SkillError(f"skill 调用超时 {timeout}s：{' '.join(cmd[-3:])}") from None
    return _UNTRUSTED.sub("", p.stdout or ""), _UNTRUSTED.sub("", p.stderr or ""), p.returncode


def _json_tail(text: str):
    """skill 输出外面包了 untrusted-content 标签，剥掉后取 JSON。"""
    text = text.strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0] or [-1])
    if start < 0:
        return None
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
    return None


def brief(slug: str) -> dict:
    """比赛事实：指标、限额、是否 notebook-only、页面清单。"""
    out, err, rc = _run([sys.executable, str(SKILL_ENTRY), "brief", slug, "--json"])
    data = _json_tail(out)
    if rc != 0 or data is None:
        raise SkillError(f"brief {slug} 失败 (exit {rc})\n{(err or out).strip()[:1200]}")
    return data


def solutions(slug: str, top: int = 10, preview: bool = True,
              fallback: bool = False, timeout: int = 90,
              refresh: bool = False) -> list[dict]:
    """按名次列的解法 writeup。无凭据也能读公开榜单。

    注意 `--fallback-search`：榜单没挂 writeup 时它会去 Kaggle 全文检索，
    实测一次 26s 且常常一无所获（titanic 这种老比赛就是空的）。
    所以默认关掉它——迭代 prompt 里塞一堆空情报没有意义，还拖慢每轮。

    ## 加缓存的原因

    实测：这个调用要 **7s 以上**（跨网络），而榜单 writeup 是**静态内容**，
    同一场比赛几小时内不会变。每次 iterate 都重新拉一遍纯属浪费。
    缓存到 `work/<slug>/writeups_cache.json`，超过 `TTL` 才重拉。
    """
    import time as _t

    cache_p = _cache_path(slug)
    if not refresh and cache_p.exists():
        try:
            c = json.loads(cache_p.read_text(encoding="utf-8"))
            age = _t.time() - float(c.get("fetched_at", 0))
            if age < CACHE_TTL and c.get("writeups"):
                return c["writeups"]
        except Exception:
            pass    # 缓存坏了就走网络

    if not DISC_SCRIPTS.exists():
        raise SkillError("缺 discussions 模块")
    cmd = [sys.executable, str(DISC_SCRIPTS / "leaderboard_writeups.py"), slug,
           "--top", str(top), "--json"]
    if preview:
        cmd.append("--preview")
    if fallback:
        cmd.append("--fallback-search")

    out, err, rc = _run(cmd, timeout=timeout)
    data = _json_tail(out)
    if rc != 0 or data is None:
        msg = (err or out or "").strip()
        # 网络抖动（SSL EOF / 超时）是可重试的，提示出来而不是让人以为是比赛问题
        transient = any(k in msg for k in
                        ("SSLEOFError", "Timeout", "timed out", "Connection", "Temporary failure"))
        # 实测教训：本机系统代理会破坏 Kaggle 的 TLS 握手，排查花了几十分钟。
        # 错误信息当时只说 SSLEOFError，完全没提示"代理"这个方向。
        # 这里把可能的根因写进去，下次能少绕。
        hint = ("\n  （网络层错误。若本机启用了系统代理，常见原因是代理破坏了 Kaggle 的 TLS 握手：\n"
                "    代码里已注入 NO_PROXY=api.kaggle.com,www.kaggle.com,kaggle.com 绕过；\n"
                "    若仍失败，检查代理是否支持该域名的 HTTPS。也可稍后重试：kauto solutions <slug> --refresh）"
                if transient else "")
        raise SkillError(f"solutions {slug} 失败 (exit {rc})\n{msg[:1000]}{hint}")

    rows = data.get("writeups") if isinstance(data, dict) else data
    rows = rows or []

    # 只缓存有内容的；空结果不缓存，否则"没 writeup"会被冻结 TTL 那么久
    if rows:
        try:
            cache_p.parent.mkdir(parents=True, exist_ok=True)
            storage.write_json(cache_p, {"fetched_at": _t.time(), "slug": slug,
                                         "writeups": rows})
        except Exception:
            pass
    return rows


def _cache_path(slug: str) -> Path:
    from . import kapi
    return kapi.WORK_DIR / slug / "writeups_cache.json"


def validate(sub_file: Path, sample: Path | None = None, slug: str | None = None) -> dict:
    """提交前形状校验（不评分，只查列/行/id/空值/有限性）。

    skill 的这个脚本把 competition 当成第一个位置参数、file 当成第二个，
    只传 file 会被当成 competition 然后报 "missing argument: file"。
    所以 slug 必须给；给了 --sample 时它不会去 Kaggle 下载样例。
    """
    script = COMP_SCRIPTS / "competition_validate.py"
    if not script.exists():
        raise SkillError("缺 competitions 模块")
    if not slug:
        # 没给真实 slug 时用一个占位名：只做本地文件比对，不联网
        slug = "local"
    cmd = [sys.executable, str(script), slug, str(sub_file)]
    if sample:
        cmd += ["--sample", str(Path(sample).resolve())]
    out, err, rc = _run(cmd, timeout=120)
    text = (out or err).strip()
    passed = "Every check passed" in text or "No check failed" in text
    return {"passed": bool(passed) and rc == 0, "exit": rc, "output": text[-2000:]}


def submit(sub_file: Path, slug: str, message: str, confirm: bool,
           expect: float | None = None, sample: Path | None = None) -> dict:
    """真提交。

    ## 第 61 轮：优先走 `kapi.direct_submit()`，skill 脚本只作退路

    实测（titanic，Windows）：skill 的 `competition_submit.py` → `kaggle
    competitions submit` 会**永久挂起**，600s 超时后报"skill 调用超时"。
    真因不在 skill 也不在 Kaggle：CLI 的可续传上传落在 www.googleapis.com，
    而 kauto 为了绕开"系统代理破坏 Kaggle TLS"把代理全局禁掉了 ——
    两个域对网络的要相反，一条 NO_PROXY 覆盖不了。

    `kapi.direct_submit()` 把提交拆成三步、每步用对的网络路径（详见其
    docstring），1~2 秒完成，还绕开了 CLI 的可续传实现。所以这里先走它。

    保留 skill 路径的理由：它带完整的 dry-run / 台账记录，某些比赛
    （notebook-only、需要 --version）只有它支持。直连失败时退回它，
    并在输出里把两条路径的失败原因都留住，避免只剩一句"超时"。
    """
    from . import kapi

    if not confirm:
        # 不确认时不做任何外部动作，也不跑 skill（它会打印计划但仍是本地行为）。
        pass

    direct = None
    if confirm:
        try:
            direct = kapi.direct_submit(slug, Path(sub_file), message)
        except Exception as e:                       # 网络/凭据层失败 → 退回 skill
            direct = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        if direct.get("ok"):
            ref = direct.get("ref")
            lines = [f"Submitted to {slug} (submission {ref})."
                     if ref else f"Submitted to {slug}.",
                     direct.get("message") or "",
                     "路径：kapi.direct_submit（三步直连，上传走代理）"]
            if expect is not None:
                lines.append(f"expected={expect:g}")
            lines.append(f"Wait for the score with: kauto receipt {slug} --wait")
            return {"confirmed": True, "exit": 0,
                    "output": "\n".join(x for x in lines if x)[-4000:],
                    "ref": ref, "via": "direct_api"}

    script = COMP_SCRIPTS / "competition_submit.py"
    if not script.exists():
        raise SkillError("缺 competitions 模块")
    # `_run` 把 cwd 设成 SKILL_ROOT —— 于是**相对路径**的 sub_file 在那边找不到。
    # 实测：传 `work/titanic/submission.csv` 时脚本报 "submission file was not found"，
    # 而文件明明存在。必须转成绝对路径再传（同一个坑可能也影响 sample）。
    sub_file = Path(sub_file).resolve()
    cmd = [sys.executable, str(script), slug, str(sub_file), "-m", message]
    if expect is not None:
        cmd += ["--expect", str(expect)]
    if sample:
        cmd += ["--sample", str(Path(sample).resolve())]
    if confirm:
        cmd.append("--yes")
    out, err, rc = _run(cmd, timeout=600)
    text = (out or err).strip()
    if direct is not None and not direct.get("ok"):
        text = (f"[直连提交失败，已退回 skill 脚本]\n"
                f"  direct: {str(direct.get('error'))[:300]}\n" + text)
    return {"confirmed": confirm, "exit": rc, "output": text[-4000:],
            "via": "skill_script"}


def status(slug: str, limit: int = 5) -> dict:
    """自己在比赛里的位置：剩余时间/剩余次数/最近分数（需凭据）。

    实测发现的不一致：skill 脚本有自己的凭据查找逻辑，只认
    `KAGGLE_API_TOKEN` 环境变量和 `~/.kaggle/access_token` 文件，
    **不认 OAuth 的 `~/.kaggle/credentials.json`**。
    于是出现「kaggle CLI 能正常用（OAuth 已登录），但 skill 脚本报
    no Kaggle credential was found」——同一台机器上两套凭据视图。

    所以这里**优先走 CLI**（它认 OAuth），拿不到再退回 skill 脚本。
    """
    from . import kapi

    # 首选：CLI 原生路径，与 `kaggle` 命令用同一套凭据，不会出现两套视图
    cli_err = None
    try:
        raw = kapi.submissions(slug)
        rows = _parse_submissions(raw)
        return {"slug": slug, "source": "kaggle-cli",
                "submissions": rows[:limit], "count": len(rows)}
    except Exception as e:
        cli_err = str(e)

    # CLI 也失败时，先判断是不是"规则未接受"——那不是程序问题，是待办动作
    if cli_err and ("403" in cli_err or "accept its rules" in cli_err.lower()):
        raise SkillError(
            f"读取 {slug} 的提交状态失败：规则未接受。\n"
            f"  打开 https://www.kaggle.com/competitions/{slug}/rules 点接受后再试。\n"
            f"  （比赛未参与时无法查询提交记录，这是 Kaggle 的限制）")

    script = COMP_SCRIPTS / "competition_status.py"
    if not script.exists():
        raise SkillError("缺 competitions 模块")
    out, err, rc = _run([sys.executable, str(script), slug,
                         "--limit", str(limit), "--json"])
    data = _json_tail(out)
    if rc != 0 or data is None:
        msg = (err or out).strip()
        if "no Kaggle credential" in msg.lower():
            raise SkillError(
                f"读取 {slug} 的提交状态失败：skill 脚本找不到凭据。\n"
                f"  已知情况：kaggle CLI 用 OAuth（~/.kaggle/credentials.json）已登录，\n"
                f"  但 skill 脚本只认 KAGGLE_API_TOKEN 或 ~/.kaggle/access_token。\n"
                f"  这是两套凭据视图，属已知不一致。")
        raise SkillError(f"status {slug} 失败 (exit {rc})\n{msg[:1200]}")
    return data


def _parse_submissions(raw: str) -> list[dict]:
    """解析 `kaggle competitions submissions -v` 的输出（CSV 表头 + 数据行）。"""
    import csv
    import io

    text = (raw or "").strip()
    if not text:
        return []
    lines = text.splitlines()
    header_idx = None
    for i, ln in enumerate(lines[:5]):
        if "," in ln and ("fileName" in ln or "date" in ln or "publicScore" in ln):
            header_idx = i
            break
    if header_idx is None:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    rows = []
    for r in reader:
        rows.append({k: (v or "").strip() for k, v in r.items() if k})
    return rows


# ---------------------------------------------------------------- 指标映射

# Kaggle 的 metric 名字五花八门，这里枚举到够用为止；认不出的原样回传，由调用方报错。
_METRIC_TABLE = [
    (r"categorization\s*accuracy|multi\s*class\s*accuracy|^accuracy", "accuracy"),
    (r"roc\s*auc|area\s*under\s*the\s*roc|auc", "auc"),
    (r"root\s*mean\s*squared\s*logarithmic|rmsle", "rmsle"),
    (r"root\s*mean\s*squared\s*error|rmse", "rmse"),
    (r"mean\s*absolute\s*error|mae", "mae"),
    (r"mean\s*absolute\s*percentage|mape", "mape"),
    (r"log\s*loss|logloss|cross.?entropy", "logloss"),
    (r"f1\s*score|macro\s*f1|micro\s*f1", "f1"),
    (r"mean\s*average\s*precision|^map$", "map"),
    (r"r\s*squared|r2|coefficient\s*of\s*determination", "r2"),
]


def normalize_metric(raw: str | None) -> tuple[str, bool] | None:
    """返回 (kauto 指标名, 是否越大越好)；认不出返回 None。"""
    if not raw:
        return None
    low = raw.lower()
    for pattern, key in _METRIC_TABLE:
        if re.search(pattern, low):
            return key, key in {"accuracy", "auc", "f1", "map", "r2"}
    return None


def profile(slug: str) -> dict:
    """把 skill 的比赛事实整理成 kauto 直接可用的形状。"""
    d = brief(slug)
    raw_metric = d.get("metric")
    norm = normalize_metric(raw_metric)
    return {
        "slug": d.get("slug") or slug,
        "title": d.get("title"),
        "url": d.get("url"),
        "raw_metric": raw_metric,
        "kauto_metric": norm[0] if norm else None,
        "higher_is_better": norm[1] if norm else None,
        "metric_recognized": norm is not None,
        "notebook_only": bool(d.get("notebook_only")),
        "max_daily_submissions": d.get("max_daily_submissions"),
        "team_count": d.get("team_count"),
        "deadline": d.get("deadline"),
        "submissions_disabled": bool(d.get("submissions_disabled")),
        "tags": d.get("tags") or [],
        "evaluation": (d.get("evaluation") or "")[:800],
    }
