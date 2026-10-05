import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useRoute } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { ws } from '@/ws'
import { currentUser } from '@/store/auth'
import { loadHouseholdUsers } from '@/store/householdUsers'
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
import { SpaceReports } from '@/components/SpaceReports'
import { openReport } from '@/components/ReportDialog'
import { SpaceLocationCard } from '@/components/SpaceLocationCard'
import { SpaceMemberList } from '@/components/SpaceMemberList'
import GalleryPage from '@/features/gallery/GalleryPage'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { SubscribeFeed } from '@/components/SubscribeFeed'
import { PostCard } from '@/components/PostCard'
import { Composer, type ComposerExtras } from '@/components/Composer'
import { announceQueued, contentWrite, isQueuedWrite } from '@/utils/contentWrite'
import { pendingDeletes, undoableDelete } from '@/utils/undoableDelete'
import { PendingReviewStrip } from '@/components/PendingReviewStrip'
import { useModerationMine } from '@/store/moderationMine'
import { itemPreview } from './moderationItems'
import { openCommentOverlay } from '@/components/CommentOverlay'
import { SpaceSubHeader, type SpaceTab } from '@/components/SpaceSubHeader'
import { SpaceTasksTab } from './SpaceTasksTab'
import { SpacePagesTab } from './SpacePagesTab'
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
  /** Another household has a member here. */
  has_remote_households?: boolean
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
      title: t('space.archived.dissolved_title'),
      body: t('space.archived.dissolved_body'),
      empty: t('space.archived.dissolved_empty'),
    }
  }
  if (reason === 'removed') {
    return {
      title: t('space.archived.removed_title'),
      body: t('space.archived.removed_body'),
      empty: t('space.archived.removed_empty'),
    }
  }
  // Normal, reversible admin archive.
  return {
    title: t('space.archived.title'),
    body: t('space.archived.body'),
    empty: t('space.archived.empty'),
  }
}

const posts = signal<FeedPost[]>([])
const loading = signal(true)
const activeTab = signal<SpaceTab>('feed')
/** Calendar tab → "Subscribe" dialog (private iCal link). */
const subscribeFeedOpen = signal(false)
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

/** A stored event id from an agenda row's id — the range query expands a
 *  recurring event into ``"{id}@{occurrence}"`` rows (see
 *  ``isOccurrenceId``); edits and deletes act on the stored series. */
export function seriesEventId(id: string): string {
  const at = id.indexOf('@')
  return at === -1 ? id : id.slice(0, at)
}

/** Delete a space event with Undo (``undoableDelete``): every row of it
 *  disappears at once, the DELETE goes out when the toast closes. Held
 *  for review (somebody else's event in a "Reviewed" calendar, §4.3): the
 *  event comes back and the toast says it waits for a moderator. A
 *  recurring event goes as a whole series — the toast says so. */
