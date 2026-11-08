# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/)；版本语义遵循 [SemVer](https://semver.org/)。

## [Unreleased]

### 修复

- **`kaggle_receipt` 的等待上限描述与引擎差 7.5 倍**：工具描述写
  `default 120`，引擎 `--wait-timeout` 实际默认 **900 秒**。用户按两分钟
  规划等待，实际最长等十五分钟。同时 `timeoutMs: 0` 让外层完全不设上限 ——
  一次卡住的 HTTP 轮询会把工具调用钉死。现在描述点名 900，spawn 有
  `(waitTimeout + 120)` 秒的兜底（比引擎宽，让引擎自己决定超时，
  它还能为部分观测到的提交写回执），并拒绝小于 10 秒的等待。

- **演练的锁在面板与工具之间没有共享**：`kaggle_smoke` 工具取 `smoke` 锁，
  但面板路径 `bridge.smoke()` 直接调 `runEngine`，绕过了 `invoke` ——
  两条路径能同时跑。引擎给每次演练各自的 `mkdtemp` 根，所以文件不会冲突，
  但**每次演练训练 3 次 LightGBM**：面板点一次 + 模型调一次 = 翻倍开销、
  零额外信息。现在面板路径取同一个锁表，`try/finally` 释放，
  且全插件只有一个 `createLock()` 实例。

- **`kaggle_run` / `kaggle_iterate` 的轮数被静默夹紧**：`maxRounds` 上限存在是对的
  （否则模型能要求跑 50 轮、白烧一小时算力），错的是夹紧**不说出来**。
  引擎跑 4 轮、调用方要求 10 轮，返回里一个字没提 —— 一个以为自己迭代了
  10 次的模型，会基于它从未收集过的证据下结论。现在返回 `rounds.requested` /
  `rounds.effective` 与一句点名两个数字的 `roundsNote`，并说明怎么调高上限。
  新增 `planRounds()` 统一处理（`run` 允许 0 轮 = 只跑基线，`iterate` 最小 1 轮）。
- **一键安装 ego 在 Windows 上 100% 失败**：`runStreaming` 用
  `spawn('pnpm.cmd', ...)`，而 Windows 上 `.cmd` shim 不能直接 spawn ——
  实测 `spawn('pnpm.cmd', ['--version'])` 抛 `EINVAL`，且异常在进程启动前
  同步抛出，`Promise` 里的 try/catch 拦不住它，表现为整个调用崩掉。

  这意味着一键安装 ego（本插件唯一会改动用户 profile 的功能）
  **在 Windows 上从未成功过**，而在其他平台看起来一切正常。

  修法：Windows 且命令以 `.cmd` / `.bat` 结尾时加 `shell: true`，
  与用户在终端里敲命令走同一条路径。

- **回滚不清理 pnpm 装进 node_modules 的包**：undo 栈原先只登记
  `package.json` 的内容。`pnpm add` 是第一步，它成功后目录已经落在
  `node_modules/` 里；如果写 manifest 那步失败，回滚只恢复清单 ——
  于是**清单说"没装"、文件却躺在磁盘上**，用户下次安装会看到半装状态。

  修法：pnpm 成功后给 undo 栈登记删除，但**只在"装之前不存在"时登记** ——
  删掉用户本来就有的包是更严重的破坏。两个方向各有一条测试。

- **上一轮的 `shell: true` 修复引入静默装错版本**：`shell: true` 让
  `.cmd` shim 可执行，但也让 **cmd.exe 重新解析每个参数**。cmd 把 `^` 当
  转义符、`>` 当重定向，实测：

  ```
  cmd /c echo dsh-ego-browser@^0.9   →  dsh-ego-browser@0.9    （^ 被吃掉）
  cmd /c echo pkg@>=1.0.0            →  （空）                  （> 当成重定向）
  ```

  而本插件默认 `egoVersion` **就是** `^0.9`。所以上一轮"修好"的一键安装
  实际会装一个不存在的 `0.9` 版本，**并且报成功**。

  这比"装不上"更糟：装不上用户会重试，装错版本用户不会知道。

  修法：shell 分支下逐参数引号化（`quoteForCmd`），内部 `"` 转义为 `\"`，
  空参数给 `""` 防止被折叠。

  这一条值得单独记：**修一个平台的兼容问题，可能给同一平台的另一处引入
  静默错误。** 上一轮我为 `EINVAL` 加了 `shell: true` 并验证了
  "`pnpm.cmd --version` 能跑" —— 但没验证带 semver range 的参数。

