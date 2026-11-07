/**
 * src/client/index.ts — the "Kaggle 自动参赛" settings section.
 *
 * Reads and writes the plugin's own Loader entry through
 * `remote.settings.describe() / mutate()`; every field is volatile, so a save
 * reaches the next engine call without a remount. Live status and the
 * one-click actions come from the host routes in ../routes.ts.
 *
 * Visual rules this file follows, because they are easy to break silently:
 *   - two weights only (default and `font-medium`) — the shell has no 600;
 *   - colours only from the semantic variables, never a literal;
 *   - one bounded box, dividers inside it;
 *   - no emoji anywhere, every glyph is drawn in ./icons.ts;
 *   - a status row is a 3-column grid, not `justify-between`, so it cannot
 *     overflow at a narrow sidebar.
 */
import * as React from 'react'
import { DEFAULTS, ENTRY_ID, resolveConfig, type Settings } from '../config.ts'
import { GLYPHS, MARK_SVG, glyphCss, type GlyphName } from './icons.ts'

interface Result<T> {
  ok: boolean
  error?: { message: string }
  value?: T
}

interface FieldView {
  path: (string | number)[]
  kind?: string
  title?: string
  description?: string
  required?: boolean
  enum?: Array<string | number>
  min?: number
  max?: number
  step?: number
  role?: string
}

interface NamespaceView {
  ns: string
  fields?: FieldView[]
  revision?: number
  values?: Record<string, unknown>
}

interface ClientContext {
  effect(fn: () => () => void, label?: string): void
  slots: {
    inject(name: string, fn: () => unknown): void
    register(options: Record<string, unknown>, render: () => unknown): unknown
  }
  remote?: {
    settings: {
      describe(): Promise<Result<{ writable: boolean; namespaces: NamespaceView[] }>>
      mutate(ns: string, ops: Array<{ op: 'set' | 'unset'; path: string[]; value?: unknown }>, expectedRevision?: number): Promise<Result<unknown>>
    }
  }
  /** Optional: lets the panel call its own host routes with the session cookie. */
  fetch?(input: string, init?: RequestInit): Promise<Response>
}

type Draft = Record<string, string | boolean | number>

const SECTION_LABEL = 'Kaggle 自动参赛'

/** One-line labels. Chinese first because the shell's default locale is zh. */
const FIELD_ORDER: Array<{ key: keyof Settings; label: string; hint: string; kind: 'text' | 'number' | 'bool' | 'select' }> = [
  { key: 'pythonPath', label: 'Python 解释器', hint: '留空自动探测。填绝对路径可指定某个环境。', kind: 'text' },
  { key: 'venvDir', label: '引擎环境目录', hint: '留空用 <DSH_HOME>/kaggle-auto/venv。', kind: 'text' },
  { key: 'workRoot', label: '工作目录', hint: '比赛数据与运行产物所在目录。留空用 <DSH_HOME>/kaggle-auto/work。', kind: 'text' },
  { key: 'kaggleCliPath', label: 'Kaggle CLI', hint: '留空用引擎环境里安装的那个。', kind: 'text' },
  { key: 'llmProvider', label: 'LLM 通道', hint: 'auto 会复用 DSH 已配置的通道，通常不需要填 key。', kind: 'select' },
  { key: 'llmBaseUrl', label: 'LLM Base URL', hint: 'OpenAI 兼容端点。留空沿用通道默认。', kind: 'text' },
  { key: 'llmModel', label: 'LLM 模型', hint: '留空沿用通道默认模型。', kind: 'text' },
  { key: 'llmApiKey', label: 'LLM API Key', hint: '留空复用宿主的凭据。填了会覆盖。', kind: 'text' },
  { key: 'uploadProxy', label: '上传代理', hint: '只用于 Kaggle 提交时访问 Google 存储。留空自动探测本机常见端口。', kind: 'text' },
  { key: 'threads', label: '线程数', hint: 'LightGBM 在部分机器上多线程会挂住，默认 1。', kind: 'number' },
  { key: 'maxRounds', label: '迭代轮数上限', hint: 'kaggle_run 的默认轮数。', kind: 'number' },
  { key: 'egoVersion', label: 'ego 安装版本', hint: '一键安装 ego 时使用的版本范围。', kind: 'text' },
  { key: 'skillBridgeDir', label: 'skill 桥接目录', hint: '可选。留空即禁用桥接，核心流程不受影响。', kind: 'text' },
  { key: 'autoSubmit', label: '允许 agent 自行提交', hint: '仅表示 agent 可以决定调用提交，不会跳过确认。', kind: 'bool' },
  { key: 'requireConfirm', label: '提交前需要确认', hint: '开启时，提交必须带 confirm: true。建议保持开启。', kind: 'bool' },
  { key: 'egoAssist', label: '启用 ego 协助', hint: '安装 ego 后，需要点按钮的场景交给浏览器工具。', kind: 'bool' },
  { key: 'sandboxEnabled', label: '候选代码沙箱', hint: '仅 Windows 生效，非 Windows 自动忽略。', kind: 'bool' },
]

