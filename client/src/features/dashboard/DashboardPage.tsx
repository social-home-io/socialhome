/**
 * DashboardPage — "My Corner" (§23).
 *
 * The corner reads as the corner-light :mod:`WelcomePage` plus more
 * rooms — same warm hero, same paper-card stripes, just with the
 * extra surfaces (Who's home, Bazaar, Spaces you follow, Quick
 * actions, Network map) appended below.  Card components and the
 * shared helpers live in :mod:`./../welcome/cards` so the two
 * pages can never visually drift.
 *
 * One round-trip to ``GET /api/me/corner`` populates every section.
 * Live WS events debounce-refetch the whole bundle (cheap because the
 * server does 1 SQL query per slice — ~10 ms total on a warm cache).
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { useTitle } from '@/store/pageTitle'
import { currentUser } from '@/store/auth'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { CardSkeleton } from '@/components/SkeletonScreen'
import { FollowedSpacesPicker } from '@/components/FollowedSpacesPicker'
import { LocationMap } from '@/components/LocationMap'
import NetworkMap from './NetworkMap'
import { installedApps, loadInstalled } from '@/store/apps'
import {
  AllClearCard,
  CatchUpCard,
  PendingCard,
  TodayCard,
  UpNextCard,
  dayShape,
  firstName,
  longDate,
  nextEvents,
  postSnippet,
  shortRelative,
  timeOfDayGreeting,
  todaysEvents,
  type WelcomeBundle,
  type WelcomeFollowedPost,
} from '../welcome/cards'
import { TodayScheduleCard } from '../welcome/TodayScheduleCard'
import { activeTimetables, lessonCount } from '../welcome/schedule'
import { addBase } from '@/baseUrl'

interface CornerPresence {
  user_id: string
  username: string
  display_name: string
  picture_url: string | null
  state: string
  zone_name: string | null
  latitude?: number | null
  longitude?: number | null
  gps_accuracy_m?: number | null
}

interface BazaarCornerSummary {
  active_listings: number
  pending_offers: number
  ending_soon: number
}

interface CornerBundle extends WelcomeBundle {
  presence: CornerPresence[]
  bazaar: BazaarCornerSummary
  followed_space_ids: string[]
}

const EMPTY: CornerBundle = {
  unread_notifications: 0,
  unread_conversations: 0,
  upcoming_events: [],
  presence: [],
  tasks_due_today: [],
  bazaar: { active_listings: 0, pending_offers: 0, ending_soon: 0 },
  followed_space_ids: [],
  followed_spaces_feed: [],
  today_timetable: [],
  today_events: [],
}

export default function DashboardPage() {
  useTitle('My Corner')
  const [bundle, setBundle] = useState<CornerBundle | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [pickerOpen, setPickerOpen] = useState(false)

  const refresh = async () => {
    try {
      const data = await api.get('/api/me/corner') as CornerBundle
      setBundle(data)
      setError(null)
    } catch (err: unknown) {
      setError((err as Error).message ?? 'Could not load corner.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadInstalled()
    void refresh()
    let timer: ReturnType<typeof setTimeout> | null = null
    const debouncedRefresh = () => {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => { void refresh() }, 200)
    }
    const relevant = [
      'notification.new', 'notification.unread_count',
      'dm.message',
      'calendar.created', 'calendar.updated',
      'calendar.deleted',
      'timetable.changed', 'timetable.deleted',
      'presence.updated',
      'task.created', 'task.updated', 'task.deleted',
      'task.completed', 'task.assigned',
      'bazaar.bid_placed', 'bazaar.listing_created',
      'bazaar.listing_closed', 'bazaar.offer_accepted',
    ]
    const offs = relevant.map(t => ws.on(t, debouncedRefresh))
    return () => {
      offs.forEach(off => off())
      if (timer) clearTimeout(timer)
    }
  }, [])

  if (loading && !bundle) {
    return (
      <div class="sh-welcome">
        <div class="sh-welcome-skeleton">
          <CardSkeleton />
          <CardSkeleton />
          <CardSkeleton />
        </div>
      </div>
    )
  }

  if (error && !bundle) {
    return (
      <div class="sh-welcome">
        <header class="sh-welcome-hero">
          <h1 class="sh-welcome-hero__greeting">My Corner</h1>
          <p class="sh-welcome-hero__sub">{error}</p>
        </header>
        <div class="sh-welcome-stack">
          <Button onClick={() => { setLoading(true); void refresh() }}>
            Retry
          </Button>
        </div>
      </div>
    )
  }

  const b = bundle ?? EMPTY
  // With a timetable in effect today the merged schedule card replaces
  // the Today card — it lists today's events (this morning's too).
  const schedule = activeTimetables(b.today_timetable ?? [])
  const hasSchedule = schedule.length > 0
  const today = hasSchedule ? (b.today_events ?? []) : todaysEvents(b.upcoming_events)
  const lessons = lessonCount(schedule)
  const upNext = today.length === 0 ? nextEvents(b.upcoming_events) : []
  const tasks = b.tasks_due_today
  const catchUp = b.followed_spaces_feed.slice(0, 3)
  // Corner has more surfaces than Welcome, so "all clear" applies
  // only when every section is empty (presence and bazaar included).
  const cornerAllClear =
    !hasSchedule
    && today.length === 0
    && upNext.length === 0
    && tasks.length === 0
    && catchUp.length === 0
    && b.presence.length === 0
    && b.bazaar.active_listings === 0
    && b.bazaar.pending_offers === 0
    && b.unread_notifications === 0
    && b.unread_conversations === 0

  const greetee = firstName(currentUser.value?.display_name)
  const heroGreeting = greetee
    ? `${timeOfDayGreeting()}, ${greetee}`
    : timeOfDayGreeting()
  const heroSub = cornerAllClear
    ? `${longDate(new Date())} · all clear`
    : `${longDate(new Date())} · ${dayShape(today, tasks, upNext, b, lessons)}`

  // Presence is "showing on the corner-only" — Welcome doesn't have
  // it, so the card lives here even on otherwise-empty days.
  const hasPresence = b.presence.length > 0
  const hasBazaar = b.bazaar.active_listings > 0 || b.bazaar.pending_offers > 0

  return (
    <div class="sh-welcome">
      <header class="sh-welcome-hero">
        <h1 class="sh-welcome-hero__greeting">{heroGreeting}</h1>
        <p class="sh-welcome-hero__sub">{heroSub}</p>
      </header>

      <div class="sh-welcome-stack">
        {/* ── Welcome-shared cards ───────────────────────────────── */}
        {hasSchedule
          ? <TodayScheduleCard timetables={schedule} events={today} />
          : today.length > 0 && <TodayCard events={today} />}
        {today.length === 0 && upNext.length > 0 && (
          <UpNextCard events={upNext} />
        )}
        {tasks.length > 0 && <PendingCard tasks={tasks} />}
        <CatchUpCard
          posts={catchUp}
          unreadNotifications={b.unread_notifications}
          unreadConversations={b.unread_conversations}
        />

        {/* ── Corner-only cards ──────────────────────────────────── */}
        {hasPresence && <PresenceCard presence={b.presence} />}
        {hasBazaar && <BazaarCard bazaar={b.bazaar} />}
        <SpacesCard
          posts={b.followed_spaces_feed}
          followedCount={b.followed_space_ids.length}
          onManage={() => setPickerOpen(true)}
        />
        <AppsCard />
        <QuickActionsCard />
        <NetworkCard />

        {/* "All clear" — only when literally nothing landed in any
         *  section.  Different from Welcome: presence + bazaar
         *  contribute, since they always have surfaces to show. */}
        {cornerAllClear && <AllClearCard />}
      </div>

      <FollowedSpacesPicker open={pickerOpen}
                            onClose={() => setPickerOpen(false)}
                            onChanged={() => { void refresh() }} />
    </div>
  )
}

