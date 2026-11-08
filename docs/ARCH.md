# 架构与维护指南

> 这个文档的唯一目的：让下一个改代码的人（包括未来的自己）在动刀之前先看懂
> 结构，知道「改哪里、怎么改、别踩什么坑」。

## 0. 一句话

一个 DSH 插件：**宿主层只做装配**（面板、工具、路由、可选项安装），
**所有 Kaggle 与建模语义留在随包分发的 Python 引擎里**。

这个切分不是审美选择，是为了让引擎的 66 项行为测试在插件包装之后依然有效 ——
如果业务逻辑长到 TypeScript 里，那些测试就只能测到一个壳。

## 1. 目录

```
dsh-yuan-kaggle-auto/
  src/
    index.ts            插件入口：装配 tools / routes / prompt
    config.ts           Config schema（schemastery）+ resolveConfig（容错读）
    paths.ts            路径解析：DSH_HOME / venv / workRoot / engineDir
    python.ts           解释器探测 + venv 生命周期 + 流式 spawn
    setup.ts            installEngine（建 venv）与 readStatus（体检）
    runner.ts           引擎调用：拼 argv/env → spawn → 三档解析输出
    tools.ts            23 个 kaggle_* 工具 + 提交双层门
    installer.ts        一键安装可选组件 + 逆序回滚
    routes.ts           面板的 9 条 HTTP 路由 + dsh-auth 守卫
    jobs.ts             作业日志环形缓冲（面板轮询）
    prompt.ts           模型可见的能力说明
    client/
      index.tsx         设置面板（React，无 emoji）
      icons.ts          8 个手绘 SVG 图标（currentColor 遮罩）
  engine/
    kauto/              引擎源码（24 个模块）—— 唯一业务逻辑所在
    tests/              72 个测试脚本（66 项注册进 run_all_tests）
    tools/              测试依赖的开发脚本
    fixtures/           合成夹具（demo-comp / dirty-comp / lz-comp）
    work/               夹具的已准备产物（测试需要）
    requirements.lock.txt
    run_all_tests.py
  lib/                  构建产物（index.js + client.js），不入库
  scripts/              开发脚本（跑引擎测试 / 冒烟）
  tests/                TypeScript 测试（vitest，43 项）
  docs/                 本文件
```

## 2. 两条边界

### 2.1 宿主 / 引擎

| 归属 | 内容 | 判据 |
|---|---|---|
| 宿主（TS） | 配置解析、路径、spawn、输出解析、面板、路由、安装器 | 与 Kaggle 语义无关的**装配**工作 |
| 引擎（Py） | 下载、切分、编码、CV、LLM 迭代、融合、校验、提交 | 任何"比赛知识" |

判断一个新功能该放哪：**问它需不需要被 Python 测试覆盖。** 需要就进 `engine/`。

### 2.2 可写 / 只读

- `engine/` 是**只读**的源码（随 npm 包分发）。
- 所有可写内容在 `KAUTO_ROOT`（默认 `<DSH_HOME>/kaggle-auto/`），由
  `engineEnv()` 注入；引擎侧只有 `kapi.py` 一处读它：

  ```python
  _ENGINE_ROOT_ENV = os.environ.get("KAUTO_ROOT", "").strip()
  ROOT = Path(_ENGINE_ROOT_ENV).expanduser().resolve() if _ENGINE_ROOT_ENV \
         else Path(__file__).resolve().parents[1]
  ```

**为什么必须这样**：装进 npm 包后 `__file__` 在 `node_modules` 里，按路径推导
会把 `data/` `work/` 写进 node_modules —— npm 升级即丢数据，只读安装直接失败。

## 3. 一次调用的完整链路

```
模型调 kaggle_iterate { slug: 'titanic', rounds: 3 }
  └─ tools.ts: invoke()
       ├─ deps.python()            python.ts: 显式 → env → venv → PATH（含依赖探测）
       ├─ deps.acquire(slug)      jobs.ts: 同 slug 串行，不同 slug 并发
       ├─ runner.ts: engineEnv()  注入 KAUTO_ROOT / THREADS / LLM / PROXY
       ├─ runner.ts: spawn        cwd = engineDir（不是 workRoot！见 §6）
       └─ runner.ts: lastJsonValue()  三档解析
              ├─ 整段 JSON.parse
              ├─ 从尾部找最后一个平衡 {} / []
              └─ 都失败 → 结构化错误（带 stderr 尾三行）
  └─ 引擎: python -m kauto.cli iterate titanic --rounds 3
       └─ 打印 JSON 到 stdout，人类进度行到 stdout 前部
```

