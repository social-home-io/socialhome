/**
 * The viewer's notification level for one space — "Your notifications"
 * in the space's More panel. The app has ONE bell (the top bar's); the
 * per-space bell that used to sit in the space header is gone (owner
 * decision 6). Sharing your location moved to the Map section.
 *
 * Levels — one vocabulary with chats (D9; enforced server-side by
 * NotificationService):
 *   - all       — "Everything": every new post, in the app
 *   - mentions  — "Replies and mentions"
 *   - muted     — "Nothing", for a while or until turned back on
 *
 * ``useSpaceNotifLevel`` reads ``GET /api/spaces/{id}/notif-prefs``;
 * ``SpaceNotifLevelPicker`` is the shared :class:`NotifLevelPicker` (a
 * native radio group; Nothing asks "For how long?") saving with
 * ``PUT …/notif-prefs``.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { NotifLevelPicker, levelLabel } from '@/components/NotifLevelPicker'
import { showToast } from '@/components/Toast'
import { ChatNotifChoices, type ChatNotif } from '@/features/dms/ChatNotifChoices'
import { t } from '@/i18n/i18n'
import {
  getSpaceLevel, setSpaceLevel, type NotifLevel, type SpaceLevelState,
} from '@/store/notifSettings'
import {
  claimSpaceChat, loadSpaceChat, patchSpaceChat, spaceChatOf,
} from '@/store/spaceChat'
import type { MuteDuration } from '@/utils/mute'

export type { NotifLevel }

const LEVELS: readonly NotifLevel[] = ['all', 'mentions', 'muted']

/** The level's label, in the UI language — looked up at render time. */
export const notifLevelLabel = levelLabel

function isLevel(v: unknown): v is NotifLevel {
  return typeof v === 'string' && (LEVELS as readonly string[]).includes(v)
}

/** The viewer's level here, ``null`` until (or unless) the server says. */
export function useSpaceNotifLevel(
  spaceId: string, enabled: boolean,
): [NotifLevel | null, (l: NotifLevel) => void] {
  const [level, setLevel] = useState<NotifLevel | null>(null)
  useEffect(() => {
    setLevel(null)
    if (!enabled) return
    let stopped = false
    api.get(`/api/spaces/${spaceId}/notif-prefs`)
      .then((body: { level?: unknown }) => {
        if (!stopped && isLevel(body?.level)) setLevel(body.level)
      })
      .catch(() => { /* no seat / offline — the row shows no level */ })
    return () => { stopped = true }
  }, [spaceId, enabled])
  return [level, setLevel]
}

interface PickerProps {
  spaceId: string
  level: NotifLevel
  /** A visible legend (beside the chat's); hidden "Your notifications"
   *  otherwise. */
  legend?: string
  /** Called with the saved level. */
  onSaved: (level: NotifLevel) => void
}

export function SpaceNotifLevelPicker({ spaceId, level, onSaved, legend }: PickerProps) {
  // The full state — default, a running mute and what comes back after it
  // — is the picker's own; the parent keeps only the level for its label.
  const [state, setState] = useState<SpaceLevelState | null>(null)
  useEffect(() => {
    let stopped = false
    getSpaceLevel(spaceId)
      .then((s) => { if (!stopped) setState(s) })
      .catch(() => { /* the level from the parent still shows */ })
    return () => { stopped = true }
  }, [spaceId])

  const shown = state ?? { level, muted_until: null, then_level: null }

  const pick = async (next: NotifLevel, duration?: MuteDuration): Promise<boolean> => {
    try {
      const saved = await setSpaceLevel(spaceId, next, duration)
      setState(saved)
      showToast(
        saved.level === 'muted'
          ? t('space.notif.muted_toast')
          : saved.level === 'mentions'
            ? t('space.notif.mentions_toast')
            : t('space.notif.all_toast'),
        'success',
      )
      onSaved(saved.level)
      return true
    } catch (err: unknown) {
      showToast(t('space.notif.save_failed', { error: (err as Error).message }), 'error')
      return false
    }
  }

  return (
    <NotifLevelPicker
      legend={legend ?? t('space.more.notifications')}
      legendHidden={!legend}
      levels={LEVELS}
      level={shown.level}
      defaultLevel={state?.default_level ?? null}
      mutedUntil={shown.muted_until}
      thenLevel={shown.then_level}
      hint={(l) => t(`space.notif.level_${l}_hint`)}
      onPick={pick}
    />
  )
}

/** The viewer's space-chat notifications, set next to the space's own
 *  level in "Your notifications" (one place — the chat has no bell of
 *  its own). */
export type SpaceChatNotif = ChatNotif

/** The space chat's notifications for a page that isn't the space page
 *  (settings): takes the space-chat store while mounted. ``null`` while
 *  loading, for a follower, or when the space keeps chat off. */
export function useSpaceChatNotif(spaceId: string, enabled: boolean): SpaceChatNotif | null {
  useEffect(() => {
    if (!enabled) return
    const release = claimSpaceChat(spaceId)
    void loadSpaceChat(spaceId)
    return release
  }, [spaceId, enabled])
  const chat = enabled ? spaceChatOf(spaceId) : null
  if (!chat?.enabled || !chat.conversation_id) return null
  return {
    convId: chat.conversation_id,
    mutedUntil: chat.muted_until ?? null,
    defaultLevel: 'mentions',
    level: chat.notif_level ?? 'mentions',
    onMute: (until) => patchSpaceChat({ muted_until: until }),
    onLevel: (next) => patchSpaceChat({ notif_level: next }),
  }
}

/** "Your notifications" — the space's posts level and, where the viewer
 *  has the chat, its level and mute beside it, in one style. The ONE place
 *  both are set: the space's More panel and its settings page render this
 *  same block. */
export function SpaceNotificationChoices({ spaceId, level, onSaved, chatNotif }: {
  spaceId: string
  level: NotifLevel
  onSaved: (level: NotifLevel) => void
  chatNotif: SpaceChatNotif | null
}) {
  return (
    <>
      <SpaceNotifLevelPicker spaceId={spaceId} level={level} onSaved={onSaved}
                             legend={chatNotif ? t('space.more.notif_posts') : undefined} />
      {chatNotif && <ChatNotifChoices chat={chatNotif} legend={t('space.more.notif_chat')} />}
    </>
  )
}
