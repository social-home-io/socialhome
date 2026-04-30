import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useRoute } from 'preact-iso'
import { api } from '@/api'
import { ws } from '@/ws'
import { currentUser } from '@/store/auth'
import { loadHouseholdUsers } from '@/store/householdUsers'
import { loadSpaceMembers } from '@/store/spaceMembers'
import { useTitle } from '@/store/pageTitle'
import { groupEventsByDay, formatMonthHeading, monthRange } from '@/utils/calendar'
import type { Comment, FeedPost, CalendarEvent } from '@/types'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { JoinRequestList } from '@/components/JoinRequestList'
import { ModerationQueue } from '@/components/ModerationQueue'
import { SpaceLocationCard } from '@/components/SpaceLocationCard'
import { SpaceMemberList } from '@/components/SpaceMemberList'
import { Gallery } from '@/components/Gallery'
import { Button } from '@/components/Button'
import { PostCard } from '@/components/PostCard'
import { Composer } from '@/components/Composer'
import { CommentThread } from '@/components/CommentThread'
import { SpaceSubHeader, type SpaceTab } from '@/components/SpaceSubHeader'
import { SpaceTasksTab, resetSpaceTasks } from './SpaceTasksTab'
import { useSpaceTheme } from '@/hooks/useSpaceTheme'
import { CalendarEventDialog, openSpaceEventDialog } from '@/components/CalendarEventDialog'
import { SpaceLinksStrip } from './SpaceLinksStrip'
import { SpaceNotifPrefsMenu } from './SpaceNotifPrefsMenu'

interface SpacePage { id: string; title: string; updated_at: string }

interface SpaceDetail {
  id: string
  name: string
  emoji: string | null
  description: string | null
  about_markdown: string | null
  cover_url: string | null
  cover_hash: string | null
  features?: {
    location?: boolean
  }
}

const posts = signal<FeedPost[]>([])
const loading = signal(true)
const activeTab = signal<SpaceTab>('feed')
const spacePages = signal<SpacePage[]>([])
const spaceCalEvents = signal<CalendarEvent[]>([])
const spaceCalCursor = signal(new Date())
const selectedSpaceEventId = signal<string | null>(null)
const viewerRole = signal<
  'owner' | 'admin' | 'member' | 'subscriber' | undefined
>(undefined)
const expandedComments = signal<Record<string, Comment[]>>({})
const spaceDetail = signal<SpaceDetail | null>(null)
const memberCount = signal<number | null>(null)

async function loadSpaceFeed(spaceId: string) {
  const rows = await api.get(`/api/spaces/${spaceId}/feed`) as FeedPost[]
  posts.value = rows
}

async function loadSpaceCalendar(spaceId: string) {
  // Use the per-space calendar endpoint directly — the route fans out
  // to the space's own calendar without us having to look up its id
  // first. Same shape as the household ``/api/calendars/{id}/events``
  // response, just space-scoped.
  try {
    const { start, end } = monthRange(spaceCalCursor.value)
    spaceCalEvents.value = await api.get(
      `/api/spaces/${spaceId}/calendar/events`,
      { start, end },
    ) as CalendarEvent[]
  } catch {
    spaceCalEvents.value = []
  }
}

function navigateSpaceMonth(direction: number, spaceId: string) {
  const next = new Date(spaceCalCursor.value)
  next.setMonth(next.getMonth() + direction)
  spaceCalCursor.value = next
  selectedSpaceEventId.value = null
  void loadSpaceCalendar(spaceId)
}

function jumpToSpaceToday(spaceId: string) {
  spaceCalCursor.value = new Date()
  selectedSpaceEventId.value = null
  void loadSpaceCalendar(spaceId)
}