// ─── Corner-only cards ───────────────────────────────────────────

function presenceDotClass(state: string): string {
  switch (state) {
    case 'home':     return 'sh-dot sh-dot--home'
    case 'away':     return 'sh-dot sh-dot--away'
    case 'zone':     return 'sh-dot sh-dot--home'
    case 'not_home': return 'sh-dot sh-dot--not-home'
    default:         return 'sh-dot sh-dot--unknown'
  }
}

/** "Who's home" — household member presence + the GPS map underneath
 *  for members who opt in.  Wraps in the same paper-card chrome the
 *  Welcome cards use so the corner reads as one stack. */
function PresenceCard({ presence }: { presence: CornerPresence[] }) {
  const withCoords = presence.filter(
    (p) => typeof p.latitude === 'number' && typeof p.longitude === 'number',
  )
  return (
    <a class="sh-welcome-card" href={addBase('/presence')}>
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">🏠</span> Who's home
      </h2>
      <div class="sh-presence-overview">
        {presence.map(p => (
          <div key={p.user_id} class="sh-presence-mini">
            <span class={presenceDotClass(p.state)} />
            <Avatar name={p.display_name} src={p.picture_url} size={28} />
            <span>{p.display_name}</span>
            <span class="sh-muted">{p.zone_name || p.state}</span>
          </div>
        ))}
      </div>
      {withCoords.length > 0 && (
        <LocationMap
          markers={withCoords.map((p) => ({
            id: p.user_id,
            lat: p.latitude as number,
            lon: p.longitude as number,
            accuracy_m: p.gps_accuracy_m ?? null,
            label: p.display_name,
            sub_label: p.zone_name,
            avatar_url: p.picture_url,
            state: p.state,
          }))}
          height={220}
          emptyLabel="No one is sharing GPS."
        />
      )}
      <span class="sh-welcome-card__more">Open presence →</span>
    </a>
  )
}

