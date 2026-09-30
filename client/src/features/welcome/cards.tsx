/**
 * Welcome cards — the paper-stripe card components shared between
 * :mod:`WelcomePage` (corner-light at ``/``) and :mod:`DashboardPage`
 * (full corner at ``/corner``).  Both surfaces share the same warm
 * "open the door" aesthetic — the dashboard is just the strict
 * superset, with extra sections for presence / bazaar / spaces /
 * network underneath.
 *
 * Only the small types + presentation helpers + render components
 * live here.  The page shells stay in their respective files and
 * own data fetching, hero copy, and section orchestration.
 */
import { Avatar } from '@/components/Avatar'
import { t } from '@/i18n/i18n'
import type { EffectiveLesson, TimetableColor } from '@/types'

// ─── Types — match the slice of ``GET /api/me/corner`` we render ───

export interface WelcomeEvent {
  id: string
  summary: string
  start: string
  end: string
  all_day: boolean
}

export interface WelcomeTask {
  id: string
  list_id: string
  title: string
  status: 'todo' | 'in_progress' | 'done'
  due_date: string | null
}

export interface WelcomeFollowedPost {
  post_id: string
  space_id: string
  space_name: string
  space_emoji: string | null
  author: string
  type: string
  content: string | null
  created_at: string
}

/** An effective lesson placed on the time line (UTC ISO instants). */
export interface TodayLesson extends EffectiveLesson {
  start_at: string
  end_at: string
}

/** One timetable's lessons for today (``today_timetable`` row). */
export interface TodayTimetable {
  timetable_id: string
  name: string
  color: TimetableColor | null
  tz: string
  date: string
  lessons: TodayLesson[]
}

export interface WelcomeBundle {
  unread_notifications: number
  unread_conversations: number
  upcoming_events: WelcomeEvent[]
  tasks_due_today: WelcomeTask[]
  followed_spaces_feed: WelcomeFollowedPost[]
  /** Today's lessons of the caller's timetables in effect today. */
  today_timetable: TodayTimetable[]
  /** Every event overlapping today — including this morning's. */
  today_events: WelcomeEvent[]
}

// ─── Time / formatting helpers ─────────────────────────────────────

/** Pick a time-of-day-aware greeting.  Uses device-local hour because
 *  the welcome line is anchored to "what the user is doing now", not
 *  to server UTC. */
export function timeOfDayGreeting(): string {
  const h = new Date().getHours()
  if (h < 5)  return 'Good night'
  if (h < 12) return 'Good morning'
  if (h < 17) return 'Good afternoon'
  if (h < 22) return 'Good evening'
  return 'Good night'
}

/** "Pascal Vizeli" → "Pascal".  Single-word names pass through. */
export function firstName(displayName: string | undefined | null): string {
  if (!displayName) return ''
  const trimmed = displayName.trim()
  const sp = trimmed.indexOf(' ')
  return sp === -1 ? trimmed : trimmed.slice(0, sp)
}

/** Long-form date — "Friday, May 8".  No year (shouting "2026" at
 *  the user every morning isn't warm). */
export function longDate(d: Date): string {
  return d.toLocaleDateString(undefined, {
    weekday: 'long', month: 'long', day: 'numeric',
  })
}

/** "08:30" — local time, 24h-aware via the locale.  Returns "" for
 *  all-day events; the caller handles the all-day formatting. */
export function eventTime(e: WelcomeEvent): string {
  if (e.all_day) return ''
  return new Date(e.start).toLocaleTimeString(undefined, {
    hour: '2-digit', minute: '2-digit',
  })
}

/** Filter the corner's ``upcoming_events`` down to events that start
 *  on the local-today date.  All-day events for today are kept. */
export function todaysEvents(events: WelcomeEvent[]): WelcomeEvent[] {
  const todayKey = new Date().toDateString()
  return events.filter(e => new Date(e.start).toDateString() === todayKey)
}

/** When today is empty, fall back to the next 1-2 events within the
 *  upcoming window so the welcome surface still answers "what's
 *  next?".  The corner endpoint already filters to upcoming-only,
 *  so the input here is naturally future-only. */
export function nextEvents(events: WelcomeEvent[]): WelcomeEvent[] {
  return events.slice(0, 2)
}

