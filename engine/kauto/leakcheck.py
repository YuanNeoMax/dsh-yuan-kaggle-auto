"""数据泄漏静态审查。

为什么需要这一层（对照 MLE-STAR 的 check_leakage 子 agent）：
泄漏方案的典型特征是 **CV 极高、LB 崩盘**，而分数本身看起来完全正常。
靠人看代码很难发现，所以那两个项目都专门加了审查环节。

我做过一个刻意的取舍：**不学它们用 LLM 审**。理由是本项目单次 LLM 调用
要 6 分钟，每轮都审一遍会把迭代成本翻倍；而泄漏的代码模式其实是有限的、
可以用静态规则覆盖大部分。LLM 审查更适合"看不懂的可疑代码"这种边缘场景，
留给 `--llm-leakcheck` 手动开启。

识别的都是真实高频的泄漏写法：
- 在 valid/test 上 fit 编码器、scaler、目标编码
- 全量数据先做 transform 再切分
- 用 test 的分布做特征（如 test 上算均值/频次）
- 在 valid 上早停或挑模型，然后又在 valid 上报分数
- 目标编码直接把 target 带进特征
"""
from __future__ import annotations

import re

# (规则名, 正则源码, 说明, 严重度)
# 严重度 high = 几乎肯定是泄漏；medium = 高度可疑，需要人看
#
# 注意：标志位统一在 compile 时用 re.IGNORECASE 传，不写内联 `(?i)` ——
# Python 要求内联全局标志必须位于表达式最开头，写在第二个分支会直接
# `re.error: global flags not at the start of the expression`（踩过）。
RULES = [
    (
        "fit_on_valid",
        r"(fit_transform|\.fit)\s*\(\s*(X_valid|X_test|valid|test|X_val|val_x|test_x)",
        "在 valid/test 上调用了 fit/fit_transform —— 编码器/scaler 会记住验证集分布",
        "high",
    ),
    (
        "fit_on_full_before_split",
        r"(fit_transform|\.fit)\s*\(\s*(X_all|X_full|full_data|df_all|whole_data|concat)",
        "在全量数据（含验证集）上 fit —— 典型的先 transform 再切分",
        "high",
    ),
    (
        "target_in_features",
        r"(map|apply|transform)[^.\n]{0,40}(y_train|y_valid|train\s*\[\s*['\"]target)",
        "用标签参与构造特征 —— 目标编码没有做折内/留出处理",
        "high",
    ),
    (
        "test_distribution_feature",
        r"(test\s*\.\s*(mean|median|value_counts|nunique))|(X_test\s*\.\s*(mean|median))",
        "用 test 的统计量做特征 —— test 信息泄漏进训练",
        "high",
    ),
    (
        "early_stop_on_valid",
        # 真实写法通常跨行：eval_set=[(X_valid, y_valid)] 与回调分开写，
        # 因此用 `[\s\S]`（跨行）而不是 `[^\n]`，并把 eval_set 和 early_stopping 各自算命中
        r"eval_set\s*=\s*\[?\s*\(?\s*(X_valid|valid|X_val)"
        r"|early_stopping[\s\S]{0,80}?(X_valid|valid|X_val)"
        r"|(X_valid|valid|X_val)[\s\S]{0,80}?early_stopping",
        "用验证集早停，随后又用同一验证集报分 —— 分数偏乐观",
        "medium",
    ),
    (
        "concat_before_split",
        r"pd\.concat\s*\(\s*\[\s*train\s*,\s*(valid|test)",
        "先 concat 全量再处理 —— 很可能在切分前引入了验证集/测试集信息",
        "high",
    ),
    (
        "branch_on_valid_distribution",
        # 实测的缺口：结构性禁写能防「读 valid 的 target」，
        # 防不了「看 valid 的特征分布再决定 train 怎么做」。
        # 这类代码不需要 target，所以 KeyError 机制完全拦不住 ——
        # 而它同样是泄漏：你在用验证集的信息构造训练特征，CV 会虚高。
        #
        # 判据：同一函数体内既出现了 valid 的聚合统计（mean/median/std/...），
        # 又出现了 if/while 之类的条件判断。宁可是 medium（让人看），
        # 也不漏 —— 漏掉的代价比误报大得多。
        # 关键细节：真实写法是 `valid["x1"].mean()` —— valid 与 .mean 之间
        # 隔着列索引。第一版正则写成 `valid\s*\.\s*mean`，完全匹配不上（实测踩到）。
        # 所以用 `(?:\[[^\]]*\]\s*)?` 允许可选的列索引。
        r"(valid|X_valid|X_val)\s*(?:\[[^\]]*\]\s*|\.\s*)?"
        r"(?:\.\s*)?(mean|median|std|min|max|quantile|describe|value_counts)"
        r"[\s\S]{0,300}?(if|while|elif)"
        r"|(if|while|elif)[\s\S]{0,300}?(valid|X_valid|X_val)\s*(?:\[[^\]]*\]\s*)?"
        r"(?:\s*\.\s*|\.\s*)(mean|median|std|min|max|quantile)",
        "用验证集的特征分布去影响训练流程（先看分布再决定特征/分支）—— "
        "不需要 target 也能泄漏，结构性禁写拦不住这类",
        # 提到 high：这不是"可疑"，是明确的泄漏。
        # 其他 medium（如"用 valid 早停"）至少还在用 valid 做正当用途（早停），
        # 而这条是**拿 valid 的信息去构造训练特征** —— CV 必然虚高，且无法自查。
        "high",
    ),
]

