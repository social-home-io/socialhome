/**
 * Discover — the spaces you can join, from three sources (owner decision
 * 5): **Your household** (``GET /api/household/spaces``), **Paired
 * households** (the public spaces paired households publish to us, §D1a
 * ``SPACE_DIRECTORY_SYNC`` → ``GET /api/peer_spaces``) and **Global** (the
 * directories of the GFSs this household added → ``GET /api/public_spaces``).
 *
 * Pure helpers — what a directory entry is, whether it belongs in Discover
 * and what its one row action is — shared by the Spaces page and the
 * space's About page.
 */
import { t } from '@/i18n/i18n'
import type { DirectoryEntry, HouseholdSpaceRow } from '@/types'

export type DiscoverSource = 'household' | 'paired' | 'global'

export const DISCOVER_SOURCES: readonly DiscoverSource[] = ['household', 'paired', 'global']

/** ``?discover=…`` → a source; anything else → ``null``. The retired
 *  ``/spaces/browse`` tab names map onto the new ones. */
export function parseDiscoverSource(raw: string | null | undefined): DiscoverSource | null {
  switch (raw) {
    case 'household': return 'household'
    case 'paired':
    case 'friends': return 'paired'
    case 'global': return 'global'
    default: return null
  }
}

/** The Discover source a directory entry came from — for the About page's
 *  way back to the list. */
export function sourceOf(entry: DirectoryEntry): DiscoverSource {
  if (entry.host_instance_id === 'local' || entry.scope === 'household') return 'household'
  return entry.scope === 'global' ? 'global' : 'paired'
}

/** Membership sets the page already derived. */
export interface MembershipSets {
  subIds: Set<string>
  pendingIds: Set<string>
}

/**
 * Map ``GET /api/household/spaces`` rows onto directory entries. The
 * server decides what is listed — every space this household hosts that
 * the caller may find, never a private one; each row carries the real
 * member count and the caller's ``role`` (``null`` = no seat; a
 * ``subscriber`` follows it).
 */
export function buildHouseholdEntries(
  rows: HouseholdSpaceRow[],
  { subIds, pendingIds }: MembershipSets,
): DirectoryEntry[] {
  return rows.map((r) => ({
    space_id:           r.space_id,
    host_instance_id:   'local',
    host_display_name:  t('spaces.browse.your_household'),
    host_is_paired:     true,
    name:               r.name,
    description:        r.description,
    emoji:              r.emoji,
    member_count:       r.member_count,
    scope:              r.space_type,
    join_mode:          r.join_mode,
    allow_subscribers:  r.allow_subscribers,
    min_age:            r.min_age,
    category:           r.category,
    already_member:     r.role !== null && r.role !== 'subscriber',
    already_subscribed: r.role === 'subscriber' || subIds.has(r.space_id),
    request_pending:    pendingIds.has(r.space_id),
  }))
}

/** A remote directory row (paired or global) with the caller's state. */
export function withMembership(
  entries: DirectoryEntry[],
  scope: 'public' | 'global',
  mine: { memberIds: Set<string>; subIds: Set<string>; pendingIds: Set<string> },
): DirectoryEntry[] {
  return entries.map((e) => ({
    ...e,
    scope,
    // Fail closed when an older backend omits these: no join path and no
    // Follow that would only be refused.
    join_mode:          e.join_mode || 'invite_only',
    allow_subscribers:  scope === 'global' ? e.allow_subscribers === true : e.allow_subscribers,
    already_member:     mine.memberIds.has(e.space_id),
    already_subscribed: mine.subIds.has(e.space_id),
    request_pending:    mine.pendingIds.has(e.space_id),
  }))
}

/** Discover lists what you can still join: never an invite-only space
 *  (owner decision 5), never one you're in or follow already. */
export function discoverable(entries: DirectoryEntry[], seated: Set<string>): DirectoryEntry[] {
  return entries.filter((e) =>
    e.join_mode !== 'invite_only'
    && !e.already_member
    && !e.already_subscribed
    && !seated.has(e.space_id),
  )
}

