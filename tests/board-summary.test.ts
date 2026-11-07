import { describe, it, expect } from 'vitest'
import { summariseBoard } from '../src/client/board.ts'

describe('summariseBoard', () => {
  it('reports the three facts that decide whether to iterate again', () => {
    const lines = summariseBoard({
      team_count: 11644,
      rows: [{ team: 'Sas Pav', score: '1.00000' }],
      user_rank: 10034,
      medal_lines: { gold: 0.9, silver: 0.85 },
      awards_medals: true,
    })
    expect(lines).toContain('队伍数  11644')
    expect(lines).toContain('榜首  Sas Pav  1.00000')
    expect(lines).toContain('我的名次  10034')
    expect(lines).toContain('金牌线  0.9')
    expect(lines).toContain('银牌线  0.85')
  })

  it('says a competition awards no medals instead of printing an empty table', () => {
    const lines = summariseBoard({
      team_count: 10,
      rows: [],
      user_rank: null,
      medal_lines: {},
      awards_medals: false,
    })
    expect(lines.some((line) => line.includes('不发奖牌'))).toBe(true)
  })

  it('distinguishes "no medal line available" from "no medals"', () => {
    // The CLI fallback cannot report medal lines. Reporting "no medals" there
    // would be a wrong claim about the competition, not a missing field.
    const lines = summariseBoard({ source: 'kaggle-cli', team_count: 7, medal_lines: {} })
    expect(lines.some((line) => line.includes('需要 skill 桥接'))).toBe(true)
    expect(lines.some((line) => line.includes('不发奖牌'))).toBe(false)
  })

  it('explains an unranked competition rather than showing null', () => {
    const lines = summariseBoard({ rows: [], user_rank: null })
    expect(lines.some((line) => line.includes('未加入或未提交'))).toBe(true)
  })

  it('tolerates the CSV-shaped field names the CLI fallback produces', () => {
    const lines = summariseBoard({
      rows: [{ TeamName: 'a', Score: '0.5' }],
      teamCount: 3,
      userRank: 2,
    })
    expect(lines).toContain('队伍数  3')
    expect(lines).toContain('榜首  a  0.5')
    expect(lines).toContain('我的名次  2')
  })

  it('never throws on an empty payload', () => {
    expect(summariseBoard({})).toEqual(['我的名次  （未加入或未提交）'])
  })
})
