/**
 * src/tools.ts — the model-visible surface: `kaggle_*` tools.
 *
 * Design rules that matter more than the individual wrappers:
 *
 *   - One shape for every result: `{ ok, ... }`. The model can branch on `ok`
 *     without remembering 23 different error formats.
 *   - Every wrapper is a thin translation of `kargv` into the engine's CLI.
 *     No business logic lives here (it is in engine/kauto), so the Python tests
 *     and the tool surface cannot drift apart.
 *   - Submission is gated twice and neither gate can be bypassed from here.
 *   - A missing optional component (ego, skill bridge) degrades to advice, never
 *     to an exception.
 */
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { Settings } from './config.ts'
import type { ResolvedPaths } from './paths.ts'
import { runEngine, type RunResult } from './runner.ts'

export interface ToolDeps {
  settings: () => Settings
  paths: () => ResolvedPaths
  python: () => Promise<{ ok: true; python: string } | { ok: false; reason: string; hint: string }>
  /** Host subprocess service, when the host exposes one. */
  subprocess?: undefined | { spawn: Parameters<typeof runEngine>[1]['subprocess'] extends infer T ? T extends { spawn: infer S } ? S : never : never }
  /** Optional ego tool presence, for the assistance hint. */
  egoAvailable: () => boolean
  /** Serialises engine calls that share one slug. */
  acquire: (key: string) => Promise<() => void>
  /** Builds the venv. Absent on hosts that only register tools (no install path). */
  setup?: (force?: boolean) => Promise<unknown>
  /** Appends a line to the active job log, when the caller is a job. */
  onLine?: (line: { stream: 'stdout' | 'stderr'; text: string }) => void
}

/** What a tool returns. `ok:false` always carries `error`. */
export interface ToolResult {
  ok: boolean
  error?: string
  [key: string]: unknown
}

const TOOL_TIMEOUT_MS = 30 * 60 * 1000

const FAIL_HINT = '先点「初始化环境」，或调用 kaggle_setup。'

function result(payload: ToolResult): ToolResult {
  return payload
}

/** Uniform translation of a failed engine run into a tool result. */
function fromRun(run: RunResult, extra: Record<string, unknown> = {}): ToolResult {
  const value = run.value
  if (run.ok) {
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      return { ok: true, ...(value as Record<string, unknown>), ...extra }
    }
    return { ok: true, stdout: run.stdout.trim() || undefined, value: value === undefined ? undefined : value, ...extra }
  }
  return {
    ok: false,
    error: run.error ?? 'engine 调用失败',
    exitCode: run.exitCode,
    stderr: run.stderr.trim().slice(0, 1200) || undefined,
    stdout: run.stdout.trim().slice(-400) || undefined,
    ...extra,
  }
}

interface InvokeOptions {
  /** Lock key; defaults to the slug when one is present. */
  lock?: string
  /** Ceiling in ms; 0 disables (iterate can legitimately run long). */
  timeoutMs?: number
}

/**
 * Run the engine with the preconditions every tool shares:
 *   ready python → (optional) per-slug lock → spawn → parse.
 *
 * The lock is per slug because two `kaggle_iterate` calls on the same
 * competition would write into the same `work/<slug>` tree and corrupt each
 * other's `best_*` artifacts. Different slugs run concurrently, which is what
 * lets one agent babysit two competitions.
 */
async function invoke(
  deps: ToolDeps,
  command: string[],
  options: InvokeOptions = {},
): Promise<ToolResult> {
  const found = await deps.python()
  if (!found.ok) {
    return result({
      ok: false,
      error: `${found.reason}\n${found.hint}\n${FAIL_HINT}`,
      needsSetup: true,
    })
  }
  const slug = command.find((arg, index) => index === 1 && !arg.startsWith('-')) ?? ''
  const lockKey = options.lock ?? (slug ? `slug:${slug}` : '')
  const release = lockKey ? await deps.acquire(lockKey) : () => {}
  try {
    const run = await runEngine(command, {
      python: found.python,
      paths: deps.paths(),
      settings: deps.settings(),
      subprocess: deps.subprocess,
    }, {
      timeoutMs: options.timeoutMs ?? TOOL_TIMEOUT_MS,
      onLine: deps.onLine,
    })
    return fromRun(run, { command: command.join(' ') })
  } finally {
    release()
  }
}

