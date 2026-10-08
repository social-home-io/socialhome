/**
 * FeedPage — household feed (§23.43/§23.44/§23.48), plus the household
 * chat as a second tab.
 *
 * "Feed | Chat" sits like Calendar | Timetable: the strip appears only
 * while the ``feat_household_chat`` toggle is on and
 * ``GET /api/household/chat`` reports it enabled; ``?tab=chat`` opens
 * the chat (the default Feed tab carries no query). Mounted at both
 * ``/feed`` and ``/`` (via LandingDispatch), so a tab switch keeps
 * whichever path it was opened on — a notification's ``/?tab=chat``
 * lands here.
 */
import { useEffect, useMemo, useRef } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { posts, feedLoading, feedHasMore, loadFeed, mergePostEdit } from '@/store/feed'
import { api } from '@/api'
import { connectionState, ws } from '@/ws'
import { loadHouseholdUsers } from '@/store/householdUsers'
import { useTitle } from '@/store/pageTitle'
import { PostCard } from '@/components/PostCard'
import { Composer } from '@/components/Composer'
import { openCommentOverlay } from '@/components/CommentOverlay'
import { HouseholdPresenceStrip } from '@/components/HouseholdPresenceStrip'
import { DmThreadSkeleton, FeedSkeleton, PostCardSkeleton } from '@/components/Skeleton'
import { Button } from '@/components/Button'
import { PullToRefresh } from '@/components/PullToRefresh'
import { TabHeader } from '@/components/TabHeader'
import { loadToggles, toggles } from '@/components/HouseholdToggles'
import { showToast } from '@/components/Toast'
import { ConversationView, type ConversationMeta } from '@/features/dms/ConversationView'
import { MuteButton } from '@/features/dms/ConversationMute'
import {
  householdChat,
  householdChatError,
  isHouseholdChatFrame,
  loadHouseholdChat,
  patchHouseholdChat,
  type HouseholdChatSummary,
} from '@/store/householdChat'
import { instanceConfig } from '@/store/instance'
import { currentUser } from '@/store/auth'
import { getLandingPath } from '@/utils/preferences'
import { isMuteActive } from '@/utils/mute'
import { t } from '@/i18n/i18n'
import type { FeedPost } from '@/types'
import { confirmDialog } from '@/components/confirm'

export type FeedTab = 'feed' | 'chat'

/** Tabs to show, in order. The Chat tab needs the household toggle on
 *  and the server's summary not saying "off". Not-yet-loaded toggles or
 *  summary count as on (no "tab appears" jump on a warm start, like the
 *  calendar); loaded toggles without the key (an older server) count as
 *  off. */
export function visibleFeedTabs(
  tg: { feat_household_chat?: boolean } | null,
  chat: Pick<HouseholdChatSummary, 'enabled'> | null,
): FeedTab[] {
  if (tg !== null && tg.feat_household_chat !== true) return ['feed']
  if (chat !== null && !chat.enabled) return ['feed']
  return ['feed', 'chat']
}

export function feedTabFromUrl(url: string | undefined): FeedTab {
  const q = (url ?? '').split('?')[1] ?? ''
  return new URLSearchParams(q).get('tab') === 'chat' ? 'chat' : 'feed'
}

