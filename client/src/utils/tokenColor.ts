/**
 * Theme-following colour tokens, derived from a name.
 *
 * Timetable subjects and task labels pick a colour from the same
 * palette: a token name (never hex) that maps to
 * ``.sh-timetable-c--<token>`` in ``styles/app.css``, which sets
 * ``--tt-bg`` (tint), ``--tt-edge`` (border / stripe) and ``--tt-fg``
 * (text, ≥ 4.5:1 on ``--tt-bg`` in both themes). The same name always
 * gets the same colour: "Garden" ≡ "garden" ≡ " Gärden "→"garden".
 */
import type { TimetableColor } from '@/types'

export const TOKEN_COLORS: readonly TimetableColor[] = [
  'terracotta', 'amber', 'olive', 'moss', 'teal', 'sky',
  'indigo', 'violet', 'rose', 'slate', 'sand', 'coral',
] as const

/** Lowercase, trimmed, diacritics stripped, inner spaces collapsed —
 *  "Música" ≡ "musica". */
export function normalizeTokenKey(name: string): string {
  return name.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase().trim()
    .replace(/\s+/g, ' ')
}

/** FNV-1a over the normalised name → one palette token. */
export function hashColor(name: string): TimetableColor {
  const key = normalizeTokenKey(name)
  let h = 0x811c9dc5
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return TOKEN_COLORS[h % TOKEN_COLORS.length]
}

export function colorClass(token: TimetableColor | null): string {
  return `sh-timetable-c--${token ?? 'neutral'}`
}