export function deleteSpaceEvent(spaceId: string, ev: CalendarEvent) {
  const id = seriesEventId(ev.id)
  selectedSpaceEventId.value = null
  undoableDelete({
    ids: [id],
    message: ev.rrule
      ? t('event.deleted_series', { title: ev.summary })
      : t('event.deleted_named', { title: ev.summary }),
    commit: async ({ keepalive }) => {
      const path = `/api/spaces/${spaceId}/calendar/events/${id}`
      try {
        const res = await (keepalive
          ? api.delete<unknown>(path, { keepalive: true })
          : api.delete<unknown>(path))
        if (isQueuedWrite(res)) announceQueued({ spaceId })
      } catch (err) {
        if ((err as { status?: unknown })?.status !== 404) throw err
      }
      // The Undo window may close after the viewer moved to another space:
      // the calendar signal is that space's now — don't refill it with ours.
      if (spaceDetail.value?.id === spaceId) void loadSpaceCalendar(spaceId)
    },
  })
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
  // ``?tab=moderation`` (the review / report notifications) waits for the
  // viewer's role: only content authority has the tab.
  const wantsModeration = query?.tab === 'moderation'

  // Apply the space's custom theme (§23 customization). The hook
  // fetches /api/spaces/{id}/theme, sets CSS vars, and cleans up on
  // unmount so household colours return as the user leaves.
  useSpaceTheme(spaceId)
  // Live header / tabs / role on a config change elsewhere; a dissolve
  // leaves for the spaces list with a toast.
  useSpaceConfigWs(spaceId, () => { void loadSpaceHeader(spaceId) })
  // The viewer's own items waiting for review (§4.3) — the
  // "Pending review (n)" strip on each tab.
  useModerationMine(spaceId)
  // Surface the space's name in the global TopBar (matches the
  // household feed pattern). Falls back to "Space" while the detail
  // request is in flight.
  const detail = spaceDetail.value
  useTitle(
    detail
      ? (detail.emoji ? `${detail.emoji} ${detail.name}` : detail.name)
      : t('spaces.space_title'),
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
  useEffect(() => {
    if (wantsModeration && roleKnown.value && roleCanModerate(viewerRole.value)) {
      activeTab.value = 'moderation'
    }
  }, [spaceId, wantsModeration, roleKnown.value, viewerRole.value])
  // …and falls back to the feed if the space turns out not to have it
  // (feature off), or turns it off while it's open.
  const features = spaceDetail.value?.features
  useEffect(() => {
    if (!spaceDetail.value || activeTab.value === 'moderation') return
    if (!visibleSpaceTabs(features, true).includes(activeTab.value)) activeTab.value = 'feed'
  }, [features])

  const loadTabData = (tab: SpaceTab) => {
    activeTab.value = tab
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
    extras?: ComposerExtras,
  ) => {
    const body: Record<string, unknown> = {
      type, content, media_url: mediaUrl ?? null,
      image_urls: extras?.imageUrls ?? [],
    }
    if (extras?.location) body.location = extras.location
    if (extras?.noLinkPreview) body.no_link_preview = true
    // Poll / schedule ride the create request (atomic with the post, so
    // a post held for review carries them).
    if (extras?.poll) {
      body.poll = {
        question: extras.poll.question,
        options: extras.poll.options,
        allow_multiple: extras.poll.allow_multiple,
        closes_at: extras.poll.closes_at,
      }
    }
    if (extras?.schedule) {
      body.schedule = { title: extras.schedule.title, slots: extras.schedule.slots }
    }
    const res = await contentWrite<{ id: string }>(
      api.post(`/api/spaces/${spaceId}/posts`, body),
      { spaceId },
    )
    // Held for review (202): nothing is in the feed yet.
    if (res.queued) return undefined
    showToast(t('feed.post_shared'), 'success')
    await loadSpaceFeed(spaceId)
    return res.data?.id
  }

  const handleReact = async (postId: string, emoji: string) => {
    await api.post(
      `/api/spaces/${spaceId}/posts/${postId}/reactions`, { emoji },
    )
    void loadSpaceFeed(spaceId)
  }

  const handleDelete = async (postId: string) => {
    if (!await confirmDialog(t('space.feed.delete_confirm'), { destructive: true })) return
    await api.delete(`/api/spaces/${spaceId}/posts/${postId}`)
    showToast(t('space.feed.deleted'), 'info')
    void loadSpaceFeed(spaceId)
  }

  /** Inline edit of a post's text: ``true`` closes the editor (saved,
   *  or held for review), ``false`` keeps it (toasted). A 403 means the
   *  viewer's seat or the space's access level changed — refetch both. */
  const handleEdit = async (postId: string, content: string): Promise<boolean> => {
    try {
      const res = await contentWrite<{ id: string; content: string; edited_at: string | null }>(
        api.patch(`/api/spaces/${spaceId}/posts/${postId}`, { content }),
        { spaceId },
      )
      if (res.queued) return true
      posts.value = posts.value.map(p => p.id === postId
        ? { ...p, content: res.data.content, edited_at: res.data.edited_at }
        : p)
      showToast(t('post.edit.saved'), 'success')
      // The link preview may have been dropped by the edit.
      void loadSpaceFeed(spaceId)
      return true
    } catch (err: unknown) {
      showToast(t('post.edit.failed', { error: String((err as Error)?.message ?? err) }), 'error')
      if ((err as { status?: unknown })?.status === 403) void loadSpaceHeader(spaceId)
      return false
    }
  }

  if (loading.value) return <Spinner />

  // Content authority (owner / admin / moderator, v_41): acting on other
  // people's posts, and the moderation queue — on the host and on every
  // household holding a moderator / admin seat alike, since items are
  // sent to each of them (federated moderation, v_43). No settings power
  // rides along with it; the server re-checks every action.
  const canModerate = roleCanModerate(viewerRole.value)
  const canModerateQueue = canModerate
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
  // Why the sticky board is read-only (no add / edit / move, and each
  // note by someone else carries its own Report): archived, kept to the
  // admins (§4.3), or a viewer who can't write (a follower). ``null`` →
  // writable.
  const stickiesReadOnly = (): string | null => {
    if (s?.archived) return t('stickies.read_only_archived')
    if (adminOnly('stickies')) return accessNote('stickies')
    if (roleKnown.value && !isWriterRole(viewerRole.value)) return t('stickies.read_only_viewer')
    return null
  }
  // Edit / Delete on a post: the author, or content authority (owner /
  // admin / moderator) on somebody else's — once the member list said
  // who the viewer is, never for a read-only subscriber, and not where
  // the space keeps posts to its admins (§4.3).
  const canActOnPost = (author: string) =>
    roleKnown.value && isWriterRole(viewerRole.value)
    && (author === currentUser.value?.user_id || canModerate)
    && !adminOnly('posts')

  return (
    <div class="sh-space-feed sh-space-scope">
      <SpaceSubHeader
        name={s?.name ?? t('spaces.space_title')}
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
              <a href={addBase(`/spaces/${spaceId}/settings`)}
                 class="sh-space-settings-btn"
                 aria-label={t('space.header.settings_aria')}>
                ⚙ {t('nav.settings')}
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
                <strong>{t('space.subscriber.title')}</strong>
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
                      return t('space.subscriber.react_comment')
                    }
                    if (canReact) {
                      return t('space.subscriber.react')
                    }
                    if (canComment) {
                      return t('space.subscriber.comment')
                    }
                    return t('space.subscriber.read_only')
                  })()}
                </p>
              </div>
              <button
                type="button"
                class="sh-subscribe-btn sh-subscribe-btn--on"
                aria-label={t('space.subscriber.unsubscribe_aria')}
                title={t('space.subscriber.unsubscribe_title')}
                onClick={async () => {
                  try {
                    await api.delete(`/api/spaces/${spaceId}/subscribe`)
                    showToast(t('space.subscriber.unsubscribed'), 'info')
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
                🔕 {t('space.subscriber.unsubscribe')}
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
          <PendingReviewStrip spaceId={spaceId} feature="posts" />
          {posts.value.length === 0 && (
            <div class="sh-empty-state">
              <div aria-hidden="true">{spaceDetail.value?.archived ? '🗄️' : '💬'}</div>
              <h3>{t('spaces.no_posts')}</h3>
              {spaceDetail.value?.archived ? (
                <p class="sh-muted">
                  {archivedCopy(
                    spaceDetail.value?.archived,
                    spaceDetail.value?.archived_reason,
                  )?.empty}
                </p>
              ) : adminOnly('posts') ? null : (
                <>
                  <p>{t('space.feed.empty_body')}</p>
                  <p class="sh-muted">{t('space.feed.empty_hint')}</p>
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
                onDelete={canActOnPost(post.author)
                  ? () => handleDelete(post.id)
                  : undefined}
                // Same rule for editing the text, and never in an archive.
                onEdit={canActOnPost(post.author) && !s?.archived
                  ? (content) => handleEdit(post.id, content)
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
        <SpacePagesTab
          spaceId={spaceId}
          role={viewerRole.value}
          level={accessLevel(f, 'pages')}
          // Read-only until the member list answers (no flash of edit
          // controls a subscriber can't use).
          writable={roleKnown.value && canWrite('pages')}
          adminOnly={adminOnly('pages')}
          archived={!!s?.archived}
          hostInstanceId={s?.owner_instance_id ?? null}
        />
      )}

      {activeTab.value === 'calendar' && visibleTabs.includes('calendar') && (
        <SpaceCalendarHost spaceId={spaceId} features={f} canEdit={canEditTimetable}
                           events={() => {
        // The visible range clamps the day expansion — the server's
        // range query is overlap-based, so an event that started before
        // the period comes back and would otherwise file an
        // out-of-range day card.
        // Rows waiting out their Undo window stay hidden.
        const hidden = pendingDeletes.value
        const shown = spaceCalEvents.value.filter(e => !hidden.has(seriesEventId(e.id)))
        const grouped = groupEventsByDay(
          shown,
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
                  + {t('calendar.new_event')}
                </Button>
              )}
            </div>
            {adminOnly('calendar') && <AccessNote feature="calendar" />}
            <PendingReviewStrip spaceId={spaceId} feature="calendar" />
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
                        // Keys: calendar.prev_{month,week,day}
                        aria-label={t(`calendar.prev_${spaceCalView.value}`)}
                        onClick={() => navigateSpaceCalendar(-1, spaceId)}>
                  &#8249;
                </Button>
                <span class="sh-calendar-heading">
                  {formatRangeHeading(spaceCalCursor.value, spaceCalView.value)}
                </span>
                <Button variant="secondary"
                        // Keys: calendar.next_{month,week,day}
                        aria-label={t(`calendar.next_${spaceCalView.value}`)}
                        onClick={() => navigateSpaceCalendar(1, spaceId)}>
                  &#8250;
                </Button>
                <Button variant="secondary"
                        onClick={() => jumpToSpaceToday(spaceId)}>
                  {t('calendar.today')}
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
                    {t(`calendar.${mode}`)}
                  </button>
                ))}
              </div>
            </div>

            {shown.length === 0 && (
              <div class="sh-empty-state">
                <div aria-hidden="true">📅</div>
                {/* Keys: calendar.empty_{month,week,day} */}
                <h3>{t(`calendar.empty_${spaceCalView.value}`)}</h3>
                {canWrite('calendar') && (
                  <p>{t('calendar.empty_space_hint')}</p>
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
                        {/* ``can_edit``: the calendar's access level lets
                         *  the viewer change this event (§4.3) — absent
                         *  from an older host: no control offered. */}
                        {((e.can_edit === true && !s?.archived)
                          || e.created_by !== currentUser.value?.user_id) && (
                          <div class="sh-event-admin sh-row">
                            {e.can_edit === true && !s?.archived && (
                              <Button variant="danger"
                                      onClick={() => deleteSpaceEvent(spaceId, e)}>
                                {t('event.delete')}
                              </Button>
                            )}
                            {e.created_by !== currentUser.value?.user_id && (
                              <Button variant="ghost"
                                      onClick={() => openReport('calendar_event', e.id, spaceId)}>
                                {t('report.action')}
                              </Button>
                            )}
                          </div>
                        )}
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
        <PendingReviewStrip spaceId={spaceId} feature="tasks" />
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
        <PendingReviewStrip spaceId={spaceId} feature="stickies" />
      )}
      {activeTab.value === 'stickies' && (
        <StickyBoardPage
          spaceId={spaceId}
          readOnly={stickiesReadOnly()}
        />
      )}

      {activeTab.value === 'gallery' && (
        <GalleryPage spaceId={spaceId} />
      )}

      {activeTab.value === 'bazaar' && (
        <PendingReviewStrip spaceId={spaceId} feature="posts"
                            filter={i => !!itemPreview(i).bazaar} />
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
        // Approving creates the item as the approver: a moderator can't
        // where the feature is kept to admins (§4.3) — rejecting stays open.
        <ModerationQueue
          spaceId={spaceId}
          canApprove={(feature) => !adminOnly(feature as AccessFeature)}
        />
      )}
      {activeTab.value === 'moderation' && canModerateQueue && (
        <SpaceReports spaceId={spaceId} onOpenTab={loadTabData} />
      )}
    </div>
  )
}