/** Bazaar summary — active listings, pending offers, ending-soon
 *  warning chip when applicable. */
function BazaarCard({ bazaar }: { bazaar: BazaarCornerSummary }) {
  return (
    <a class="sh-welcome-card" href={addBase('/bazaar')}>
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">🛍</span> Bazaar
      </h2>
      <div class="sh-corner-bazaar">
        <div class="sh-corner-bazaar-stat">
          <span class="sh-corner-bazaar-value">{bazaar.active_listings}</span>
          <span class="sh-muted">Active</span>
        </div>
        <div class="sh-corner-bazaar-stat">
          <span class="sh-corner-bazaar-value">{bazaar.pending_offers}</span>
          <span class="sh-muted">Offers to review</span>
        </div>
        {bazaar.ending_soon > 0 && (
          <div class="sh-corner-bazaar-stat sh-corner-bazaar-stat--warn">
            <span class="sh-corner-bazaar-value">{bazaar.ending_soon}</span>
            <span class="sh-muted">Ending &lt; 24h</span>
          </div>
        )}
      </div>
      <span class="sh-welcome-card__more">Open bazaar →</span>
    </a>
  )
}

/** Followed spaces — recent posts from spaces the user has pinned.
 *  Renders the empty-state CTA that opens the picker via the
 *  passed-in handler so the picker stays mounted at the page root. */