const LLM_OPTIONS = ['auto', 'ofm', 'deepseek', 'trae', 'openai', 'dashscope', 'siliconflow', 'openrouter', 'custom']

interface StatusItem {
  id: string
  label: string
  level: 'ok' | 'warn' | 'missing'
  detail?: string
  fix?: string
}

interface StatusPayload {
  ok: boolean
  ready: boolean
  items: StatusItem[]
  paths: { venvDir: string; workRoot: string; engineDir: string }
  ego?: { available: boolean }
}

interface JobPayload {
  ok: boolean
  id?: string
  kind?: string
  lines?: string[]
  nextOffset?: number
  done?: boolean
  succeeded?: boolean
  error?: string
}

const LEVEL_GLYPH: Record<StatusItem['level'], GlyphName> = {
  ok: 'ok',
  warn: 'warn',
  missing: 'miss',
}

function draftFrom(settings: Settings): Draft {
  const draft: Draft = {}
  for (const field of FIELD_ORDER) {
    const value = settings[field.key]
    draft[field.key] = typeof value === 'boolean' || typeof value === 'number' ? value : String(value ?? '')
  }
  return draft
}

/** Turn the draft back into settings the host can persist. */
function settingsFromDraft(draft: Draft): Settings {
  const base: Record<string, unknown> = { ...DEFAULTS }
  for (const field of FIELD_ORDER) {
    const value = draft[field.key]
    if (field.kind === 'bool') base[field.key] = value === true
    else if (field.kind === 'number') {
      const numeric = typeof value === 'number' ? value : Number(String(value).trim())
      base[field.key] = Number.isFinite(numeric) ? numeric : DEFAULTS[field.key]
    } else base[field.key] = String(value ?? '')
  }
  return resolveConfig(base)
}

export function registerPanel(ctx: ClientContext): void {
  ctx.effect(() => {
    const style = document.createElement('style')
    style.setAttribute('data-yuan-kaggle-auto', 'glyphs')
    style.textContent = glyphCss
    document.head.appendChild(style)
    return () => { style.remove() }
  }, 'yuan-kaggle-auto: glyph stylesheet')

  ctx.slots.inject('settings.section', () =>
    ctx.slots.register(
      {
        name: 'settings.section',
        id: ENTRY_ID,
        order: 62,
        label: () => SECTION_LABEL,
      },
      () => React.createElement(Section, { ctx }),
    ),
  )
}

