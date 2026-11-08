# dsh-yuan-kaggle-auto

把 Kaggle 竞赛的全流程交给 AI 的 DSH 插件：下载数据、切分验证集、跑基线、
LLM 迭代特征工程、融合、校验、提交、读回真实榜分。

装上、点一次初始化，之后对 AI 说"帮我打这个比赛"就够了。

---

## 它是什么

一个 **DSH 插件 + 内置 Python 引擎**的组合：

- **插件层**（TypeScript）：配置面板、23 个 `kaggle_*` 工具、状态探测、一键安装可选组件、HTTP 路由。
- **引擎层**（Python，随包分发）：真正干活的确定性流水线 —— 数据下载与切分、特征编码、交叉验证、LLM 候选生成与择优、融合、提交闭环。

两层分开是有意的：建模与 Kaggle 语义留在 Python（那里有完整的行为测试），
插件只负责装配与暴露。所以引擎的 66 项回归测试在插件包装之后依然有效。

## 它解决什么

Kaggle 自动化的难点不在"能不能训练一个模型"，而在这些环节：

| 环节 | 常见失败 | 本插件的做法 |
|---|---|---|
| 切分 | 泄漏导致 CV 虚高、榜分崩盘 | 结构性禁写（特征构建拿不到标签）+ 12 条静态泄漏审查 |
| 特征编码 | 高基数列被压成常数或噪声列 | 编码后退化判据 + 待预测侧覆盖率检查 |
| 提交 | 上传挂死 600 秒、配额白烧 | 三步直连 API、形状校验、双层确认门 |
| 状态判据 | 把"没点过 Accept"当成"没加入比赛" | 只用能回答问题的那一个字段 |
| 复现 | 分数与代码对不上 | 特征指纹 + 溯源 + 只增归档 |

## 安装

DSH 桌面端：**插件 → 添加插件**，输入 `dsh-yuan-kaggle-auto`，安装后启用。

命令行：

```bash
dsh plugin --profile <profile> add dsh-yuan-kaggle-auto@latest
```

装好后打开 **设置 → Kaggle 自动参赛**，点「初始化环境」。它会：

1. 找到（或提示你装）Python 3.10+；
2. 在 `<DSH_HOME>/kaggle-auto/venv` 建一个独立虚拟环境；
3. 安装 pandas / numpy / scikit-learn / lightgbm / kaggle；
4. 做一次导入冒烟。

这一步只需一次，之后命中标记即秒回。

## 三步上手

```
1. 设置页 → 点「初始化环境」           （一次，约数分钟）
2. 设置页 → 点「跑通演练」             （真跑一遍管线，不碰配额；约 5 秒）
   设置页 → 点「体检」                 （确认凭据 / CLI / LLM / ego 状态）
3. 对 AI 说：帮我打 titanic，跑两轮迭代，先别提交
```

AI 内部会调 `kaggle_run`，它按
`prepare → baseline → iterate → write-sub →（提交）→ receipt` 顺序执行。

## 工具

### 核心

| 工具 | 作用 |
|---|---|
| `kaggle_help` | 流水线索引：六阶段、最短路径、故障排查 |
| `kaggle_doctor` | 环境体检，逐项状态与修复建议 |
| `kaggle_setup` | 初始化引擎环境（幂等） |
| `kaggle_smoke` | 离线演练：真跑 prepare→baseline→write-sub→validate，不联网不消耗配额 |
| `kaggle_connect` | 检查 Kaggle 凭据与 LLM 通道（只读，不改凭据） |
| `kaggle_list` | 列可参赛比赛 |
| `kaggle_info` | 官方指标、每日限额、是否 notebook-only |
| `kaggle_status` | 自己在这场比赛里的排名与剩余次数 |
| `kaggle_leaderboard` | 榜首、我的名次、奖牌线 —— 判断"还值不值得再迭代"的依据 |
| `kaggle_rules` | 读官方评测页，把硬约束变成可执行判据（行数/列数/允许列名/值域） |
| `kaggle_prepare` | 下载 + 切分 + schema 推断 |
| `kaggle_baseline` | LightGBM 基线 + K 折 CV |
| `kaggle_iterate` | LLM 树搜索迭代特征工程 |
| `kaggle_write_sub` | 生成 submission.csv（预测陈旧时自动重训） |
| `kaggle_validate` | 形状校验（不评分，但能挡住白烧配额） |
| `kaggle_submit` | 提交（默认需要 `confirm: true`） |
| `kaggle_receipt` | 读回执与真实榜分 |
| `kaggle_run` | 一条命令跑完整链路 |

