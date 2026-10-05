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
import { isOne, locale, t } from '@/i18n/i18n'
import type { EffectiveLesson, TimetableColor } from '@/types'
import { addBase } from '@/baseUrl'

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

/** Pick a time-of-day-aware greeting, with the first name when there
 *  is one ("Good morning, Pascal").  Uses device-local hour because
 *  the welcome line is anchored to "what the user is doing now", not
 *  to server UTC. */
export function timeOfDayGreeting(name = ''): string {
  const h = new Date().getHours()
  const part = h < 5 ? 'night'
    : h < 12 ? 'morning'
      : h < 17 ? 'afternoon'
        : h < 22 ? 'evening'
          : 'night'
  // Keys: welcome.greet.{night,morning,afternoon,evening}[_named]
  return name
    ? t(`welcome.greet.${part}_named`, { name })
    : t(`welcome.greet.${part}`)
}

/** "Pascal Vizeli" → "Pascal".  Single-word names pass through. */
export function firstName(displayName: string | undefined | null): string {
  if (!displayName) return ''
  const trimmed = displayName.trim()
  const sp = trimmed.indexOf(' ')
  return sp === -1 ? trimmed : trimmed.slice(0, sp)
}

/** The UI language for ``Intl`` formatters (browser default when unset). */
function lang(): string | undefined {
  return locale.value || undefined
}

/** Long-form date in the UI language — "Friday, May 8" /
 *  "Freitag, 8. Mai".  No year (shouting "2026" at the user every
 *  morning isn't warm). */
export function longDate(d: Date): string {
  try {
    return new Intl.DateTimeFormat(lang(), {
      weekday: 'long', month: 'long', day: 'numeric',
    }).format(d)
  } catch {
    return d.toDateString()
  }
}

/** "08:30" — local time, 24h-aware via the locale.  Returns "" for
 *  all-day events; the caller handles the all-day formatting. */
