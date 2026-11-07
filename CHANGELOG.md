# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/)；版本语义遵循 [SemVer](https://semver.org/)。

## [Unreleased]

### 修复

- **`kaggle_run` / `kaggle_iterate` 的轮数被静默夹紧**：`maxRounds` 上限存在是对的
  （否则模型能要求跑 50 轮、白烧一小时算力），错的是夹紧**不说出来**。
  引擎跑 4 轮、调用方要求 10 轮，返回里一个字没提 —— 一个以为自己迭代了
  10 次的模型，会基于它从未收集过的证据下结论。现在返回 `rounds.requested` /
  `rounds.effective` 与一句点名两个数字的 `roundsNote`，并说明怎么调高上限。
  新增 `planRounds()` 统一处理（`run` 允许 0 轮 = 只跑基线，`iterate` 最小 1 轮）。

### 优化

- **排行榜缓存**：实测单次 **15.7 秒**（跨网往返 + 快照落盘），而面板上
  「查排行」是可重复的只读操作，反复点就是反复等十几秒。加按 slug 的缓存后
  实测 **17.59s → 1.81s**（约 10 倍）。TTL 默认 3 分钟（榜单会随别人提交变化，
  所以比 writeup 的 6 小时短得多），可用 `KAUTO_LEADERBOARD_TTL` 覆盖。

  - 命中缓存时结果带 `cached` 与 `cached_age_seconds`，并在摘要里注明
    「缓存：N 秒前取的」—— 否则用户会把几分钟前的名次当成实时值；
  - `--refresh` / `refresh: true` 可绕过（刚提交完要看新名次）；
  - 提交成功后自动作废该 slug 的缓存（best-effort，刷新失败不影响提交结果）；
  - 缓存损坏或写盘失败都**走网络**而不是报错 —— 缓存是加速手段，不是正确性前提。

- **工具层与引擎 CLI 参数一致性**：新增 `test_tool_cli_parity.py` 静态核对
  `cli.py` 的 argparse 与 `tools.ts` 的 argv 构造。它抓出两类真实缺陷：
  `kaggle_prepare` 传引擎不存在的 `--force`、`kaggle_archive` 传不存在的
  `--node`（两者带参数调用必然 exit 2）；以及 `kaggle_oof` 缺 `--node`
  （不带它引擎会遍历所有归档节点，每个都是完整 K 折重训）、
  `kaggle_blend` 缺 `--top-k`。

### 新增

- **`kauto smoke` / `kaggle_smoke` / 面板「跑通演练」**：用包内夹具真跑一遍
  prepare → baseline → write-sub → validate。真实读 csv、真实训练 LightGBM、
  真实写 submission、真实跑形状校验，**不联网、不提交、不消耗 Kaggle 配额**。
  报告明确列出未覆盖的三项（凭据有效性 / 上传通道可达性 / 规则是否接受），
  避免一次全绿被误读成"连 Kaggle 都通了"。

  此前面板上那个「跑冒烟」按钮只调体检函数 —— 名字叫冒烟，一个建模步骤都没跑。

- **`smoke` 的夹具定位修复**：夹具在 `engine/fixtures/`，属包内只读资源，
  不随可写根移动。首版用 `kapi.ROOT` 定位，而宿主与测试都会把
  `KAUTO_ROOT` 指到别处，于是"找夹具"跑去临时目录里找、一律报缺失。
  改为按模块位置定位，并在失败时列出包内可用夹具。

## [0.2.0] - 2026-11-08

### 新增

- **`kaggle_rules` / `kauto rules`**：读比赛官方页面（评测页、规则页），
  把其中的硬约束抽成**可执行判据**（行数 / 列数 / 允许列名 / 值域）。
  写 `write-sub` 前先查，少一次返工。
- **`kaggle_leaderboard` / `kauto leaderboard`**：榜首、我的名次、奖牌线。
  这是判断"还值不值得继续迭代"的唯一依据 —— 奖牌线只有排行榜接口给。
- **面板「查排行」**：填 slug 即查，不需要轮询（一次网络往返，
  套上轮询反而让面板显得迟钝）。
- **截止门**：提交前检查比赛是否还收文件。`submissionsDisabled=true` 硬拦；
  仅截止已过给 warning（有的比赛接受迟到提交）；读不到不拦
  并提示"查不到 ≠ 已结束"。
- **`docs/ARCH.md`**：架构与维护指南，含引擎侧改动清单、5 个踩过的坑、
  "想加东西改哪里"对照表。

