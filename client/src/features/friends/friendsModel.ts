/**
 * friendsModel — the pure shaping behind the Friends page.
 *
 * ``GET /api/friends`` hands over the whole tree (our household + every
 * paired household with its people). Everything the page decides about
 * that tree lives here, unit-tested without a DOM:
 *
 *   • households: problems (unreachable) first, then A–Z by name;
 *     households with nobody shared become a footer line, not a card;
 *   • people: A–Z, our own row first in "At home";
 *   • People view: one A–Z list with letter dividers — anything that does
 *     not fold to a Latin letter (CJK, Arabic, digits…) files under "#";
 *   • "Find someone": a case- and accent-insensitive substring match over
 *     the shown name, the real name, the username and the household.
 *
 * Sorting uses ``Intl.Collator`` in the UI language, so "Ömer" sorts with
 * the O's for a German reader and "Élodie" with the E's.
 */
import type { UserStatus } from '@/types'

export interface LocalMember {
  user_id: string
  username: string
  display_name: string
  /** Viewer's private nickname (§4.1.6 personal_alias). Renders in place
   *  of ``display_name`` when set; only visible to this viewer. */
  personal_alias?: string | null
  picture_url: string | null
  is_online?: boolean
  is_idle?: boolean
  last_seen_at?: string | null
  /** Emoji + text status; null when unset or expired. */
  status?: UserStatus | null
  bio?: string | null
}

export interface RemoteMember {
  user_id: string
  instance_id: string
  remote_username: string
  display_name: string
  personal_alias?: string | null
  picture_url: string | null
  is_online?: boolean
  is_idle?: boolean
  last_seen_at?: string | null
  status?: UserStatus | null
  bio?: string | null
}

export interface LocalInstance {
  instance_id: string | null
  display_name: string
  home_lat: number | null
  home_lon: number | null
  members: LocalMember[]
  member_count: number
}

export interface Household {
  instance_id: string
  display_name: string
  home_lat: number | null
  home_lon: number | null
  paired_at: string | null
  reachable: boolean
  /** UTC time the household stopped answering; null while reachable
   *  (and on servers that predate the field). */
  unreachable_since?: string | null
  members: RemoteMember[]
  member_count: number
}

export interface FriendsPayload {
  instance: LocalInstance
  households: Household[]
  totals: { households: number; people: number }
}

export type FriendsView = 'households' | 'people' | 'map'

/** Where a person lives — the shape ``openPerson`` takes. */
export interface PersonHome {
  instance_id: string | null
  display_name: string
  is_own: boolean
}

/** One row on the page: a local or remote member plus their household. */
export interface Person {
  user_id: string
  /** Nickname when the viewer set one, else their display name. */
  shown: string
  /** Their own display name (differs from ``shown`` under a nickname). */
  real: string
  /** Username (local) / remote username — searchable, never shown. */
  handle: string
  aliased: boolean
  isLocal: boolean
  isSelf: boolean
  home: PersonHome
  member: LocalMember | RemoteMember
}

/** Households with more people than this collapse to ``COLLAPSED_SHOWN``
 *  plus an "N more" expander. */
export const COLLAPSE_OVER = 6
export const COLLAPSED_SHOWN = 5
/** "Find someone" only earns its place on a list longer than this. */
export const FIND_OVER = 8

export function collatorFor(locale: string): Intl.Collator {
  return new Intl.Collator(locale, { sensitivity: 'base', numeric: true })
}

/** Case- and accent-folded text for substring matching: "Müller" and
 *  "MULLER" both become "muller". */
export function fold(s: string): string {
  return s.normalize('NFKD').replace(/\p{M}+/gu, '').toLocaleLowerCase()
}

function toPerson(
  m: LocalMember | RemoteMember,
  home: PersonHome,
  isLocal: boolean,
  selfId: string | null | undefined,
): Person {
  const alias = (m.personal_alias ?? '').trim()
  return {
    user_id: m.user_id,
    shown: alias || m.display_name,
    real: m.display_name,
    handle: isLocal ? (m as LocalMember).username : (m as RemoteMember).remote_username,
    aliased: !!alias && alias !== m.display_name,
    isLocal,
    isSelf: isLocal && !!selfId && m.user_id === selfId,
    home,
    member: m,
  }
}

/** Self first, then A–Z by the name the viewer sees. */
export function sortPeople(people: Person[], collator: Intl.Collator): Person[] {
  return [...people].sort((a, b) =>
    Number(b.isSelf) - Number(a.isSelf) || collator.compare(a.shown, b.shown),
  )
}

export interface HouseholdGroup {
  household: Household
  people: Person[]
}