export function eventTime(e: WelcomeEvent): string {
  if (e.all_day) return ''
  return new Date(e.start).toLocaleTimeString(lang(), {
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
  if (d.toDateString() === tomorrow.toDateString()) return t('welcome.tomorrow')
  const diffDays = Math.floor(
    (d.setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 86_400_000,
  )
  if (diffDays > 0 && diffDays < 7) {
    return new Date(iso).toLocaleDateString(lang(), { weekday: 'long' })
  }
  return new Date(iso).toLocaleDateString(lang(), {
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
  if (days < 0)  return { text: t('welcome.due.overdue', { n: String(-days) }), tone: 'overdue' }
  if (days === 0) return { text: t('welcome.due.today'), tone: 'today' }
  if (days === 1) return { text: t('welcome.tomorrow'), tone: 'normal' }
  return { text: t('welcome.due.in_days', { n: String(days) }), tone: 'normal' }
}

/** "5m" / "2h" / "Mon" — relative-short for catch-up rows. */
export function shortRelative(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60_000)
  if (mins < 1)   return t('welcome.ago.now')
  if (mins < 60)  return t('welcome.ago.minutes', { n: String(mins) })
  const hours = Math.floor(mins / 60)
  if (hours < 24) return t('welcome.ago.hours', { n: String(hours) })
  const days = Math.floor(hours / 24)
  if (days < 7)   return t('welcome.ago.days', { n: String(days) })
  return new Date(iso).toLocaleDateString(lang(), { weekday: 'short' })
}

/** Replace empty post bodies with a typed placeholder ("📷 Image").
 *  Keeps catch-up rows readable when the feed entry is media-only. */
export function postSnippet(content: string | null, type: string): string {
  if (!content || !content.trim()) {
    switch (type) {
      case 'image':    return `📷 ${t('welcome.snippet.image')}`
      case 'video':    return `🎬 ${t('welcome.snippet.video')}`
      case 'file':     return `📄 ${t('welcome.snippet.file')}`
      case 'poll':     return `📊 ${t('welcome.snippet.poll')}`
      case 'schedule': return `📅 ${t('welcome.snippet.schedule')}`
      case 'bazaar':   return `🛍 ${t('welcome.snippet.bazaar')}`
      case 'location': return `📍 ${t('welcome.snippet.location')}`
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
  const count = (key: string, n: number) =>
    t(isOne(n) ? `${key}_one` : key, { n: String(n) })
  // Keys: welcome.shape.{events,tasks,messages,alerts}[_one]
  if (events.length > 0) parts.push(count('welcome.shape.events', events.length))
  if (tasks.length > 0) parts.push(count('welcome.shape.tasks', tasks.length))
  if (parts.length > 0) return parts.join(' · ')

  if (upNext.length > 0) {
    const label = dayLabel(upNext[0].start)
    return label === t('welcome.tomorrow')
      ? t('welcome.shape.next_tomorrow')
      : t('welcome.shape.next_day', { day: label })
  }

  const inboxParts: string[] = []
  if (b.unread_conversations > 0) {
    inboxParts.push(count('welcome.shape.messages', b.unread_conversations))
  }
  if (b.unread_notifications > 0) {
    inboxParts.push(count('welcome.shape.alerts', b.unread_notifications))
  }
  if (inboxParts.length > 0) {
    return t('welcome.shape.to_read', { items: inboxParts.join(' · ') })
  }

  return t('welcome.shape.waiting')
}

// ─── Cards ─────────────────────────────────────────────────────────

export function TodayCard({ events }: { events: WelcomeEvent[] }) {
  return (
    <a class="sh-welcome-card" href={addBase('/calendar')}>
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">📅</span> {t('welcome.schedule.title')}
      </h2>
      <ul class="sh-welcome-card__list">
        {events.map(e => (
          <li key={e.id} class="sh-welcome-card__row">
            <time class="sh-welcome-card__time">
              {e.all_day ? t('welcome.schedule.all_day') : eventTime(e)}
            </time>
            <span class="sh-welcome-card__line">{e.summary}</span>
          </li>
        ))}
      </ul>
      <span class="sh-welcome-card__more">{t('welcome.schedule.open_calendar')} →</span>
    </a>
  )
}

export function UpNextCard({ events }: { events: WelcomeEvent[] }) {
  return (
    <a class="sh-welcome-card" href={addBase('/calendar')}>
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">🗓</span> {t('welcome.card.up_next')}
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
      <span class="sh-welcome-card__more">{t('welcome.schedule.open_calendar')} →</span>
    </a>
  )
}

export function PendingCard({ tasks }: { tasks: WelcomeTask[] }) {
  const visible = tasks.slice(0, 5)
  const overflow = tasks.length - visible.length
  return (
    <a class="sh-welcome-card" href={addBase('/organize')}>
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">✅</span> {t('welcome.card.pending')}
      </h2>
      <ul class="sh-welcome-card__list">
        {visible.map(task => {
          const due = taskDueLabel(task.due_date)
          return (
            <li key={task.id} class="sh-welcome-card__row">
              <span class="sh-welcome-card__bullet" aria-hidden="true">⬜</span>
              <span class="sh-welcome-card__line">{task.title}</span>
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
            {t('welcome.card.more', { n: String(overflow) })}
          </li>
        )}
      </ul>
      <span class="sh-welcome-card__more">{t('welcome.card.open_tasks')} →</span>
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
        <span aria-hidden="true">✨</span> {t('welcome.card.catch_up')}
      </h2>
      <div class="sh-welcome-chips">
        {unreadConversations > 0 && (
          <a class="sh-welcome-chip" href={addBase('/dms')}>
            <span aria-hidden="true">💬</span>
            <strong>{unreadConversations}</strong>
            <span>{t(isOne(unreadConversations) ? 'welcome.chip.messages_one' : 'welcome.chip.messages')}</span>
          </a>
        )}
        {unreadNotifications > 0 && (
          <a class="sh-welcome-chip" href={addBase('/notifications')}>
            <span aria-hidden="true">🔔</span>
            <strong>{unreadNotifications}</strong>
            <span>{t(isOne(unreadNotifications) ? 'welcome.chip.alerts_one' : 'welcome.chip.alerts')}</span>
          </a>
        )}
      </div>
      {posts.length > 0 && (
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
                    <span class="sh-muted">{t('welcome.catchup.in_space', { space: p.space_name })}</span>
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
      <h2 class="sh-welcome-allclear__title">{t('welcome.all_clear.title')}</h2>
      <p class="sh-welcome-allclear__sub sh-muted">
        {t('welcome.all_clear.body')}
      </p>
    </div>
  )
}
