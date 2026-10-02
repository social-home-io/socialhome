import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useRoute } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { ws } from '@/ws'
import { currentUser } from '@/store/auth'
import { loadHouseholdUsers } from '@/store/householdUsers'
import { instanceConfig } from '@/store/instance'
import { loadSpaceMembers, setSpaceHereAllowed } from '@/store/spaceMembers'
import { useTitle } from '@/store/pageTitle'
import { t } from '@/i18n/i18n'
import {
  advanceDate, dateRangeForMode, formatDayLabel, formatEventBounds, formatRangeHeading,
  groupEventsByDay,
  type CalendarViewMode,
} from '@/utils/calendar'
import type { FeedPost, CalendarEvent } from '@/types'
import { EventRowMeta } from '@/components/EventRowMeta'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { JoinRequestList } from '@/components/JoinRequestList'
import { ModerationQueue } from '@/components/ModerationQueue'
import { SpaceLocationCard } from '@/components/SpaceLocationCard'
import { SpaceMemberList } from '@/components/SpaceMemberList'
import GalleryPage from '@/features/gallery/GalleryPage'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { SubscribeFeed } from '@/components/SubscribeFeed'
import { PostCard } from '@/components/PostCard'
import { Composer } from '@/components/Composer'
import { openCommentOverlay } from '@/components/CommentOverlay'
import { SpaceSubHeader, type SpaceTab } from '@/components/SpaceSubHeader'
import { SpaceTasksTab } from './SpaceTasksTab'
import { SpaceCalendarHost } from './SpaceCalendarHost'
import { calendarTabLabel, parseSpaceTab, visibleSpaceTabs } from './spaceTabs'
import {
  canContribute,
  canModerate as roleCanModerate,
  hasSettingsAuthority,
  isWriterRole,
  parseSpaceRole,
  type SpaceRole,
} from './spaceRoles'
import { AccessNote } from './AccessNote'
import { accessLevel, accessNote, blockedByAdminOnly, type AccessFeature } from './spaceAccess'
import { SpaceBazaarTab } from './SpaceBazaarTab'
import StickyBoardPage from '@/features/stickies/StickyBoardPage'
import { useSpaceTheme } from '@/hooks/useSpaceTheme'
import { useSpaceConfigWs } from '@/hooks/useSpaceConfigWs'
import { CalendarEventDialog, openSpaceEventDialog } from '@/components/CalendarEventDialog'
import { SpaceLinksStrip } from './SpaceLinksStrip'
import { SpaceProposalsBanner } from '@/components/SpaceProposalsBanner'
import { SpaceVersionBanner } from '@/components/SpaceVersionBanner'
import { SpaceHero } from '@/components/SpaceHero'
import { SpaceNotifPrefsMenu } from './SpaceNotifPrefsMenu'
import { confirmDialog } from '@/components/confirm'

interface SpacePage { id: string; title: string; updated_at: string }

interface SpaceDetail {
  id: string
  name: string
  /** §23.42 — gates the ``@here`` entry in the mention picker. */
  allow_here_mention?: boolean
  emoji: string | null
  description: string | null
  about_markdown: string | null
  cover_url: string | null
  cover_hash: string | null
  icon_url: string | null
  icon_hash: string | null
  features?: {
    pages?: boolean
    calendar?: boolean
    /** A shared weekly timetable (a class schedule) in the Calendar
     *  tab. Opt-in: absent → off. */
    timetable?: boolean
    todo?: boolean
    stickies?: boolean
    gallery?: boolean
    bazaar?: boolean
    location?: boolean
    /** §23.49 — post types members may compose here; gates the
     *  composer's type picker. Absent → all types offered. */
    allowed_post_types?: string[]
  }
  /** §D1b — the originating instance. When it differs from
   *  ``instanceConfig.value.instance_id``, this is a stub of a
   *  remote-hosted space and local admin gestures are suppressed. */
  owner_instance_id?: string
  /** Read-only archive state — hide the composer + show a banner. */
  archived?: boolean
  /** Why archived. ``null`` = a reversible admin archive; ``'dissolved'``
   *  = the owner host dissolved the space; ``'removed'`` = this household
   *  was removed. The latter two are remote-terminated read-only archives
   *  that can't be revived — the banner copy adapts to this. */
  archived_reason?: 'dissolved' | 'removed' | null
}

