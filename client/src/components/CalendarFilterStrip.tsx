/**
 * CalendarFilterStrip — whose calendars the household calendar shows.
 *
 * One flat avatar chip per household member, after two presets
 * ("Everyone", "Just me"). A chip is a toggle (``aria-pressed``) with
 * one selected style for every person — the primary tint the people
 * pickers use — so "showing" never depends on a person's colour. The
 * small dot is that person's calendar hue: the same colour as the rail
 * on their events below, a legend rather than a state.
 *
 * A member with more than one calendar is one chip that toggles the
 * whole batch (``aria-pressed="mixed"`` while only some show). The
 * presets read as pressed when they describe what is showing.
 */
import { Avatar } from './Avatar'
import { householdUsers } from '@/store/householdUsers'
import { currentUser } from '@/store/auth'
import { resolveCalendarColor } from '@/utils/calendar'
import { t } from '@/i18n/i18n'

/** Smaller summary shape than the one in the route — only the fields
 *  the strip needs. Callers pass the full list and we ignore the rest. */
export interface CalendarStripSummary {
  id: string
  name: string
  owner_username: string
  color?: string | null
}

/** One presence-style card per household member; clicking toggles the
 *  visibility of every calendar that belongs to that member. */
interface MemberCard {
  ownerKey: string
  displayName: string
  pictureUrl: string | null
  calendarIds: string[]
  /** Representative hue for the tape — taken from the first calendar so
   *  the eye anchors on the same colour the events strip carries. */
  hue: string
  isMine: boolean
}

function groupCalendarsByOwner(
  calendars: CalendarStripSummary[],
): MemberCard[] {
  const cards = new Map<string, MemberCard>()
  // Build a username → user_id reverse index once. The household-user
  // map is keyed by user_id, but calendars carry username — we resolve
  // each calendar owner to a display name + avatar via the index.
  const userIndex = new Map<
    string,
    { user_id: string; display_name: string; picture_url: string | null }
  >()
  for (const u of householdUsers.value.values()) {
    userIndex.set(u.username, {
      user_id: u.user_id,
      display_name: u.display_name || u.username,
      picture_url: u.picture_url ?? null,
    })
  }
  const me = currentUser.value?.username
  for (const c of calendars) {
    const card = cards.get(c.owner_username)
    if (card) {
      card.calendarIds.push(c.id)
      continue
    }
    const u = userIndex.get(c.owner_username)
    cards.set(c.owner_username, {
      ownerKey: c.owner_username,
      displayName: u?.display_name ?? c.owner_username,
      pictureUrl: u?.picture_url ?? null,
      calendarIds: [c.id],
      hue: resolveCalendarColor(c),
      isMine: c.owner_username === me,
    })
  }
  // Stable ordering: own card first, then alphabetic by display name.
  return Array.from(cards.values()).sort((a, b) => {
    if (a.isMine !== b.isMine) return a.isMine ? -1 : 1
    return a.displayName.localeCompare(b.displayName)
  })
}

interface CalendarFilterStripProps {
  calendars: CalendarStripSummary[]
  visibleCalendarIds: Set<string>
  /** Called with the calendar ids that should be visible after the
   *  click. Caller persists + re-fetches. */
  onChange: (next: Set<string>) => void
  /** "Everyone" — every household calendar. */
  onShowAll: () => void
  /** "Just me" — the caller's own calendar(s) only. */
  onShowOnlyMine: () => void
}

export function CalendarFilterStrip({
  calendars,
  visibleCalendarIds,
  onChange,
  onShowAll,
  onShowOnlyMine,
}: CalendarFilterStripProps) {
  const cards = groupCalendarsByOwner(calendars)
  // One person: nothing to filter.
  if (cards.length < 2) return null

  const shows = (ids: string[]) => ids.every(id => visibleCalendarIds.has(id))
  const mine = cards.filter(c => c.isMine).flatMap(c => c.calendarIds)
  const everyone = shows(calendars.map(c => c.id))
  const justMe = mine.length > 0 && !everyone
    && visibleCalendarIds.size === mine.length && shows(mine)

  const toggle = (card: MemberCard) => {
    const next = new Set(visibleCalendarIds)
    if (shows(card.calendarIds)) {
      // Never hide every calendar — an empty view with no way back
      // besides "Everyone".
      for (const id of card.calendarIds) next.delete(id)
      if (next.size === 0) return
    } else {
      for (const id of card.calendarIds) next.add(id)
    }
    onChange(next)
  }

  return (
    <div class="sh-cal-people" role="group" aria-label={t('calendar.strip.aria')}>
      <button type="button"
              class={`sh-cal-person sh-cal-person--preset${everyone ? ' is-on' : ''}`}
              aria-pressed={everyone} onClick={onShowAll}>
        {t('calendar.strip.everyone')}
      </button>
      {mine.length > 0 && (
        <button type="button"
                class={`sh-cal-person sh-cal-person--preset${justMe ? ' is-on' : ''}`}
                aria-pressed={justMe} onClick={onShowOnlyMine}>
          {t('calendar.strip.just_me')}
        </button>
      )}
      <span class="sh-cal-people__sep" aria-hidden="true" />
      {cards.map(card => {
        const on = shows(card.calendarIds)
        const partial = !on && card.calendarIds.some(id => visibleCalendarIds.has(id))
        return (
          <button
            key={card.ownerKey}
            type="button"
            class={`sh-cal-person${on ? ' is-on' : ''}`}
            style={{ '--cal-hue': card.hue } as Record<string, string>}
            aria-pressed={on ? 'true' : partial ? 'mixed' : 'false'}
            onClick={() => toggle(card)}
          >
            <span class="sh-cal-person__avatar" aria-hidden="true">
              <Avatar name={card.displayName} src={card.pictureUrl} size={28} />
            </span>
            <span class="sh-cal-person__name">
              {card.isMine ? t('calendar.page.owner_you') : card.displayName}
            </span>
            <span class="sh-cal-person__hue" aria-hidden="true" />
          </button>
        )
      })}
    </div>
  )
}