export default function FeedPage() {
  // ``useLocation`` is ``undefined`` outside a router (unit tests that
  // render the page bare) — every read is optional.
  const loc = useLocation() as ReturnType<typeof useLocation> | undefined
  // The household's federated display name from instanceConfig — the
  // single source of truth (set in admin Settings; what peers also
  // see). Falls back to "Home" while the cold-start config fetch is in
  // flight, matching the default the backend ships with on first boot.
  const householdName = instanceConfig.value?.instance_name ?? t('nav.home')
  useTitle(householdName)

  const chatToggle = toggles.value?.feat_household_chat
  const tabs = visibleFeedTabs(toggles.value, householdChat.value)
  const wanted = feedTabFromUrl(loc?.url)
  const active: FeedTab = tabs.includes(wanted) ? wanted : 'feed'
  const activeRef = useRef(active)
  activeRef.current = active

  // The summary (conversation id, unread, mute, level), reloaded when
  // the toggle flips. Toggles not loaded yet: ask anyway — the server
  // answers ``enabled: false`` while the chat is off.
  useEffect(() => {
    if (chatToggle === false) return
    void loadHouseholdChat()
  }, [chatToggle])

  useEffect(() => {
    // A toggle flip on another device shows / hides the Chat tab live.
    const offCfg = ws.on('household.config_changed', () => { void loadToggles() })
    // Unread on the Chat tab: count other people's new messages while
    // the Feed tab shows; the open chat reads them itself. Nothing while
    // the viewer muted the chat, and nothing at "Only @mentions": the
    // frame doesn't say who a message mentions (the server resolves
    // that), so counting it would light the pill for chatter the viewer
    // asked not to hear about.
    const offMsg = ws.on('dm.message', (e) => {
      if (!isHouseholdChatFrame(e.data)) return
      if (activeRef.current === 'chat') return
      const d = e.data as { message?: { sender_user_id?: string } }
      if (d.message?.sender_user_id === currentUser.value?.user_id) return
      const chat = householdChat.value
      if (!chat || isMuteActive(chat.muted_until)) return
      if (chat.notif_level === 'mentions') return
      patchHouseholdChat({ unread: chat.unread + 1 })
    })
    // Frames missed while the socket was down: re-read the summary.
    let prevConn = connectionState.value
    const offConn = connectionState.subscribe((next) => {
      if (prevConn === 'reconnecting' && next === 'open') void loadHouseholdChat()
      prevConn = next
    })
    return () => { offCfg(); offMsg(); offConn() }
  }, [])

  // Leaving the Chat tab: re-read the summary (the server's unread and
  // read watermark after what the open chat marked read).
  const prevActive = useRef(active)
  useEffect(() => {
    if (prevActive.current === 'chat' && active !== 'chat' && chatToggle !== false) {
      void loadHouseholdChat()
    }
    prevActive.current = active
  }, [active, chatToggle])

  // Viewing the chat reads it (ConversationView posts the watermark).
  const unread = householdChat.value?.unread ?? 0
  // A muted chat keeps its count on the server but raises no pill —
  // the same rule as the Chats badge.
  const muted = isMuteActive(householdChat.value?.muted_until)
  useEffect(() => {
    if (active === 'chat' && unread > 0) patchHouseholdChat({ unread: 0 })
  }, [active, unread])

  const onSelectTab = (tab: FeedTab) => {
    // Keep the path the page was opened on — except a bare ``/`` that
    // only shows the feed because of ``?tab=chat`` (the landing page is
    // the welcome or dashboard): there the Feed tab goes to ``/feed``.
    let path = loc?.path || '/feed'
    if (path === '/' && getLandingPath() !== '/feed') path = '/feed'
    const next = tab === 'feed' ? path : `${path}?tab=chat`
    if (loc && loc.url !== next) loc.route(next, true)
  }
  const labels: Record<FeedTab, string> = {
    feed: t('feed.tabs.feed'),
    chat: t('feed.tabs.chat'),
  }

  return (
    <div class={active === 'chat' ? 'sh-feed-host sh-feed-host--chat' : 'sh-feed-host'}>
      {tabs.length > 1 && (
        <TabHeader<FeedTab>
          activeTab={active}
          visibleTabs={tabs}
          labels={labels}
          badges={{ chat: active === 'chat' || muted ? 0 : unread }}
          ariaLabel={t('feed.tabs.aria')}
          onSelectTab={onSelectTab}
        />
      )}
      {active === 'chat' ? <HouseholdChatTab /> : <FeedTabBody />}
    </div>
  )
}

/** The Chat tab — the household chat thread under a slim bar with who
 *  can read it and the viewer's mute / notification level. Outside the
 *  feed's PullToRefresh, so scrolling the thread never pulls. */
function HouseholdChatTab() {
  const chat = householdChat.value
  const convId = chat?.enabled ? chat.conversation_id : null
  const level = chat?.notif_level ?? 'all'
  const mutedUntil = chat?.muted_until ?? null
  const meta = useMemo<ConversationMeta | null>(() => (convId
    ? {
        type: 'group_dm',
        name: null,
        managed_here: false,
        muted_until: mutedUntil,
        notif_level: level,
        unread: householdChat.value?.unread ?? 0,
        last_read_at: householdChat.value?.last_read_at ?? null,
      }
    : null
  // eslint-disable-next-line react-hooks/exhaustive-deps -- ``unread`` / ``last_read_at`` only place the first window and its divider; a later change must not rebuild
  ), [convId, mutedUntil, level])

  if (!convId || !meta) {
    // An error, or an enabled chat the server didn't name (it should
    // never happen, but a skeleton would spin forever): offer a retry.
    if (householdChatError.value || (chat?.enabled && !chat.conversation_id)) {
      return (
        <div class="sh-empty-state sh-feed-chat-error" role="alert">
          <p>{t('feed.chat.load_failed')}</p>
          <Button variant="secondary" onClick={() => { void loadHouseholdChat() }}>
            {t('common.retry')}
          </Button>
        </div>
      )
    }
    return <DmThreadSkeleton />
  }
  const householdName = instanceConfig.value?.instance_name ?? t('nav.home')
  return (
    <>
      <div class="sh-feed-chat-bar">
        <span class="sh-feed-chat-audience">
          {t('feed.chat.audience', { household: householdName })}
        </span>
        <MuteButton
          convId={convId}
          mutedUntil={mutedUntil}
          onChange={(until) => patchHouseholdChat({ muted_until: until })}
          level={level}
          onLevelChange={(next) => patchHouseholdChat({ notif_level: next })}
        />
      </div>
      <ConversationView
        key={convId}
        conversationId={convId}
        embedded
        showHeader={false}
        showGroupInfo={false}
        allowCalls={false}
        meta={meta}
      />
    </>
  )
}