### 进阶

`kaggle_solutions`（公开解法检索）、`kaggle_blend`（多方案融合）、
`kaggle_archive`（节点归档）、`kaggle_restore`（回退）、
`kaggle_oof`（补 OOF）、`kaggle_prune`（清理中间产物）。

### 集成

`kaggle_ego_status`、`kaggle_ego_install`（可选组件状态与安装）。

## 配置项

| 项 | 默认 | 说明 |
|---|---|---|
| Python 解释器 | 留空 | 自动探测；也可填绝对路径 |
| 引擎环境目录 | 留空 | `<DSH_HOME>/kaggle-auto/venv` |
| 工作目录 | 留空 | `<DSH_HOME>/kaggle-auto/work`；可指向你已有的工作区 |
| Kaggle CLI | 留空 | 用引擎环境里安装的那个 |
| LLM 通道 | `auto` | 复用 DSH 已配置的通道，通常不需要填 key |
| LLM Base URL / 模型 / Key | 留空 | 留空沿用通道默认 |
| 上传代理 | 留空 | 只用于提交时访问 Google 存储；留空自动探测本机端口 |
| 线程数 | 1 | LightGBM 在部分机器上多线程会挂住 |
| 迭代轮数上限 | 4 | `kaggle_run` 的默认轮数 |
| 允许 agent 自行提交 | 关 | 只表示 agent 可以决定调用，不跳过确认 |
| 提交前需要确认 | 开 | 开启时提交必须带 `confirm: true` |
| 启用 ego 协助 | 开 | 需要点按钮的场景交给浏览器工具 |
| 候选代码沙箱 | 开 | 仅 Windows 生效 |
| skill 桥接目录 | 留空 | 可选；留空即禁用，核心流程不受影响 |

## 提交为什么需要两次确认

提交会消耗当日配额，且有的比赛**提交失败也扣次数**。所以留出两道门：

- `requireConfirm` 开启时，`kaggle_submit` / `kaggle_run --submit` 必须带 `confirm: true`，
  否则只返回"将提交什么"的摘要（含形状校验结果）；
- `autoSubmit` 只表示"agent 可以自己决定提交"，**不跳过 confirm**。

关闭 `requireConfirm` 之后仍需要 `confirm: true` —— 那一步是"我确实要花这次配额"。

### 官方格式约束会进本地校验

本地校验分两层：

1. **结构层**（永远可跑）：列名、行数、id 与 `sample_submission` 对齐（含前导零与精度）、
   重复 id、空值、取值分布。
2. **官方层**（装了 skill 桥接时可跑）：读比赛的**评测页原文**，把其中会导致提交被判废的
   硬约束抽成可执行判据 —— 例如 titanic 的
   "exactly 418 entries plus a header row … error if you have extra columns"。

第二层为什么必要：一次**报错的提交同样可能占用当日配额**。能在本地读到官方口径，
就把"会不会白烧一次提交"从猜测变成可判定。

`kaggle_rules` 可以在写 `write-sub` **之前**先查一遍规则，少一次返工。

## 可选组件

两者都**不是前置依赖**，缺了核心流程照常可用。

### ego 浏览器

装 `dsh-ego-browser` 之后，AI 在必须点按钮的场景（某些比赛要求在站内接受规则）
可以用 `ego_navigate` / `ego_click` 自己完成。面板上有「安装 ego」一键装。

不装也没问题：实测 **Kaggle 的提交动作本身会把账号加入比赛**，
所以绝大多数比赛不需要浏览器介入。

### skill 桥接

`shepsci/kaggle-skill` 提供更强的公开解法检索与提交台账。面板给出克隆命令，
填了目录才启用。缺失时 `solutions` 会说明原因，`validate` / `submit` 走引擎内置实现。

## 故障排查

