/**
 * Today's highlight audience — the composer (``HighlightComposerPage``),
 * its "Change for all of today…" sheet and Settings › Highlights.
 *
 * A day's frames share one audience: the server keys a highlight on
 * ``(author, highlight_date)`` and refuses a frame that asks for other
 * people (``409 HIGHLIGHT_AUDIENCE_MISMATCH``) instead of silently showing
 * it to today's audience. So once the author has posted today, the
 * composer shows today's audience locked. Every frame names the audience
 * the composer showed (``audienceBody``) — never none: a frame that lands
 * after UTC midnight starts a new day, and without an audience the server
 * would give it the Settings default instead of the people the author saw.
 * Before the first frame of the day, the starting audience follows
 * Settings › Highlights › Default audience. Once today's highlight exists,
 * "Change for all of today…" changes it for every frame
 * (``PATCH /api/highlights/{id}/audience``, owner decision 1).
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

/** The lock card's share note: later frames join today's highlight, so
 *  they also reach anyone with its public link, or wherever it was shared
 *  (whether it was shared to a feed or space isn't on the author's row). */
export function lockShareNote(published: boolean): string {
  return t(published ? 'highlight.lock.public_note' : 'highlight.lock.shared_note')
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

/** How long the author's highlights stay (Settings › Highlights), 1–90
 *  days, 30 when unset — never a fixed number in the copy. */
export function retentionDays(): number {
  const raw = Number((getPreferences().highlights as { retention_days?: unknown } | undefined)
    ?.retention_days)
  return Number.isFinite(raw) && raw >= 1 ? Math.min(90, Math.round(raw)) : 30
}

/** Who an audience reaches, as a phrase for "Goes to: {who}". ``nameOf``
 *  resolves ids to names when the caller has them; any unresolved id
 *  falls back to a count. */
export function describeAudience(
  kind: HighlightAudienceKind,
  ids: readonly string[],
  nameOf?: (id: string) => string | undefined,
): string {
  if (kind === 'all_paired') return t('highlight.audience.who_all')
  // ``households`` naming none reaches only the author's own household.
  if (kind === 'households' && ids.length === 0) return t('highlight.audience.who_household')
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

/** A person the per-person picker offers. ``household_name`` is null for
 *  members of our own household. */
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
    display_name?: string
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

/** Who a highlight can reach, by name — for "what changes for everyone".
 *  Built from ``GET /api/friends`` (:func:`directoryFromFriends`). */
export interface AudienceDirectory {
  /** This household's name. */
  ownName: string
  /** The author's housemates (the author left out). */
  housemates: { user_id: string, display_name: string }[]
  households: {
    instance_id: string
    display_name: string
    members: { user_id: string, display_name: string }[]
  }[]
}

export function directoryFromFriends(
  payload: FriendsPayload,
  selfUserId: string | null | undefined,
): AudienceDirectory {
  const name = (m: { display_name: string, personal_alias?: string | null }) =>
    m.personal_alias || m.display_name
  return {
    ownName: payload.instance?.display_name ?? '',
    housemates: (payload.instance?.members ?? [])
      .filter(m => m.user_id !== selfUserId)
      .map(m => ({ user_id: m.user_id, display_name: name(m) })),
    households: (payload.households ?? []).map(h => ({
      instance_id: h.instance_id,
      display_name: h.display_name,
      members: (h.members ?? []).map(m => ({ user_id: m.user_id, display_name: name(m) })),
    })),
  }
}

/** The three ways the composer offers to pick an audience. ``choose``
 *  keeps both pick lists, so flipping between households and people
 *  loses nothing. */
export type AudienceMode = 'all' | 'household' | 'choose'
export interface AudienceChoice {
  mode: AudienceMode
  chooseKind: 'households' | 'users'
  households: string[]
  users: string[]
}

/** The option showing *kind* / *ids*: ``households`` naming none is "your
 *  household only". */
export function choiceFromAudience(
  kind: HighlightAudienceKind,
  ids: readonly string[],
): AudienceChoice {
  const base: AudienceChoice = { mode: 'all', chooseKind: 'households', households: [], users: [] }
  if (kind === 'all_paired') return base
  if (kind === 'households') {
    return ids.length === 0
      ? { ...base, mode: 'household' }
      : { ...base, mode: 'choose', households: [...ids] }
  }
  return { ...base, mode: 'choose', chooseKind: 'users', users: [...ids] }
}

/** The audience a choice sends. */
export function audienceOfChoice(c: AudienceChoice): { kind: HighlightAudienceKind, ids: string[] } {
  if (c.mode === 'all') return { kind: 'all_paired', ids: [] }
  if (c.mode === 'household') return { kind: 'households', ids: [] }
  return c.chooseKind === 'households'
    ? { kind: 'households', ids: [...c.households] }
    : { kind: 'users', ids: [...c.users] }
}

/** "Choose households or people…" needs at least one pick (B21). */
export function choiceIsComplete(c: AudienceChoice): boolean {
  if (c.mode !== 'choose') return true
  return (c.chooseKind === 'households' ? c.households : c.users).length > 0
}

/** A new day's starting choice, from Settings. Settings' "chosen" with
 *  nobody picked means "ask each time": the choose option, nothing ticked. */
export function defaultChoice(): AudienceChoice {
  const d = defaultAudience()
  if (d.kind === 'households' && d.ids.length === 0) {
    return { mode: 'choose', chooseKind: 'households', households: [], users: [] }
  }
  return choiceFromAudience(d.kind, d.ids)
}

/** Everyone *kind* / *ids* reaches, as ids: people, plus ``h:<id>`` for a
 *  household we know no members of. */
function reach(
  kind: HighlightAudienceKind,
  ids: readonly string[],
  dir: AudienceDirectory,
): Set<string> {
  const out = new Set<string>()
  if (kind === 'users') {
    for (const id of ids) out.add(id)
    return out
  }
  for (const m of dir.housemates) out.add(m.user_id)
  for (const h of dir.households) {
    if (kind !== 'all_paired' && !ids.includes(h.instance_id)) continue
    if (h.members.length === 0) out.add(`h:${h.instance_id}`)
    for (const m of h.members) out.add(m.user_id)
  }
  return out
}

/** Name a set of reached ids: a whole paired household by its name (your
 *  own only when it's more than one housemate), else its people. */
function label(set: Set<string>, dir: AudienceDirectory): string[] {
  const out: string[] = []
  const groups = [
    { name: dir.ownName, members: dir.housemates, key: null as string | null },
    ...dir.households.map(h => ({ name: h.display_name, members: h.members, key: h.instance_id })),
  ]
  for (const g of groups) {
    if (g.members.length === 0) {
      if (g.key !== null && set.has(`h:${g.key}`)) out.push(g.name)
      continue
    }
    const hit = g.members.filter(m => set.has(m.user_id))
    if (hit.length === 0) continue
    const whole = hit.length === g.members.length && (g.key !== null || g.members.length > 1)
    if (whole && g.name) out.push(g.name)
    else out.push(...hit.map(m => m.display_name))
  }
  return out
}

/** What a change from one audience to another does, by name: who gets
 *  today's frames (``gains``) and who loses the highlight (``losses``). */
export function audienceChange(
  oldKind: HighlightAudienceKind,
  oldIds: readonly string[],
  newKind: HighlightAudienceKind,
  newIds: readonly string[],
  dir: AudienceDirectory,
): { gains: string[], losses: string[] } {
  const before = reach(oldKind, oldIds, dir)
  const after = reach(newKind, newIds, dir)
  const gained = new Set([...after].filter(x => !before.has(x)))
  const lost = new Set([...before].filter(x => !after.has(x)))
  return { gains: label(gained, dir), losses: label(lost, dir) }
}

/** "Anna, Bob and 3 more" — at most three names. */
export function nameList(names: readonly string[], max = 3): string {
  if (names.length <= max) return names.join(', ')
  return t('highlight.change.and_more', {
    names: names.slice(0, max).join(', '),
    n: String(names.length - max),
  })
}
