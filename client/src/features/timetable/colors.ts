/**
 * Timetable colour tokens.
 *
 * The backend stores a token name (never hex) so a colour follows the
 * light / dark theme. Each token maps to ``.sh-timetable-c--<token>``
 * in ``styles/app.css``, which sets ``--tt-bg`` (block tint),
 * ``--tt-edge`` (left border / stripe) and ``--tt-fg`` (text, ≥ 4.5:1
 * on ``--tt-bg`` in both themes).
 */
import type { Timetable, TimetableColor, TimetableEntry } from '@/types'
import {
  TOKEN_COLORS, colorClass, hashColor, normalizeTokenKey,
} from '@/utils/tokenColor'

// The palette and the name → colour hash are shared with task labels.
export { colorClass, hashColor }

export const TIMETABLE_COLORS: readonly TimetableColor[] = TOKEN_COLORS

/** Lowercase, trimmed, diacritics stripped — "Música" ≡ "musica". */
export const normalizeSubject = normalizeTokenKey

const cache = new WeakMap<Timetable, Map<string, TimetableColor>>()

/**
 * Automatic colours for a timetable's uncoloured subjects.
 *
 * The distinct (normalised) titles of lessons without a colour are
 * sorted (plain code-unit order on the case-folded title — the same in
 * every locale) and handed palette tokens in that order, skipping
 * tokens other subjects already use explicitly; only titles beyond the
 * palette fall back to a hash. So twelve subjects get twelve colours,
 * and adding another "Mathe" lesson never recolours anything. Cached
 * per timetable object (a new version is a new object).
 */
export function subjectColors(tt: Timetable): Map<string, TimetableColor> {
  const hit = cache.get(tt)
  if (hit) return hit
  const explicit = new Set<TimetableColor>()
  const titles = new Set<string>()
  for (const e of tt.entries) {
    if (e.kind !== 'lesson') continue
    const key = normalizeSubject(e.title ?? '')
    if (e.color) explicit.add(e.color)
    else if (key) titles.add(key)
  }
  const free = TIMETABLE_COLORS.filter(c => !explicit.has(c))
  const out = new Map<string, TimetableColor>()
  ;[...titles].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0)).forEach((key, i) => {
    out.set(key, i < free.length ? free[i] : hashColor(key))
  })
  cache.set(tt, out)
  return out
}

/** The colour an entry renders with: its own, else its subject's
 *  automatic one; ``null`` (neutral) for an untitled slot. */
export function entryColor(
  entry: Pick<TimetableEntry, 'color' | 'title'>,
  tt: Timetable,
): TimetableColor | null {
  if (entry.color) return entry.color
  const key = normalizeSubject(entry.title ?? '')
  return key ? (subjectColors(tt).get(key) ?? hashColor(key)) : null
}
