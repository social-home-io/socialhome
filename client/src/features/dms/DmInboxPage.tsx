/**
 * DmInboxPage — the **Chats** panel (Talk → Chats).
 *
 * Three tabs: ``dms`` (1:1 conversations), ``groups`` (multi-party
 * conversations), ``calls`` (active call tray, replaces the old
 * standalone ``/calls`` page). Tab state is URL-driven via the
 * ``?tab=`` query param so deep-links from elsewhere in the app and
 * external bookmarks (e.g. ``/calls`` redirects to
 * ``/dms?tab=calls``) land on the right tab.
 */
import { useEffect } from 'preact/hooks'
import { signal, computed } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { ws } from '@/ws'
import { alertingUnread, dmUnreadTotal } from '@/store/dms'
import type { Conversation } from '@/types'
import { DmInboxSkeleton } from '@/components/Skeleton'
import { Button } from '@/components/Button'
import { TabHeader } from '@/components/TabHeader'
import { openNewDm } from '@/components/NewDmDialog'
import { Avatar } from '@/components/Avatar'
import { PullToRefresh } from '@/components/PullToRefresh'
import { useTitle } from '@/store/pageTitle'
import { relativeChatTime } from '@/utils/relativeTime'
import { isMuteActive, mutedLabel } from '@/utils/mute'
import CallsTab from './CallsTab'

type ChatsTab = 'dms' | 'groups' | 'calls'

const TABS: readonly ChatsTab[] = ['dms', 'groups', 'calls'] as const
const TAB_LABELS: Readonly<Record<ChatsTab, string>> = {
  dms:    'DMs',
  groups: 'Groups',
  calls:  'Calls',
}

const conversations = signal<Conversation[]>([])
const loading = signal(true)
const activeTab = signal<ChatsTab>('dms')

const dmsList = computed(() =>
  conversations.value.filter((c) => c.type !== 'group_dm'),
)
const groupsList = computed(() =>
  conversations.value.filter((c) => c.type === 'group_dm'),
)


function tabFromUrl(url: string): ChatsTab {
  // ``url`` is the location object's ``url`` — full path + query.
  // ``URLSearchParams`` parses the query string after the first '?'.
  const q = url.split('?')[1] ?? ''
  const t = new URLSearchParams(q).get('tab')
  if (t === 'dms' || t === 'groups' || t === 'calls') return t
  return 'dms'
}


