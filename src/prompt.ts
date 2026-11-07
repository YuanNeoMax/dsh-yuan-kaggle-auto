/**
 * src/prompt.ts — the model-facing capability note.
 *
 * Kept under 180 characters of instruction per section and injected as ONE
 * section: it competes with every other byte of system prompt, so it says what
 * the model cannot infer (the pipeline order, the submit gate) and nothing it
 * could discover by calling `kaggle_help`.
 */
import type { Settings } from './config.ts'

export function promptText(settings: Settings): string {
  const lines = [
    'Kaggle 竞赛自动化（kauto）已接入。',
    '最短路径：kaggle_run { slug, rounds, submit, confirm }，它会跑 下载→切分→基线→LLM迭代→写提交→(可选)提交→读分。',
    `提交是外部副作用：${settings.requireConfirm ? '必须带 confirm: true 才会真正提交' : '已关闭确认门，但仍需 confirm: true'}，否则只做预检。`,
    '迭代消耗本地时间与 LLM，不消耗 Kaggle 配额；提交消耗当日配额。',
    '不确定下一步就调 kaggle_help。',
  ]
  return lines.join('\n').slice(0, 900)
}
