/**
 * src/config.ts — the plugin's own configuration surface.
 *
 * Two readers, one source of truth:
 *   - the host imports `Config` (schemastery) so DSH validates the Loader entry
 *     and the settings form can render fields;
 *   - the client imports `DEFAULTS` + `resolveConfig` (plain TS, no schemastery)
 *     so the browser bundle stays small.
 *
 * Every user-facing field is `.volatile()`: the Loader then takes the
 * live-commit path on save and a settings edit reaches the next engine
 * invocation without a remount. `.volatile()` returns a COPY, so the marked
 * schemas are collected into an object (calling it for its side effect leaves
 * every field non-volatile and the settings service reports
 * "no volatile fields" for the entry — measured on dsh-ego-browser).
 */
import z from '@deepseek-ai/schemastery'

/** The Loader entry id; also the settings namespace and the client row key. */
export const ENTRY_ID = 'yuan-kaggle-auto'

export type LlmProvider =
  | 'auto'
  | 'ofm'
  | 'deepseek'
  | 'trae'
  | 'openai'
  | 'dashscope'
  | 'siliconflow'
  | 'openrouter'
  | 'custom'

export interface Settings {
  /** Node-executable name or absolute path to a Python ≥3.10 interpreter. Empty = auto-detect. */
  pythonPath: string
  /** Where the engine venv lives. Empty = <DSH_HOME>/kaggle-auto/venv. */
  venvDir: string
  /** Where competition data, splits and run products go. Empty = <DSH_HOME>/kaggle-auto/work. */
  workRoot: string
  /** Kaggle CLI entry. Empty = the `kaggle` inside the engine venv. */
  kaggleCliPath: string
  /**
   * LLM channel for feature-engineering iteration. `auto` reuses whatever the
   * host already configured (DSH gateway / our-free-model / provider keys), so
   * the common case needs no key at all.
   */
  llmProvider: LlmProvider
  /** Override the provider base URL (OpenAI-compatible). */
  llmBaseUrl: string
  /** Override the model id. */
  llmModel: string
  /** Optional API key. Empty = reuse the host's credentials. */
  llmApiKey: string
  /**
   * Proxy used ONLY to reach the Google storage endpoint that Kaggle's
   * submission upload lands on. Empty = probe common local ports.
   * Kaggle's own domains always bypass the proxy (a system proxy breaks their TLS).
   */
  uploadProxy: string
  /** LightGBM / OpenMP threads. 1 is the safe default — n_jobs>1 can hang here. */
  threads: number
  /**
   * Whether the agent may call `kaggle_submit` / `kaggle_run --submit` on its
   * own initiative. It never bypasses `requireConfirm`.
   */
  autoSubmit: boolean
  /** When true, a submission needs an explicit `confirm: true` argument. Recommended. */
  requireConfirm: boolean
  /** Advertise optional ego-browser assistance to the model when it is available. */
  egoAssist: boolean
  /** Version range installed by the one-click ego installer. */
  egoVersion: string
  /** Optional vendored kaggle-skill checkout. Empty = bridge disabled (everything still works). */
  skillBridgeDir: string
  /** Use the OS sandbox for candidate code. Windows only; a no-op elsewhere. */
  sandboxEnabled: boolean
  /** Upper bound for `kaggle_run` iteration rounds. */
  maxRounds: number
}

export const DEFAULTS: Settings = {
  pythonPath: '',
  venvDir: '',
  workRoot: '',
  kaggleCliPath: '',
  llmProvider: 'auto',
  llmBaseUrl: '',
  llmModel: '',
  llmApiKey: '',
  uploadProxy: '',
  threads: 1,
  autoSubmit: false,
  requireConfirm: true,
  egoAssist: true,
  egoVersion: '^0.9',
  skillBridgeDir: '',
  sandboxEnabled: true,
  maxRounds: 4,
}

const LLM_PROVIDERS: readonly LlmProvider[] = [
  'auto', 'ofm', 'deepseek', 'trae', 'openai', 'dashscope', 'siliconflow', 'openrouter', 'custom',
]

const trim = (value: unknown, fallback: string, max = 1024): string =>
  typeof value === 'string' ? value.trim().slice(0, max) : fallback

const bool = (value: unknown, fallback: boolean): boolean =>
  typeof value === 'boolean' ? value : fallback

const int = (value: unknown, fallback: number, min: number, max: number): number =>
  typeof value === 'number' && Number.isFinite(value)
    ? Math.min(max, Math.max(min, Math.round(value)))
    : fallback