- **Windows 下配了「上传代理」却没传给 pip**：`pipArgs` 的原逻辑是
  「Windows 且**没配**代理 → 加 `--proxy ''`」，配了代理就什么都不加。

  实测确认：

  ```
  win32  proxy=(none)                 -> --proxy: [""]
  win32  proxy="http://127.0.0.1:7890" -> --proxy: []        ← 配置被忽略
  ```

  后果：用户填了代理，pip 仍走那个会破坏 PyPI TLS 的**系统代理** ——
  与没填代理得到相同结果，而错误信息完全指向 PyPI 或网络。
  用户会以为"代理没配好"，实际是代码没读它。

  修法：Windows 上**总是**显式给出 `--proxy`（有值用它，无值给空串）。
  `--proxy` 能覆盖注册表里的 `ProxyEnable`/`ProxyServer`，而环境变量不能 ——
  这正是本机 TLS 失败的根因。

### 测试

- **`install-engine.test.ts`（13 项）：`installEngine` 此前零覆盖**。
  它是唯一会创建 venv、跑 pip 的路径，最慢（几分钟）且失败场景最多，
  而用户看到的只有"初始化失败"。

  覆盖：无解释器时提前失败并带安装指引、**失败时点名是哪一步**
  （而不是笼统的"安装失败"）、stamp 命中时**完全不 spawn**（"秒回"的全部实现）、
  `force` 真的绕过 fast-path、lock 文件变化时复用 venv 但重装依赖、
  pip 失败时把真实报错放进 `tail`、**pip 成功但导入失败**（wheel 平台不匹配，
  最容易被误判成"装好了"）、步骤顺序与面板可渲染性。

- **`pipArgs` 的 5 条单测**：Windows 总是带 flag、配了代理就用它（本轮修复）、
  POSIX 仅在配置时加、总是从 lock 文件安装、非交互（一次卡在
  `Do you want to continue?` 的 pip 看起来像死机）。

- **两次有效破坏实验**：
  - 回退 pip 代理逻辑 → 1 项红；
  - 把 fast-path 简化成「只要 venv 存在」→ **5 项红**。

- **一次无效的破坏实验，值得记**：第一次破坏只把条件里的一行
  （`stamp.pythonVersion === ...`）换成 `true`，其余四个条件仍在，
  于是 13 项全绿。我一度以为测试无效 —— 实际是**破坏不够彻底**。
  改成把整个条件换成 `if (venvUsable)` 后立刻 5 项红。

  教训：破坏实验要破坏**整条判断**，而不是其中一行。否则会得出
  "测试没用"的错误结论。

- **`shell-quoting.test.ts`（11 项）**：8 条单测逐字符覆盖 `quoteForCmd`
  （普通 token 不加引号、caret/comparator/全部 cmd 元字符/空格/内嵌引号/
  空参数/Windows 带空格路径），2 条真实 spawn 覆盖 shell 分支
  （`pnpm.cmd --version` 证明 EINVAL 回归不再出现）。

- **两次断言修正，都因为原版"能因错误的原因通过"**：
  - 第一版用 `pnpm add ... --dry-run`，而 `--dry-run` 不是 `pnpm add` 的选项，
    命令在选项解析阶段就失败 —— spec 从未出现在输出里，`if (includes(spec))`
    于是假通过；
  - 第二版用 `pnpm add <spec>`，失败时 pnpm 的错误信息也不回显 spec。

  最终把端到端断言收敛到"shell 分支不崩"，逐字符行为交给单测 ——
  这让测试从 17 秒降到 0.5 秒，且不再依赖网络。

- 破坏实验：移除元字符检测（永不加引号）→ **6 项红**。