/** Reason-aware copy for the read-only archive banner + empty-state.
 *  Returns ``null`` when the space isn't archived. Pure (no DOM, no
 *  signals) so the wording can be unit-tested without rendering the page. */
export function archivedCopy(
  archived: boolean | undefined,
  reason: 'dissolved' | 'removed' | null | undefined,
): { title: string; body: string; empty: string } | null {
  if (!archived) return null
  if (reason === 'dissolved') {
    return {
      title: 'This space was dissolved by its owner.',
      body:
        'This is a read-only archive of what you had — no new posts or '
        + 'comments. It can’t be revived.',
      empty:
        'This space was dissolved by its owner. This is a read-only archive '
        + 'of what you had.',
    }
  }
  if (reason === 'removed') {
    return {
      title: 'You’re no longer a member of this space.',
      body:
        'This is a read-only archive of what you had — no new posts or '
        + 'comments.',
      empty:
        'You’re no longer a member of this space. This is a read-only '
        + 'archive of what you had.',
    }
  }
  // Normal, reversible admin archive.
  return {
    title: 'This space is archived.',
    body:
      'It’s read-only — existing content is kept, but no new posts or '
      + 'comments can be added until an admin unarchives it.',
    empty:
      'This space is archived (read-only). Unarchive it from settings to '
      + 'start posting again.',
  }
}

const posts = signal<FeedPost[]>([])
const loading = signal(true)
const activeTab = signal<SpaceTab>('feed')
/** Calendar tab → "Subscribe" dialog (private iCal link). */
const subscribeFeedOpen = signal(false)
const spacePages = signal<SpacePage[]>([])
const spaceCalEvents = signal<CalendarEvent[]>([])
const spaceCalCursor = signal(new Date())
const spaceCalView = signal<CalendarViewMode>('month')
/** Which agenda row is expanded, as ``"<dayKey>:<eventId>"``. Keyed
 *  by the DAY CARD, not the event: a multi-day event renders one row
 *  per covered day, so an event-id key would expand every one of them
 *  at once. */
const selectedSpaceEventId = signal<string | null>(null)
const viewerRole = signal<SpaceRole | undefined>(undefined)
const spaceDetail = signal<SpaceDetail | null>(null)
/** The member list answered (or failed) — until then ``viewerRole`` is
 *  unknown, not "no role". */
const roleKnown = signal(false)
const memberCount = signal<number | null>(null)

/** The space header + the viewer's role: ``GET /api/spaces/{id}`` (name,
 *  emoji, cover, features → tabs, archive state) and the member list
 *  (role → admin-only UI, member count). Runs on open and again on every
 *  ``space.config.changed`` frame — a rename, feature toggle or admin
 *  grant elsewhere shows up without a reload. A failed refetch keeps
 *  what's on screen. */
async function loadSpaceHeader(spaceId: string) {
  const me = currentUser.value?.user_id
  await Promise.all([
    api.get(`/api/spaces/${spaceId}`).then((d) => {
      spaceDetail.value = d as SpaceDetail
      setSpaceHereAllowed(spaceId, (d as SpaceDetail).allow_here_mention === true)
    }).catch(() => { /* non-fatal */ }),
    // Derive viewer's role from the member list so admin-only UI renders.
    me
      ? api.get(`/api/spaces/${spaceId}/members`)
        .then((members: { user_id: string; role: string }[]) => {
          memberCount.value = members.length
          const mine = members.find(m => m.user_id === me)
          viewerRole.value = mine ? parseSpaceRole(mine.role) : undefined
          roleKnown.value = true
        })
        .catch(() => { roleKnown.value = true /* keep the last role */ })
      : Promise.resolve().then(() => { roleKnown.value = true }),
  ])
}

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
    const { start, end } = dateRangeForMode(
      spaceCalCursor.value, spaceCalView.value,
    )
    spaceCalEvents.value = await api.get(
      `/api/spaces/${spaceId}/calendar/events`,
      { start, end },
    ) as CalendarEvent[]
  } catch {
    spaceCalEvents.value = []
  }
}

function navigateSpaceCalendar(direction: number, spaceId: string) {
  spaceCalCursor.value = advanceDate(
    spaceCalCursor.value, direction, spaceCalView.value,
  )
  selectedSpaceEventId.value = null
  void loadSpaceCalendar(spaceId)
}

function jumpToSpaceToday(spaceId: string) {
  spaceCalCursor.value = new Date()
  selectedSpaceEventId.value = null
  void loadSpaceCalendar(spaceId)
}

