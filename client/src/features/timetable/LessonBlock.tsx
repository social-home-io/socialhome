/**
 * One slot of the timetable — a lesson or a break — as a button that
 * opens the EntryDialog. Shared by the Periods table, the Timeline, the
 * mobile day view and the list's print fallback look.
 *
 * Lessons are tinted with their colour token (``--tt-bg`` fill,
 * ``--tt-edge`` left border, ``--tt-fg`` text); breaks are a calm
 * hatched band; an untitled lesson is a dashed "+" slot. In Picture
 * view the icon leads (or the title's first letter in a big circle)
 * and the room is dropped, for children who can't read yet.
 *
 * Two modes change what the block does:
 *
 *  * **Brush mode** (``brush.ts``): a click / tap / Enter applies the
 *    active brush instead of opening the dialog, a pointer drag paints
 *    across blocks, and the accessible name leads with "Fill with
 *    Mathe:". Breaks are inert.
 *  * **Week mode** (``WeekContext``): the block shows this week's
 *    status — cancelled (muted, struck through, "Cancelled" badge),
 *    changed (dashed outline + "Room 204 → 112") or extra ("Extra"
 *    badge). Days too far in the past to change are ``aria-disabled``
 *    with a tooltip saying why.
 *
 * Grid keyboard navigation (``useGridNav``) finds blocks by their
 * ``data-tt-nav`` / ``data-day`` / ``data-start`` attributes and owns
 * their ``tabindex``.
 */
import type { JSX } from 'preact'
import { useContext } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import type { Timetable, TimetableEntry } from '@/types'
import {
  brushAriaPrefix, brushClick, brushOn, strokeEnter, strokeIds, strokeStart,
} from './brush'
import { colorClass, entryColor } from './colors'
import { displayTitle, entryAriaLabel, metaLine } from './labels'
import { useTimetableScope } from './scope'
import { formatRange, toMinutes } from './time'
import { WeekContext, changesOf, isLocked, statusAria } from './weekView'

export type BlockVariant = 'cell' | 'timeline' | 'day'

interface Props {
  tt: Timetable
  entry: TimetableEntry
  variant: BlockVariant
  picture?: boolean
  /** Hide the time line (a short timeline block) — the aria-label keeps it. */
  hideTime?: boolean
  /** Hide room / teacher (a very short timeline block). */
  hideMeta?: boolean
  /** A merged double lesson: show (and announce) the span to here. */
  spanEnd?: string
  style?: JSX.CSSProperties
  /** A merged double lesson: every entry id it covers (brush, Delete,
   *  paste and week-mode actions apply to all). Defaults to ``entry``. */
  runIds?: string[]
  onOpen: (entry: TimetableEntry, run?: string[]) => void
}

export function LessonBlock({
  tt, entry, variant, picture = false, hideTime = false, hideMeta = false, spanEnd, style, runIds,
  onOpen,
}: Props) {
  const ids = runIds && runIds.length > 0 ? runIds : [entry.id]
  const week = useContext(WeekContext)
  const lesson = week?.lessons.get(entry.id)
  const status = lesson?.status ?? 'normal'
  const locked = week ? isLocked(week, entry.weekday) : false
  const brushing = !week && brushOn(tt.id)
  const { editable } = useTimetableScope()
  const title = displayTitle(entry)
  const isBreak = entry.kind === 'break'
  const empty = !isBreak && !entry.title?.trim() && !entry.icon
  // A view-only viewer has nothing to open on an empty slot.
  const inert = (brushing && isBreak) || locked || (!editable && empty)

  const meta = metaLine(entry)
  const changes = lesson ? changesOf(lesson) : []
  const cls = [
    'sh-timetable-block',
    `sh-timetable-block--${variant}`,
    isBreak ? 'sh-timetable-block--break' : colorClass(entryColor(entry, tt)),
    empty ? 'sh-timetable-block--empty' : '',
    picture ? 'sh-timetable-block--picture' : '',
    status !== 'normal' ? `sh-timetable-block--${status}` : '',
    brushing && !isBreak ? 'sh-timetable-block--brush' : '',
    brushing && strokeIds.value.has(entry.id) ? 'is-stroke' : '',
    inert ? 'sh-timetable-block--inert' : '',
  ].filter(Boolean).join(' ')
  const initial = !entry.icon && entry.title?.trim()
    ? Array.from(entry.title.trim())[0].toUpperCase()
    : null

  let label = entryAriaLabel(tt, entry, spanEnd)
  if (lesson) label += statusAria(lesson)
  const prefix = brushing ? brushAriaPrefix(entry) : null
  if (prefix) label = `${prefix}: ${label}`
  // The status badge rides on the time line, so the title keeps the
  // full width of a narrow column.
  const badge = status === 'cancelled' ? t('timetable.week.cancelled_badge')
    : status === 'added' ? t('timetable.week.extra') : null
  const tooltip = locked ? t('timetable.week.locked')
    : changes.length > 0 ? changes.join('\n') : undefined

  const onClick = (e: MouseEvent) => {
    e.stopPropagation()
    if (inert) return
    if (brushing) { brushClick(tt.id, ids); return }
    if (ids.length > 1) onOpen(entry, ids)
    else onOpen(entry)
  }

  return (
    <button
      type="button"
      class={cls}
      style={style}
      aria-label={label}
      aria-disabled={inert ? 'true' : undefined}
      title={tooltip}
      data-tt-nav=""
      data-entry-id={entry.id}
      data-run-ids={ids.length > 1 ? ids.join(' ') : undefined}
      data-day={entry.weekday}
      data-start={toMinutes(entry.start)}
      data-kind={entry.kind}
      onClick={onClick}
      onPointerDown={brushing ? (e) => strokeStart(tt.id, entry, ids, e) : undefined}
      onPointerEnter={brushing ? () => strokeEnter(entry, ids) : undefined}
    >
      {empty ? (
        editable ? <span class="sh-timetable-block__plus" aria-hidden="true">+</span> : null
      ) : (
        <>
          {entry.icon && (
            <span class="sh-timetable-block__icon" aria-hidden="true">{entry.icon}</span>
          )}
          {picture && !isBreak && initial && (
            <span class="sh-timetable-block__initial" aria-hidden="true">{initial}</span>
          )}
          <span class="sh-timetable-block__text" aria-hidden="true">
            <span class="sh-timetable-block__title">{title}</span>
            {!picture && !hideMeta && !isBreak && meta && status !== 'cancelled' && (
              <span class="sh-timetable-block__meta">{meta}</span>
            )}
            {changes.length > 0 && (
              <span class="sh-timetable-block__change">{changes.join(' · ')}</span>
            )}
            {(badge || (!hideTime && !picture && !(changes.length > 0 && variant !== 'day'))) && (
              <span class="sh-timetable-block__time">
                {badge && (
                  <span class={`sh-timetable-badge sh-timetable-badge--${status}`}>{badge}</span>
                )}
                {!hideTime && !picture && formatRange(entry.start, spanEnd ?? entry.end)}
              </span>
            )}
          </span>
        </>
      )}
    </button>
  )
}
