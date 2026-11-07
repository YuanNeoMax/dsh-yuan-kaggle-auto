/**
 * src/paths.ts — where every runtime artifact lives.
 *
 * The engine is a Python program shipped as source inside this package. It
 * must NOT write into `node_modules`: an npm upgrade replaces that directory,
 * and a read-only install would fail outright. So the engine's writable root
 * is a per-user directory under DSH_HOME, passed in via KAUTO_ROOT, and the
 * engine learned to honour it (kapi.ROOT fallback chain, see engine/kauto/kapi.py).
 */
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/** Package root: …/node_modules/dsh-yuan-kaggle-auto (or the checkout when developing). */
export const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

/** Shipped Python engine sources (read-only from the plugin's point of view). */
export const ENGINE_DIR = join(PACKAGE_ROOT, 'engine')

/** DSH home: honours DSH_HOME, else <home>/.dsh (mirrors the host's own rules). */
export function dshHome(env: NodeJS.ProcessEnv = process.env): string {
  const explicit = env.DSH_HOME?.trim()
  if (explicit) return resolve(explicit)
  return join(homedir(), '.dsh')
}

/** Per-user runtime root; everything the engine writes lives under it. */
export function defaultRuntimeRoot(env: NodeJS.ProcessEnv = process.env): string {
  return join(dshHome(env), 'kaggle-auto')
}

export function isWindows(platform: NodeJS.Platform = process.platform): boolean {
  return platform === 'win32'
}

/** `<venv>/Scripts/python.exe` on Windows, `<venv>/bin/python` elsewhere. */
export function venvPython(venvDir: string, platform: NodeJS.Platform = process.platform): string {
  return isWindows(platform) ? join(venvDir, 'Scripts', 'python.exe') : join(venvDir, 'bin', 'python')
}

/** The venv's own Kaggle CLI entry, when the engine installed one there. */
export function venvKaggle(venvDir: string, platform: NodeJS.Platform = process.platform): string {
  return isWindows(platform) ? join(venvDir, 'Scripts', 'kaggle.exe') : join(venvDir, 'bin', 'kaggle')
}

/** Resolve a user-supplied path against cwd; empty stays empty. */
export function expandUserPath(value: string): string {
  const raw = value.trim()
  if (!raw) return ''
  const expanded = raw.startsWith('~') ? join(homedir(), raw.slice(1)) : raw
  return isAbsolute(expanded) ? expanded : resolve(expanded)
}

export interface ResolvedPaths {
  /** Writable root handed to the engine as KAUTO_ROOT. */
  runtimeRoot: string
  /** engine reads data/ and work/ and reports/ under this. */
  workRoot: string
  venvDir: string
  engineDir: string
  /** Absolute Kaggle CLI path, or '' when the user did not pin one. */
  kaggleCli: string
  skillRoot: string
}

/**
 * Turn the settings into concrete paths.
 *
 * `workRoot` is deliberately separate from `runtimeRoot` so a user can point
 * the plugin at an existing kauto workspace (`D:\\mumu\\kaggle-auto\\work`)
 * without the plugin also owning that tree's venv.
 */
export function resolvePaths(settings: {
  venvDir: string
  workRoot: string
  kaggleCliPath: string
  skillBridgeDir: string
}, env: NodeJS.ProcessEnv = process.env): ResolvedPaths {
  const root = defaultRuntimeRoot(env)
  const workRoot = expandUserPath(settings.workRoot) || join(root, 'work')
  const venvDir = expandUserPath(settings.venvDir) || join(root, 'venv')
  return {
    runtimeRoot: root,
    workRoot,
    venvDir,
    engineDir: ENGINE_DIR,
    kaggleCli: expandUserPath(settings.kaggleCliPath),
    skillRoot: expandUserPath(settings.skillBridgeDir),
  }
}

/**
 * The engine derives its own writable root from KAUTO_ROOT. Because it places
 * `data/` `work/` `reports/` directly under that root, and `workRoot` may be
 * overridden independently, the parent is what gets exported — pointing it at
 * `workRoot` itself would nest `work/work/`.
 */
export function engineRootFor(workRoot: string): string {
  return dirname(workRoot)
}

export function engineEntryExists(engineDir: string = ENGINE_DIR): boolean {
  return existsSync(join(engineDir, 'kauto', 'cli.py'))
}
