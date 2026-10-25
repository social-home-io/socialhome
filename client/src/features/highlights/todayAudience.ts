/**
 * Today's highlight audience — shared by the full composer
 * (``HighlightComposerPage``) and quick share (``HighlightQuickShareDialog``).
 *
 * A day's frames share one audience: the server keys a highlight on
 * ``(author, highlight_date)`` and refuses a frame that asks for other
 * people (``409 HIGHLIGHT_AUDIENCE_MISMATCH``) instead of silently showing
 * it to today's audience. So once the author has posted today, both
 * composers show today's audience as fixed and send frames without one.
 * Before the first frame of the day, the starting audience follows
 * Settings › Highlights › Default audience.
 */
import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { getPreferences } from '@/utils/preferences'
import { t, isOne } from '@/i18n/i18n'
import type { Highlight, HighlightAudienceKind, HighlightInboxItem } from '@/types'

/** Today's date key as the server writes ``highlight_date`` (UTC). */
export function todayKey(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10)
}

/** The author's highlight for today plus its frame count, or ``null``
 *  when they haven't posted today. Throws when the inbox can't load. */
export async function loadMyToday(): Promise<{ highlight: Highlight, frames: number } | null> {
  const rows = (await api.get('/api/highlights')) as HighlightInboxItem[]
  const me = currentUser.value?.user_id
  if (!me) return null
  const key = todayKey()
  const mine = (rows ?? []).find(
    s => s.highlight.author_user_id === me && s.highlight.highlight_date === key,
  )
  return mine ? { highlight: mine.highlight, frames: mine.frames.length } : null
}

/** Settings › Highlights › Default audience — the kind a new day's
 *  highlight starts with. ``all_paired`` when unset or unreadable. */
export function defaultAudienceKind(): HighlightAudienceKind {
  const hl = getPreferences().highlights as
    { default_audience?: { kind?: unknown } } | undefined
  const kind = hl?.default_audience?.kind
  return kind === 'households' || kind === 'users' ? kind : 'all_paired'
}

/** Who an audience reaches, as a phrase for "Today's highlight is for
 *  {who}". ``nameOf`` resolves ids to names when the caller has them;
 *  any unresolved id falls back to a count. */
export function describeAudience(
  kind: HighlightAudienceKind,
  ids: readonly string[],
  nameOf?: (id: string) => string | undefined,
): string {
  if (kind === 'all_paired') return t('highlight.audience.who_all')
  if (ids.length === 0) return t('highlight.audience.who_only_you')
  const names = nameOf ? ids.map(nameOf) : []
  if (nameOf && names.every((n): n is string => !!n)) return names.join(', ')
  const n = String(ids.length)
  if (kind === 'households') {
    return t(isOne(ids.length) ? 'highlight.audience.who_households_one' : 'highlight.audience.who_households', { n })
  }
  return t(isOne(ids.length) ? 'highlight.audience.who_people_one' : 'highlight.audience.who_people', { n })
}