function Section({ ctx }: { ctx: ClientContext }): React.ReactElement {
  const [writable, setWritable] = React.useState(true)
  const [revision, setRevision] = React.useState<number | undefined>(undefined)
  const [draft, setDraft] = React.useState<Draft>(() => draftFrom(DEFAULTS))
  const [dirty, setDirty] = React.useState(false)
  const [status, setStatus] = React.useState<StatusPayload | undefined>(undefined)
  const [busy, setBusy] = React.useState<string>('')
  const [log, setLog] = React.useState<string[]>([])
  const [error, setError] = React.useState<string>('')

  const dirtyRef = React.useRef(false)
  dirtyRef.current = dirty

  const load = React.useCallback(async () => {
    const result = await ctx.remote?.settings.describe()
    if (!result || !result.ok) {
      setError(result?.error?.message ?? '读取设置失败')
      return
    }
    setWritable(result.value?.writable !== false)
    const view = result.value?.namespaces.find((item) => item.ns === ENTRY_ID)
    setRevision(view?.revision)
    if (!dirtyRef.current) {
      setDraft(draftFrom(resolveConfig(view?.values ?? {})))
    }
  }, [ctx])

  const refreshStatus = React.useCallback(async () => {
    if (!ctx.fetch) return
    try {
      const response = await ctx.fetch('/api/yuan-kaggle/status')
      const payload = (await response.json()) as StatusPayload
      if (payload.ok) setStatus(payload)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [ctx])

  React.useEffect(() => {
    void load()
    void refreshStatus()
  }, [load, refreshStatus])

  const save = React.useCallback(async () => {
    const settings = settingsFromDraft(draft)
    const ops: Array<{ op: 'set'; path: string[]; value: unknown }> = []
    for (const field of FIELD_ORDER) ops.push({ op: 'set', path: [field.key], value: settings[field.key] })
    const result = await ctx.remote?.settings.mutate(ENTRY_ID, ops, revision)
    if (!result || !result.ok) {
      setError(result?.error?.message ?? '保存失败')
      return
    }
    setDirty(false)
    dirtyRef.current = false
    setError('')
    void load()
    void refreshStatus()
  }, [ctx, draft, revision, load, refreshStatus])

  const runJob = React.useCallback(
    async (kind: string, path: string, body?: unknown) => {
      if (!ctx.fetch) {
        setError('宿主未提供 fetch，无法执行动作')
        return
      }
      setBusy(kind)
      setLog([])
      setError('')
      try {
        const response = await ctx.fetch(path, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body ?? {}),
        })
        const started = (await response.json()) as { ok: boolean; jobId?: string; error?: string; command?: string; note?: string }
        if (!started.ok) {
          setError(started.error ?? '动作未能启动')
          return
        }
        if (started.command) {
          setLog([`需要手动执行：`, started.command])
          return
        }
        const jobId = started.jobId
        if (!jobId) {
          setLog([started.note ?? '已完成'])
          return
        }
        // Poll until the job reports done. 500 ms keeps the log feeling live
        // without hammering the host; the cap is generous because venv creation
        // legitimately takes minutes.
        let offset = 0
        for (let tick = 0; tick < 2400; tick += 1) {
          await new Promise((resolve) => setTimeout(resolve, 500))
          const poll = await ctx.fetch(`/api/yuan-kaggle/job?id=${encodeURIComponent(jobId)}&offset=${offset}`)
          const payload = (await poll.json()) as JobPayload
          if (!payload.ok) {
            setError(payload.error ?? '任务查询失败')
            break
          }
          if (payload.lines && payload.lines.length > 0) {
            offset = payload.nextOffset ?? offset + payload.lines.length
            setLog((previous) => [...previous, ...payload.lines!].slice(-400))
          }
          if (payload.done) {
            setBusy('')
            void refreshStatus()
            break
          }
        }
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause))
      } finally {
        setBusy((current) => (current === kind ? '' : current))
      }
    },
    [ctx, refreshStatus],
  )

  const statusRows = status?.items ?? []
  const ready = status?.ready === true

  return (
    <div className="bg-layer border border-line rounded-lg p-4 text-label">
      <Header ready={ready} status={status} />

      <section className="py-3 border-t border-line">
        <div className="flex flex-wrap items-center gap-2">
          <PrimaryButton
            label={busy === 'setup' ? '初始化中…' : '初始化环境'}
            onClick={() => void runJob('setup', '/api/yuan-kaggle/setup', { force: false })}
            busy={busy === 'setup'}
            disabled={Boolean(busy)}
          />
          <SecondaryButton
            label="体检"
            onClick={() => void runJob('doctor', '/api/yuan-kaggle/doctor')}
            busy={busy === 'doctor'}
            disabled={Boolean(busy)}
          />
          <SecondaryButton
            label="安装 ego"
            onClick={() => void runJob('install-ego', '/api/yuan-kaggle/install-ego')}
            busy={busy === 'install-ego'}
            disabled={Boolean(busy)}
          />
          <SecondaryButton
            label="skill 桥接"
            onClick={() => void runJob('install-skill', '/api/yuan-kaggle/install-skill')}
            busy={busy === 'install-skill'}
            disabled={Boolean(busy)}
          />
        </div>
        <p className="text-muted text-sm mt-2">
          初始化会创建独立 venv 并安装 pandas / numpy / scikit-learn / lightgbm / kaggle，只需一次。之后命中标记即秒回。
        </p>
      </section>

      <section className="py-3 border-t border-line">
        <div className="text-sm text-muted mb-2">状态</div>
        {statusRows.length === 0 ? (
          <p className="text-muted text-sm">尚未探测。点「体检」刷新。</p>
        ) : (
          <div className="divide-y divide-line">
            {statusRows.map((item) => (
              <StatusRow key={item.id} item={item} />
            ))}
          </div>
        )}
        {status && (
          <dl className="grid grid-cols-1 gap-1 mt-3 text-sm text-muted @[30rem]:grid-cols-[auto_1fr] @[30rem]:gap-x-3">
            <dt>工作目录</dt>
            <dd className="text-label break-all">{status.paths.workRoot}</dd>
            <dt>引擎环境</dt>
            <dd className="text-label break-all">{status.paths.venvDir}</dd>
          </dl>
        )}
      </section>

      <section className="py-3 border-t border-line">
        <div className="text-sm text-muted mb-2">配置</div>
        <div className="grid grid-cols-1 gap-3 @[36rem]:grid-cols-2">
          {FIELD_ORDER.map((field) => (
            <Field
              key={field.key}
              label={field.label}
              hint={field.hint}
              kind={field.kind}
              value={draft[field.key] ?? (field.kind === 'bool' ? false : '')}
              disabled={!writable}
              onChange={(value) => {
                setDirty(true)
                dirtyRef.current = true
                setDraft((previous) => ({ ...previous, [field.key]: value }))
              }}
            />
          ))}
        </div>
        <div className="flex items-center gap-3 mt-3">
          <PrimaryButton label="保存" onClick={() => void save()} disabled={!dirty || !writable} busy={false} />
          <span className="text-muted text-sm">{dirty ? '有未保存的修改' : '已保存'}</span>
        </div>
      </section>

      {(log.length > 0 || busy !== '' || error !== '') && (
        <section className="py-3 border-t border-line">
          <div className="text-sm text-muted mb-2">日志</div>
          {error !== '' && <p className="text-danger text-sm mb-2 break-words">{error}</p>}
          <pre className="bg-page border border-line rounded-md p-2 text-xs text-label max-h-64 overflow-auto whitespace-pre-wrap break-words">
            {log.length === 0 ? '（等待输出…）' : log.join('\n')}
          </pre>
        </section>
      )}
    </div>
  )
}

