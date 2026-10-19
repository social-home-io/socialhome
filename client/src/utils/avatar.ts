/**
 * Avatar URL resolver — space override → household user → null.
 *
 * Centralises the picture-URL lookup for any context that renders a
 * user's avatar. When ``spaceId`` is supplied, the per-space store is
 * consulted first; otherwise (or when no override exists) the
 * household user cache is consulted for the user's current picture URL.
 */
import { householdUsers } from '@/store/householdUsers'
import { spaceMembers } from '@/store/spaceMembers'
import { t } from '@/i18n/i18n'

export function resolveAvatar(
  spaceId: string | null | undefined,
  userId: string | null | undefined,
  fallback: string | null = null,
): string | null {
  if (!userId) return fallback
  if (spaceId) {
    const m = spaceMembers.value[spaceId]?.get(userId)
    if (m?.picture_url) return m.picture_url
  }
  const household = householdUsers.value.get(userId)
  if (household?.picture_url) return household.picture_url
  return fallback
}

/**
 * Display name for a bare user id: the member's per-space name → their
 * display name on the space roster (a remote member's household name for
 * them) → the household user → ``fallback`` ("Someone" when omitted).
 * Never returns the raw id — it is not a name.
 */
export function resolveDisplayName(
  spaceId: string | null | undefined,
  userId: string | null | undefined,
  fallback?: string,
): string {
  const otherwise = fallback ?? t('person.someone')
  if (!userId) return otherwise
  if (spaceId) {
    const m = spaceMembers.value[spaceId]?.get(userId)
    if (m?.space_display_name) return m.space_display_name
    if (m?.display_name) return m.display_name
  }
  const household = householdUsers.value.get(userId)
  if (household?.display_name) return household.display_name
  return otherwise
}