/** The Feed tab — presence strip, composer, posts. */
function FeedTabBody() {
  useEffect(() => {
    void loadHouseholdUsers()
    loadFeed()
  }, [])

  const handleLoadMore = () => {
    const last = posts.value[posts.value.length - 1]
    if (last) loadFeed(last.created_at)
  }

  const handleSubmit = async (
    type: string,
    content: string,
    mediaUrl?: string,
    extras?: {
      location?: { lat: number; lon: number; label: string | null }
      imageUrls?: string[]
      noLinkPreview?: boolean
    },
  ) => {
    const body: Record<string, unknown> = {
      type, content,
      media_url: mediaUrl ?? null,
      image_urls: extras?.imageUrls ?? [],
    }
    if (extras?.location) body.location = extras.location
    if (extras?.noLinkPreview) body.no_link_preview = true
    const post = await api.post('/api/feed/posts', body) as FeedPost
    showToast(t('feed.post_shared'), 'success')
    // No local prepend here — wireFeedWs() handles `post.created` and
    // dedupes by id, so the new post lands at the top exactly once.
    return post?.id
  }

  const handleReact = async (postId: string, emoji: string) => {
    const updated = await api.post(
      `/api/feed/posts/${postId}/reactions`, { emoji },
    ) as FeedPost
    posts.value = posts.value.map((p) => (p.id === postId ? updated : p))
  }

  const handleDelete = async (postId: string) => {
    if (!await confirmDialog(t('feed.delete_confirm'), { destructive: true })) return
    await api.delete(`/api/feed/posts/${postId}`)
    showToast(t('feed.post_deleted'), 'info')
    // wireFeedWs() removes the row on `post.deleted`. No reload.
  }

  /** Inline edit of a post's text (author or household admin — the
   *  server's rule). ``true`` closes the editor. */
  const handleEdit = async (postId: string, content: string): Promise<boolean> => {
    try {
      const updated = await api.patch(`/api/feed/posts/${postId}`, { content }) as FeedPost
      posts.value = posts.value.map((p) => (p.id === postId ? mergePostEdit(p, updated) : p))
      showToast(t('post.edit.saved'), 'success')
      return true
    } catch (err: unknown) {
      showToast(t('post.edit.failed', { error: String((err as Error)?.message ?? err) }), 'error')
      return false
    }
  }
  const me = currentUser.value

  // Cold-start: no posts yet AND a fetch in flight → show the
  // layout-stable skeleton instead of an isolated spinner so the eye
  // can register the page chrome immediately. Subsequent fetches
  // (paging) keep the spinner since the layout is already mounted.
  const isInitialLoad = feedLoading.value && posts.value.length === 0

  if (isInitialLoad) {
    return <FeedSkeleton />
  }

  return (
    <PullToRefresh onRefresh={() => loadFeed()}>
    <div class="sh-feed">
      <HouseholdPresenceStrip />
      <Composer onSubmit={handleSubmit} context="Household" />
      {posts.value.map(post => (
        <div key={post.id} class="sh-feed-item">
          <PostCard
            post={post}
            onReact={(emoji) => handleReact(post.id, emoji)}
            onComment={() => openCommentOverlay(post, null)}
            onDelete={() => handleDelete(post.id)}
            onEdit={me && (post.author === me.user_id || me.is_admin)
              ? (content) => handleEdit(post.id, content)
              : undefined}
          />
        </div>
      ))}
      {/* Pagination spinner — full reload uses the FeedSkeleton above. */}
      {feedLoading.value && posts.value.length > 0 && <PostCardSkeleton />}
      {!feedLoading.value && posts.value.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">📝</div>
          <h3>{t('feed.empty.title')}</h3>
          <p>{t('feed.empty.body')}</p>
          <p class="sh-muted">{t('feed.empty.hint')}</p>
        </div>
      )}
      {feedHasMore.value && !feedLoading.value && posts.value.length > 0 && (
        <Button variant="secondary" onClick={handleLoadMore}>{t('feed.load_more')}</Button>
      )}
    </div>
    </PullToRefresh>
  )
}