function SpacesCard({
  posts, followedCount, onManage,
}: {
  posts: WelcomeFollowedPost[]
  followedCount: number
  onManage: () => void
}) {
  return (
    <section class="sh-welcome-card sh-welcome-card--catchup">
      <div class="sh-welcome-card-header">
        <h2 class="sh-welcome-card__title">
          <span aria-hidden="true">🛰</span> Spaces you follow
        </h2>
        <button type="button" class="sh-link-button"
                onClick={onManage} aria-label="Manage followed spaces">
          Manage →
        </button>
      </div>
      {followedCount === 0 ? (
        <div class="sh-welcome-card-empty">
          <span class="sh-muted">
            Pin spaces here to keep an eye on their posts without
            opening each one.
          </span>
          <Button onClick={onManage}>Choose spaces</Button>
        </div>
      ) : posts.length === 0 ? (
        <div class="sh-welcome-card-empty">
          <span class="sh-muted">
            No new posts in the spaces you follow.
          </span>
        </div>
      ) : (
        <ul class="sh-welcome-card__list">
          {posts.map(p => (
            <li key={p.post_id}>
              <a class="sh-welcome-catchup-row" href={addBase(`/spaces/${p.space_id}`)}>
                <span class="sh-welcome-catchup-emoji" aria-hidden="true">
                  {p.space_emoji || '🪐'}
                </span>
                <span class="sh-welcome-catchup-body">
                  <span class="sh-welcome-catchup-meta">
                    <Avatar
                      name={p.author}
                      src={null}
                      size={18}
                    />
                    <strong>{p.author}</strong>
                    <span class="sh-muted">in {p.space_name}</span>
                  </span>
                  <span class="sh-welcome-catchup-snippet">
                    {postSnippet(p.content, p.type)}
                  </span>
                </span>
                <time class="sh-welcome-catchup-when sh-muted">
                  {shortRelative(p.created_at)}
                </time>
              </a>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/** Quick-actions card — same six links the previous corner shipped,
 *  rewrapped in the welcome-card chrome for visual coherence. */
function QuickActionsCard() {
  return (
    <section class="sh-welcome-card sh-welcome-card--quick">
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">⚡</span> Quick actions
      </h2>
      <div class="sh-quick-actions">
        <a href={addBase('/feed')} class="sh-btn sh-btn--secondary">Feed</a>
        <a href={addBase('/dms')} class="sh-btn sh-btn--secondary">Messages</a>
        <a href={addBase('/calendar')} class="sh-btn sh-btn--secondary">Calendar</a>
        <a href={addBase('/organize')} class="sh-btn sh-btn--secondary">Tasks</a>
        <a href={addBase('/organize?tab=shopping')} class="sh-btn sh-btn--secondary">Shopping</a>
        <a href={addBase('/bazaar')} class="sh-btn sh-btn--secondary">Bazaar</a>
      </div>
    </section>
  )
}

/** Network map card — the federation network visualisation already
 *  ships its own "Network · X paired" header line, so the welcome
 *  card here is just the paper-card frame; we don't duplicate the
 *  title at the top. */
function NetworkCard() {
  return (
    <section class="sh-welcome-card sh-welcome-card--network">
      <NetworkMap />
    </section>
  )
}

/** Apps snapshot card — up to 4 enabled installed apps with a link
 *  to the full Apps page. Hidden when no apps are installed so the
 *  corner doesn't show an empty card on fresh installs. */
function AppsCard() {
  const apps = installedApps.value.filter(a => a.enabled).slice(0, 4)
  if (apps.length === 0) return null
  return (
    <a class="sh-welcome-card" href={addBase('/apps')}>
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">📦</span> Apps
      </h2>
      <ul class="sh-welcome-card__list">
        {apps.map(app => (
          <li key={app.app_id} class="sh-apps-corner-row">
            {app.icon ? (
              <img src={app.icon} alt="" class="sh-apps-corner-icon" aria-hidden="true" />
            ) : (
              <span class="sh-apps-corner-icon-placeholder" aria-hidden="true">📦</span>
            )}
            <span>{app.name}</span>
            <span class="sh-muted sh-apps-corner-version">v{app.version}</span>
          </li>
        ))}
      </ul>
      <span class="sh-welcome-card__more">Open Apps →</span>
    </a>
  )
}
