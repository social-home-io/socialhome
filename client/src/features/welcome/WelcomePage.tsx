/**
 * WelcomePage — the warm "open the door" landing surface (§Welcome).
 *
 * Mounted at ``/`` by :mod:`LandingDispatch` for any user who hasn't
 * picked a different home page in Settings.  Optimised for the
 * "person just opened the app" moment — the goal is for the first
 * three seconds to answer:
 *
 *   1. Did the app remember me? → time-of-day greeting + first name.
 *   2. What's actually on today? → today's events + pending tasks,
 *      content-conditional (cards collapse when empty rather than
 *      showing nag-ish "no events today" placeholders).
 *   3. Anything I missed? → catch-up tray with the most recent post
 *      from a followed space + chips for unread DMs / notifications.
 *
 * Card components + helpers are shared with :mod:`DashboardPage`
 * via :mod:`./cards` so the corner-light here and the full corner
 * read as the same surface, just with more rooms.
 */
import { useEffect, useState } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { api } from '@/api'
import { ws } from '@/ws'
import { currentUser } from '@/store/auth'
import { CardSkeleton } from '@/components/SkeletonScreen'
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
  timeOfDayGreeting,
  todaysEvents,
  type WelcomeBundle,
} from './cards'
import { TodayScheduleCard } from './TodayScheduleCard'
import { activeTimetables, lessonCount } from './schedule'

const EMPTY: WelcomeBundle = {
  unread_notifications: 0,
  unread_conversations: 0,
  upcoming_events: [],
  tasks_due_today: [],
  followed_spaces_feed: [],
  today_timetable: [],
  today_events: [],
}

export default function WelcomePage() {
  useTitle('Welcome')
  const [bundle, setBundle] = useState<WelcomeBundle | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = async () => {
    try {
      const data = await api.get('/api/me/corner') as WelcomeBundle
      setBundle(data)
      setError(null)
    } catch (err: unknown) {
      setError((err as Error).message ?? 'Could not load.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
    let timer: ReturnType<typeof setTimeout> | null = null
    const debounced = () => {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => { void refresh() }, 200)
    }
    const events = [
      'notification.new', 'notification.unread_count',
      'dm.message',
      'calendar.created', 'calendar.updated',
      'calendar.deleted',
      'timetable.changed', 'timetable.deleted',
      'task.created', 'task.updated', 'task.deleted', 'task.completed',
    ]
    const offs = events.map(e => ws.on(e, debounced))
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
        </div>
      </div>
    )
  }

  // Errors fall through to a calm card instead of an alarming alert
  // — the welcome page should never feel hostile.
  if (error && !bundle) {
    return (
      <div class="sh-welcome">
        <header class="sh-welcome-hero">
          <h1 class="sh-welcome-hero__greeting">Welcome back</h1>
          <p class="sh-welcome-hero__sub">
            {longDate(new Date())} · we couldn't load your day just now
          </p>
        </header>
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
  const allClear =
    !hasSchedule
    && today.length === 0
    && upNext.length === 0
    && tasks.length === 0
    && catchUp.length === 0
    && b.unread_notifications === 0
    && b.unread_conversations === 0

  const greetee = firstName(currentUser.value?.display_name)
  const heroGreeting = greetee
    ? `${timeOfDayGreeting()}, ${greetee}`
    : timeOfDayGreeting()
  const heroSub = allClear
    ? `${longDate(new Date())} · all clear`
    : `${longDate(new Date())} · ${dayShape(today, tasks, upNext, b, lessons)}`

  return (
    <div class="sh-welcome">
      <header class="sh-welcome-hero">
        <h1 class="sh-welcome-hero__greeting">{heroGreeting}</h1>
        <p class="sh-welcome-hero__sub">{heroSub}</p>
      </header>

      {allClear ? (
        <AllClearCard />
      ) : (
        <div class="sh-welcome-stack">
          {hasSchedule ? (
            <TodayScheduleCard timetables={schedule} events={today} />
          ) : today.length > 0 && (
            <TodayCard events={today} />
          )}
          {today.length === 0 && upNext.length > 0 && (
            <UpNextCard events={upNext} />
          )}
          {tasks.length > 0 && (
            <PendingCard tasks={tasks} />
          )}
          <CatchUpCard
            posts={catchUp}
            unreadNotifications={b.unread_notifications}
            unreadConversations={b.unread_conversations}
          />
        </div>
      )}
    </div>
  )
}
