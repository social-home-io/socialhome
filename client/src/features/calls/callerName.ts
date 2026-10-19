/**
 * The name a ringing (or missed) call shows for its caller.
 *
 * A caller from this household is in ``householdUsers``. A caller from
 * another household is not — but they ring from a conversation we share,
 * so its roster (``conversationMembers``, loaded on ring) has their name.
 * Anyone still unknown is "Someone", never the raw 32-character user id.
 */
import { t } from '@/i18n/i18n'
import { conversationMembers } from '@/store/conversationMembers'
import { householdUsers } from '@/store/householdUsers'

export function callerName(
  userId: string | null | undefined,
  conversationId?: string | null,
): string {
  if (userId) {
    const local = householdUsers.value.get(userId)
    const name = local?.display_name || local?.username
    if (name) return name
    const member = conversationId ? conversationMembers.value[conversationId]?.get(userId) : undefined
    if (member?.display_name) return member.display_name
  }
  return t('person.someone')
}
