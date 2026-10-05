/**
 * CalendarFilterStrip — pinned-to-the-wall row of household-member
 * calendars shown above the calendar header.
 *
 * Mirrors the look + feel of :class:`HouseholdPresenceStrip` so the
 * calendar reads as the same surface family as the feed: each member's
 * calendar is a polaroid-style card carrying their avatar, name, a
 * one-line "Calendar" / "{N} calendars" label, and a washi-tape strip
 * coloured by the calendar's hue. Click → toggles visibility of that
 * member's events in the calendar view.
 *
 * A member with more than one calendar collapses into one card that
 * toggles the whole batch; that keeps the strip glance-able even when
 * a household has dozens of calendars between them.
 *
 * The card visually exposes its on/off state:
 *   ON  → coloured washi tape + filled name + un-faded avatar
 *   OFF → muted tape + faded avatar (the card is still clickable)
 */
import type { ComponentChildren } from 'preact'
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
  /** "Show all" affordance — kept on this component so the strip
   *  carries every overlay control. */
  onShowAll: () => void
  /** "Just me" — collapses overlays to the caller's own calendar(s). */
  onShowOnlyMine: () => void
  /** Optional primary action rendered alongside the quick-filter row
   *  below the strip — typically the page's "+ New event" button so
   *  it sits in a meaningful neighbourhood rather than floating
   *  alone. */
  primaryAction?: ComponentChildren
}

export function CalendarFilterStrip({
  calendars,
  visibleCalendarIds,
  onChange,
  onShowAll,
  onShowOnlyMine,
  primaryAction,
}: CalendarFilterStripProps) {
  if (calendars.length === 0) {
    // Even with no calendars to filter we still want to surface the
    // primary action — without it the page would have no "+ New event"
    // button at all. Render the action by itself in that case.
    if (primaryAction) {
      return (
        <div class="sh-cal-strip-wrap sh-cal-strip-wrap--actions-only">
          <div class="sh-cal-strip-actions">{primaryAction}</div>
        </div>
      )
    }
    return null
  }
  const cards = groupCalendarsByOwner(calendars)
  // Lone-calendar case: skip the polaroid strip (nothing to filter)
  // but keep the action row so the page still owns its "+ New event".
  if (cards.length < 2) {
    if (primaryAction) {
      return (
        <div class="sh-cal-strip-wrap sh-cal-strip-wrap--actions-only">
          <div class="sh-cal-strip-actions">{primaryAction}</div>
        </div>
      )
    }
    return null
  }

  const toggle = (card: MemberCard) => {
    const next = new Set(visibleCalendarIds)
    const allOn = card.calendarIds.every(id => next.has(id))
    if (allOn) {
      // Never let the user hide every calendar — that would leave an
      // empty view with no way back besides "Show all".
      const remaining = new Set(next)
      for (const id of card.calendarIds) remaining.delete(id)
      if (remaining.size === 0) return
      for (const id of card.calendarIds) next.delete(id)
    } else {
      for (const id of card.calendarIds) next.add(id)
    }
    onChange(next)
  }

  return (
    <div class="sh-cal-strip-wrap">
      <nav
        class="sh-cal-strip"
        aria-label={t('calendar.strip.aria')}
      >
        <div class="sh-cal-strip-inner">
          {cards.map((card, i) => {
            const on = card.calendarIds.every(id =>
              visibleCalendarIds.has(id),
            )
            const partial = !on && card.calendarIds.some(id =>
              visibleCalendarIds.has(id),
            )
            // Pin state is conveyed visually by the ``--on`` / ``--partial``
            // modifier classes (colour saturation + tape hue + opacity).
            // The aria-label keeps a screen-reader-friendly text version of
            // the same state.
            const stateLabel = on
              ? t('calendar.strip.state_on')
              : partial ? t('calendar.strip.state_partial') : t('calendar.strip.state_off')
            return (
              <button
                key={card.ownerKey}
                type="button"
                class={
                  'sh-cal-strip-pin'
                  + (on ? ' sh-cal-strip-pin--on' : '')
                  + (partial ? ' sh-cal-strip-pin--partial' : '')
                }
                style={{
                  '--cal-hue': card.hue,
                  '--sh-pin-rot': `${PIN_ROTATIONS[i % PIN_ROTATIONS.length]}deg`,
                } as Record<string, string>}
                aria-pressed={on}
                aria-label={t('calendar.strip.pin_aria', { name: card.displayName, state: stateLabel })}
                onClick={() => toggle(card)}
              >
                <span class="sh-cal-strip-tape" aria-hidden="true" />
                <Avatar
                  name={card.displayName}
                  src={card.pictureUrl}
                  size={44}
                />
                <span class="sh-cal-strip-name">
                  {card.isMine ? t('calendar.page.owner_you') : card.displayName}
                </span>
              </button>
            )
          })}
        </div>
      </nav>
      <div class="sh-cal-strip-actions">
        <div class="sh-cal-strip-quick" role="group" aria-label={t('calendar.strip.filters_aria')}>
          <button
            type="button"
            class="sh-cal-strip-quick__btn"
            onClick={onShowOnlyMine}
          >
            {t('calendar.strip.just_me')}
          </button>
          <button
            type="button"
            class="sh-cal-strip-quick__btn"
            onClick={onShowAll}
          >
            {t('calendar.strip.everyone')}
          </button>
        </div>
        {primaryAction && (
          <div class="sh-cal-strip-actions__primary">{primaryAction}</div>
        )}
      </div>
    </div>
  )
}

/** Tiny rotation stagger — adjacent pins tilt the opposite way so the
 *  row reads as hand-pinned rather than algorithmic. Matches the
 *  presence strip's vocabulary. */
const PIN_ROTATIONS = [-1.5, 1, -2, 1.5, -1, 2] as const