export interface FriendsModel {
  /** Our household's people (self first, then A–Z). */
  atHome: Person[]
  /** Paired households that shared people: problems first, then A–Z. */
  groups: HouseholdGroup[]
  /** Paired households with nobody shared (yet) — footer lines, A–Z. */
  quiet: Household[]
  /** Everyone visible, across all households. */
  peopleCount: number
  /** Our household + every paired one (shared people or not). */
  householdCount: number
}

/** Shape the payload. ``hidden`` drops people (the client-side block
 *  cache, for blocks made after the page loaded). */
export function buildModel(
  data: FriendsPayload,
  opts: {
    selfId?: string | null
    locale: string
    hidden?: (userId: string) => boolean
  },
): FriendsModel {
  const collator = collatorFor(opts.locale)
  const keep = (m: { user_id: string }) => !(opts.hidden?.(m.user_id))
  const ownHome: PersonHome = {
    instance_id: data.instance.instance_id,
    display_name: data.instance.display_name,
    is_own: true,
  }
  const atHome = sortPeople(
    data.instance.members.filter(keep).map((m) => toPerson(m, ownHome, true, opts.selfId)),
    collator,
  )
  const groups: HouseholdGroup[] = []
  const quiet: Household[] = []
  for (const h of data.households) {
    const home: PersonHome = { instance_id: h.instance_id, display_name: h.display_name, is_own: false }
    const people = sortPeople(
      h.members.filter(keep).map((m) => toPerson(m, home, false, opts.selfId)),
      collator,
    )
    if (people.length === 0) quiet.push(h)
    else groups.push({ household: h, people })
  }
  groups.sort((a, b) =>
    Number(a.household.reachable) - Number(b.household.reachable)
    || collator.compare(a.household.display_name, b.household.display_name),
  )
  quiet.sort((a, b) => collator.compare(a.display_name, b.display_name))
  const peopleCount = atHome.length + groups.reduce((n, g) => n + g.people.length, 0)
  return {
    atHome,
    groups,
    quiet,
    peopleCount,
    householdCount: 1 + data.households.length,
  }
}

/** Does *p* match the folded query? Name, real name, username and the
 *  household's name all count, so "okafor" finds the whole family. */
export function matches(p: Person, foldedQuery: string): boolean {
  if (!foldedQuery) return true
  return [p.shown, p.real, p.handle, p.home.display_name]
    .some((s) => !!s && fold(s).includes(foldedQuery))
}

/** The divider a name files under: its first letter folded to A–Z, or
 *  "#" for anything that is not a Latin letter. */
export function letterOf(name: string): string {
  const first = fold(name.trim()).charAt(0).toUpperCase()
  return /^[A-Z]$/.test(first) ? first : '#'
}

export interface LetterGroup {
  letter: string
  people: Person[]
}

/** The People view: everyone A–Z (no self-first), grouped by letter,
 *  with "#" last. */
export function peopleByLetter(people: Person[], locale: string): LetterGroup[] {
  const collator = collatorFor(locale)
  const sorted = [...people].sort((a, b) => {
    const la = letterOf(a.shown)
    const lb = letterOf(b.shown)
    if (la !== lb) {
      if (la === '#') return 1
      if (lb === '#') return -1
      return la < lb ? -1 : 1
    }
    return collator.compare(a.shown, b.shown)
  })
  const out: LetterGroup[] = []
  for (const p of sorted) {
    const letter = letterOf(p.shown)
    const last = out[out.length - 1]
    if (last && last.letter === letter) last.people.push(p)
    else out.push({ letter, people: [p] })
  }
  return out
}

/** A status that carries an emoji and no words: it rides on the avatar
 *  as a badge instead of filling the second line. */
export function emojiOnly(status: UserStatus | null | undefined): string | null {
  if (!status) return null
  const emoji = (status.emoji ?? '').trim()
  const text = (status.text ?? '').trim()
  return emoji && !text ? emoji : null
}

/** "🏃 Out for a run" — the status as one line, or null. */
export function statusLine(status: UserStatus | null | undefined): string | null {
  if (!status) return null
  const text = (status.text ?? '').trim()
  if (!text) return null
  const emoji = (status.emoji ?? '').trim()
  return emoji ? `${emoji} ${text}` : text
}

function parseIso(iso: string | null | undefined): number | null {
  if (!iso) return null
  const s = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(iso)
    ? `${iso.replace(' ', 'T')}Z`
    : iso
  const ms = Date.parse(s)
  return Number.isNaN(ms) ? null : ms
}

/** Earliest and latest ``paired_at`` — "Together since …", "newest: …". */
export function pairingSpan(households: Household[]): { since: number | null; newest: Household | null } {
  let since: number | null = null
  let newest: Household | null = null
  let newestAt = -Infinity
  for (const h of households) {
    const at = parseIso(h.paired_at)
    if (at === null) continue
    if (since === null || at < since) since = at
    if (at > newestAt) { newestAt = at; newest = h }
  }
  return { since, newest }
}