export default function DmInboxPage() {
  useTitle('Chats')
  const loc = useLocation()

  // Sync tab state from the URL on mount and any subsequent route
  // change (back/forward, deep links from another tab in the SPA).
  useEffect(() => {
    activeTab.value = tabFromUrl(loc.url)
  }, [loc.url])

  useEffect(() => {
    void reload().finally(() => { loading.value = false })
    // Refresh on any DM frame the server fans out — new conversations
    // and new messages both bump ``last_message_at`` ordering.
    const offMsg  = ws.on('dm.message',              () => { void reload() })
    const offConv = ws.on('dm.conversation.created', () => { void reload() })
    return () => { offMsg(); offConv() }
  }, [])

  const onSelectTab = (tab: ChatsTab) => {
    activeTab.value = tab
    const next = tab === 'dms' ? '/dms' : `/dms?tab=${tab}`
    if (loc.url !== next) loc.route(next, true)
  }

  // Action lives inside the sticky subheader (TabHeader's ``actions``
  // slot) so the "+ New message" button doesn't collide with the
  // sticky tabs on first render — the previous layout had a separate
  // ``.sh-page-header`` row sitting at the same y-position as the
  // sticky tabs, hiding the button behind the subheader's tinted
  // background.
  const newMessageBtn = (
    <Button onClick={() => openNewDm()}>+ New message</Button>
  )
  const tabHeader = (
    <TabHeader<ChatsTab>
      activeTab={activeTab.value}
      visibleTabs={TABS}
      labels={TAB_LABELS}
      ariaLabel="Chats sections"
      onSelectTab={onSelectTab}
      actions={newMessageBtn}
    />
  )

  if (loading.value && activeTab.value !== 'calls') {
    return (
      <div class="sh-dms">
        {tabHeader}
        <DmInboxSkeleton />
      </div>
    )
  }

  if (activeTab.value === 'calls') {
    return (
      <div class="sh-dms">
        {tabHeader}
        <CallsTab />
      </div>
    )
  }

  const list = activeTab.value === 'groups' ? groupsList.value : dmsList.value
  const emptyCopy = activeTab.value === 'groups'
    ? 'Group conversations are 3+ people. Start one to see it here.'
    : 'Direct messages are 1:1 chats with people in your household or connected households.'

  return (
    <PullToRefresh onRefresh={reload}>
      <div class="sh-dms">
        {tabHeader}
        {list.length === 0 && (
          <div class="sh-empty-state">
            <div aria-hidden="true">💬</div>
            <h3>
              {activeTab.value === 'groups' ? 'No groups yet' : 'No conversations yet'}
            </h3>
            <p>{emptyCopy}</p>
            <Button onClick={() => openNewDm()}>+ Start a conversation</Button>
          </div>
        )}
        {list.map((c) => {
          const peers = c.members ?? []
          // Display-name fallback: explicit name > peer display names
          // (joined by "·") > "Direct message". The peer-name path is
          // what most groups end up with — even unnamed ones read as
          // "Anna · Bob · Carol" instead of an opaque "Direct message".
          const fallbackName = peers.length > 0
            ? peers.map((p) => p.display_name).join(' · ')
            : 'Direct message'
          const displayName = c.name || fallbackName
          // Stack up to 3 avatars, then a "+N" overflow chip. Falls
          // back to a single avatar from the conversation name when
          // members are missing (legacy rows from before this field).
          const visiblePeers = peers.slice(0, 3)
          const overflow = peers.length - visiblePeers.length
          const muted = isMuteActive(c.muted_until)
          return (
            <a key={c.id} href={`/dms/${c.id}`} class="sh-dm-row">
              <div class="sh-dm-avatars">
                {peers.length === 0 ? (
                  <Avatar name={displayName} size={40} />
                ) : (
                  <>
                    {visiblePeers.map((p) => (
                      <Avatar
                        key={p.user_id}
                        name={p.display_name}
                        src={p.picture_url}
                        size={32}
                      />
                    ))}
                    {overflow > 0 && (
                      <span class="sh-dm-avatar-more">+{overflow}</span>
                    )}
                  </>
                )}
              </div>
              <div class="sh-dm-info">
                <strong>
                  {displayName}
                  {muted && (
                    <span
                      class="sh-dm-muted"
                      role="img"
                      aria-label={mutedLabel(c.muted_until as string)}
                      title={mutedLabel(c.muted_until as string)}
                    >🔕</span>
                  )}
                </strong>
                <span class="sh-badge">
                  {c.type === 'group_dm'
                    ? `Group · ${c.member_count ?? peers.length + 1}`
                    : 'DM'}
                </span>
              </div>
              {c.last_message_at && (
                <time
                  class="sh-muted sh-dm-time"
                  dateTime={c.last_message_at}
                  title={new Date(c.last_message_at).toLocaleString()}
                >
                  {relativeChatTime(c.last_message_at)}
                </time>
              )}
              {c.unread && c.unread > 0 ? (
                <span
                  class={muted ? 'sh-dm-unread sh-dm-unread--muted' : 'sh-dm-unread'}
                  aria-label={`${c.unread} unread`}
                >
                  {c.unread > 99 ? '99+' : c.unread}
                </span>
              ) : null}
            </a>
          )
        })}
      </div>
    </PullToRefresh>
  )
}


const reload = () =>
  api
    .get('/api/conversations')
    .then((data: Conversation[]) => {
      conversations.value = data
      // Re-sum the sidebar badge from the just-fetched payload so the
      // inbox and the badge can never drift while this page is open.
      // Muted conversations keep their row count but stay out of it.
      dmUnreadTotal.value = alertingUnread(data)
    })
    .catch(() => {
      /* noop — keep current list on transient failures */
    })
