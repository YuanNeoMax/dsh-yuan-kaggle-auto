/**
 * src/client/board.ts — leaderboard payload → a few readable lines.
 *
 * Extracted from the panel component on purpose: this is the piece with
 * judgement in it (what to show, what to say when a fact is genuinely
 * unavailable), and judgement belongs somewhere a test can reach. The
 * component keeps only the wiring.
 *
 * The rule that matters here: **"no medals" and "no medal line available"
 * are different claims.** The CLI fallback cannot report medal lines, so
 * saying "this competition awards no medals" there would be wrong about the
 * competition rather than merely incomplete.
 */

export interface BoardPayload extends Record<string, unknown> {
  rows?: Array<Record<string, unknown>>
  medal_lines?: Record<string, unknown>
  team_count?: unknown
  teamCount?: unknown
  user_rank?: unknown
  userRank?: unknown
  awards_medals?: unknown
  source?: unknown
  deadline?: unknown
  ended?: unknown
}

const label = (zone: string): string =>
  zone === 'gold' ? '金牌线' : zone === 'silver' ? '银牌线' : '铜牌线'

export function summariseBoard(payload: BoardPayload): string[] {
  const lines: string[] = []
  const rows = payload.rows
  const medals = payload.medal_lines ?? {}
  const total = payload.team_count ?? payload.teamCount
  const mine = payload.user_rank ?? payload.userRank

  if (typeof total === 'number' || typeof total === 'string') {
    lines.push(`队伍数  ${total}`)
  }
  if (Array.isArray(rows) && rows.length > 0) {
    const top = rows[0] ?? {}
    const team = String(top.team ?? top.TeamName ?? '?')
    const score = String(top.score ?? top.Score ?? '')
    lines.push(`榜首  ${team}  ${score}`.trimEnd())
  }

  lines.push(
    `我的名次  ${mine === undefined || mine === null ? '（未加入或未提交）' : String(mine)}`,
  )

  const zones = ['gold', 'silver', 'bronze']
  const present = zones.filter((zone) => medals[zone] !== undefined)
  if (present.length > 0) {
    for (const zone of present) lines.push(`${label(zone)}  ${String(medals[zone])}`)
  } else if (payload.awards_medals === false) {
    lines.push('奖牌  这场比赛不发奖牌')
  } else if (typeof payload.source === 'string' && payload.source === 'kaggle-cli') {
    // Do not imply "no medals" when the answer simply was not fetched.
    lines.push('奖牌线  需要 skill 桥接才能读到')
  }

  // Deadline is worth showing: a finished competition means further iteration
  // cannot change anything, which is exactly the decision this panel serves.
  const deadline = payload.deadline
  if (typeof deadline === 'string' && deadline !== '') {
    lines.push(`${payload.ended === true ? '已结束' : '截止'}  ${deadline}`)
  }

  return lines
}