/**
 * Tolerant reader for the Loader entry config.
 *
 * `.volatile()` fields arrive as `{ get() }` references once the settings
 * service owns them, so each value is unwrapped before validation. Anything
 * unrecognised falls back to the default instead of failing the entry —
 * a bad field must never take the plugin down.
 */
export function resolveConfig(raw: unknown): Settings {
  const out: Record<string, unknown> = {}
  if (raw !== null && typeof raw === 'object') {
    for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
      out[key] =
        value !== null && typeof value === 'object' && typeof (value as { get?: unknown }).get === 'function'
          ? (value as { get: () => unknown }).get()
          : value
    }
  }
  const provider = out.llmProvider
  return {
    pythonPath: trim(out.pythonPath, DEFAULTS.pythonPath),
    venvDir: trim(out.venvDir, DEFAULTS.venvDir),
    workRoot: trim(out.workRoot, DEFAULTS.workRoot),
    kaggleCliPath: trim(out.kaggleCliPath, DEFAULTS.kaggleCliPath),
    llmProvider:
      typeof provider === 'string' && (LLM_PROVIDERS as readonly string[]).includes(provider)
        ? (provider as LlmProvider)
        : DEFAULTS.llmProvider,
    llmBaseUrl: trim(out.llmBaseUrl, DEFAULTS.llmBaseUrl),
    llmModel: trim(out.llmModel, DEFAULTS.llmModel, 128),
    llmApiKey: trim(out.llmApiKey, DEFAULTS.llmApiKey),
    uploadProxy: trim(out.uploadProxy, DEFAULTS.uploadProxy, 256),
    threads: int(out.threads, DEFAULTS.threads, 1, 32),
    autoSubmit: bool(out.autoSubmit, DEFAULTS.autoSubmit),
    requireConfirm: bool(out.requireConfirm, DEFAULTS.requireConfirm),
    egoAssist: bool(out.egoAssist, DEFAULTS.egoAssist),
    egoVersion: trim(out.egoVersion, DEFAULTS.egoVersion, 64) || DEFAULTS.egoVersion,
    skillBridgeDir: trim(out.skillBridgeDir, DEFAULTS.skillBridgeDir),
    sandboxEnabled: bool(out.sandboxEnabled, DEFAULTS.sandboxEnabled),
    maxRounds: int(out.maxRounds, DEFAULTS.maxRounds, 1, 12),
  }
}

const field = <T>(schema: T): T => (schema as unknown as { volatile(): T }).volatile()

// `.volatile()` RETURNED A COPY, so it must be applied to the map, not
// discarded per field — otherwise every field is non-volatile.
const volatilePrefs = Object.fromEntries(
  Object.entries({
    pythonPath: z.string().description('Python ≥3.10 interpreter. Empty = auto-detect.'),
    venvDir: z.string().description('Engine venv directory. Empty = <DSH_HOME>/kaggle-auto/venv.'),
    workRoot: z.string().description('Competition data and run products. Empty = <DSH_HOME>/kaggle-auto/work.'),
    kaggleCliPath: z.string().description('Kaggle CLI entry. Empty = the one inside the engine venv.'),
    llmProvider: z
      .union(LLM_PROVIDERS.map((value) => z.const(value)) as [never, ...never[]])
      .description('LLM channel for iteration. auto reuses the host configuration.'),
    llmBaseUrl: z.string().description('OpenAI-compatible base URL override.'),
    llmModel: z.string().description('Model id override.'),
    llmApiKey: z.string().role('secret').description('API key. Empty = reuse the host credentials.'),
    uploadProxy: z.string().description('Proxy for the Google storage upload endpoint only. Empty = probe local ports.'),
    threads: z.number().min(1).max(32).step(1).description('Engine threads. 1 is the safe default.'),
    autoSubmit: z.boolean().description('Allow the agent to submit on its own initiative (never bypasses confirm).'),
    requireConfirm: z.boolean().description('Require an explicit confirm argument before submitting.'),
    egoAssist: z.boolean().description('Offer optional ego-browser assistance when it is installed.'),
    egoVersion: z.string().description('Version range for the one-click ego installer.'),
    skillBridgeDir: z.string().description('Optional kaggle-skill checkout. Empty = bridge disabled.'),
    sandboxEnabled: z.boolean().description('Sandbox candidate code (Windows only).'),
    maxRounds: z.number().min(1).max(12).step(1).description('Iteration round ceiling for kaggle_run.'),
  }).map(([key, schema]) => [key, field(schema)]),
)

export const Config = z.object({
  ...volatilePrefs,
  /** Internal: engine build stamp, written by the host after a successful setup. */
  engineVersion: z.string(),
}) as unknown as z<Record<string, unknown>>