_COMPILED = [(name, re.compile(src, re.IGNORECASE), why, sev)
             for name, src, why, sev in RULES]


def scan(code: str) -> dict:
    """静态扫描候选代码，返回 {ok, findings}。

    ok=False 表示发现 high 级问题；调用方可以据此拒收该方案或强制修复。
    """
    findings = []
    for name, pattern, why, severity in _COMPILED:
        m = pattern.search(code)
        if not m:
            continue
        # 取命中的上下文片段，方便定位
        start = max(0, m.start() - 60)
        snippet = code[start:m.end() + 60].replace("\n", " ")
        snippet = " ".join(snippet.split())[:200]
        findings.append({"rule": name, "severity": severity, "why": why,
                         "snippet": snippet})

    # ── 变量追踪：把 valid 统计存进变量、再用它做分支 ──────────────────────
    #
    # 正则难覆盖这类（语句顺序自由：赋值 → 中间隔几行 → if 用那个变量）。
    # 实测踩到：`stats = valid[["x3"]].describe()` + `if stats.loc["mean"]... > 2.0`
    # 用反向引用写正则，表述不了"中间可以隔任意语句"。
    #
    # 改成两步：
    #   (1) 找出所有「变量 = valid/X_valid 的聚合统计」的赋值
    #   (2) 看这个变量是否出现在某个 if/while 的条件里
    import re as _re
    # 关键：列索引可能嵌套（`valid[["x3"]].describe()` 是双层方括号），
    # `[^\]]*` 会在第一个 `]` 就停 —— 实测匹配数 0。
    # 用非贪婪 `[[\s\S]*?]` 吃掉整个索引，再要求后面紧跟 `.方法`。
    _stats = _re.finditer(
        r"^(\s*)(\w+)\s*=\s*[^\n]*?(valid|X_valid|X_val)\s*"
        r"\[\[[\s\S]*?\]\]\s*\.\s*(mean|median|std|min|max|quantile|describe|value_counts)"
        r"|^(\s*)(\w+)\s*=\s*[^\n]*?(valid|X_valid|X_val)\s*"
        r"\.\s*(mean|median|std|min|max|quantile|describe|value_counts)",
        code, _re.MULTILINE | _re.IGNORECASE)
    for m in _stats:
        var = m.group(2) or m.group(6)
        # 在该赋值之后找 if/while 条件里是否用到这个变量
        tail = code[m.end():]
        cond = _re.search(r"(if|while|elif)\s+[^:\n]*\b" + _re.escape(var) + r"\b", tail)
        if cond:
            findings.append({
                "rule": "valid_stats_used_in_branch",
                "severity": "high",
                "why": "把验证集统计量存进变量、又用它决定训练流程 —— "
                       "不需要 target 也能泄漏，结构性禁写拦不住",
                "snippet": " ".join((m.group(0) + " ... " + cond.group(0)).split())[:200],
            })
            break

    has_high = any(f["severity"] == "high" for f in findings)
    return {"ok": not has_high, "findings": findings,
            "n_high": sum(1 for f in findings if f["severity"] == "high"),
            "n_medium": sum(1 for f in findings if f["severity"] == "medium")}


def describe(result: dict, limit: int = 3) -> str:
    """把扫描结果压成给 prompt 用的一行警告。"""
    if result["ok"] and not result["findings"]:
        return ""
    parts = []
    for f in result["findings"][:limit]:
        parts.append(f"[{f['severity']}] {f['why']}（片段：{f['snippet'][:90]}）")
    return "\n".join(parts)
