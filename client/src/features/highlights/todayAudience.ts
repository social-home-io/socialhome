/**
 * Today's highlight audience — shared by the full composer
 * (``HighlightComposerPage``) and quick share (``HighlightQuickShareDialog``).
 *
 * A day's frames share one audience: the server keys a highlight on
 * ``(author, highlight_date)`` and refuses a frame that asks for other
 * people (``409 HIGHLIGHT_AUDIENCE_MISMATCH``) instead of silently showing
 * it to today's audience. So once the author has posted today, both
 * composers show today's audience as fixed. Every frame names the audience
 * the composer showed (``audienceBody``) — never none: a frame that lands
 * after UTC midnight starts a new day, and without an audience the server
 * would give it the Settings default instead of the people the author saw.
 * Before the first frame of the day, the starting audience follows
 * Settings › Highlights › Default audience.
 */
import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { getPreferences } from '@/utils/preferences'
import { t, isOne } from '@/i18n/i18n'
import type { HighlightAudienceKind, HighlightInboxItem, OwnHighlight } from '@/types'

/** Today's date key as the server writes ``highlight_date`` (UTC). */
export function todayKey(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10)
}

/** The author's highlight for today: its frame count and whether it has
 *  a public link (``published``). ``null`` when they haven't posted today.
 *  Throws when the inbox can't load. */
export async function loadMyToday(): Promise<
  { highlight: OwnHighlight, frames: number, published: boolean } | null
> {
  const rows = (await api.get('/api/highlights')) as HighlightInboxItem[]
  const me = currentUser.value?.user_id
  if (!me) return null
  const key = todayKey()
  const mine = (rows ?? []).find(
    s => s.highlight.author_user_id === me && s.highlight.highlight_date === key,
  )
  if (!mine) return null
  return {
    // The author's own row always names its audience; fail closed to
    // none (the server then refuses a mismatched frame) if it did not.
    highlight: { ...mine.highlight, audience: mine.highlight.audience ?? [] },
    frames: mine.frames.length,
    published: await hasPublicLink(mine.highlight.id),
  }
}

/** Whether the author's highlight has a public link — the inbox row
 *  doesn't carry it, the author-only ``GET …/publish`` does. ``false``
 *  when it can't be read (the note then names shares in general). */
async function hasPublicLink(id: string): Promise<boolean> {
  try {
    const res = await api.get(`/api/highlights/${id}/publish`) as { published?: unknown }
    return res?.published === true
  } catch {
    return false
  }
}

/** The audience fields of a ``POST /api/highlights/frames`` body: exactly
 *  the audience the composer showed, sent with every frame. */
export function audienceBody(
  kind: HighlightAudienceKind,
  ids: readonly string[],
): { audience_kind: HighlightAudienceKind, audience: string[] } {
  return { audience_kind: kind, audience: kind === 'all_paired' ? [] : [...ids] }
}

/** "Today's highlight is for {who}…" — later frames join it, so they also
 *  reach a feed / space it was shared to and anyone with its public link. */
export function todayFixedNote(
  highlight: OwnHighlight,
  published: boolean,
  nameOf?: (id: string) => string | undefined,
): string {
  const who = describeAudience(highlight.audience_kind, highlight.audience, nameOf)
  return t(
    published ? 'highlight.audience.today_fixed_public' : 'highlight.audience.today_fixed',
    { who },
  )
}

/** Settings › Highlights › Who sees a new highlight — the audience a new
 *  day's highlight starts with: the kind and, for chosen households /
 *  people, their ids. ``all_paired`` when unset or unreadable. */
export function defaultAudience(): { kind: HighlightAudienceKind, ids: string[] } {
  const hl = getPreferences().highlights as
    { default_audience?: { kind?: unknown, ids?: unknown } } | undefined
  const raw = hl?.default_audience
  const kind = raw?.kind === 'households' || raw?.kind === 'users' ? raw.kind : 'all_paired'
  const ids = kind !== 'all_paired' && Array.isArray(raw?.ids)
    ? raw.ids.filter((x): x is string => typeof x === 'string' && x !== '')
    : []
  return { kind, ids }
}

/** Just the kind of :func:`defaultAudience`. */
export function defaultAudienceKind(): HighlightAudienceKind {
  return defaultAudience().kind
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

/** A household the audience picker offers — a confirmed social peer. */
export interface RemoteHousehold {
  instance_id: string
  display_name: string
}

/** A person the per-person picker offers. ``household_name`` is null
 *  for members of our own household. */
export interface ConnectedPerson {
  user_id: string
  display_name: string
  household_name: string | null
}

/** The slice of ``GET /api/friends`` (``routes/friends.py``) the picker
 *  reads. ``households`` is the *social* peer list
 *  (``list_social_instances``) — the same set the highlight outbound
 *  (``HighlightFederationOutbound._resolve_audience``) fans out to, so
 *  a household invited only through a space link is never offered. */
export interface FriendsPayload {
  instance: {
    members: { user_id: string, display_name: string, personal_alias?: string | null }[]
  }
  households: {
    instance_id: string
    display_name: string
    members: { user_id: string, display_name: string, personal_alias?: string | null }[]
  }[]
}

/** Flatten ``/api/friends`` into picker rows. The author is left out of
 *  the people list — they always see their own highlight. */
export function audienceFromFriends(
  payload: FriendsPayload,
  selfUserId: string | null | undefined,
): { households: RemoteHousehold[], people: ConnectedPerson[] } {
  const households: RemoteHousehold[] = (payload.households ?? []).map(h => ({
    instance_id: h.instance_id, display_name: h.display_name,
  }))
  const people: ConnectedPerson[] = []
  for (const m of payload.instance?.members ?? []) {
    if (m.user_id === selfUserId) continue
    people.push({
      user_id: m.user_id,
      display_name: m.personal_alias || m.display_name,
      household_name: null,
    })
  }
  for (const h of payload.households ?? []) {
    for (const m of h.members ?? []) {
      people.push({
        user_id: m.user_id,
        display_name: m.personal_alias || m.display_name,
        household_name: h.display_name,
      })
    }
  }
  return { households, people }
}