/** Does ``entry`` match the "Find a space" box? Name or description. */
export function matchesTerm(
  term: string, name: string, description?: string | null,
): boolean {
  const q = term.trim().toLocaleLowerCase()
  if (!q) return true
  return name.toLocaleLowerCase().includes(q)
    || (description ?? '').toLocaleLowerCase().includes(q)
}

export type DiscoverAction = 'join' | 'request' | 'follow' | 'pending' | 'pair-first' | null

/** Is the entry's host reachable for a join (this household, or paired)? */
function reachable(e: DirectoryEntry): boolean {
  return e.host_instance_id === 'local' || e.scope === 'household' || e.host_is_paired
}

/**
 * Whether Follow is offered: a public / global space this household hosts,
 * or a global space found on a GFS (the GFS relays it, no pairing needed)
 * — and only when its owner explicitly takes followers. A paired
 * household's public space is joined through a request instead (there is
 * no remote follow path for it).
 */
export function canFollow(e: DirectoryEntry): boolean {
  const followable = e.scope === 'global'
    || (e.host_instance_id === 'local' && e.scope === 'public')
  return followable && e.allow_subscribers === true && !e.already_member
}

/**
 * The one row action — Join (open), Ask to join (approval), Follow (when
 * you can't join from here but may read along), "Requested" while a
 * request is out. A paired household's space whose host is no longer
 * confirmed asks for pairing first; a global space you can neither join
 * nor follow from here has no button (its row still opens the About page).
 */
export function discoverAction(e: DirectoryEntry): DiscoverAction {
  if (e.request_pending) return 'pending'
  if (reachable(e)) {
    if (e.join_mode === 'open') return 'join'
    if (e.join_mode === 'request') return 'request'
  }
  if (canFollow(e)) return 'follow'
  if (e.scope === 'public' && !e.host_is_paired) return 'pair-first'
  return null
}

/**
 * Human-readable label for the host household. A GFS-listed space is
 * usually hosted by a household we never paired with, so the backend
 * falls back to the raw 52-char instance id — shorten it to ``abcd1234…``
 * like the rest of the app; a real display name is shown in full.
 */
export function hostLabel(entry: DirectoryEntry): string {
  const name = entry.host_display_name
  if (!name || name === entry.host_instance_id) {
    return `${entry.host_instance_id.slice(0, 8)}…`
  }
  return name
}

/**
 * Whether this space is KNOWN to withhold its content from non-members:
 * the owner's readability opt-in (``allow_subscribers``) is explicitly off.
 * Public / global scope only — a household or private space is never
 * published, so the flag carries no information there. Absent (the peer
 * directory doesn't carry it) means unknown: say nothing.
 */
export function contentIsGated(entry: DirectoryEntry): boolean {
  return entry.scope !== 'household'
    && entry.scope !== 'private'
    && entry.allow_subscribers === false
}

/** The 🔒 chip that says so. ``label`` follows the UI language. */
export const GATED_CHIP = {
  cls:   'sh-join-mode-chip sh-join-mode-chip--private',
  icon:  '🔒',
  get label(): string { return t('space.card.content_private') },
}

/** How a person becomes someone who can post here (membership only —
 *  readability is {@link GATED_CHIP}). */
export function joinModeChip(mode: DirectoryEntry['join_mode']) {
  switch (mode) {
    case 'open':
      return { cls: 'sh-join-mode-chip sh-join-mode-chip--open', icon: '🔓', label: t('space.card.open_to_join') }
    case 'request':
      return { cls: 'sh-join-mode-chip sh-join-mode-chip--request', icon: '✉', label: t('space.card.approval_required') }
    case 'invite_only':
      return { cls: 'sh-join-mode-chip sh-join-mode-chip--invite', icon: '🎟', label: t('space.card.invite_only') }
  }
}