- **`installCompanion` 的步骤文案与实际行为不符**：写 manifest 那一步报告
  "写入 dependencies"，但 `dependencies` 是 **pnpm** 写的，这一步只补
  `dsh.profile.bundles`。若 pnpm 因 `--no-save` 或版本差异没有记录依赖，
  用户会以为这一步已经保证过。

  改为如实报告两种情形：`已在 dependencies（0.9.7）` 或
  `未出现在 dependencies —— pnpm 没有记录它，请检查 pnpm 版本`。

  这类"文案比行为承诺得更多"的缺陷，与前几轮修的"轮数静默夹紧"、
  "描述差 7.5 倍"是同一类：**不会报错，只是让人形成错误预期**。

- **`installer-success.test.ts`（7 项）：`installCompanion` 的成功路径**。
  上一轮只覆盖了回滚；这一轮补主干。

  重点断言 **`loader.create` 的入参**，因为它是"免重启生效"的唯一机制，
  而 `id` / `name` 传错会产生两种静默后果：

  - `name` 错 → Loader 用错的 specifier 去 require，插件加载不出来，
    但安装步骤里 `load.ok` 仍是 true；
  - `id` 错 → 设置面板的命名空间对不上，面板静默消失。

  覆盖的分支：

  - 完整成功：清单写入、bundles 追加、`loader.create` 收到正确的
    `id` / `name`；
  - 步骤顺序是承诺：`pnpm` < `manifest` < `load`，最后一步是 `verify`；
  - 宿主没有 loader → 仍然成功，但明确说"重启后生效"；
  - `loader.create` 抛异常 → 仍然成功（文件与清单已就位），
    说明里带上原始错误并提示重启；
  - 版本号取自 `node_modules` 里实际落地的包，而不是请求的 range；
  - 一切就绪时不调用 pnpm（避免无谓地动用户的 lockfile）；
  - `entryId` 与 `packageName` 是两个不同标识，不能混用。

- **两次破坏实验**：
  - 把 `loader.create` 的 `id` / `name` 互换 → **2 项红**；
  - 移除 `verify` 步骤 → **2 项红**。

- **一次桩不真实导致的假失败**：第一版 pnpm 桩只创建了
  `node_modules/<pkg>`，没写 `dependencies`，于是断言失败。查证真实 pnpm
  行为（`pnpm add is-number --save-exact` 之后清单里出现
  `"dependencies":{"is-number":"7.0.0"}`）后修正桩 —— 那不是产品缺陷，
  是桩没有覆盖被测代码依赖的那部分契约。

- **`kaggle_run` 失败时丢掉引擎的结构化输出**：引擎在失败路径上**同样**
  打印 JSON —— `kauto run` 某一步失败时会输出
  `{"steps": [{"step": "prepare", "ok": false, "error": "…"}]}`（实测用一个
  不存在的 slug 确认过）。而 `fromRun` 在 `ok: false` 时只截 stderr 尾部，
  把整个 JSON 丢掉。

  后果：模型只看到一段人读的文本，拿不到"哪一步失败、为什么"这个唯一的
  可分支信息 —— 而那正是它决定下一步做什么的依据。面板的 `doctor` 早就
  把 steps 逐条显示了，工具侧却一直在丢。

  现在两种结局都先展开引擎返回的对象，再叠加错误字段。顺序是刻意的：
  **结构在前、stderr 在后** —— 引擎明确给出的 `error` 不能被一段 stderr
  摘要覆盖。

  这类损失很隐蔽：工具确实返回了、`ok: false` 也是对的，
  只是**把有用的结构换成了没用的文本**。

- **`from-run.test.ts`（9 项）**：`fromRun` 是引擎输出到工具结果的唯一通道，
  此前零覆盖。覆盖：成功时透传结构、**失败时保留 `steps`**、
  同时保留一句话错误与结构化数据、回传 `exitCode`（提交门用 4、
  形状校验用 3，工具层要能分支）、无解析值时退回原始文本、
  **stderr 不得覆盖引擎明确给出的字段**、调用方 extras 最后叠加、
  裸数组不被展开成 `{0: …}`。

- **两次破坏实验**：
  - 移除结构展开（回到"只返回 stderr"的旧行为）→ **3 项红**；
  - 把结构展开挪到 `error` 之前（顺序颠倒）→ **1 项红**。

