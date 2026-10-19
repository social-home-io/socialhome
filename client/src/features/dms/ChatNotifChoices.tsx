/**
 * ChatNotifChoices — the notification choices of a system chat (a
 * space's chat, the household chat), outside the chat itself.
 */
import { useId, useState } from 'preact/hooks'
import { Button } from '@/components/Button'
import { t } from '@/i18n/i18n'
import type { ConversationNotifLevel } from '@/store/dms'
import { MUTE_DURATIONS, isMuteActive, muteDurationLabel, mutedLabel, type MuteDuration } from '@/utils/mute'
import { applyLevel, applyMute } from './ConversationMute'

/** A system chat's notification state and its setters. */
export interface ChatNotif {
  convId: string
  mutedUntil: string | null
  level: ConversationNotifLevel
  onMute: (mutedUntil: string | null) => void
  onLevel: (level: ConversationNotifLevel) => void
}

/** A system chat's level (all / @mentions), as radio cards in the same
 *  style as a space's posts level, and its mute: a labelled "Mute the
 *  chat for…" group while it's on, the muted state and Unmute while it's
 *  muted. Used for a space's chat ("Your notifications") and the
 *  household chat (Settings › Notifications) — neither chat has a bell
 *  of its own. */
export function ChatNotifChoices({ chat, legend }: { chat: ChatNotif; legend: string }) {
  const [saving, setSaving] = useState(false)
  const name = useId()
  const labelId = useId()
  const muted = isMuteActive(chat.mutedUntil)
  const pick = async (next: ConversationNotifLevel) => {
    if (saving || next === chat.level) return
    setSaving(true)
    await applyLevel(chat.convId, next, chat.onLevel)
    setSaving(false)
  }
  const mute = async (d: MuteDuration | null) => {
    if (saving) return
    setSaving(true)
    await applyMute(chat.convId, d, chat.onMute)
    setSaving(false)
  }
  return (
    <div class="sh-space-chat-notif" data-testid="space-chat-notif">
      <fieldset class="sh-space-notif-levels" disabled={saving}>
        <legend class="sh-space-notif-levels__legend">{legend}</legend>
        {(['all', 'mentions'] as const).map(opt => (
          <label key={opt} class="sh-space-notif-levels__option">
            <input type="radio" name={name} value={opt} checked={chat.level === opt}
                   onChange={() => void pick(opt)} />
            <span class="sh-space-notif-levels__text">
              <span class="sh-space-notif-levels__title">
                {t(opt === 'all' ? 'dms.notif.all' : 'dms.notif.mentions')}
              </span>
              <span class="sh-space-notif-levels__meta">
                {t(opt === 'all' ? 'space.notif.chat_all_hint' : 'space.notif.chat_mentions_hint')}
              </span>
            </span>
          </label>
        ))}
      </fieldset>
      {muted ? (
        <div class="sh-space-chat-notif__muted" role="status">
          <span><span aria-hidden="true">🔕 </span>{mutedLabel(chat.mutedUntil as string)}</span>
          <Button variant="secondary" disabled={saving} onClick={() => void mute(null)}>
            {t('dms.mute.unmute')}
          </Button>
        </div>
      ) : (
        <div class="sh-space-chat-notif__mute" role="group" aria-labelledby={labelId}>
          <span id={labelId} class="sh-space-chat-notif__mute-label">
            {t('space.notif.mute_chat_for')}
          </span>
          <div class="sh-space-chat-notif__chips">
            {MUTE_DURATIONS.map(d => (
              <button key={d} type="button" class="sh-chip" disabled={saving}
                      onClick={() => void mute(d)}>
                {muteDurationLabel(d)}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