function Header({ ready, status }: { ready: boolean; status?: StatusPayload }): React.ReactElement {
  const glyph: GlyphName = status === undefined ? 'spin' : ready ? 'check' : 'warn'
  return (
    <div className="flex items-start gap-3">
      <span
        className="shrink-0 w-6 h-6 text-label"
        style={{
          background: `currentColor`,
          WebkitMask: `url("data:image/svg+xml,${MARK_SVG.replace(/\s+/g, ' ').replace(/#/g, '%23')}") center/contain no-repeat`,
          mask: `url("data:image/svg+xml,${MARK_SVG.replace(/\s+/g, ' ').replace(/#/g, '%23')}") center/contain no-repeat`,
        } as React.CSSProperties}
      />
      <div className="min-w-0">
        <h3 className="text-label font-medium">Kaggle 自动参赛</h3>
        <p className="text-muted text-sm">
          下载数据、切分验证集、跑基线、LLM 迭代特征、融合、校验、提交、读回榜分，全流程交给 AI。
        </p>
      </div>
      <span className="ml-auto shrink-0 text-muted" data-ykg-glyph={glyph} />
    </div>
  )
}

function StatusRow({ item }: { item: StatusItem }): React.ReactElement {
  return (
    <div className="grid grid-cols-[16px_1fr] gap-x-3 gap-y-0.5 py-2 @[30rem]:grid-cols-[16px_9rem_1fr]">
      <span
        className={item.level === 'ok' ? 'text-label' : item.level === 'warn' ? 'text-warn' : 'text-danger'}
        data-ykg-glyph={LEVEL_GLYPH[item.level]}
        aria-hidden="true"
      />
      <span className="text-label text-sm">{item.label}</span>
      <span className="text-muted text-sm break-words @[30rem]:col-start-3">
        {item.detail ?? ''}
        {item.level !== 'ok' && item.fix ? <> · {item.fix}</> : null}
      </span>
    </div>
  )
}