- **`invoke.test.ts`（12 项）：25 个工具的共同底座，此前零覆盖**。
  每个 `kaggle_*` 工具最终都走到 `invoke`，它负责四件事，每一件出错都
  不报错：

  | 职责 | 出错时的表现 |
  |---|---|
  | 无 Python 时前置拒绝 | 白白 spawn 一次，得到看不懂的报错 |
  | 按 slug 串行 | 同场比赛两个调用互相踩 `work/<slug>` |
  | 超时传递 | 长任务被隐性上限腰斩，看起来像"引擎慢" |
  | 锁释放 | **一次失败之后那场比赛永久卡死，后续调用只是静静等锁** |

  最后一条是本文件的核心：`try/finally` 写错的话，运行期完全没有征兆 ——
  第一次失败之后所有调用都不返回，也没有任何错误信息。

  覆盖：无解释器时不 spawn 也不占锁、同 slug 串行、不同 slug 不互相阻塞、
  显式锁键（`smoke` 没有 slug 但必须全局串行）、无需锁的命令
  （`doctor` 是诊断命令，让它排长队等于堵住排障入口）、
  **异常路径仍释放锁**、超时透传（含默认值）、`command` 回传便于溯源。

- **`createLock` 的 3 条单测**：同键排队与顺序、不同键立即通过、
  重复释放不 panic。

- **三次破坏实验**：
  - 把 `finally { release() }` 改成 `catch { throw }` → **2 项红**；
  - 忽略调用方的 `timeoutMs` → **1 项红**。

- **一条自己写的假测试，改写后才有意义**：超时那条初版写成
  `void seen; expect(result).toBeTruthy()` —— 用**错误的原因**也能通过。
  改成 `vi.doMock('../src/runner.ts')` 拦截真实调用，断言
  `calls[0].options.timeoutMs === 1000` 与默认 `30 * 60 * 1000`。

- **导出两个此前不可测的内部函数**：`invoke`、`createLock`。
  它们是全部工具的底座与并发保证，理应可测。

### 文档

- **`docs/ARCH.md` 补齐到当前代码状态**：引擎侧改动清单从 6 处补到 **11 处**
  （补上 `work_dir` 校正、排行榜、`smoke`、缓存、截止门），每处标注对应测试。
  新增两节维护者必须知道的约束：
  - **改引擎前必须知道的两条**：可写根只能来自 `KAUTO_ROOT`（任何按
    `__file__` 推导的地方装进 npm 包后都指向 `node_modules`）；
    包内只读资源（`fixtures/`）不能跟着可写根走，否则设了 `KAUTO_ROOT`
    就找不到夹具。
  - **两类静默失败**：这几轮修掉的缺陷大多是同一类 —— 可见的失败会自动暴露，
    静默的失败才是真问题（轮数夹紧不说明、少暴露关键选项、缓存命中不标注、
    描述与实现不符、OAuth token 被桥接忽略）。处理原则是把"我做了什么"
    和"实际发生了什么"都变成返回值的一部分。
  - 新增三个坑：`meta.json` 的绝对路径会压过 `KAUTO_ROOT`；
    面板与工具是两条代码路径（只有 `invoke` 有锁）；PowerShell here-string 吃反引号。

- **`test_docs_sync.py` 从 10 项扩到 20 项**：检查 ARCH.md 是否记录了
  全部 11 处引擎改动、"两类静默失败"章节及其三个例子、以及三个新增的坑。
  文档落后于代码正是这个测试要抓的东西 —— 这一轮它抓到自己的清单只记到 6 处。

- **`routes-behaviour.test.ts`（16 项）**：`registerRoutes` 此前从未被调用过，
  `routes-guard.test.ts` 只覆盖 `isTrustedRequest` / `readBody` 两个纯函数。
  新测试用假的 `register` 收集注册项、假的 `req`/`res` 驱动 handler，覆盖：
  10 条路由全部注册 / 无 cookie 返回 401 / 非 POST 返回 405 /
  `setup` 立刻回 jobId 而不是等安装完成 / `doctor` 把修复建议写进日志 /
  `smoke` 在引擎未就绪时**拒绝演练且不 spawn** / `smoke` 输出未覆盖项 /
  `leaderboard` 空 slug 不调引擎 / `install-skill` 回命令而不静默 clone /
  job 路由 404 与增量读取 / cancel 幂等 / **handler 抛异常仍回 JSON**
  （面板没有"路由永不响应"的错误通道）。