export default function SpaceFeedPage() {
  const { params } = useRoute()
  const spaceId = params.id

  // Apply the space's custom theme (§23 customization). The hook
  // fetches /api/spaces/{id}/theme, sets CSS vars, and cleans up on
  // unmount so household colours return as the user leaves.
  useSpaceTheme(spaceId)
  // Surface the space's name in the global TopBar (matches the
  // household feed pattern). Falls back to "Space" while the detail
  // request is in flight.
  const detail = spaceDetail.value
  useTitle(
    detail
      ? (detail.emoji ? `${detail.emoji} ${detail.name}` : detail.name)
      : 'Space',
  )

  useEffect(() => {
    activeTab.value = 'feed'
    loading.value = true
    viewerRole.value = undefined
    expandedComments.value = {}
    spaceDetail.value = null
    memberCount.value = null
    resetSpaceTasks()
    spaceCalEvents.value = []
    spaceCalCursor.value = new Date()
    selectedSpaceEventId.value = null
    void loadHouseholdUsers()
    void loadSpaceMembers(spaceId)
    api.get(`/api/spaces/${spaceId}`).then((d) => {
      spaceDetail.value = d as SpaceDetail
    }).catch(() => { /* non-fatal */ })
    loadSpaceFeed(spaceId)
      .catch(() => { posts.value = [] })
      .finally(() => { loading.value = false })
    // Derive viewer's role from the member list so admin-only UI renders.
    const me = currentUser.value?.user_id
    if (me) {
      api.get(`/api/spaces/${spaceId}/members`)
        .then((members: { user_id: string; role: string }[]) => {
          memberCount.value = members.length
          const mine = members.find(m => m.user_id === me)
          if (
            mine
            && (mine.role === 'owner' || mine.role === 'admin'
                || mine.role === 'member' || mine.role === 'subscriber')
          ) {
            viewerRole.value = mine.role
          }
        })
        .catch(() => { viewerRole.value = undefined })
    }

    const refreshIfExpanded = (postId: string) => {
      if (!expandedComments.value[postId]) return
      void api.get(
        `/api/spaces/${spaceId}/posts/${postId}/comments`,
      ).then((rows) => {
        expandedComments.value = {
          ...expandedComments.value,
          [postId]: rows as Comment[],
        }
      })
    }
    const off1 = ws.on('comment.added', (e) => {
      const d = e.data as { post_id: string; space_id?: string | null }
      if (d.space_id === spaceId) refreshIfExpanded(d.post_id)
    })
    const off2 = ws.on('comment.updated', (e) => {
      const d = e.data as { post_id: string; space_id?: string | null }
      if (d.space_id === spaceId) refreshIfExpanded(d.post_id)
    })
    const off3 = ws.on('comment.deleted', (e) => {
      const d = e.data as { post_id: string; space_id?: string | null }
      if (d.space_id === spaceId) refreshIfExpanded(d.post_id)
    })
    const off4 = ws.on('space.post.created', (e) => {
      const d = e.data as { space_id?: string | null }
      if (d.space_id === spaceId) void loadSpaceFeed(spaceId)
    })
    return () => { off1(); off2(); off3(); off4() }
  }, [spaceId])

  const loadTabData = (tab: SpaceTab) => {
    activeTab.value = tab
    if (tab === 'pages') {
      api.get(`/api/spaces/${spaceId}/pages`).then((data: SpacePage[]) => {
        spacePages.value = data
      }).catch(() => { spacePages.value = [] })
    }
    if (tab === 'calendar') {
      void loadSpaceCalendar(spaceId)
    }
  }

  const handleSubmit = async (
    type: string,
    content: string,
    mediaUrl?: string,
    extras?: {
      location?: { lat: number; lon: number; label: string | null }
      imageUrls?: string[]
    },
  ) => {
    const body: Record<string, unknown> = {
      type, content, media_url: mediaUrl ?? null,
      image_urls: extras?.imageUrls ?? [],
    }
    if (extras?.location) body.location = extras.location
    const post = await api.post(
      `/api/spaces/${spaceId}/posts`,
      body,
    ) as { id: string }
    showToast('Post shared', 'success')
    await loadSpaceFeed(spaceId)
    return post?.id
  }

  const handleReact = async (postId: string, emoji: string) => {
    await api.post(
      `/api/spaces/${spaceId}/posts/${postId}/reactions`, { emoji },
    )
    void loadSpaceFeed(spaceId)
  }

  const handleDelete = async (postId: string) => {
    if (!confirm('Delete this post?')) return
    await api.delete(`/api/spaces/${spaceId}/posts/${postId}`)
    showToast('Post deleted', 'info')
    void loadSpaceFeed(spaceId)
  }

  const refreshComments = async (postId: string) => {
    const rows = await api.get(
      `/api/spaces/${spaceId}/posts/${postId}/comments`,
    ) as Comment[]
    expandedComments.value = {
      ...expandedComments.value, [postId]: rows,
    }
  }

  const handleToggleComments = async (postId: string) => {
    if (expandedComments.value[postId]) {
      const { [postId]: _dropped, ...rest } = expandedComments.value
      expandedComments.value = rest
    } else {
      await refreshComments(postId)
    }
  }

  const handleReply = async (
    postId: string, parentId: string | null, content: string,
  ) => {
    await api.post(
      `/api/spaces/${spaceId}/posts/${postId}/comments`,
      { content, parent_id: parentId },
    )
    await refreshComments(postId)
    void loadSpaceFeed(spaceId)
  }

  const handleCommentEdit = async (
    postId: string, commentId: string, content: string,
  ) => {
    try {
      await api.patch(
        `/api/spaces/${spaceId}/posts/${postId}/comments/${commentId}`,
        { content },
      )
      await refreshComments(postId)
      showToast('Comment updated', 'success')
    } catch (err: unknown) {
      showToast(`Edit failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  const handleCommentDelete = async (postId: string, commentId: string) => {
    try {
      await api.delete(
        `/api/spaces/${spaceId}/posts/${postId}/comments/${commentId}`,
      )
      await refreshComments(postId)
      void loadSpaceFeed(spaceId)
      showToast('Comment deleted', 'info')
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  if (loading.value) return <Spinner />

  const canAdmin = viewerRole.value === 'owner' || viewerRole.value === 'admin'
  const s = spaceDetail.value

  const visibleTabs: readonly SpaceTab[] = [
    'feed', 'members', 'pages', 'calendar', 'tasks', 'gallery',
    ...(s?.features?.location ? (['map'] as const) : []),
    ...(canAdmin ? (['moderation'] as const) : []),
  ]

  return (
    <div class="sh-space-feed sh-space-scope">
      <SpaceSubHeader
        name={s?.name ?? 'Space'}
        emoji={s?.emoji ?? null}
        coverUrl={s?.cover_url ?? null}
        memberCount={memberCount.value}
        activeTab={activeTab}
        visibleTabs={visibleTabs}
        onSelectTab={loadTabData}
        actions={
          <>
            {s && viewerRole.value !== undefined && (
              <SpaceNotifPrefsMenu spaceId={spaceId} />
            )}
            {canAdmin && (
              <a href={`/spaces/${spaceId}/settings`}
                 class="sh-space-settings-btn"
                 aria-label="Space settings">
                ⚙ Settings
              </a>
            )}
          </>
        }
      />
      {s && <SpaceLinksStrip spaceId={spaceId} />}

      {activeTab.value === 'feed' && (
        <div class="sh-feed sh-space-feed-content">
          {viewerRole.value === 'subscriber' ? (
            <div class="sh-subscriber-banner" role="status">
              <span class="sh-subscriber-banner__icon" aria-hidden="true">🔔</span>
              <div class="sh-subscriber-banner__body">
                <strong>You're subscribed to this space.</strong>
                <p class="sh-muted">
                  You see new posts here but can't post, comment, or react.
                  Ask an admin to upgrade you to a full member if you want to join in.
                </p>
              </div>
              <button
                type="button"
                class="sh-subscribe-btn sh-subscribe-btn--on"
                aria-label="Unsubscribe from this space"
                title="Stop receiving updates from this space."
                onClick={async () => {
                  try {
                    await api.delete(`/api/spaces/${spaceId}/subscribe`)
                    showToast('Unsubscribed', 'info')
                    window.location.href = '/spaces'
                  } catch (exc) {
                    showToast((exc as Error).message, 'error')
                  }
                }}
              >
                🔕 Unsubscribe
              </button>
            </div>
          ) : (
            <Composer onSubmit={handleSubmit} context="Space" spaceId={spaceId} />
          )}
          {posts.value.length === 0 && (
            <p class="sh-muted">No posts in this space yet.</p>
          )}
          {posts.value.map(post => (
            <div key={post.id} class="sh-feed-item">
              <PostCard
                post={post}
                onReact={(emoji) => handleReact(post.id, emoji)}
                onComment={() => handleToggleComments(post.id)}
                onDelete={() => handleDelete(post.id)}
                showSpaceBadge={spaceId}
              />
              {expandedComments.value[post.id] && (
                <CommentThread
                  comments={expandedComments.value[post.id]}
                  spaceId={spaceId}
                  onReply={(parentId, content) =>
                    handleReply(post.id, parentId, content)}
                  onEdit={(commentId, content) =>
                    handleCommentEdit(post.id, commentId, content)}
                  onDelete={(commentId) =>
                    handleCommentDelete(post.id, commentId)}
                />
              )}
            </div>
          ))}
        </div>
      )}

      {activeTab.value === 'members' && (
        <>
          {(viewerRole.value === 'owner' || viewerRole.value === 'admin') && (
            <JoinRequestList spaceId={spaceId} />
          )}
          <SpaceMemberList spaceId={spaceId} viewerRole={viewerRole.value} />
        </>
      )}

      {activeTab.value === 'pages' && (
        <div class="sh-space-pages">
          <h2>Pages</h2>
          {spacePages.value.length === 0 && <p class="sh-muted">No pages in this space.</p>}
          {spacePages.value.map(p => (
            <div key={p.id} class="sh-page-card">
              <strong>{p.title}</strong>
              <time class="sh-muted">{new Date(p.updated_at).toLocaleString()}</time>
            </div>
          ))}
        </div>
      )}

      {activeTab.value === 'calendar' && (() => {
        const grouped = groupEventsByDay(spaceCalEvents.value)
        const dayKeys = Object.keys(grouped).sort(
          (a, b) => new Date(a).getTime() - new Date(b).getTime(),
        )
        return (
          <div class="sh-calendar">
            <div class="sh-page-header">
              <Button onClick={() => openSpaceEventDialog(spaceId)}>
                + New event
              </Button>
            </div>

            <div class="sh-calendar-controls">
              <div class="sh-calendar-nav">
                <Button variant="secondary"
                        aria-label="Previous month"
                        onClick={() => navigateSpaceMonth(-1, spaceId)}>
                  &#8249;
                </Button>
                <span class="sh-calendar-heading">
                  {formatMonthHeading(spaceCalCursor.value)}
                </span>
                <Button variant="secondary"
                        aria-label="Next month"
                        onClick={() => navigateSpaceMonth(1, spaceId)}>
                  &#8250;
                </Button>
                <Button variant="secondary"
                        onClick={() => jumpToSpaceToday(spaceId)}>
                  Today
                </Button>
              </div>
            </div>

            {spaceCalEvents.value.length === 0 && (
              <div class="sh-empty-state">
                <div style={{ fontSize: '2rem' }}>📅</div>
                <h3>No events this month</h3>
                <p>
                  Click <strong>+ New event</strong> to schedule something
                  in this space.
                </p>
              </div>
            )}

            {dayKeys.map(dayKey => (
              <div key={dayKey} class="sh-calendar-day-group">
                <h3 class="sh-calendar-day-heading">{dayKey}</h3>
                {grouped[dayKey].map(e => (
                  <div
                    key={e.id}
                    class="sh-event"
                    onClick={() => {
                      selectedSpaceEventId.value =
                        selectedSpaceEventId.value === e.id ? null : e.id
                    }}
                  >
                    <div class="sh-event-header">
                      <strong>{e.summary}</strong>
                      <time>
                        {new Date(e.start).toLocaleTimeString(undefined, {
                          hour: '2-digit', minute: '2-digit',
                        })}
                      </time>
                      {e.all_day && <span class="sh-badge">All day</span>}
                    </div>
                    {selectedSpaceEventId.value === e.id && (
                      <div class="sh-event-detail">
                        {e.description && <p>{e.description}</p>}
                        <div class="sh-event-times">
                          <span>Starts {new Date(e.start).toLocaleString()}</span>
                          <span>Ends {new Date(e.end).toLocaleString()}</span>
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ))}

            <CalendarEventDialog onCreated={() => loadTabData('calendar')} />
          </div>
        )
      })()}

      {activeTab.value === 'tasks' && (
        <SpaceTasksTab spaceId={spaceId} />
      )}

      {activeTab.value === 'gallery' && (
        <Gallery spaceId={spaceId} />
      )}

      {activeTab.value === 'map' && s?.features?.location && (
        <SpaceLocationCard spaceId={spaceId} />
      )}

      {activeTab.value === 'moderation' && canAdmin && (
        <ModerationQueue spaceId={spaceId} />
      )}
    </div>
  )
}