// ── submission gate ─────────────────────────────────────────────────────────

export interface SubmitGate {
  /** Arguments actually appended to the CLI. */
  argv: string[]
  /** True when `--yes` was appended, i.e. the engine will really submit. */
  willSubmit: boolean
  /** Why `--yes` was withheld, when it was. */
  withheldReason?: string
}

/**
 * The one place that decides whether a submission is real.
 *
 * Both conditions are required: the caller passed `confirm: true` AND the
 * plugin's `requireConfirm` is on. Turning `autoSubmit` on does NOT relax this
 * — it only means the agent may *decide* to submit, not that confirmation
 * becomes optional. A user who turns `requireConfirm` off has said "I have
 * seen the file, go ahead", so `confirm` alone then suffices.
 */
export function submitGate(settings: Settings, args: { confirm?: unknown }): SubmitGate {
  const confirmed = args.confirm === true
  if (settings.requireConfirm && !confirmed) {
    return {
      argv: [],
      willSubmit: false,
      withheldReason:
        '提交需要显式确认。请确认工作目录里的 submission.csv 与本次方案一致，' +
        '然后带 confirm: true 再次调用；否则只返回将提交的摘要。',
    }
  }
  if (!settings.requireConfirm && !confirmed) {
    // Even with the guard off, a bare call must not silently consume a slot:
    // the engine's own --yes is what spends it, so leave it out and report.
    return {
      argv: [],
      willSubmit: false,
      withheldReason:
        '已关闭「提交前确认」，但本次调用没有带 confirm: true。' +
        '为避免误消耗提交配额，仍然只做预检；确认后带 confirm: true 即可真正提交。',
    }
  }
  return { argv: ['--yes'], willSubmit: true }
}

// ── ego assistance ──────────────────────────────────────────────────────────

/**
 * The browser hint attached to results that may need a human-in-the-loop step.
 *
 * Measured fact: Kaggle's submit call joins the competition on its own, so a
 * browser is never *required*. It is only useful when a competition insists on
 * an on-site acceptance that the API refuses — so the hint is advice, and it
 * names the concrete tool calls rather than saying "use a browser".
 */
function browserHint(deps: ToolDeps, url: string | undefined): string | undefined {
  const settings = deps.settings()
  if (!settings.egoAssist) return undefined
  const target = url ?? 'https://www.kaggle.com/competitions'
  if (!deps.egoAvailable()) {
    return `若页面要求人工确认，请自行打开：${target}`
  }
  return `若 API 返回 403 要求接受规则，用 ego_navigate 打开 ${target}，用 ego_click 点「Join Competition」后重试。`
}

// ── registration ───────────────────────────────────────────────────────────

const commonOutput = {
  type: 'object',
  additionalProperties: true,
  properties: { ok: { type: 'boolean', required: true } },
} as const

/** Render the canonical value as one text block; the model reads JSON. */
const textRender = (
  _args: Record<string, unknown>,
  value: unknown,
): Array<{ type: 'text'; text: string }> => [
  {
    type: 'text',
    text:
      value !== null && typeof value === 'object' && typeof (value as { ok?: unknown }).ok === 'boolean'
        ? JSON.stringify(value, null, 2)
        : String(value),
  },
]

type Reg = (tool: unknown) => void