function setSpaceCalendarView(mode: CalendarViewMode, spaceId: string) {
  if (spaceCalView.value === mode) return
  spaceCalView.value = mode
  selectedSpaceEventId.value = null
  void loadSpaceCalendar(spaceId)
}

export default function SpaceFeedPage() {
  const { params, query } = useRoute()
  const spaceId = params.id
  // ``?tab=tasks`` (e.g. from a task notification) opens that tab.
  const linkedTab = parseSpaceTab(query?.tab)

  // Apply the space's custom theme (§23 customization). The hook
  // fetches /api/spaces/{id}/theme, sets CSS vars, and cleans up on
  // unmount so household colours return as the user leaves.
  useSpaceTheme(spaceId)
  // Live header / tabs / role on a config change elsewhere; a dissolve
  // leaves for the spaces list with a toast.
  useSpaceConfigWs(spaceId, () => { void loadSpaceHeader(spaceId) })
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
    roleKnown.value = false
    spaceDetail.value = null
    memberCount.value = null
    spaceCalEvents.value = []
    spaceCalCursor.value = new Date()
    spaceCalView.value = 'month'
    selectedSpaceEventId.value = null
    void loadHouseholdUsers()
    void loadSpaceMembers(spaceId)
    loadSpaceFeed(spaceId)
      .catch(() => { posts.value = [] })
      .finally(() => { loading.value = false })
    void loadSpaceHeader(spaceId)

    const off4 = ws.on('space.post.created', (e) => {
      const d = e.data as { space_id?: string | null }
      if (d.space_id === spaceId) void loadSpaceFeed(spaceId)
    })
    // Live comment counts — the space analogue of store/feed.ts. The
    // frame fans out to space members as
    // {type, post_id, space_id, comment}; bump the matching post's
    // comment_count in place so the badge updates without a refetch.
    const offComment = ws.on('comment.added', (e) => {
      const d = e.data as { space_id?: string | null; post_id?: string }
      if (d.space_id === spaceId) {
        posts.value = posts.value.map(p =>
          p.id === d.post_id
            ? { ...p, comment_count: (p.comment_count ?? 0) + 1 }
            : p,
        )
      }
    })
    // space.post.moderated is the only space-post mutation the backend
    // broadcasts to space members (realtime_service._on_space_post_moderated);
    // post.edited / post.deleted / post.reaction_changed go to the
    // household only and carry no space_id, so they're not wired here.
    const offModerated = ws.on('space.post.moderated', (e) => {
      const d = e.data as { space_id?: string | null }
      if (d.space_id === spaceId) void loadSpaceFeed(spaceId)
    })
    // Live space calendar — the shared store/calendar.ts won't refresh
    // this tab (its activeCalendarScope is null on the space view), so
    // refetch the space calendar signal directly. created/updated carry
    // event.calendar_id; deleted carries calendar_id.
    const offCalCreated = ws.on('calendar.created', (e) => {
      const d = e.data as { event?: { calendar_id?: string } }
      if (d.event?.calendar_id === spaceId) void loadSpaceCalendar(spaceId)
    })
    const offCalUpdated = ws.on('calendar.updated', (e) => {
      const d = e.data as { event?: { calendar_id?: string } }
      if (d.event?.calendar_id === spaceId) void loadSpaceCalendar(spaceId)
    })
    const offCalDeleted = ws.on('calendar.deleted', (e) => {
      const d = e.data as { calendar_id?: string | null }
      if (d.calendar_id === spaceId) void loadSpaceCalendar(spaceId)
    })
    return () => {
      off4()
      offComment()
      offModerated()
      offCalCreated()
      offCalUpdated()
      offCalDeleted()
    }
  }, [spaceId])

  // A deep link picks the tab once the page reset to the feed above…
  useEffect(() => {
    if (linkedTab && linkedTab !== activeTab.value) loadTabData(linkedTab)
  }, [spaceId, linkedTab]) // eslint-disable-line react-hooks/exhaustive-deps
  // …and falls back to the feed if the space turns out not to have it
  // (feature off), or turns it off while it's open.
  const features = spaceDetail.value?.features
  useEffect(() => {
    if (!spaceDetail.value || activeTab.value === 'moderation') return
    if (!visibleSpaceTabs(features, true).includes(activeTab.value)) activeTab.value = 'feed'
  }, [features])

  const loadTabData = (tab: SpaceTab) => {
    activeTab.value = tab
    if (tab === 'pages') {
      api.get(`/api/spaces/${spaceId}/pages`).then((data: SpacePage[]) => {
        spacePages.value = data
      }).catch(() => { spacePages.value = [] })
    }
    // The events endpoint refuses while the calendar feature is off
    // (the tab then holds only the timetable).
    if (tab === 'calendar' && (spaceDetail.value?.features?.calendar ?? true)) {
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
      noLinkPreview?: boolean
    },
  ) => {
    const body: Record<string, unknown> = {
      type, content, media_url: mediaUrl ?? null,
      image_urls: extras?.imageUrls ?? [],
    }
    if (extras?.location) body.location = extras.location
    if (extras?.noLinkPreview) body.no_link_preview = true
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
    if (!await confirmDialog('Delete this post?', { destructive: true })) return
    await api.delete(`/api/spaces/${spaceId}/posts/${postId}`)
    showToast('Post deleted', 'info')
    void loadSpaceFeed(spaceId)
  }

  if (loading.value) return <Spinner />

  // §D1b — a stub of a remote-hosted space looks like a normal row
  // locally, but its moderation queue lives on the host (a MODERATED post
  // queues there), so a stub's queue is always empty until federated
  // moderation lands (follow-up). Show the Moderation tab on the host only.
  const isRemoteSpace = !!(
    spaceDetail.value?.owner_instance_id
    && instanceConfig.value?.instance_id
    && spaceDetail.value.owner_instance_id !== instanceConfig.value.instance_id
  )
  // Content authority (owner / admin / moderator, v_41): acting on other
  // people's posts, and — on the host — the moderation queue. No settings
  // power rides along with it; the server re-checks every action.
  const canModerate = roleCanModerate(viewerRole.value)
  const canModerateQueue = canModerate && !isRemoteSpace
  const s = spaceDetail.value

  // Per-space feature toggles hide their tab when off (``spaceTabs``).
  const f = s?.features
  const visibleTabs: readonly SpaceTab[] = visibleSpaceTabs(f, canModerateQueue)
  // Space timetables: owners / admins edit, everyone else reads. The
  // server's role check is the authority (a remote-hosted space's
  // admin edits too — the host verifies it); an archive is read-only.
  const canEditTimetable = !s?.archived && hasSettingsAuthority(viewerRole.value)
  // §4.3 per-feature access levels: may the viewer create / edit there?
  // ``adminOnly`` is the case worth a note — a member or moderator in an
  // ADMIN_ONLY feature (a subscriber reads everywhere anyway). The server
  // refuses the writes regardless (403 ACCESS_ADMIN_ONLY).
  const canWrite = (feature: AccessFeature) =>
    canContribute(accessLevel(f, feature), viewerRole.value)
  const adminOnly = (feature: AccessFeature) =>
    roleKnown.value && blockedByAdminOnly(accessLevel(f, feature), viewerRole.value)

  return (
    <div class="sh-space-feed sh-space-scope">
      <SpaceSubHeader
        name={s?.name ?? 'Space'}
        emoji={s?.emoji ?? null}
        iconUrl={s?.icon_url ?? null}
        memberCount={memberCount.value}
        activeTab={activeTab}
        visibleTabs={visibleTabs}
        tabLabels={{ calendar: calendarTabLabel(f) }}
        onSelectTab={loadTabData}
        actions={
          <>
            {s && viewerRole.value !== undefined && (
              <SpaceNotifPrefsMenu spaceId={spaceId} />
            )}
            {/* Every full member can open space settings — the page itself
             *  gates what's shown: a non-admin (member or moderator) sees
             *  only their own surface (Bots), a remote admin sees the
             *  forwarding-capable tabs, a local admin sees the full hub.
             *  Subscribers (read-only) don't. */}
            {isWriterRole(viewerRole.value) && (
              <a href={`/spaces/${spaceId}/settings`}
                 class="sh-space-settings-btn"
                 aria-label="Space settings">
                ⚙ Settings
              </a>
            )}
          </>
        }
      />
      {s && (
        <SpaceProposalsBanner
          spaceId={spaceId}
          canVote={hasSettingsAuthority(viewerRole.value)}
          isOwner={viewerRole.value === 'owner'}
        />
      )}
      {s && hasSettingsAuthority(viewerRole.value) && (
          <SpaceVersionBanner spaceId={spaceId} />
        )}
      {s && <SpaceLinksStrip spaceId={spaceId} />}

      {/* Branded header (Space → Settings → About + Theme). On the feed
       *  tab: the full hero (cover + avatar + name + members + About) when
       *  the admin set a cover, icon or About. On other tabs: a slim
       *  variant (short banner + avatar + name) when there's a visual brand
       *  (cover or icon), so the space stays branded without eating the
       *  vertical space a tool tab needs. */}
      {s && (() => {
        const isFeed = activeTab.value === 'feed'
        const branded = !!(s.cover_url || s.icon_url)
        const show = isFeed ? branded || !!s.about_markdown : branded
        if (!show) return null
        return (
          <SpaceHero
            name={s.name}
            emoji={s.emoji ?? null}
            coverUrl={s.cover_url ?? null}
            iconUrl={s.icon_url ?? null}
            about={s.about_markdown ?? null}
            memberCount={memberCount.value}
            slim={!isFeed}
          />
        )
      })()}

      {activeTab.value === 'feed' && (
        <div class="sh-feed sh-space-feed-content">
          {viewerRole.value === 'subscriber' ? (
            <div class="sh-subscriber-banner" role="status">
              <span class="sh-subscriber-banner__icon" aria-hidden="true">🔔</span>
              <div class="sh-subscriber-banner__body">
                <strong>You're following this space.</strong>
                <p class="sh-muted">
                  {(() => {
                    // Subscriber-engagement opt-ins (§23.49) — admins can
                    // open one or both paths.  Banner copy reflects what
                    // the viewer can actually do without contacting an
                    // admin.
                    const f = s?.features as {
                      allow_subscriber_react?: boolean
                      allow_subscriber_comment?: boolean
                    } | undefined
                    const canReact   = !!f?.allow_subscriber_react
                    const canComment = !!f?.allow_subscriber_comment
                    if (canReact && canComment) {
                      return 'You can react and comment, but posting is for full members. ' +
                             'Ask an admin if you want to start posts of your own.'
                    }
                    if (canReact) {
                      return 'You can leave reactions, but commenting and posting are for full members. ' +
                             'Ask an admin if you want to join the conversation.'
                    }
                    if (canComment) {
                      return 'You can leave comments, but reactions and posting are for full members. ' +
                             'Ask an admin if you want to react too.'
                    }
                    return 'You see new posts here but can\'t post, comment, or react. ' +
                           'Ask an admin to upgrade you to a full member if you want to join in.'
                  })()}
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
                    // ``addBase`` prepends the HA Supervisor ingress
                    // prefix (no-op for standalone) so the
                    // hard-navigate stays inside the SPA shell instead
                    // of bouncing the iframe to HA Core's frontend.
                    window.location.href = addBase('/spaces')
                  } catch (exc) {
                    showToast((exc as Error).message, 'error')
                  }
                }}
              >
                🔕 Unsubscribe
              </button>
            </div>
          ) : spaceDetail.value?.archived ? (
            <div class="sh-subscriber-banner" role="status">
              <span class="sh-subscriber-banner__icon" aria-hidden="true">🗄️</span>
              <div class="sh-subscriber-banner__body">
                <strong>
                  {archivedCopy(
                    spaceDetail.value?.archived,
                    spaceDetail.value?.archived_reason,
                  )?.title}
                </strong>
                <p class="sh-muted">
                  {archivedCopy(
                    spaceDetail.value?.archived,
                    spaceDetail.value?.archived_reason,
                  )?.body}
                </p>
              </div>
            </div>
          ) : adminOnly('posts') ? (
            <AccessNote feature="posts" />
          ) : (
            <Composer onSubmit={handleSubmit} context="Space" spaceId={spaceId}
              allowedTypes={spaceDetail.value?.features?.allowed_post_types}
              bazaarEnabled={spaceDetail.value?.features?.bazaar ?? true} />
          )}
          {posts.value.length === 0 && (
            <div class="sh-empty-state">
              <div aria-hidden="true">{spaceDetail.value?.archived ? '🗄️' : '💬'}</div>
              <h3>No posts in this space</h3>
              {spaceDetail.value?.archived ? (
                <p class="sh-muted">
                  {archivedCopy(
                    spaceDetail.value?.archived,
                    spaceDetail.value?.archived_reason,
                  )?.empty}
                </p>
              ) : adminOnly('posts') ? null : (
                <>
                  <p>
                    Be the first to share something with the rest of the space.
                    Members from connected households see what you post here.
                  </p>
                  <p class="sh-muted">
                    Use the composer above ↑ to start the conversation.
                  </p>
                </>
              )}
            </div>
          )}
          {posts.value.map(post => (
            <div key={post.id} class="sh-feed-item">
              <PostCard
                post={post}
                onReact={(emoji) => handleReact(post.id, emoji)}
                onComment={() => openCommentOverlay(post, spaceId)}
                // The author, or content authority (owner / admin /
                // moderator) acting on somebody else's post — unless the
                // space keeps posts to its admins (§4.3).
                onDelete={(post.author === currentUser.value?.user_id || canModerate)
                  && !adminOnly('posts')
                  ? () => handleDelete(post.id)
                  : undefined}
                spaceId={spaceId}
                surface="space"
              />
            </div>
          ))}
        </div>
      )}

      {activeTab.value === 'members' && (
        <>
          {hasSettingsAuthority(viewerRole.value) && (
            <JoinRequestList spaceId={spaceId} />
          )}
          <SpaceMemberList spaceId={spaceId} viewerRole={viewerRole.value} />
        </>
      )}

      {activeTab.value === 'pages' && (
        <div class="sh-space-pages">
          <h2>Pages</h2>
          {adminOnly('pages') && <AccessNote feature="pages" />}
          {spacePages.value.length === 0 && <p class="sh-muted">No pages in this space.</p>}
          {spacePages.value.map(p => (
            <div key={p.id} class="sh-page-card">
              <strong>{p.title}</strong>
              <time class="sh-muted">{new Date(p.updated_at).toLocaleString()}</time>
            </div>
          ))}
        </div>
      )}

      {activeTab.value === 'calendar' && visibleTabs.includes('calendar') && (
        <SpaceCalendarHost spaceId={spaceId} features={f} canEdit={canEditTimetable}
                           events={() => {
        // The visible range clamps the day expansion — the server's
        // range query is overlap-based, so an event that started before
        // the period comes back and would otherwise file an
        // out-of-range day card.
        const grouped = groupEventsByDay(
          spaceCalEvents.value,
          dateRangeForMode(spaceCalCursor.value, spaceCalView.value),
        )
        // Keys are ``YYYY-MM-DD`` (see ``groupEventsByDay``) so a plain
        // lexicographic sort is chronological — no locale-fragile
        // ``new Date(key)`` round-trip required.
        const dayKeys = Object.keys(grouped).sort()
        return (
          <div class="sh-calendar">
            <div class="sh-page-header sh-space-cal-header">
              <Button variant="secondary"
                      onClick={() => { subscribeFeedOpen.value = true }}>
                {t('event.subscribe.button')}
              </Button>
              {canWrite('calendar') && (
                <Button onClick={() => openSpaceEventDialog(spaceId)}>
                  + New event
                </Button>
              )}
            </div>
            {adminOnly('calendar') && <AccessNote feature="calendar" />}
            <Modal
              open={subscribeFeedOpen.value}
              onClose={() => { subscribeFeedOpen.value = false }}
              title={t('event.subscribe.heading')}
            >
              <SubscribeFeed spaceId={spaceId} />
            </Modal>

            <div class="sh-calendar-controls">
              <div class="sh-calendar-nav">
                <Button variant="secondary"
                        aria-label={`Previous ${spaceCalView.value}`}
                        onClick={() => navigateSpaceCalendar(-1, spaceId)}>
                  &#8249;
                </Button>
                <span class="sh-calendar-heading">
                  {formatRangeHeading(spaceCalCursor.value, spaceCalView.value)}
                </span>
                <Button variant="secondary"
                        aria-label={`Next ${spaceCalView.value}`}
                        onClick={() => navigateSpaceCalendar(1, spaceId)}>
                  &#8250;
                </Button>
                <Button variant="secondary"
                        onClick={() => jumpToSpaceToday(spaceId)}>
                  Today
                </Button>
              </div>
              <div class="sh-calendar-views" role="tablist">
                {(['month', 'week', 'day'] as CalendarViewMode[]).map(mode => (
                  <button
                    key={mode}
                    type="button"
                    role="tab"
                    aria-selected={spaceCalView.value === mode}
                    class={
                      spaceCalView.value === mode
                        ? 'sh-tab sh-tab--active'
                        : 'sh-tab'
                    }
                    onClick={() => setSpaceCalendarView(mode, spaceId)}
                  >
                    {mode.charAt(0).toUpperCase() + mode.slice(1)}
                  </button>
                ))}
              </div>
            </div>

            {spaceCalEvents.value.length === 0 && (
              <div class="sh-empty-state">
                <div aria-hidden="true">📅</div>
                <h3>No events in this {spaceCalView.value}</h3>
                {canWrite('calendar') && (
                  <p>
                    Click <strong>+ New event</strong> to schedule something
                    in this space.
                  </p>
                )}
              </div>
            )}

            {dayKeys.map(dayKey => {
              const friendly = formatDayLabel(dayKey)
              return (
              <div key={dayKey} class="sh-calendar-day-group">
                <h3
                  class={
                    friendly.isToday
                      ? 'sh-calendar-day-heading sh-calendar-day-heading--today'
                      : 'sh-calendar-day-heading'
                  }
                >
                  {friendly.long}
                  {friendly.relative && (
                    <span class="sh-calendar-day-heading__rel">{friendly.relative}</span>
                  )}
                </h3>
                {grouped[dayKey].map(entry => {
                  // ``e`` stays bound to the underlying event row;
                  // ``entry`` only carries this day card's place in the
                  // event's span.
                  const e = entry.event
                  const rowKey = `${dayKey}:${e.id}`
                  // Disclosure wiring — mirrors CalendarPage: the header
                  // is a native <button>, the detail panel its sibling, and
                  // the id stays a clean token (no ``:`` from ``rowKey``).
                  const isOpen = selectedSpaceEventId.value === rowKey
                  const detailId = `sh-event-detail-${dayKey}-${e.id}`
                  // One call, two labels — the helper builds two Dates
                  // and up to two Intl formatters per invocation.
                  const bounds = formatEventBounds(e)
                  return (
                  <div
                    key={rowKey}
                    class={
                      'sh-event'
                      + (entry.isFirst ? '' : ' sh-event--continued')
                      + (entry.isLast ? '' : ' sh-event--continues')
                    }
                  >
                    <button
                      type="button"
                      class="sh-event-header"
                      aria-expanded={isOpen}
                      aria-controls={detailId}
                      onClick={() => {
                        selectedSpaceEventId.value = isOpen ? null : rowKey
                      }}
                    >
                      <strong>{e.summary}</strong>
                      <EventRowMeta entry={entry} />
                    </button>
                    {isOpen && (
                      <div class="sh-event-detail" id={detailId}>
                        {e.description && <p>{e.description}</p>}
                        <div class="sh-event-times">
                          <span>{t('event.starts')} {bounds.starts}</span>
                          <span>{t('event.ends')} {bounds.ends}</span>
                        </div>
                      </div>
                    )}
                  </div>
                  )
                })}
              </div>
              )
            })}

            <CalendarEventDialog onCreated={() => loadTabData('calendar')} />
          </div>
        )
      }} />
      )}

      {activeTab.value === 'tasks' && (
        <SpaceTasksTab
          spaceId={spaceId}
          // Unknown until the member list answers: the tab waits rather
          // than flashing locked cards at a member.
          writable={roleKnown.value ? canWrite('tasks') : undefined}
          adminOnly={accessLevel(f, 'tasks') === 'admin_only'}
          archived={!!s?.archived}
        />
      )}

      {activeTab.value === 'stickies' && (
        <StickyBoardPage
          spaceId={spaceId}
          readOnly={adminOnly('stickies') ? accessNote('stickies') : null}
        />
      )}

      {activeTab.value === 'gallery' && (
        <GalleryPage spaceId={spaceId} />
      )}

      {activeTab.value === 'bazaar' && (
        <SpaceBazaarTab spaceId={spaceId} canSell={!adminOnly('posts')} />
      )}

      {activeTab.value === 'map' && s?.features?.location && (
        // Passing ``currentUserId`` lights up the "Share my location"
        // chip + button at the top of the map. Without it the share
        // surface stays hidden (the card otherwise renders the map
        // read-only for spectators).
        <SpaceLocationCard
          spaceId={spaceId}
          currentUserId={currentUser.value?.user_id}
        />
      )}

      {activeTab.value === 'moderation' && canModerateQueue && (
        <ModerationQueue spaceId={spaceId} canApprove={!adminOnly('posts')} />
      )}
    </div>
  )
}
