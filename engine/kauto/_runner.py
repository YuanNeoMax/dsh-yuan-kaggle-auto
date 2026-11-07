"""候选代码执行器：LLM 生成的 build_features / fit_predict 在独立子进程里跑。

为什么用子进程：候选代码可能死循环、可能把内存吃掉、可能 raise。放子进程里，
失败只丢一轮，主流程照常继续。

为什么写自包含脚本而不是 `python -m kauto._runner`：
`python -m` 会先导入 kauto 包，包里的 __init__ 已经把 _runner 放进 sys.modules，
runpy 于是不会重新执行模块体——表现为"子进程无输出"。改成直接跑一个生成的
独立脚本，绕开整个包导入，确定且可复现。
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pandas as pd

from . import appcontainer, storage, winjob
from .kapi import ROOT
from .model import encode_y, score

_RUNNER_SRC = '''\
"""候选代码执行器（由 kauto 自动生成，可删除）。

完全自包含：不 import kauto、不依赖项目路径。这样沙箱（见 _install_guard）
才是真的封住了 —— 否则候选代码可以顺着 `import kauto` 反向摸到包装载链。
"""
import json, os, sys

# 必须在 numpy/sklearn/lightgbm 之前钉死线程数。
# 本机 OpenMP 多线程会让 LightGBM 卡死（12 核 n_jobs 默认全开时无返回）。
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
# ── AppContainer 下不能创建命名管道 ────────────────────────────────────────
# 实测：真实 LLM 生成的代码写了 `n_jobs=-1`（sklearn 习惯写法），
# 在 AppContainer 里执行时报：
#   PermissionError: [WinError 5] 拒绝访问
#   _winapi.CreateNamedPipe (multiprocessing.connection.Pipe)
# 即 **沙箱禁止创建命名管道**，而 sklearn 的多进程后端要建 Pipe。
# 下面几行把作业库强制成单进程串行，避免候选代码踩这个坑。
os.environ["JOBLIB_MULTIPROCESSING"] = "0"

import numpy as np
import pandas as pd

GEN = {gen!r}
WORK = {work!r}


def _install_guard():
    """把 prompt 里的"硬约束"变成真的执行约束。

    实测（tools/test_sandbox.py）：不加这层时，候选代码可以随便写文件、
    起子进程、联网 —— 因为那些只是写在 prompt 里的口头约定，没有任何强制力。
    而同一进程既能读 ~/.dsh 里的凭据，又能写 work 目录，所以这不是理论风险。

    拦的是最危险的三类，而不是做一个完整沙箱（本机没有可用的容器/权限隔离）：
      1. 写文件 —— 只允许写自己那个 gen 子目录和 work 目录
      2. 起子进程 / os.system —— 直接拒绝
      3. 联网 —— 直接拒绝

    实现方式是替换标准库入口而不是改 AST：候选代码正常用 pandas/sklearn，
    它们不碰这些入口，所以对合法代码零影响；而任何试图越权的调用都会在
    调用点当场失败，并给出明确原因。
    """
    import builtins

    # ── 把 n_jobs 钉成 1 ────────────────────────────────────────────────────
    # 实测（真实 LLM 跑 titanic）：候选代码写了 `n_jobs=-1`（sklearn 的常见写法），
    # 在 AppContainer 下直接崩：
    #   PermissionError: [WinError 5] 拒绝访问
    #   _winapi.CreateNamedPipe (multiprocessing.connection.Pipe)
    # 沙箱禁止创建命名管道，而 sklearn 的多进程后端要建 Pipe。
    #
    # 单设 JOBLIB_MULTIPROCESSING=0 不够（sklearn 有自己的一条路径），
    # 所以在沙箱内直接**改写参数**：凡带 n_jobs 的调用一律钉成 1。
    # 对结果无影响（串行与并行的计算结果相同），只是慢一点。
    import functools

    def _pin_n_jobs(func):
        @functools.wraps(func)
        def wrapper(*a, **kw):
            if "n_jobs" in kw and kw["n_jobs"] not in (None, 1):
                kw["n_jobs"] = 1
            return func(*a, **kw)
        return wrapper

    try:
        import inspect as _insp
        for _modname in ("sklearn.ensemble", "sklearn.linear_model",
                         "sklearn.model_selection", "sklearn.svm",
                         "sklearn.tree", "sklearn.neighbors",
                         "sklearn.cluster", "sklearn.decomposition"):
            try:
                _m = __import__(_modname, fromlist=["*"])
            except Exception:
                continue
            for _nm in dir(_m):
                _obj = getattr(_m, _nm)
                if not _insp.isclass(_obj):
                    continue
                try:
                    _sig = _insp.signature(_obj.__init__)
                except (ValueError, TypeError):
                    continue
                if "n_jobs" in _sig.parameters:
                    try:
                        setattr(_m, _nm, _pin_n_jobs(_obj))
                    except Exception:
                        pass
    except Exception:
        pass

    allowed_prefixes = tuple(os.path.abspath(p) for p in (GEN, WORK))

    def _check_path(path):
        try:
            p = os.path.abspath(os.fspath(path))
        except Exception:
            return
        if not p.startswith(allowed_prefixes):
            raise PermissionError(
                "kauto 沙箱：只允许在 gen/ 与 work/ 目录内写文件，拒绝 " + p)

    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **kw):
        if any(ch in str(mode) for ch in ("w", "a", "x", "+")):
            _check_path(file)
        return real_open(file, mode, *a, **kw)

    builtins.open = guarded_open

    def _deny(what):
        def _f(*a, **kw):
            raise PermissionError("kauto 沙箱：候选代码不允许" + what)
        return _f

    import subprocess, socket

    # ⚠️ 不要整体替换 subprocess.Popen / subprocess.run。
    # 实测（tools/bisect_guard.py）：把 Popen 换成普通函数后，
    # `import lightgbm` 会在导入期直接崩：
    #     TypeError: function() argument 'code' must be code, not str
    # 因为 lightgbm 在导入时会把 Popen 当成类来 introspect。
    # 这会把**所有合法候选代码**判成 buggy —— 比不加沙箱更糟。
    #
    # 正确做法：保留原对象，只把 `__call__` 路径封住（下面用子类继承原类，
    # 保持 isinstance / 类属性全部可查），而对 os.system / os.popen
    # 这类纯函数入口直接替换是安全的（bisect 验证过 OK）。

    _RealPopen = subprocess.Popen

    class _GuardedPopen(_RealPopen):
        def __init__(self, *a, **kw):
            raise PermissionError("kauto 沙箱：候选代码不允许启动子进程")

    subprocess.Popen = _GuardedPopen
    subprocess.run = _deny("启动子进程")
    subprocess.call = _deny("启动子进程")
    subprocess.check_output = _deny("启动子进程")
    os.system = _deny("调用 os.system")
    os.popen = _deny("调用 os.popen")

    # 联网：只换 create_connection（bisect 验证过对 import 无影响）。
    # 不碰 socket.socket.connect —— 那是 C 层槽位，替换会打断 asyncio 的导入链。
    socket.create_connection = _deny("建立网络连接")


_install_guard()


def _pin_lgb_defaults():
    """LLM 生成的代码几乎不会写 n_jobs，而 LightGBM 默认吃满所有核心 → 卡死。
    这里直接改它构造函数的默认参数，让候选代码不写 n_jobs 也强制单线程。"""
    try:
        import functools
        import lightgbm as lgb
    except Exception:
        return
    for cls_name in ("LGBMClassifier", "LGBMRegressor", "LGBMRanker", "LGBMModel"):
        cls = getattr(lgb, cls_name, None)
        if cls is None:
            continue
        orig = cls.__init__

        @functools.wraps(orig)
        def patched(self, *a, __orig=orig, __cls=cls, **kw):
            kw.setdefault("n_jobs", 1)
            kw.setdefault("force_row_wise", True)
            return __orig(self, *a, **kw)

        cls.__init__ = patched


_pin_lgb_defaults()

ns = {{}}
src = open(GEN + "/candidate.py", encoding="utf-8").read()
exec(compile(src, "candidate.py", "exec"), ns)

for fn in ("build_features", "fit_predict"):
    if fn not in ns:
        print(json.dumps({{"ok": False, "error": "candidate.py 缺少 " + fn}}))
        raise SystemExit(0)

tr = pd.read_csv(WORK + "/train.csv", low_memory=False)
va = pd.read_csv(WORK + "/valid.csv", low_memory=False)
te = pd.read_csv(WORK + "/test.csv", low_memory=False)

# ── 结构性禁写：验证集标签在 build_features 阶段物理不可见 ──────────────────
#
# 为什么不是"加规则识别泄漏"：
# 实测正则能抓到的泄漏写法只是冰山一角 —— 五种常见写法漏掉四种，
# 连"把 valid 的 target 当特征塞进去"（CV 接近完美）都完全放过。
# 泄漏写法是开放集合，规则永远追不上。
#
# 所以改成**让它物理上做不到**：
#   build_features 收到的 valid/test 只含特征列（target 已被剥掉）
#   验证集标签通过独立通道直接交给 fit_predict
# 这样候选代码即使想读 fva["target"] 也读不到 —— KeyError 而不是静默泄漏。
#
# 兼容性：fit_predict 的签名不变（仍然收 y_valid），
# 只是 build_features 返回的 y_valid 被忽略，改用执行器侧的权威标签。
TARGET = {target!r}

def _split_label(df, name):
    """把标签从 DataFrame 里剥离，返回 (features_df, y)。"""
    if TARGET not in df.columns:
        raise RuntimeError(name + " 缺少标签列 " + TARGET)
    y = df[TARGET].reset_index(drop=True)
    return df.drop(columns=[TARGET]).reset_index(drop=True), y

tr_feat, y_tr_full = _split_label(tr, "train")
va_feat, y_va_full = _split_label(va, "valid")

# K 折 OOF ──────────────────────────────────────────────────────────────────
FOLDS = {folds!r}
if FOLDS and FOLDS > 1:
    full_feat = pd.concat([tr_feat, va_feat], ignore_index=True)
    y_full = pd.concat([y_tr_full, y_va_full], ignore_index=True)
    n = len(full_feat)
    rng = np.random.default_rng(42)
    if y_full.dtype.kind in "iu" and y_full.nunique() <= 20:
        idx = np.arange(n)
        fold_of = np.empty(n, dtype=int)
        for cls in y_full.unique():
            pos = idx[(y_full.values == cls)]
            rng.shuffle(pos)
            fold_of[pos] = np.arange(len(pos)) % FOLDS
    else:
        fold_of = rng.permutation(n) % FOLDS

    oof = np.full(n, np.nan, dtype=float)
    test_folds = []
    for k in range(FOLDS):
        in_fold = fold_of == k
        ftr = full_feat.loc[~in_fold].reset_index(drop=True)
        fva = full_feat.loc[in_fold].reset_index(drop=True)
        ftr_y = y_full.loc[~in_fold].reset_index(drop=True)
        fva_y = y_full.loc[in_fold].reset_index(drop=True)
        # 注意：fva 里没有 target 列；标签只能从 fva_y 走
        Xf, Xv, Xt, _ignored_yf, _ignored_yv = ns["build_features"](ftr, fva, te)
        pv, pt = ns["fit_predict"](Xf, ftr_y, Xv, fva_y, Xt)
        pv = np.asarray(pv).ravel()
        if pv.shape[0] != len(fva):
            raise RuntimeError("fold " + str(k) + " 预测行数 " + str(pv.shape[0]) +
                               " 与折内 " + str(len(fva)) + " 不符")
        oof[np.where(in_fold)[0]] = pv
        test_folds.append(np.asarray(pt).ravel())

    if np.isnan(oof).any():
        raise RuntimeError("OOF 有未覆盖的样本")

    tp = np.mean(test_folds, axis=0)
    Xtr, Xva, Xte, _y1, _y2 = ns["build_features"](tr_feat, va_feat, te)
    vp, _tp_ignored = ns["fit_predict"](Xtr, y_tr_full, Xva, y_va_full, Xte)
else:
    Xtr, Xva, Xte, _y1, _y2 = ns["build_features"](tr_feat, va_feat, te)
    vp, tp = ns["fit_predict"](Xtr, y_tr_full, Xva, y_va_full, Xte)
    oof = None

vp = np.asarray(vp)
tp = np.asarray(tp)

# ── 行数契约校验 ────────────────────────────────────────────────────────────
#
# 实测过的缺陷：候选把 test_pred 写成 250 行（实际 test 有 400 行），
# 执行器仍然返回 ok=True —— 这个坏产物会一路流到 write-sub，
# 生成一份 250 行的 submission，直到提交时才被 Kaggle 拒。
#
# 而 valid_pred 长度错时虽然评分阶段会报错，但错误信息是
# "评分失败：valid_pred 行数 100 与验证集 240 不符" ——
# 看起来像评分器的问题，不像候选违反了契约，会把人往错方向带。
#
# 所以在源头明确报出"你返回的行数不对"，并给出两边实际值。
class _ContractError(Exception):
    pass


def _check_rows(name, arr, expect):
    got = int(np.asarray(arr).shape[0]) if np.asarray(arr).ndim else 1
    if got != expect:
        raise _ContractError(
            name + " 行数 " + str(got) + " 与期望 " + str(expect) + " 不符"
            "（应当为每个样本返回一个预测值；多分类返回 (n, K) 概率矩阵，"
            "不要返回展平后的多维数组）")


try:
    _check_rows("valid_pred", vp, len(va_feat))
    _check_rows("test_pred", tp, len(te))
    if oof is not None:
        _check_rows("oof", oof, len(full_feat))
except _ContractError as _e:
    # 契约错误要**干净报出**：不带 traceback（那是给写代码的人看的噪音），
    # 只说明"哪里不对、两边实际值多少"，直接可行动。
    print(json.dumps({{"ok": False, "error": str(_e)}}))
    raise SystemExit(0)

# 自包含写盘：不 import kauto（那会拉起整个包装载，在沙箱里会失败）。
def _save(path, arr, expect_varied=True):
    with open(path, "wb") as f:
        np.save(f, arr)
    back = np.load(path)
    if back.shape != arr.shape:
        raise RuntimeError("npy 形状回读不一致：" + path)
    if arr.size and not np.allclose(back.astype(float), arr.astype(float),
                                    equal_nan=True, rtol=0, atol=1e-12):
        raise RuntimeError("npy 内容回读不一致（疑似被清零）：" + path)
    # 语义校验：实测出现过预测全为 0.0000 却"写盘完全成功"的产物，
    # 它会让 blend 拟合出一个毫无意义的权重（单模 logloss 高达 7.28）。
    # 退化预测必须当场失败，而不是留给下游发现。
    if expect_varied and arr.size > 1:
        flat = arr.astype(float).ravel()
        flat = flat[np.isfinite(flat)]
        if flat.size > 1 and float(np.ptp(flat)) < 1e-9:
            raise RuntimeError(
                "预测退化（全为常数 " + str(float(flat.flat[0])) + "）：" + path +
                " —— 模型没有学到东西，检查 build_features/fit_predict 的返回值")


_save(GEN + "/valid_pred.npy", vp)
_save(GEN + "/test_pred.npy", tp)
if oof is not None:
    _save(GEN + "/oof.npy", oof)
print(json.dumps({{"ok": True, "n_valid": int(vp.shape[0]), "n_test": int(tp.shape[0]),
                  "oof": oof is not None, "folds": FOLDS}}))
'''


def run(work: Path, code: str, timeout: int = 900, tag: str | None = None,
        folds: int = 0, winjob_limits: dict | None = None,
        container: str | None = "kauto.sandbox") -> dict:
    """在子进程里跑候选代码。

    work 永远是**数据集目录**（train/valid/test.csv 在那里）；
    tag 只决定候选产物写到哪个子目录 —— 并发跑多个候选时，
    每个候选必须有自己的 candidate.py / *.npy，否则互相覆盖。

    folds>1 时执行器会在外层跑 K 折，额外产出 `oof.npy`（折外预测）。
    这是拟合 blend 权重的正确依据：用留出集搜权重会拟合噪声。
    代价是候选的耗时变成约 folds 倍 —— 所以默认 0（不跑 OOF），
    只在需要融合时（`--folds 5`）才开。
    """
    work = Path(work).resolve()
    gen = work / "gen" if not tag else work / f"gen_{tag}"
    gen.mkdir(parents=True, exist_ok=True)
    storage.write_text(gen / "candidate.py", code)
    script = (gen / "_run.py").resolve()
    # target 名从 meta.json 读：结构性禁写需要知道哪一列是标签。
    # 读不到就退回 "target" 并让执行器自己报缺列（比猜错列名更安全）。
    tgt = "target"
    mp = work / "meta.json"
    if mp.exists():
        try:
            import json as _json
            tgt = _json.loads(mp.read_text(encoding="utf-8")).get("target") or "target"
        except Exception:
            pass
    storage.write_text(script, _RUNNER_SRC.format(gen=str(gen), work=str(work),
                                                  folds=int(folds), target=tgt))

    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
           "OMP_NUM_THREADS": "1",
           # 从子进程侧再断一次联网：环境变量挡不住已 import 的库，
           # 但能挡住任何遵守代理设置的新连接（requests/urllib 都看它）
           "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1"}

    # ── AppContainer：文件系统级隔离（可绕不过）───────────────────────────
    #
    # 这是"根治"的那一层。Python 层的 open/Popen 守卫可被 ctypes 绕过（实测证实），
    # Job Object 只管资源不管文件。AppContainer 是 Windows 安全边界：
    # 容器内的进程默认**看不到**容器外的文件，靠 OS 检查访问，与代码怎么写无关。
    #
    # 实测（tools/test_appcontainer.py）：
    #   读 ~/.dsh/.credentials.yaml → DENIED
    #   读已授权的 work 目录        → READABLE
    #   numpy/pandas/sklearn/lightgbm 在容器内正常 import
    #
    # 注意 available() 返回 False 时**不能静默跳过**：
    # 早期写法 `if container and appcontainer.available()["ok"]:` 在不可用时
    # 什么都不做，直接走下面的无隔离分支 —— 于是"以为跑在沙箱里"和
    # "其实没有文件隔离"两件事完全无法区分。这和下面那条"拒绝降级"自相矛盾。
    if container:
        _av = appcontainer.available()
        if not _av["ok"]:
            return {"ok": False,
                    "error": (f"AppContainer 不可用（拒绝降级到无文件隔离）：{_av.get('reason')}"
                              "\n  可用 kauto doctor 查看隔离层状态；"
                              "或以 container=None 显式放弃文件隔离（不推荐）")}
    if container and appcontainer.available()["ok"]:
        try:
            # 只授权"必须给的最小面"。
            # 解释器/stdlib/site-packages 给只读（RX）—— 它不该被改。
            # 候选自己的产物目录给读写（M）—— 它必须能写 candidate.py 与 *.npy。
            # 凭据目录、用户目录一律不授权 —— 那正是要挡住的东西。
            import sysconfig
            base_root = Path(sys.base_prefix)
            for d in (base_root, base_root / "DLLs", base_root / "Lib",
                      Path(sys.prefix), Path(sysconfig.get_paths()["purelib"]), work):
                appcontainer.grant_read(str(d), container)
            appcontainer.grant_readwrite(str(gen), container)
            res = appcontainer.run_in_container(
                [sys.executable, str(script)],
                cwd=str(gen), name=container, timeout=timeout)
            if not res.get("ok"):
                raise RuntimeError(res.get("error") or "AppContainer 启动失败")
            out = res.get("stdout") or ""
            err = res.get("stderr") or ""
            rc = res.get("rc")
            lines = [l for l in out.strip().splitlines() if l.strip().startswith("{")]
            if not lines:
                return {"ok": False,
                        "error": f"容器内无 JSON 输出，rc={rc} stdout={out[-600:]!r} "
                                 f"stderr={err[-600:]!r}"}
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError:
                return {"ok": False, "error": f"输出无法解析: {lines[-1][:800]}"}
        except Exception as e:
            # 隔离失败不静默降级：降级到无文件隔离等于悄悄放弃了安全性，
            # 用户会以为代码跑在沙箱里。宁可失败并明示原因。
            return {"ok": False, "error": f"AppContainer 隔离失败（拒绝降级）："
                                          f"{type(e).__name__}: {str(e)[:300]}"}

    # 用 Popen 而不是 subprocess.run：需要拿到 pid 后**在它干活之前**塞进 Job，
    # run() 会阻塞到进程结束，那时再 assign 已经没有意义。
    job = None
    try:
        p = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", cwd=str(gen), env=env,
        )
    except OSError as e:
        return {"ok": False, "error": f"启动子进程失败: {e}"}

    # Windows 原生资源限制。失败不阻塞执行 —— 隔离层是加固，不是前提。
    if winjob.available()["ok"] and winjob_limits:
        try:
            job = winjob.create_job(memory_mb=winjob_limits.get("memory_mb", 4096),
                                   active_processes=winjob_limits.get(
                                       "active_processes", 8),
                                   cpu_seconds=winjob_limits.get("cpu_seconds", 1800))
            winjob.assign(job, p.pid)
        except Exception:
            job = None    # 拿不到就退化为"仅超时保护"，不冒失败整个候选的风险

    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            out, err = p.communicate(timeout=10)
        except Exception:
            out, err = "", ""
        # 超时有两种原因，不能一概当成"候选代码有问题"：
        #   1) 代码真的有死循环/极慢  → 该判失败
        #   2) 机器被别的任务占满      → 重跑大概率就过
        # 实测：同一份代码空闲时 39.6s 跑完，CPU 满载时套件里见过 522s。
        # 所以错误信息里带上"可能是机器负载"，并给可行动建议。
        return {"ok": False,
                "error": f"候选代码超时 {timeout}s（如刚有其它重任务并行，可能是机器负载而非代码问题；"
                         f"可用 --folds-for-oof 之外的参数或重跑确认）",
                "timeout": timeout}
    finally:
        if job is not None:
            try:
                winjob.close(job)   # KILL_ON_JOB_CLOSE 会顺带清掉残留子进程
            except Exception:
                pass

    if p.returncode != 0:
        return {"ok": False, "error": (err or out or "")[-2500:]}
    lines = [l for l in (out or "").strip().splitlines() if l.strip().startswith("{")]
    if not lines:
        return {"ok": False, "error": f"子进程无 JSON 输出，stdout={out[-800:]!r} stderr={err[-800:]!r}"}
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return {"ok": False, "error": f"输出无法解析: {lines[-1][:1000]}"}


def evaluate(work: Path, meta: dict, metric: str, tag: str | None = None) -> float:
    work = Path(work)
    gen = work / "gen" if not tag else work / f"gen_{tag}"
    va = pd.read_csv(work / "valid.csv", low_memory=False)
    y = va[meta["target"]]
    vp = storage.load_npy(gen / "valid_pred.npy").ravel()

    if meta["task"] == "classification":
        tr = pd.read_csv(work / "train.csv", usecols=[meta["target"]], low_memory=False)
        _, _, cats = encode_y(tr[meta["target"]], y)
        mapping = {c: i for i, c in enumerate(cats)}
        y = y.map(mapping)
        # 指标吃标签（accuracy/f1）时把预测取整；吃概率（auc/logloss）时保留原值。
        # 旧代码用 `metric not in {auc,logloss}` 判断，f1/accuracy 会因为
        # 概率值全部落在 [0,1] 里被误当作类别下标而取整成 0，等于把预测全废掉。
        if vp.dtype != object and metric not in {"auc", "logloss", "map"}:
            vp = np.rint(vp)
    # 形状自检：防止 LLM 交回 n×K 概率矩阵导致静默错评
    if vp.shape[0] != len(y):
        raise ValueError(f"valid_pred 行数 {vp.shape[0]} 与验证集 {len(y)} 不符")
    return float(score(metric, np.asarray(y), vp))
