/**
 * SpaceChatView — the Chat view of a space's Feed tab: the space chat
 * thread under a slim bar naming who can read it, with the viewer's mute
 * / notification level (the household feed's Chat tab twin).
 *
 * The thread is the shared :func:`ConversationView`, embedded with the
 * summary's metadata (a system chat 404s on ``GET /api/conversations/{id}``):
 * text only (no attachments, no calls, no group info / header). Delete
 * is offered on the viewer's own messages and — ``canModerate``, the
 * space's owner / admins / moderators — on anyone's. In an archived space
 * the thread is read-only (no composer, reply, edit or reactions; delete
 * stays), as the server refuses those writes there.
 */
import { useEffect, useMemo, useRef } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { connectionState, ws } from '@/ws'
import { currentUser } from '@/store/auth'
import { isMuteActive } from '@/utils/mute'
import { Button } from '@/components/Button'
import { DmThreadSkeleton } from '@/components/Skeleton'
import { ConversationView, type ConversationMeta } from '@/features/dms/ConversationView'
import { MuteButton } from '@/features/dms/ConversationMute'
import {
  clearSpaceChatUnread,
  isSpaceChatFrame,
  loadSpaceChat,
  patchSpaceChat,
  resetSpaceChat,
  spaceChatError,
  spaceChatOf,
} from '@/store/spaceChat'

interface Props {
  spaceId: string
  spaceName: string
  /** Owner / admin / moderator — may delete other people's messages. */
  canModerate: boolean
  /** The space is archived: the chat reads (and takes deletes) but no
   *  longer takes posts, edits or reactions — the server's rule. */
  archived?: boolean
}

export function SpaceChatView({ spaceId, spaceName, canModerate, archived = false }: Props) {
  const chat = spaceChatOf(spaceId)
  const convId = chat?.enabled ? chat.conversation_id : null
  const level = chat?.notif_level ?? 'mentions'
  const mutedUntil = chat?.muted_until ?? null
  const meta = useMemo<ConversationMeta | null>(() => (convId
    ? {
        type: 'group_dm',
        name: null,
        managed_here: false,
        muted_until: mutedUntil,
        notif_level: level,
        unread: chat?.unread ?? 0,
        last_read_at: chat?.last_read_at ?? null,
      }
    : null
  // eslint-disable-next-line react-hooks/exhaustive-deps -- ``unread`` / ``last_read_at`` only place the first window and its divider; a later change must not rebuild
  ), [convId, mutedUntil, level])

  if (!convId || !meta) {
    // An error, or an enabled chat the server didn't name (a skeleton
    // would spin forever): offer a retry.
    if (spaceChatError.value === spaceId || (chat?.enabled && !chat.conversation_id)) {
      return (
        <div class="sh-empty-state sh-feed-chat-error" role="alert">
          <p>{t('space.chat.load_failed')}</p>
          <Button variant="secondary" onClick={() => { void loadSpaceChat(spaceId) }}>
            {t('common.retry')}
          </Button>
        </div>
      )
    }
    return <DmThreadSkeleton />
  }
  return (
    <>
      <div class="sh-feed-chat-bar">
        <span class="sh-feed-chat-audience">
          {t('space.chat.audience', { space: spaceName })}
        </span>
        <MuteButton
          convId={convId}
          mutedUntil={mutedUntil}
          onChange={(until) => patchSpaceChat({ muted_until: until })}
          level={level}
          onLevelChange={(next) => patchSpaceChat({ notif_level: next })}
        />
      </div>
      <ConversationView
        key={convId}
        conversationId={convId}
        embedded
        showHeader={false}
        showGroupInfo={false}
        allowCalls={false}
        allowAttachments={false}
        allowDelete
        canModerate={canModerate}
        readOnly={archived}
        readOnlyNote={t('space.chat.read_only_archived')}
        meta={meta}
      />
    </>
  )
}

/**
 * The space chat summary behind the Feed | Chat switch, kept current
 * while the space page is open.
 *
 * - Fetched once the viewer may have it (``eligible``: the ``chat``
 *   feature on and a writer seat), again when that flips back on, on a
 *   WS reconnect (frames missed while the socket was down) and on
 *   leaving the Chat view (the server's unread and watermark after what
 *   the open chat marked read).
 * - Unread counts other people's new messages while the Chat view is
 *   closed — never while the viewer muted the chat, and at "Only
 *   @mentions" only the ones that mention the viewer (the frame's
 *   per-recipient ``mentions_you``), the rule the server's summary count
 *   follows too — so the badge never shows chatter the viewer asked not
 *   to hear about.
 * - Viewing the chat reads it (ConversationView posts the watermark), so
 *   the badge — and the spaces list's dot for this space — clears while
 *   it's open.
 */
export function useSpaceChatSummary(spaceId: string, eligible: boolean, chatOpen: boolean): void {
  const openRef = useRef(chatOpen)
  openRef.current = chatOpen
  const eligibleRef = useRef(eligible)
  eligibleRef.current = eligible

  useEffect(() => {
    resetSpaceChat(spaceId)
    return () => resetSpaceChat(null)
  }, [spaceId])

  useEffect(() => {
    if (eligible) void loadSpaceChat(spaceId)
  }, [spaceId, eligible])

  useEffect(() => {
    const offMsg = ws.on('dm.message', (e) => {
      if (!isSpaceChatFrame(e.data, spaceId) || openRef.current) return
      const d = e.data as { message?: { sender_user_id?: string }; mentions_you?: boolean }
      if (d.message?.sender_user_id === currentUser.value?.user_id) return
      const chat = spaceChatOf(spaceId)
      if (!chat || !chat.enabled || isMuteActive(chat.muted_until)) return
      if (chat.notif_level === 'mentions' && d.mentions_you !== true) return
      patchSpaceChat({ unread: chat.unread + 1 })
    })
    let prevConn = connectionState.value
    const offConn = connectionState.subscribe((next) => {
      if (prevConn === 'reconnecting' && next === 'open' && eligibleRef.current) {
        void loadSpaceChat(spaceId)
      }
      prevConn = next
    })
    return () => { offMsg(); offConn() }
  }, [spaceId])

  const prevOpen = useRef(chatOpen)
  useEffect(() => {
    if (prevOpen.current && !chatOpen && eligibleRef.current) void loadSpaceChat(spaceId)
    prevOpen.current = chatOpen
  }, [chatOpen, spaceId])

  const unread = spaceChatOf(spaceId)?.unread ?? 0
  useEffect(() => {
    if (chatOpen && unread > 0) patchSpaceChat({ unread: 0 })
  }, [chatOpen, unread])
  // The spaces list's dot for this space goes once its chat is viewed.
  useEffect(() => {
    if (chatOpen) clearSpaceChatUnread(spaceId)
  }, [chatOpen, spaceId, unread])
}