**输出解析为什么需要三档**：引擎是纯 CLI，没有哨兵行；不同子命令的输出形态不同
（`doctor` 是纯 JSON、`baseline` 先打进度行再打 JSON、`validate` 是人读文本）。
`tests/runner-parse.test.ts` 用真实形态覆盖了这四类。

## 4. 引擎侧改动清单（相对本地 CLI 版，共 11 处）

每处都有对应测试，改坏了会被抓住。

| # | 改动 | 文件 | 为什么 | 测试 |
|---|---|---|---|---|
| 1 | `KAUTO_ROOT` 覆盖 | `kapi.py` | 可写根不能落在 node_modules | `test_workdir_relocation` |
| 2 | `KAUTO_SKILL_ROOT` 覆盖 | `skillbridge.py` | 桥接目录由用户选，不随包带 | `test_submit_paths` |
| 3 | `try_or_none()` | `skillbridge.py` | 六条调用路径在无桥接时降级 | `test_accept_rules_probe` |
| 4 | `list --format json` | `cli.py` | 宿主需要结构化结果 | `test_tool_cli_parity` + smoke |
| 5 | 官方评测页 → 可执行判据 | `skillbridge.py` + `subcheck.py` + `cli.py` | 评测页常写本地看不到的硬约束，而废提交照样耗配额 | `test_official_rules` |
| 6 | `run_all_tests.py` 自适应布局 | `run_all_tests.py` | tests 可能在 `tests/` 或 `tools/` | 套件本身 |
| 7 | `load_work()` 校正 `work_dir` | `prep.py` | meta.json 存的是**打包者机器**的绝对路径；不校正则包内 5 个夹具在别人机器上全失效，且 `KAUTO_ROOT` 重定向失效会让测试污染真实工作区 | `test_workdir_relocation` |
| 8 | `leaderboard` 子命令 + 奖牌线 | `skillbridge.py` + `cli.py` | `status` 答不了"离奖牌还差多少"，而那是决定要不要继续迭代的唯一依据 | `test_leaderboard_bridge` |
| 9 | `smoke` 子命令（离线演练） | `cli.py` | 面板那个"跑冒烟"按钮原先只调体检，一个建模步骤都没跑 | `test_smoke_command` |
| 10 | 排行榜缓存 + `--refresh` | `skillbridge.py` + `cli.py` | 实测单次 15.7 秒，而面板"查排行"是可重复的只读操作 | `test_leaderboard_cache` |
| 11 | `_deadline_gate` + `run` 感知截止 | `submit.py` + `cli.py` | 比赛结束后提交被拒的错误信息会把人引向"改文件重试"，而真正原因是这个比赛不收了 | `test_deadline_gate` |

### 改引擎前必须知道的两条

**可写根只能来自 `KAUTO_ROOT`。** 引擎里任何"按 `__file__` 推导目录"的地方，
装进 npm 包之后都指向 `node_modules` —— 升级即丢数据、只读安装直接失败。
目前只有 `kapi.py` 一处推导，其余模块都从它导入。

**包内只读资源不能跟着可写根走。** `fixtures/`、`requirements.lock.txt` 属包内，
用 `Path(__file__).parent.parent` 定位；`work/` `data/` `reports/` 属可写，
用 `kapi.ROOT`。混用会导致"设了 `KAUTO_ROOT` 就找不到夹具"（`smoke` 首版就这样）。

## 5. 提交门（两道，缺一不可）

```
kaggle_submit { slug, confirm }        tools.ts: submitGate()
  ├─ requireConfirm=true 且 confirm≠true  → 不追加 --yes，只跑 validate
  ├─ requireConfirm=false 且 confirm≠true → 同样不追加（没说过"要花配额"）
  └─ confirm=true                        → 追加 --yes，引擎真提交
```

`autoSubmit` 只表示"agent 可以自己决定调 submit"，**从不放宽 confirm**。
`tests/submit-gate.test.ts` 用 6 个用例钉住，包括 `autoSubmit=true` 仍被拦。

引擎侧还有自己的门（`_pre_submit_gate` 溯源指纹、形状校验、官方规则），
外壳不越权、也不替它做决定。

## 5.1 两类静默失败（本项目最贵的教训）

这几轮修掉的缺陷大多是同一类：**代码能跑、测试能过，但用户得到的信息是错的**。
按可见度分成两种，处理方式完全不同：

