/**
 * src/client/icons.ts — every glyph in the panel, drawn by hand.
 *
 * No icon font, no emoji, no SVG library: a settings panel that pulls in a glyph
 * package pays a peer dependency and a bundle for eight shapes. These are the
 * eight, as `currentColor` masks so hover / active / dark mode all follow the
 * shell's own colour without per-state CSS.
 *
 * Geometry rules (kept consistent so the set reads as one family):
 *   - 24×24 viewBox, 2px stroke, no fill
 *   - round caps and joins
 *   - optical centre at 12,12; nothing touches the edge (min 3px margin)
 */

type Glyph = { viewBox?: string; path: string; extra?: string }

const encode = (svg: string): string =>
  `url("data:image/svg+xml,${svg
    .replace(/\s+/g, ' ')
    .replace(/#/g, '%23')
    .replace(/"/g, "'")
    .trim()}")`

const wrap = (glyph: Glyph): string => {
  const viewBox = glyph.viewBox ?? '0 0 24 24'
  return encode(
    `<svg xmlns='http://www.w3.org/2000/svg' viewBox='${viewBox}' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>${glyph.path}${glyph.extra ?? ''}</svg>`,
  )
}

/**
 * The plugin's own mark: three bars over a baseline, with a small square at
 * the top right — "leaderboard + submission", which is what the plugin is
 * about. Deliberately not a trophy: that is the obvious choice and every
 * competition tool already uses it.
 */
export const MARK_SVG =
  "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>" +
  "<path d='M4 20h16'/>" +
  "<path d='M7 20v-6'/><path d='M12 20v-10'/><path d='M17 20v-4'/>" +
  "<rect x='15.5' y='3.5' width='5' height='5' rx='1'/>" +
  '</svg>'

/** Masks: `background: currentColor` + one of these. */
export const GLYPH_OK = wrap({ path: "<circle cx='12' cy='12' r='9'/><path d='M8 12.5l2.6 2.6L16.5 9'/>" })
export const GLYPH_WARN = wrap({ path: "<path d='M12 3.5l9 16H3z'/><path d='M12 9.5v4.5'/><path d='M12 17.2v.3'/>" })
export const GLYPH_MISS = wrap({ path: "<circle cx='12' cy='12' r='9'/><path d='M8 12h8'/>" })
export const GLYPH_SPIN = wrap({
  path: "<circle cx='12' cy='12' r='9'/><path d='M12 3a9 9 0 0 1 0 18' opacity='0'/>",
  extra: "<path d='M12 3.5a8.5 8.5 0 0 1 8.5 8.5'/>",
})
export const GLYPH_GEAR = wrap({
  path: "<circle cx='12' cy='12' r='3.2'/>",
  extra: "<path d='M12 3v2.4M12 18.6V21M4.6 12H2M22 12h-2.6M6.7 6.7l1.7 1.7M15.6 15.6l1.7 1.7M17.3 6.7l-1.7 1.7M8.4 15.6l-1.7 1.7'/>",
})
export const GLYPH_PLAY = wrap({ path: "<path d='M8 5.5l11 6.5-11 6.5z'/>" })
export const GLYPH_CHECK = wrap({ path: "<path d='M5 13l4.5 4.5L19 7'/>" })
export const GLYPH_ARROW_RIGHT = wrap({ path: "<path d='M4 12h15'/><path d='M13.5 6l6 6-6 6'/>" })

export type GlyphName = 'ok' | 'warn' | 'miss' | 'spin' | 'gear' | 'play' | 'check' | 'arrow'

export const GLYPHS: Record<GlyphName, string> = {
  ok: GLYPH_OK,
  warn: GLYPH_WARN,
  miss: GLYPH_MISS,
  spin: GLYPH_SPIN,
  gear: GLYPH_GEAR,
  play: GLYPH_PLAY,
  check: GLYPH_CHECK,
  arrow: GLYPH_ARROW_RIGHT,
}

/**
 * One stylesheet for every glyph, keyed by `data-glyph`.
 *
 * A single style block beats inline masks: the SVG data URI is long, and
 * repeating it per row would triple the panel's CSS for no benefit.
 */
export const glyphCss = (() => {
  const rules = Object.entries(GLYPHS).map(
    ([name, uri]) =>
      `[data-ykg-glyph="${name}"]{-webkit-mask:${uri} center/contain no-repeat;mask:${uri} center/contain no-repeat}`,
  )
  return `
[data-ykg-glyph]{display:inline-block;flex:none;width:16px;height:16px;background:currentColor}
${rules.join('\n')}
[data-ykg-glyph="spin"]{animation:ykg-spin 1.1s linear infinite}
@keyframes ykg-spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion: reduce){
  [data-ykg-glyph="spin"]{animation:none}
}
`.trim()
})()