/** Register every `kaggle_*` tool. */
export function registerTools(reg: Reg, deps: ToolDeps): void {
  const make = (options: {
    name: string
    description: string
    parameters: Record<string, unknown>
    run: (args: Record<string, unknown>) => Promise<ToolResult>
  }): void => {
    // The parameter spec is built at runtime from plain objects; the tool
    // factory wants literal-typed schema nodes for argument inference. We keep
    // our own authoring shape (easy to read, one `required` flag per field) and
    // assert at the boundary — inference buys nothing here because every
    // handler takes `Record<string, unknown>` anyway.
    const definition = defineTool({
      name: options.name,
      description: options.description,
      parameters: options.parameters,
      output: { schema: commonOutput, render: textRender },
      timeoutMs: TOOL_TIMEOUT_MS,
      execute: async (args: Record<string, unknown>) => options.run(args),
    } as never)
    reg(definition)
  }

  const slugParam = {
    slug: { type: 'string', required: true, description: 'Competition slug, e.g. "titanic".' },
    space: undefined,
  }

  make({
    name: 'kaggle_help',
    description:
      'kauto 工具索引：这条流水线的六个阶段、最短路径、以及每个工具的用途。不确定下一步做什么时先调它。',
    parameters: {
      topic: { type: 'string', description: "Optional: 'pipeline' | 'tools' | 'submit' | 'trouble'." },
    },
    run: async (args) => result(helpPayload(typeof args.topic === 'string' ? args.topic : '')),
  })

  make({
    name: 'kaggle_doctor',
    description:
      '环境体检：Python、引擎 venv、依赖、Kaggle 凭据、Kaggle CLI、LLM 通道、ego 浏览器。返回逐项状态与修复建议。',
    parameters: {},
    run: async () => {
      const run = await invoke(deps, ['doctor'])
      if (!run.ok) return run
      return result({ ok: true, ...(run as Record<string, unknown>), env: await deps.python() })
    },
  })

  make({
    name: 'kaggle_setup',
    description:
      '初始化引擎环境（幂等）：创建 venv 并安装 pandas/numpy/scikit-learn/lightgbm/kaggle。首次约数分钟，之后命中标记即秒回。',
    parameters: {
      force: { type: 'boolean', description: 'Rebuild even when the stamp matches.' },
    },
    run: async (args) => {
      // Setup does NOT go through the engine: it creates the venv the engine
      // will later run in, so there is no interpreter to call yet. The host
      // half owns the install and hands back the same outcome the panel route
      // reports, so an agent-driven setup and a panel click behave identically.
      if (!deps.setup) {
        return result({
          ok: false,
          error: '宿主未提供初始化能力，请在设置面板点「初始化环境」',
          needsSetup: true,
        })
      }
      const outcome = await deps.setup(args.force === true)
      const record = outcome as {
        ok?: boolean
        reused?: boolean
        python?: string
        pythonVersion?: string
        steps?: Array<{ step: string; ok: boolean; detail?: string }>
        error?: string
        tail?: string
      }
      if (!record.ok) {
        return result({
          ok: false,
          error: record.error ?? '初始化失败',
          steps: record.steps,
          tail: record.tail?.slice(0, 3000),
          hint: record.tail?.includes('proxy') || record.tail?.includes('SSL')
            ? 'pip 访问 PyPI 失败：本机系统代理会破坏 TLS，插件已默认加 `--proxy ""`；若仍失败，检查网络或设置「上传代理」。'
            : '看 tail 里的 pip 输出定位具体包。',
        })
      }
      return result({
        ok: true,
        python: record.python,
        pythonVersion: record.pythonVersion,
        reused: record.reused === true,
        steps: record.steps,
        note: record.reused
          ? '环境已是最新（命中标记，未重新安装）'
          : '环境就绪，可以开始跑比赛了',
      })
    },
  })

  make({
    name: 'kaggle_connect',
    description:
      '检查/说明如何接入 Kaggle 与 LLM。kaggle 侧只读探测凭据；llm 侧探测可用通道。不改动任何凭据。',
    parameters: {
      target: { type: 'string', description: "'kaggle' | 'llm' | 'both' (default both)." },
    },
    run: async (args) => {
      const target = typeof args.target === 'string' ? args.target : 'both'
      const run = await invoke(deps, ['doctor'])
      if (!run.ok) return run
      const value = run as Record<string, unknown>
      const kaggle = value.kaggle_credentials as Record<string, unknown> | undefined
      const llm = value.llm as Record<string, unknown> | undefined
      const payload: ToolResult = { ok: true, asked: target }
      if (target === 'kaggle' || target === 'both') {
        payload.kaggle = kaggle ?? { ok: false }
        if (!kaggle?.ok) {
          payload.kaggleHint =
            '在 Kaggle 账号 Settings → API 生成 Token，存到 ~/.kaggle/access_token，或执行 `kaggle auth login`。'
        }
      }
      if (target === 'llm' || target === 'both') {
        payload.llm = llm ?? { ok: false }
        if (!llm?.ready) {
          payload.llmHint =
            '迭代需要 LLM。默认会自动复用 DSH 已配置的通道；也可在设置里填 base URL / model / key。' +
            'baseline、write-sub、submit 不依赖 LLM，可以先跑。'
        }
      }
      return result(payload)
    },
  })

  make({
    name: 'kaggle_list',
    description: '列出可参赛的 Kaggle 比赛（公开列表，不需要凭据）。按关键词过滤。',
    parameters: {
      search: { type: 'string', description: 'Keyword, e.g. "playground" or "tabular".' },
      limit: { type: 'number', description: 'Max rows (default 20).' },
    },
    run: async (args) => {
      const argv = ['list', '--format', 'json']
      if (typeof args.search === 'string' && args.search.trim()) argv.push('--search', args.search.trim())
      if (typeof args.limit === 'number' && Number.isFinite(args.limit)) argv.push('--limit', String(Math.round(args.limit)))
      return invoke(deps, argv)
    },
  })

  make({
    name: 'kaggle_info',
    description:
      '比赛事实：标题、官方指标、每日提交限额、截止时间、是否 notebook-only。决定用 accuracy 还是 logloss 就靠它。',
    parameters: { slug: slugParam.slug },
    run: async (args) => invoke(deps, ['info', String(args.slug ?? '')])
  })

  make({
    name: 'kaggle_status',
    description: '自己在这场比赛里的状态：今日剩余提交次数、历次提交与得分、最好公开分。',
    parameters: {
      slug: slugParam.slug,
      limit: { type: 'number', description: 'How many recent submissions to include (default 5).' },
    },
    run: async (args) => {
      const argv = ['status', String(args.slug ?? '')]
      if (typeof args.limit === 'number' && Number.isFinite(args.limit)) {
        argv.push('--limit', String(Math.round(args.limit)))
      }
      return invoke(deps, argv)
    },
  })

  make({
    name: 'kaggle_prepare',
    description:
      '下载比赛数据并切分训练/验证集，推断 schema（id 列、标签列、任务类型、切分策略）。产物落在工作目录的 work/<slug>/。',
    parameters: {
      slug: slugParam.slug,
      force: { type: 'boolean', description: 'Re-download even when data is present.' },
    },
    run: async (args) => {
      const argv = ['prepare', String(args.slug ?? '')]
      if (args.force === true) argv.push('--force')
      return invoke(deps, argv)
    },
  })

  make({
    name: 'kaggle_baseline',
    description:
      'LightGBM 基线 + K 折交叉验证。产出 CV 分数、特征集指纹与列体检报告，是后续所有改进的参照点。大比赛可能要几分钟。',
    parameters: {
      slug: slugParam.slug,
      folds: { type: 'number', description: 'CV folds (default 5).' },
      seed: { type: 'number', description: 'Random seed (default 42).' },
    },
    run: async (args) => {
      const argv = ['baseline', String(args.slug ?? '')]
      if (typeof args.folds === 'number' && Number.isFinite(args.folds)) argv.push('--folds', String(Math.round(args.folds)))
      if (typeof args.seed === 'number' && Number.isFinite(args.seed)) argv.push('--seed', String(Math.round(args.seed)))
      return invoke(deps, argv, { timeoutMs: 0 })
    },
  })

  make({
    name: 'kaggle_iterate',
    description:
      'LLM 树搜索迭代特征工程：生成候选方案、跑本地 CV、按分数择优。需要 LLM 通道；单轮 3–5 分钟。不消耗 Kaggle 提交次数。',
    parameters: {
      slug: slugParam.slug,
      rounds: { type: 'number', description: 'Iteration rounds.' },
      parallel: { type: 'number', description: 'Concurrent candidates (default 1).' },
      foldsForOof: { type: 'number', description: 'Extra OOF folds per kept node (0 = skip).' },
    },
    run: async (args) => {
      const settings = deps.settings()
      const argv = ['iterate', String(args.slug ?? '')]
      const rounds = typeof args.rounds === 'number' && Number.isFinite(args.rounds)
        ? Math.min(settings.maxRounds, Math.max(1, Math.round(args.rounds)))
        : settings.maxRounds
      argv.push('--rounds', String(rounds))
      if (typeof args.parallel === 'number' && Number.isFinite(args.parallel)) {
        argv.push('--parallel', String(Math.max(1, Math.round(args.parallel))))
      }
      if (typeof args.foldsForOof === 'number' && Number.isFinite(args.foldsForOof)) {
        argv.push('--folds-for-oof', String(Math.max(0, Math.round(args.foldsForOof))))
      }
      return invoke(deps, argv, { timeoutMs: 0 })
    },
  })

  make({
    name: 'kaggle_write_sub',
    description:
      '生成 submission.csv。预测与当前特征集不一致时会拒绝复用旧预测并自动全量重训，避免提交与 CV 脱节的文件。',
    parameters: { slug: slugParam.slug },
    run: async (args) => invoke(deps, ['write-sub', String(args.slug ?? '')], { timeoutMs: 0 })
  })

  make({
    name: 'kaggle_leaderboard',
    description:
      '排行榜：榜首、我的名次、奖牌线。这是判断"还值不值得继续迭代"的唯一依据 —— 差 0.0005 值得再跑一轮，差 0.05 不如换方向。需要 Kaggle 凭据。',
    parameters: {
      slug: slugParam.slug,
      top: { type: 'number', description: 'How many top rows to include (default 10).' },
    },
    run: async (args) => {
      const argv = ['leaderboard', String(args.slug ?? '')]
      if (typeof args.top === 'number' && Number.isFinite(args.top)) {
        argv.push('--top', String(Math.round(args.top)))
      }
      return invoke(deps, argv)
    },
  })

  make({
    name: 'kaggle_rules',
    description:
      '读取比赛的官方页面（默认评测页）并把其中的硬约束变成可执行判据：行数、列数、允许的列名、取值范围。写提交文件前先查它，能少一次返工。公开页面不需要凭据。',
    parameters: {
      slug: slugParam.slug,
      page: {
        type: 'string',
        description: "Page name: 'evaluation' (default) | 'rules' | 'data-description'.",
      },
      text: {
        type: 'boolean',
        description: 'Also return the page text itself (default false).',
      },
    },
    run: async (args) => {
      const argv = ['rules', String(args.slug ?? '')]
      if (typeof args.page === 'string' && args.page.trim()) argv.push('--page', args.page.trim())
      if (args.text === true) argv.push('--format', 'text')
      return invoke(deps, argv)
    },
  })

  make({
    name: 'kaggle_validate',
    description: '形状校验：列名、行数、id 是否与 sample_submission 完全一致。不评分，但能挡住会白烧配额的错文件。',
    parameters: { slug: slugParam.slug },
    run: async (args) => invoke(deps, ['validate', String(args.slug ?? '')])
  })

  make({
    name: 'kaggle_submit',
    description:
      '把 submission.csv 提交到 Kaggle。默认需要 confirm: true 才真正提交，否则只返回将提交的摘要。会消耗一次当日提交配额。',
    parameters: {
      slug: slugParam.slug,
      message: { type: 'string', description: 'Submission description shown on Kaggle.' },
      confirm: { type: 'boolean', description: 'Set true to actually submit.' },
      expect: { type: 'number', description: 'Expected score, recorded in the receipt.' },
    },
    run: async (args) => {
      const settings = deps.settings()
      const gate = submitGate(settings, { confirm: args.confirm })
      const slug = String(args.slug ?? '')
      if (!gate.willSubmit) {
        const pre = await invoke(deps, ['validate', slug])
        return result({
          ok: true,
          submitted: false,
          reason: gate.withheldReason,
          precheck: pre,
          hint: browserHint(deps, `https://www.kaggle.com/competitions/${slug}`),
        })
      }      const argv = ['submit', slug]
      if (typeof args.message === 'string' && args.message.trim()) argv.push('--message', args.message.trim())
      if (typeof args.expect === 'number' && Number.isFinite(args.expect)) argv.push('--expect', String(args.expect))
      argv.push(...gate.argv)
      const run = await invoke(deps, argv, { timeoutMs: 0 })
      if (run.ok) return run
      // Exit 4 is the engine's deadline gate: the competition no longer accepts
      // submissions. Say that plainly, because the generic failure text reads
      // like "your file is wrong" and sends people into a pointless retrain loop.
      if (typeof run.exitCode === 'number' && run.exitCode === 4) {
        return result({
          ok: false,
          submitted: false,
          reason: 'competition_closed',
          error: run.error,
          advice:
            '这个比赛已不再接受提交。不要去改文件或重训 —— 那不会改变结果。' +
            '换一个仍在进行的比赛，或查看：' +
            `https://www.kaggle.com/competitions/${slug}/leaderboard`,
        })
      }
      return result({
        ...run,
        hint: browserHint(deps, `https://www.kaggle.com/competitions/${slug}/rules`),
      })
    },
  })

  make({
    name: 'kaggle_receipt',
    description: '读（或刷新）提交回执：这次提交的 public / private 分数、状态与溯源。refresh: true 会重新拉取实时分数。',
    parameters: {
      slug: slugParam.slug,
      refresh: { type: 'boolean', description: 'Re-poll Kaggle instead of reading the stored receipt.' },
      waitTimeout: { type: 'number', description: 'Poll ceiling in seconds (default 120).' },
    },
    run: async (args) => {
      const argv = ['receipt', String(args.slug ?? '')]
      if (args.refresh === true) argv.push('--refresh')
      if (typeof args.waitTimeout === 'number' && Number.isFinite(args.waitTimeout)) {
        argv.push('--wait-timeout', String(Math.round(args.waitTimeout)))
      }
      return invoke(deps, argv, { timeoutMs: 0 })
    },
  })

  make({
    name: 'kaggle_run',
    description:
      '一条命令跑完整链路：prepare → baseline → iterate → write-sub →（可选）提交 → 读分。最短路径，先用它。',
    parameters: {
      slug: slugParam.slug,
      rounds: { type: 'number', description: 'Iteration rounds (0 skips iteration).' },
      submit: { type: 'boolean', description: 'Submit at the end (still needs confirm).' },
      confirm: { type: 'boolean', description: 'Required together with submit: true.' },
      message: { type: 'string', description: 'Submission description.' },
    },
    run: async (args) => {
      const settings = deps.settings()
      const slug = String(args.slug ?? '')
      const argv = ['run', slug]
      const rounds = typeof args.rounds === 'number' && Number.isFinite(args.rounds)
        ? Math.min(settings.maxRounds, Math.max(0, Math.round(args.rounds)))
        : settings.maxRounds
      argv.push('--rounds', String(rounds))
      const wantsSubmit = args.submit === true
      let withheld: string | undefined
      if (wantsSubmit) {
        const gate = submitGate(settings, { confirm: args.confirm })
        if (gate.willSubmit) {
          if (typeof args.message === 'string' && args.message.trim()) argv.push('--message', args.message.trim())
          argv.push(...gate.argv)
        } else {
          withheld = gate.withheldReason
        }
      }
      const run = await invoke(deps, argv, { timeoutMs: 0 })
      if (withheld) {
        return result({
          ...run,
          submitWithheld: withheld,
          hint: browserHint(deps, `https://www.kaggle.com/competitions/${slug}`),
        })
      }
      return run
    },
  })

  // ── advanced ─────────────────────────────────────────────────────────────

  make({
    name: 'kaggle_solutions',
    description: '检索榜单上的公开解法 writeup。需要可选的 kaggle-skill 桥接；没装时返回安装方式而不是报错。',
    parameters: {
      slug: slugParam.slug,
      top: { type: 'number', description: 'How many writeups (default 3).' },
    },
    run: async (args) => {
      const argv = ['solutions', String(args.slug ?? '')]
      if (typeof args.top === 'number' && Number.isFinite(args.top)) argv.push('--top', String(Math.round(args.top)))
      return invoke(deps, argv)
    },
  })

  make({
    name: 'kaggle_blend',
    description: '多方案融合：按 OOF 拟合权重、剔除高相关候选。需要已归档节点带 OOF（先用 kaggle_oof 补）。',
    parameters: { slug: slugParam.slug },
    run: async (args) => invoke(deps, ['blend', String(args.slug ?? '')], { timeoutMs: 0 })
  })

  make({
    name: 'kaggle_archive',
    description: '归档某个迭代节点的产物（代码 + 预测），只增不删，用于复现与回退。',
    parameters: {
      slug: slugParam.slug,
      node: { type: 'string', description: 'Node id from journal.json.' },
    },
    run: async (args) => {
      const argv = ['archive', String(args.slug ?? '')]
      if (typeof args.node === 'string' && args.node.trim()) argv.push('--node', args.node.trim())
      return invoke(deps, argv)
    },
  })

  make({
    name: 'kaggle_restore',
    description: '回退到某个已归档节点（之后 write-sub 会用它的预测）。',
    parameters: {
      slug: slugParam.slug,
      node: { type: 'string', required: true, description: 'Archived node id.' },
    },
    run: async (args) => {
      const node = String(args.node ?? '').trim()
      if (!node) return result({ ok: false, error: 'node 不能为空（见 journal.json 的节点 id）' })
      return invoke(deps, ['restore', String(args.slug ?? ''), '--node', node])
    },
  })

  make({
    name: 'kaggle_oof',
    description: '给已归档节点补跑 K 折 OOF 预测。融合权重需要它；只在确定要留的节点上跑，费时。',
    parameters: {
      slug: slugParam.slug,
      folds: { type: 'number', description: 'OOF folds (default 5).' },
    },
    run: async (args) => {
      const argv = ['oof', String(args.slug ?? '')]
      if (typeof args.folds === 'number' && Number.isFinite(args.folds)) argv.push('--folds', String(Math.round(args.folds)))
      return invoke(deps, argv, { timeoutMs: 0 })
    },
  })

  make({
    name: 'kaggle_prune',
    description: '清理无用的 gen_* 候选目录（保留已归档与当前 best）。dryRun 默认 true，先看清单再删。',
    parameters: {
      slug: slugParam.slug,
      dryRun: { type: 'boolean', description: 'Default true: only list what would be deleted.' },
    },
    run: async (args) => {
      const argv = ['prune', String(args.slug ?? '')]
      if (args.dryRun !== false) argv.push('--dry-run')
      return invoke(deps, argv)
    },
  })

  // ── integration ──────────────────────────────────────────────────────────

  make({
    name: 'kaggle_ego_status',
    description: 'ego 浏览器是否可用（可选组件）。可用时 agent 能在必须点按钮的场景自动接管。',
    parameters: {},
    run: async () =>
      result({
        ok: true,
        available: deps.egoAvailable(),
        detail: deps.egoAvailable()
          ? 'ego 工具已注入，可用 ego_navigate / ego_click'
          : '未安装 dsh-ego-browser。可选：面板「安装 ego」会把它装进当前 profile。',
      }),
  })

  make({
    name: 'kaggle_ego_install',
    description:
      '一键安装 dsh-ego-browser 到当前 profile（可选组件）。会修改 profile 的 package.json 与装配层，失败自动回滚。',
    parameters: {
      version: { type: 'string', description: 'Version range; default from settings.' },
    },
    run: async (args) =>
      result({
        ok: true,
        delegated: true,
        version: typeof args.version === 'string' && args.version.trim() ? args.version.trim() : deps.settings().egoVersion,
        note: '安装动作由宿主执行（面板「安装 ego」或 /api/yuan-kaggle/install-ego），工具侧只报告状态',
        available: deps.egoAvailable(),
      }),
  })
}