**可见的失败**会自动暴露，代价低：

- 工具传了引擎不认的选项 → `unrecognized arguments`，exit 2；
- 提交前形状校验不过 → 报错并拒绝提交。

**静默的失败**才是真问题，它们不产生任何错误信息：

| 缺陷 | 表现 | 为什么难发现 |
|---|---|---|
| 轮数被夹紧不说明 | 要求 10 轮、实际跑 4 轮 | 返回里一切正常，只是少做了事 |
| 工具少暴露关键选项 | 用户不知道有更便宜的路 | "没有"和"不存在"在界面上一样 |
| 缓存命中不标注 | 几分钟前的名次被当成实时值 | 数字看起来是对的，只是旧的 |
| 描述与实现不符 | 描述写 120 秒、实际等 900 秒 | 用户按描述规划，代码不读描述 |
| OAuth token 被桥接忽略 | CLI 能用，桥接说"没凭据" | 两套凭据视图，各自都"正常" |

处理这类问题的原则：**把"我做了什么"和"实际发生了什么"都变成返回值的一部分。**
`rounds.requested` / `rounds.effective`、`cached_age_seconds`、
`work_dir_relocated_from`、`official_rules_applied` 都是这个原则的产物。

而"少暴露"那一半无法靠运行错误发现，只能静态核对 ——
这就是 `test_tool_cli_parity.py` 存在的原因。

## 6. 踩过的坑（改代码前先读）

### 6.1 cwd 不能是工作目录

`python -m <pkg>` 把 **cwd 放在 `sys.path[0]`**，优先于 `PYTHONPATH`。
若 `cwd = workRoot` 而 workRoot 恰在另一个也含 `kauto/` 的 checkout 里
（很常见：用户把工作目录指向自己的 kaggle-auto 克隆），
**加载的是那一份引擎**。表现是"改了代码没生效"，实际在测别人的副本。

→ `runner.ts` 固定 `cwd = paths.engineDir`；引擎的可写根只从 `KAUTO_ROOT` 读。

### 6.2 依赖探测不能只看版本号

`python3 --version` 答 3.12 很痛快，然后 `import pandas` 就死。
`scripts/run-engine-tests.mjs` 曾经因此把整套测试变成 66 个 import 错误，
看起来像代码全崩。→ 探测直接尝试导入重依赖，只让通过的跑。

### 6.3 gitignore 的否定规则要逐层放行

`work/` 整目录被忽略后，git **不会递归进去**，里面 `!work/**/xxx.json` 一条也不生效。
正确写法是 `work/**` → `!work/*/` → `!work/*/<具体文件名>`（父目录先放行）。

### 6.4 官方页面读不到不能当失败

`evaluation_page()` 取不到时返回 `{text:"", error}` 而不是抛异常：
官方页不可用不该阻断建模（本地结构校验仍然有效）。

### 6.5 布局差异不是缺陷

原 66 项测试有 7 项断言的是**用户工作区**的东西（README、真实比赛产物、vendor）。
插件包里这些前提不成立，失败语义是"问错了地方"。
→ `tests/_layout_guard.py`：目标不存在时以退出码 77 SKIP（附原因），
存在则照原样执行。`run_all_tests.py` 认识 77。

### 6.6 `meta.json` 里的绝对路径会压过 `KAUTO_ROOT`

`prepare` 把 `work_dir` 写成当时的绝对路径。这个值落盘后固定在文件里，于是：

- 随包分发的 5 个夹具在别人机器上指向不存在的目录；
- `KAUTO_ROOT` 重定向失效，用临时根跑测试会**写回真实工作区**
  （实测：一次演练把真实夹具的 journal 从 5 个节点改成 7 个）。

→ `load_work()` 无条件把 `work_dir` 校正为当前根，原值保留在
`work_dir_relocated_from`。**无条件**是关键：字段为空时 `Path("")` 会变成 `"."`，
产物落到进程 cwd，比写回旧路径更难查。

### 6.7 面板与工具的代码路径是两条

面板路由调 `bridge.*`，模型调 `tools.ts` 的 `invoke`。两者都会走到 `runEngine`，
但**只有 `invoke` 有锁和前置检查**。`bridge.smoke()` 首版就没锁，
于是面板点一次 + 模型调一次会同时跑两个演练（每次训练 3 次 LightGBM）。

→ 锁表放在 `createBridge` 里，两边通过 `bridge.lock` / `bridge.smokeLock()` 共用。
新增面板动作时先问：它要不要和某个工具串行？