function Field(props: {
  label: string
  hint: string
  kind: 'text' | 'number' | 'bool' | 'select'
  value: string | boolean | number
  disabled: boolean
  onChange: (value: string | boolean | number) => void
}): React.ReactElement {
  const id = React.useId()
  const inputClass =
    'bg-page border border-line rounded-md px-2 py-1 text-sm text-label w-full disabled:opacity-60'
  return (
    <div className="min-w-0">
      <label htmlFor={id} className="block text-sm text-label font-medium">
        {props.label}
      </label>
      {props.kind === 'bool' ? (
        <div className="flex items-center gap-2 mt-1">
          <input
            id={id}
            type="checkbox"
            checked={props.value === true}
            disabled={props.disabled}
            onChange={(event) => props.onChange(event.target.checked)}
            className="accent-current"
          />
          <span className="text-muted text-sm">{props.value === true ? '开启' : '关闭'}</span>
        </div>
      ) : props.kind === 'select' ? (
        <select
          id={id}
          value={String(props.value ?? '')}
          disabled={props.disabled}
          onChange={(event) => props.onChange(event.target.value)}
          className={`${inputClass} mt-1`}
        >
          {LLM_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      ) : (
        <input
          id={id}
          type={props.kind === 'number' ? 'number' : 'text'}
          value={String(props.value ?? '')}
          disabled={props.disabled}
          onChange={(event) => {
            // Keep the raw string so a cleared number field stays empty instead
            // of snapping to 0 while the reader is still typing.
            props.onChange(props.kind === 'number' ? event.target.value : event.target.value)
          }}
          className={`${inputClass} mt-1`}
        />
      )}
      <p className="text-muted text-xs mt-1 break-words">{props.hint}</p>
    </div>
  )
}

function PrimaryButton(props: {
  label: string
  onClick: () => void
  disabled?: boolean
  busy: boolean
}): React.ReactElement {
  return (
    <button
      type="button"
      onClick={props.onClick}
      disabled={props.disabled === true}
      className="inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm bg-[var(--dsw-alias-button-primary-fill)] text-[var(--dsw-alias-label-primary-foreground)] disabled:opacity-50"
    >
      {props.busy && <span data-ykg-glyph="spin" aria-hidden="true" />}
      {props.label}
    </button>
  )
}

function SecondaryButton(props: {
  label: string
  onClick: () => void
  disabled?: boolean
  busy: boolean
}): React.ReactElement {
  return (
    <button
      type="button"
      onClick={props.onClick}
      disabled={props.disabled === true}
      className="inline-flex items-center gap-2 rounded-md border border-line-2 px-3 py-1.5 text-sm text-label hover:bg-hover disabled:opacity-50"
    >
      {props.busy && <span data-ykg-glyph="spin" aria-hidden="true" />}
      {props.label}
    </button>
  )
}

export const BETA = false
export const GLYPH_NAMES = Object.keys(GLYPHS)