| 现象 | 处理 |
|---|---|
| 提示"未初始化" | 点「初始化环境」，或调 `kaggle_setup` |
| 初始化报 pip 失败 | 看日志尾部的 pip 输出；本机需 `--proxy ""` 绕开系统代理（插件已默认加） |
| 提交报"上传通道未就绪" | Kaggle 上传落在 Google 存储，需要本地代理；在设置里填「上传代理」 |
| 提交 403 要求接受规则 | 装 ego 后由 AI 打开 rules 页点 Join；或手动点一次 |
| CV 高但榜分低 | 小数据集 CV 波动大（179 行验证集标准误约 3%），先看 `kaggle_status` 的历史提交再判断 |
| 迭代很慢 | 单轮 3–5 分钟属正常；`threads` 保持 1 |
| 找不到 Python | 装 3.10+ 后重试；或在设置里填绝对路径 |

## 数据与隐私

- Kaggle 凭据**只读不写**：插件读 `~/.kaggle/` 是否存在，不读取、不显示 token 内容。
- 比赛数据与运行产物都在你指定的工作目录，不进包、不上传。
- 截图/日志只留在本地；没有遥测。
- `kaggle_submit` 是唯一会改变外部状态的动作，且默认需要显式确认。

## 开发

```bash
pnpm install
pnpm typecheck        # tsc
pnpm test             # vitest（TS 层，231 项）
pnpm test:engine      # 引擎 Python 回归（76 项）
pnpm build            # lib/index.js + lib/client.js
pnpm smoke            # 用真实引擎跑 doctor + list
```

`engine/` 是随包分发的 Python 源码；`lib/` 是构建产物。
`pnpm test:engine` 会自动找一个**装了依赖**的解释器（venv 优先），
找不到就提示先初始化。

### 仓库卫生

两件事必须守住，`tests/repo-hygiene.test.ts` 会检查：

**真实比赛数据不进版本库。** `kauto prepare` 会把 Kaggle 下载的 zip 落在
`engine/` 根下，跑真实比赛时这些文件就出现在工作区里，
一次 `git add -A` 就会提交。这不只是仓库变大 —— 数据集各有自己的许可，
随代码再分发通常不允许；真实比赛数据动辄几百 MB，进历史后只能靠
filter-branch 清理；后来的人也容易把它当成夹具引用。

`.gitignore` 已挡住 `engine/*.zip` 与 `engine/*.csv`。
`engine/fixtures/` 里的是**合成**夹具，可以入库 —— 离线演练依赖它们。

**运行产物不入库。** `engine/work/` 与 `engine/data/` 的内容随每次运行变化，
已在 `.gitignore` 里整目录排除。

### 提交前

```bash
pnpm typecheck && pnpm test && pnpm test:engine && pnpm build
```

四项全过再提交。其中 `pnpm test` 里的 `install-shape.test.ts` 与
`pack-contents.test.ts` 会真的 `npm pack`，它们验证的是**装成包之后**
还对不对 —— 那是仓库内测试看着全绿、用户装上就坏的那类问题的唯一防线。

## 致谢与许可

本插件自身 MIT。

**内置引擎**源自本项目的 `kauto` 实现，随包分发；其中 Windows 沙箱部分使用
`kernel32` 的 Job Object 与 AppContainer API（纯 `ctypes`，无第三方原生依赖）。

**可选组件 `dsh-ego-browser`** 由 [Fisfzy](https://github.com/Fisfzy/dsh-ego-browser) 维护（MIT）。
本插件只做**代为安装**：不修改其代码、不改其配置、不对其行为负责。
安装动作会改动当前 profile 的 `package.json` 与装配层，失败会自动回滚。
它内部嵌入 ego-lite 的 MIT 代码（[CitroLabs/ego-lite](https://github.com/CitroLabs/ego-lite)），
Windows 下为社区移植，属实验性支持。

**可选组件 `kaggle-skill`** 由 [shepsci](https://github.com/shepsci/kaggle-skill) 维护。
本插件不携带它的代码，只在你指定目录时通过桥接调用。

Kaggle 是 Kaggle Inc. 的商标；本插件由社区维护，与 Kaggle 无隶属关系。
使用本插件进行比赛时，请遵守各比赛自身的规则与 Kaggle 的服务条款。