### 修复

- **`_run` 在桥接目录不存在时崩溃**：`cwd` 原为硬编码 `SKILL_ROOT`，
  未安装 skill 时子进程抛 `NotADirectoryError(WinError 267)` ——
  用户看到的是"引擎崩了"，而不是"这个可选组件没装"。
- **两条 `validate` 路径判据不一致**：装了桥接与没装桥接会给同一文件
  不同结论，且桥接路径反而丢了本地几条更强的检查（前导零比对、重复 id、
  取值分布）。现在本地结构校验始终跑，skill 复核作为补充，任一侧报错都拦。
- **凭据双视图**：skill 脚本只认 `KAGGLE_API_TOKEN` / `~/.kaggle/access_token`，
  不认 OAuth 的 `~/.kaggle/credentials.json`，而 CLI 认 OAuth。
  新增 `_run(extra_env=...)` 把 OAuth token **只交给那一个子进程**
  （不改本进程环境、不落盘）。

### 测试

- TypeScript **57 项**（新增真实 spawn 的 `runner-integration`，
  断言 `kauto.__file__` 落在本包内 —— 直指"不会加载别人的引擎"这条性质本身）。
- 引擎 **69 项**（新增 `test_official_rules`、`test_leaderboard_bridge`、
  `test_deadline_gate`）。

## [0.1.0] - 2026-11-08

首次发布。把一个本地 CLI 流水线封装成可安装的 DSH 插件。

### 新增

- **23 个 `kaggle_*` 工具**：环境体检与初始化、比赛列表与事实、数据准备与基线、
  LLM 迭代、写提交与形状校验、提交与回执、一条命令跑完整链路，以及融合/归档/
  回退/清理六项进阶能力。
- **设置面板**「Kaggle 自动参赛」：状态盘（9 项逐条结论 + 修复建议）、六个一键动作、
  16 个可配置项、实时日志抽屉。所有图标为手绘 SVG，无 emoji。
- **一键初始化**：自动探测 Python 3.10+，建立独立 venv，安装引擎依赖，
  以「标记文件」记录引擎版本与依赖指纹，命中即秒回。
- **一键安装 ego**：把可选组件 `dsh-ego-browser` 装进当前 profile 并热装配，
  失败按写入顺序逆序回滚。
- **引擎随包分发**：`engine/` 携带 Python 源码与 66 项回归测试，首次初始化即完整可用。

### 设计决定（都来自实测，写进代码注释）

- **提交双层门**：`requireConfirm` 与 `confirm: true` 缺一不可；`autoSubmit`
  只表示允许 agent 决定调用，不跳过确认。
- **上传走代理、Kaggle 域禁代理**：提交上传落在 Google 存储
  （`www.googleapis.com`），与 `api.kaggle.com` 的网络要求相反，
  一条 NO_PROXY 覆盖不了两者；代理地址现场探测，不写死端口。
- **预测与特征集绑定**：`write-sub` 比对特征指纹，预测陈旧即拒绝复用并全量重训，
  避免"重跑基线却提交旧预测"的静默错误。
- **参赛状态只提示不拦截**：只用能真正回答"是否加入"的字段；
  实测提交动作本身会加入比赛，因此不做前置拦截。
- **引擎可写根目录可注入**（`KAUTO_ROOT`）：装进 npm 包后 `__file__` 位于
  `node_modules`，若仍按推导写盘，升级即丢数据、只读安装直接失败。
- **子进程 cwd 不落在工作目录**：`python -m` 会把 cwd 放在 `sys.path[0]`，
  工作目录若在另一个 checkout 里，会静默加载那一份引擎。

### 引擎侧改动（相对本地 CLI 版本，共 4 处）

1. `KAUTO_ROOT` / `KAUTO_SKILL_ROOT` 覆盖可写根与桥接目录。
2. `skillbridge.try_or_none()`：桥接缺失时六条调用路径降级而非抛异常。
3. `list --format json`：宿主需要结构化结果；默认仍是 TSV。
4. `run_all_tests.py` 自适应 `tests/` 与 `tools/` 两种布局；
   面向用户工作区的断言在插件包里以 SKIP（退出码 77）退出，不记成失败。

### 测试

- TypeScript：43 项（配置容错、提交门、输出解析、作业板、路由守卫、安装规格）。
- Python：66 项全绿（其中 4 项因插件包不带用户工作区产物而正确 SKIP）。



