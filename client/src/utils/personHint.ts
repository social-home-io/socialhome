/**
 * What a call site already knows about a person, shaped as the
 * :func:`openPerson` hint so the person sheet paints instantly.
 *
 * Every surface that shows someone (a post author, a comment, a chat
 * sender, an @mention, a space member row) has a name and usually a
 * picture in hand — from the space roster, the conversation roster or
 * the household user cache. These helpers turn that into a hint.
 *
 * Deliberately never a *complete* hint (``is_local`` + ``household`` +
 * name): the sheet then always asks ``GET /api/friends/{id}`` for the
 * presence, status and bio a roster row doesn't carry. When that 404s
 * (someone only met through a shared space) the sheet keeps the name,
 * picture and household from the hint and offers no Message — the hint
 * never sets ``is_local: false`` for a remote person, so the sheet never
 * guesses that a DM would reach them.
 */
import type { PersonContext } from '@/components/PersonSheet'
import { householdUsers } from '@/store/householdUsers'
import { spaceMembers } from '@/store/spaceMembers'

export type PersonHint = NonNullable<PersonContext['person']>

/** A roster row — a space member, a conversation member, a mention
 *  candidate. Every field is optional; only what's set lands in the hint. */
export interface RosterPerson {
  user_id: string
  display_name?: string | null
  space_display_name?: string | null
  personal_alias?: string | null
  picture_url?: string | null
  username?: string | null
  /** Set for someone from another household. */
  instance_id?: string | null
  household_name?: string | null
  is_online?: boolean
  is_idle?: boolean
}

/** Hint from one roster row. The household name (the real one, not a
 *  per-space override) is preferred so the heading doesn't jump when
 *  the fetch lands. */
export function rosterPersonHint(m: RosterPerson): PersonHint {
  const hint: PersonHint = {}
  const name = m.display_name || m.space_display_name
  if (name) hint.display_name = name
  if (m.personal_alias) hint.personal_alias = m.personal_alias
  if (m.picture_url) hint.picture_url = m.picture_url
  if (m.instance_id) {
    if (m.household_name) {
      hint.household = {
        instance_id: m.instance_id,
        display_name: m.household_name,
        is_own: false,
      }
    }
  } else if (m.username) {
    // Someone in the viewer's own household: the DM endpoint addresses
    // them by username, so Message works even before the fetch lands.
    hint.is_local = true
    hint.username = m.username
  }
  if (m.is_online !== undefined) hint.is_online = m.is_online
  if (m.is_idle !== undefined) hint.is_idle = m.is_idle
  return hint
}

/**
 * Best hint for ``userId`` from the caches: the space roster when
 * ``spaceId`` is given, then the household user list. Unknown → ``{}``
 * (the sheet fetches everything).
 */
export function personHint(
  spaceId: string | null | undefined,
  userId: string,
): PersonHint {
  const member = spaceId ? spaceMembers.value[spaceId]?.get(userId) : undefined
  const local = householdUsers.value.get(userId)
  const hint: PersonHint = member ? rosterPersonHint(member) : {}
  if (local) {
    if (!hint.display_name) hint.display_name = local.display_name || local.username
    if (!hint.picture_url && local.picture_url) hint.picture_url = local.picture_url
    hint.is_local = true
    hint.username = local.username
  }
  return hint
}
