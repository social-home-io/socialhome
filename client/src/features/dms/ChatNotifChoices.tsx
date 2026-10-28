/**
 * ChatNotifChoices — the notification choices of a chat outside the chat
 * itself (a space's chat, the household chat, a group's info).
 *
 * The shared :class:`NotifLevelPicker` (D9): Everything / Replies and
 * mentions / Nothing. A chat's "Nothing" is its mute (``PUT …/mute`` with
 * a length), so while it lasts the level from before stays stored and
 * comes back afterwards.
 */
import { NotifLevelPicker } from '@/components/NotifLevelPicker'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import {
  setConversationMute, setConversationNotifLevel, type ConversationNotifLevel,
} from '@/store/dms'
import type { NotifLevel } from '@/store/notifSettings'
import { isMuteActive, type MuteDuration } from '@/utils/mute'

/** A chat's notification state and its setters. */
export interface ChatNotif {
  convId: string
  mutedUntil: string | null
  level: ConversationNotifLevel
  onMute: (mutedUntil: string | null) => void
  onLevel: (level: ConversationNotifLevel) => void
  /** The level a new seat starts at (gets the "Default" tag). */
  defaultLevel?: ConversationNotifLevel
}

const GROUP_LEVELS: readonly NotifLevel[] = ['all', 'mentions', 'muted']

/** Save *next* for *chat*: a mute with its length, or a level (ending a
 *  running mute first). One toast; ``false`` on a failure. */
export async function pickChatLevel(
  chat: ChatNotif,
  next: NotifLevel,
  duration?: MuteDuration,
): Promise<boolean> {
  try {
    if (next === 'muted') {
      chat.onMute(await setConversationMute(chat.convId, duration ?? 'forever'))
      showToast(t('dms.mute.toast_muted'), 'success')
      return true
    }
    if (isMuteActive(chat.mutedUntil)) {
      chat.onMute(await setConversationMute(chat.convId, null))
    }
    if (next !== chat.level) {
      chat.onLevel(await setConversationNotifLevel(chat.convId, next))
    }
    showToast(t(next === 'all' ? 'dms.notif.toast_all' : 'dms.notif.toast_mentions'), 'success')
    return true
  } catch {
    showToast(t('dms.mute.error'), 'error')
    return false
  }
}

export function ChatNotifChoices({ chat, legend, legendHidden }: {
  chat: ChatNotif
  legend: string
  /** A heading above already names it. */
  legendHidden?: boolean
}) {
  const muted = isMuteActive(chat.mutedUntil)
  return (
    <div class="sh-space-chat-notif" data-testid="space-chat-notif">
      <NotifLevelPicker
        legend={legend}
        legendHidden={legendHidden}
        levels={GROUP_LEVELS}
        level={muted ? 'muted' : chat.level}
        defaultLevel={chat.defaultLevel ?? null}
        mutedUntil={muted ? chat.mutedUntil : null}
        thenLevel={muted ? chat.level : null}
        hint={(l) => t(`notif.level.chat_${l}_hint`)}
        onPick={(next, d) => pickChatLevel(chat, next, d)}
      />
    </div>
  )
}