- **`pack-contents.test.ts`（9 项）：检查发布物而不是源码树**。
  既有两条检查都不够：

  - `manifest.test.ts` 只比对 `files` 数组里的**字符串** —— 写了
    `engine/kauto/*.py` 就算通过，哪怕磁盘上一个 `.py` 都没有；
  - 我这一轮加的 `every publish pattern actually matches files on disk`
    又只证明文件在磁盘上存在。

  两者加起来仍然回答不了"用户装到的包里到底有什么"。**实测确认过**：
  把 `engine/fixtures/**` 从 `files` 里删掉，上面两条**全都还是绿的** ——
  而 `npm pack` 已经不含夹具，「跑通演练」会在每个全新安装上失败，
  且只在用户那边失败。

  新测试直接跑 `npm pack --dry-run --json` 读 npm 自己的文件选择结果，
  断言 tarball 里有：两个 bundle、两个 manifest、≥20 个引擎模块、
  ≥50 个测试脚本、`run_all_tests.py`、`requirements.lock.txt`、
  demo-comp 的三个夹具、四份文档；并且**没有** `__pycache__` /
  `.tmp-*` / `.pyc`、没有带盘符或 `..` 的路径。

  用同样的破坏实验验证过：删掉 fixtures 后这条测试会红，
  报错直接点名 `train.csv` 缺失。


- **`manifest.test.ts` 补 4 项**：每个非通配模式都真的在磁盘上匹配到文件、
  引擎模块数与三个入口（`cli.py` / `kapi.py` / `prep.py`）、
  演练夹具三件套、不发布缓存与临时目录。

- **跨平台路径比较**：在 `manifest.test.ts` 里用 `p.endsWith('/cli.py')`
  判断引擎入口，而 Windows 的 glob 返回反斜杠 —— 这条断言会在 mac 上通过、
  在这里失败（或反过来）。改为先统一 `\` → `/` 再比较。
  写测试时自己踩了一遍，说明这类断言确实需要显式处理平台差异。

- **跨平台路径比较**：新测试里用 `p.endsWith('/cli.py')` 判断引擎入口，
  而 Windows 的 glob 返回反斜杠 —— 这条断言会在 mac 上通过、在这里失败
  （或反过来）。改为先统一 `\\` → `/` 再比较。写测试时自己踩了一遍，
  说明这类断言确实需要显式处理平台差异。
- **`setup-status.test.ts`（11 项）：`readStatus` 此前零覆盖**。
  它决定面板状态盘上的每一行、`ready` 的真假（直接控制「跑通演练」能不能点），
  以及用户对"我的环境到底缺什么"的**唯一**信息来源。
  说错了，用户会拿着错误的指引去修错的东西。

  覆盖的分支：

  - 没有 venv → `missing`（不是 `warn`）；
  - 有 venv 但 stamp 过期 → `warn`（说 `ok` 会藏住待升级，说 `missing`
    会让用户去重建已有的东西）；
  - venv + 依赖 + kaggle CLI + 新鲜 stamp → `ok` 且 `ready=true`；
  - 有 venv 但缺 lightgbm → `warn` 且 `ready=false`（pandas/numpy/sklearn
    都在，跑到 baseline 才会炸）；
  - 凭据行**从不**输出形似 token 的长串；
  - 可选的 skill 桥接永远是 `ok`（把可选组件标成警告，会教用户忽略警告）；
  - 没有 LLM → `warn` 且**明确指出确定性路径仍然可用**；
  - ego 探测结果如实反映；
  - **每个非 ok 项都必须带 `fix`** —— 这是状态盘的核心契约，少了它
    用户只知道"坏了"；
  - 回传解析后的路径供面板展示；
  - 显式 `kaggleCliPath` 优先于 venv 里的那个。

- **两次破坏实验验证测试有效**（不是只看绿）：
  - 把 `deps` 的 `warn` 改成恒 `ok` → 1 项红；
  - 把 `ready` 从「venv + 新鲜 stamp + lightgbm + kaggle CLI」简化成
    「venv 存在」→ **2 项红**。

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