/** `kaggle_help` content. Kept short: it competes for the model's attention. */
export function helpPayload(topic: string): ToolResult {
  const pipeline = {
    ok: true,
    topic: 'pipeline',
    stages: [
      '1 kaggle_setup — 初始化引擎环境（首次一次性）',
      '2 kaggle_info + kaggle_prepare — 看指标、下数据、切分',
      '3 kaggle_baseline — 拿到 CV 参照分',
      '4 kaggle_iterate — LLM 迭代特征工程（需要 LLM）',
      '5 kaggle_write_sub + kaggle_validate — 生成并校验提交文件',
      '6 kaggle_submit → kaggle_receipt — 提交并读回真实榜分',
    ],
    shortest: 'kaggle_run { slug, rounds, submit, confirm }',
    notes: [
      '提交会消耗当日配额；不带 confirm 只做预检。',
      '迭代不消耗 Kaggle 配额，只花本地时间与 LLM。',
      '大比赛先跑 baseline 看耗时，再决定 iterate 轮数。',
    ],
  }
  const tools = {
    ok: true,
    topic: 'tools',
    core: [
      'kaggle_doctor / kaggle_setup / kaggle_connect',
      'kaggle_list / kaggle_info / kaggle_status / kaggle_leaderboard',
      'kaggle_prepare / kaggle_baseline / kaggle_iterate',
      'kaggle_write_sub / kaggle_rules / kaggle_validate / kaggle_submit / kaggle_receipt',
      'kaggle_run / kaggle_help',
    ],
    advanced: [
      'kaggle_solutions / kaggle_blend / kaggle_archive / kaggle_restore / kaggle_oof / kaggle_prune',
      'kaggle_ego_status / kaggle_ego_install',
    ],
  }
  const submit = {
    ok: true,
    topic: 'submit',
    rules: [
      'requireConfirm 开启时，必须有 confirm: true 才会真正提交。',
      'autoSubmit 只允许 agent 自行决定调用，不跳过 confirm。',
      '未加入比赛不是门槛：实测提交动作本身会把账号加入比赛。',
      '校验（kaggle_validate）不评分，但能挡住白烧配额的错文件。',
    ],
  }
  const trouble = {
    ok: true,
    topic: 'trouble',
    items: [
      '提示先初始化 → 调 kaggle_setup 或点面板「初始化环境」。',
      '提交报「上传通道未就绪」→ Kaggle 上传落在 Google 存储，需要本地代理；面板可填 uploadProxy。',
      '提交报 403 要求接受规则 → 用 ego 打开 /competitions/<slug>/rules 点 Join，或手动点一次。',
      'CV 很高但榜分很低 → 小数据集的 CV 波动大，先看 kaggle_status 的历史提交再判断。',
      '迭代很卡 → 本机 LightGBM n_jobs>1 会挂，threads 保持 1。',
    ],
  }
  if (topic === 'tools') return tools
  if (topic === 'submit') return submit
  if (topic === 'trouble') return trouble
  return { ...pipeline, also: ['tools', 'submit', 'trouble'] }
}

/** True when the engine sources are where we expect them. */
export function enginePresent(engineDir: string): boolean {
  return existsSync(join(engineDir, 'kauto', 'cli.py'))
}