### 6.8 PowerShell here-string 会吃掉反引号

用 `@"..."@` 写含反引号的中文文档时，反引号被当转义符：
`` `rounds.effective` `` 变成了换行加 `ounds.effective`。

→ 中文 + 反引号的内容用 `write` 工具或文件拼接，不要用 here-string 生成。

## 7. 加东西时改哪里

| 想加什么 | 改哪里 | 别忘了 |
|---|---|---|
| 新的引擎能力（比赛知识） | `engine/kauto/` | 写 `engine/tests/test_*.py` 并注册进 `run_all_tests.py` |
| 新工具（暴露给模型） | `tools.ts` 的 `make({...})` | 在 `helpPayload()` 的索引里补一条 |
| 新配置项 | `config.ts`（三处：`Settings`、`DEFAULTS`、`volatilePrefs`） | 面板 `FIELD_ORDER` 补一行 |
| 新面板动作 | `routes.ts` 的 `add(...)` | 客户端 `runJob()` 补一个按钮 |
| 新可选组件安装 | 复用 `installCompanion()` | 在 `THIRD_PARTY_NOTICES.md` 记许可与归属 |

## 8. 验证清单（提交前必跑）

```bash
pnpm typecheck        # tsc 两种配置都不许报错
pnpm test             # vitest 54 项（含真实 spawn 的集成测试）
pnpm test:engine      # 引擎 68 项（含 SKIP）
pnpm build            # lib/index.js + lib/client.js
pnpm smoke            # 真引擎跑 doctor + list --format json
```

面板改动另外要人工确认：**无 emoji、只用两档字重、颜色全走语义变量、
一个包围盒、状态行三列网格**。

### 关于集成测试

`tests/runner-integration.test.ts` 会**真的 spawn Python**。它存在的理由：
`runner-parse.test.ts` 能证明解析器正确，但证明不了 argv、环境约定、cwd
是对的 —— 而那三样正好是历史上出过问题的地方（cwd 落进别的 checkout、
`KAUTO_ROOT` 指错层级）。解析器测试抓不到这类错。

没有装依赖的解释器时它 **skip 而不是 fail**（一个没建 venv 的 checkout 是正常
状态，不是回归）。它还会直接断言 `kauto.__file__` 落在本包的 `engine/` 内 ——
这是"不会加载别人的引擎"这条性质本身，而不是它的间接迹象。

### 关于安装形态

开发机上永远看不到的一类问题：**装成 npm 包之后路径还对吗**。

`PACKAGE_ROOT` 由 `lib/index.js` 的位置推导（`dirname(import.meta.url)/..`）。
在仓库里它指向 checkout 根，`engine/` 就在旁边；装上之后它指向
`node_modules/dsh-yuan-kaggle-auto/`，而 `engine/` 也必须在那里 ——
否则 25 个工具全部找不到引擎，报出的还是"未初始化"这种指向错误方向的提示。

`tests/install-shape.test.ts` 真实 `npm pack` → 解压 → 断言
`lib/index.js` 与 `engine/` 相邻、`engineDirectory` 落在包内、运行时必需文件齐全。
它不联网、不装依赖，只验证路径推导与文件布局。

两条必须一起看：

| 测试 | 回答的问题 |
|---|---|
| `pack-contents.test.ts` | 包里**有没有**这些文件 |
| `install-shape.test.ts` | 包里的文件**摆在对的位置**吗 |

第二个才是路径推导的前提。`main` 改成 `./index.js`、`engine/` 被挪进 `lib/`、
或 `files` 漏掉 `engine/kauto/*.py` —— 三种都会让推导指错一层，
而只有第二个测试抓得到。

## 9. 与 kaggle-skill 的关系

`shepsci/kaggle-skill` 提供了 13 个比赛脚本与 20 篇参考资料，其中有几项能力
本引擎原本没有（读取官方页面、官方格式复核、模拟比赛 episodes、奖牌线）。

本项目的取舍：**能确定性落地的才合并**。

- 已合并：官方评测页读取（`pages`）、评测页约束 → 本地判据（`rules_from_evaluation`）。
- 通过桥接使用：`brief`（事实比本地 `info` 丰富）、`solutions`（writeup 检索）。
- 未合并：notebook 推送/运行、dataset/model 发布、badges、benchmarks ——
  这些需要账号写权限或与本插件定位无关，交给桥接或用户自己在站内做。

桥接层始终是**可选**：没有它，下载/建模/校验/提交全部照常可用。