/** "Tomorrow" / "Mon" / "May 12" — date label for non-today rows. */
export function dayLabel(iso: string): string {
  const d = new Date(iso)
  const today = new Date()
  const tomorrow = new Date(today)
  tomorrow.setDate(today.getDate() + 1)
  if (d.toDateString() === tomorrow.toDateString()) return 'Tomorrow'
  const diffDays = Math.floor(
    (d.setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 86_400_000,
  )
  if (diffDays > 0 && diffDays < 7) {
    return new Date(iso).toLocaleDateString(undefined, { weekday: 'long' })
  }
  return new Date(iso).toLocaleDateString(undefined, {
    month: 'short', day: 'numeric',
  })
}

/** "Overdue · 2d" / "Today" / "" — short, glanceable due chip. */
export function taskDueLabel(
  iso: string | null,
): { text: string; tone: 'overdue' | 'today' | 'normal' } {
  if (!iso) return { text: '', tone: 'normal' }
  const due = new Date(`${iso}T00:00:00`)
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const days = Math.floor((due.getTime() - today.getTime()) / 86_400_000)
  if (days < 0)  return { text: `Overdue · ${-days}d`, tone: 'overdue' }
  if (days === 0) return { text: 'Today', tone: 'today' }
  if (days === 1) return { text: 'Tomorrow', tone: 'normal' }
  return { text: `In ${days}d`, tone: 'normal' }
}

/** "5m" / "2h" / "Mon" — relative-short for catch-up rows. */
export function shortRelative(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60_000)
  if (mins < 1)   return 'now'
  if (mins < 60)  return `${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  if (days < 7)   return `${days}d`
  return new Date(iso).toLocaleDateString(undefined, { weekday: 'short' })
}

/** Replace empty post bodies with a typed placeholder ("📷 Image").
 *  Keeps catch-up rows readable when the feed entry is media-only. */
export function postSnippet(content: string | null, type: string): string {
  if (!content || !content.trim()) {
    switch (type) {
      case 'image':    return '📷 Image'
      case 'video':    return '🎬 Video'
      case 'file':     return '📄 File'
      case 'poll':     return '📊 Poll'
      case 'schedule': return '📅 Schedule'
      case 'bazaar':   return '🛍 Listing'
      case 'location': return '📍 Location'
      default:         return ''
    }
  }
  const flat = content.replace(/\s+/g, ' ').trim()
  return flat.length > 90 ? `${flat.slice(0, 90)}…` : flat
}

/** Compose the hero sub-line — "2 events · 3 tasks" style.  Punchy
 *  enough that the user can decide in 1s whether they need to dig
 *  in.  Cascades through several signals so the line is always
 *  specific.  Used by both Welcome + Corner heros. */
export function dayShape(
  events: WelcomeEvent[],
  tasks: WelcomeTask[],
  upNext: WelcomeEvent[],
  b: Pick<WelcomeBundle, 'unread_notifications' | 'unread_conversations'>,
  lessons = 0,
): string {
  const parts: string[] = []
  if (lessons > 0) {
    parts.push(t(lessons === 1 ? 'welcome.schedule.shape_one' : 'welcome.schedule.shape', {
      n: String(lessons),
    }))
  }
  if (events.length > 0) {
    parts.push(events.length === 1 ? '1 event' : `${events.length} events`)
  }
  if (tasks.length > 0) {
    parts.push(tasks.length === 1 ? '1 task' : `${tasks.length} tasks`)
  }
  if (parts.length > 0) return parts.join(' · ')

  if (upNext.length > 0) {
    const next = upNext[0]
    return `nothing today · next up ${dayLabel(next.start).toLowerCase()}`
  }

  const inboxParts: string[] = []
  if (b.unread_conversations > 0) {
    inboxParts.push(b.unread_conversations === 1
      ? '1 message'
      : `${b.unread_conversations} messages`)
  }
  if (b.unread_notifications > 0) {
    inboxParts.push(b.unread_notifications === 1
      ? '1 alert'
      : `${b.unread_notifications} alerts`)
  }
  if (inboxParts.length > 0) return `${inboxParts.join(' · ')} to read`

  return 'a few things waiting'
}

// ─── Cards ─────────────────────────────────────────────────────────

export function TodayCard({ events }: { events: WelcomeEvent[] }) {
  return (
    <a class="sh-welcome-card" href="/calendar">
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">📅</span> Today
      </h2>
      <ul class="sh-welcome-card__list">
        {events.map(e => (
          <li key={e.id} class="sh-welcome-card__row">
            <time class="sh-welcome-card__time">
              {e.all_day ? 'All day' : eventTime(e)}
            </time>
            <span class="sh-welcome-card__line">{e.summary}</span>
          </li>
        ))}
      </ul>
      <span class="sh-welcome-card__more">Open calendar →</span>
    </a>
  )
}

export function UpNextCard({ events }: { events: WelcomeEvent[] }) {
  return (
    <a class="sh-welcome-card" href="/calendar">
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">🗓</span> Up next
      </h2>
      <ul class="sh-welcome-card__list">
        {events.map(e => (
          <li key={e.id} class="sh-welcome-card__row">
            <time class="sh-welcome-card__time sh-welcome-card__time--day">
              {dayLabel(e.start)}
            </time>
            <span class="sh-welcome-card__line">
              {e.summary}
              {!e.all_day && (
                <span class="sh-welcome-card__when sh-muted">
                  {' · '}{eventTime(e)}
                </span>
              )}
            </span>
          </li>
        ))}
      </ul>
      <span class="sh-welcome-card__more">Open calendar →</span>
    </a>
  )
}

export function PendingCard({ tasks }: { tasks: WelcomeTask[] }) {
  const visible = tasks.slice(0, 5)
  const overflow = tasks.length - visible.length
  return (
    <a class="sh-welcome-card" href="/organize">
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">✅</span> Pending
      </h2>
      <ul class="sh-welcome-card__list">
        {visible.map(t => {
          const due = taskDueLabel(t.due_date)
          return (
            <li key={t.id} class="sh-welcome-card__row">
              <span class="sh-welcome-card__bullet" aria-hidden="true">⬜</span>
              <span class="sh-welcome-card__line">{t.title}</span>
              {due.text && (
                <span class={`sh-welcome-card__chip sh-welcome-card__chip--${due.tone}`}>
                  {due.text}
                </span>
              )}
            </li>
          )
        })}
        {overflow > 0 && (
          <li class="sh-welcome-card__row sh-welcome-card__row--more">
            +{overflow} more
          </li>
        )}
      </ul>
      <span class="sh-welcome-card__more">Open tasks →</span>
    </a>
  )
}

export function CatchUpCard({
  posts, unreadNotifications, unreadConversations,
}: {
  posts: WelcomeFollowedPost[]
  unreadNotifications: number
  unreadConversations: number
}) {
  const hasAnything = posts.length > 0
    || unreadNotifications > 0
    || unreadConversations > 0
  if (!hasAnything) return null
  return (
    <section class="sh-welcome-card sh-welcome-card--catchup">
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">✨</span> Catch up
      </h2>
      <div class="sh-welcome-chips">
        {unreadConversations > 0 && (
          <a class="sh-welcome-chip" href="/dms">
            <span aria-hidden="true">💬</span>
            <strong>{unreadConversations}</strong>
            <span>{unreadConversations === 1 ? 'message' : 'messages'}</span>
          </a>
        )}
        {unreadNotifications > 0 && (
          <a class="sh-welcome-chip" href="/notifications">
            <span aria-hidden="true">🔔</span>
            <strong>{unreadNotifications}</strong>
            <span>{unreadNotifications === 1 ? 'alert' : 'alerts'}</span>
          </a>
        )}
      </div>
      {posts.length > 0 && (
        <ul class="sh-welcome-card__list">
          {posts.map(p => (
            <li key={p.post_id}>
              <a class="sh-welcome-catchup-row" href={`/spaces/${p.space_id}`}>
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

export function AllClearCard() {
  return (
    <div class="sh-welcome-allclear">
      <span class="sh-welcome-allclear__sun" aria-hidden="true">☀️</span>
      <h2 class="sh-welcome-allclear__title">All clear</h2>
      <p class="sh-welcome-allclear__sub sh-muted">
        Nothing on your plate today. Enjoy the quiet.
      </p>
    </div>
  )
}
