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
 */
import type { JSX } from 'preact'
import type { Timetable, TimetableEntry } from '@/types'
import { colorClass, entryColor } from './colors'
import { displayTitle, entryAriaLabel, metaLine } from './labels'
import { formatRange } from './time'

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
  onOpen: (entry: TimetableEntry) => void
}

export function LessonBlock({
  tt, entry, variant, picture = false, hideTime = false, hideMeta = false, spanEnd, style, onOpen,
}: Props) {
  const title = displayTitle(entry)
  const isBreak = entry.kind === 'break'
  const empty = !isBreak && !entry.title?.trim() && !entry.icon
  const meta = metaLine(entry)
  const cls = [
    'sh-timetable-block',
    `sh-timetable-block--${variant}`,
    isBreak ? 'sh-timetable-block--break' : colorClass(entryColor(entry, tt)),
    empty ? 'sh-timetable-block--empty' : '',
    picture ? 'sh-timetable-block--picture' : '',
  ].filter(Boolean).join(' ')
  const initial = !entry.icon && entry.title?.trim()
    ? Array.from(entry.title.trim())[0].toUpperCase()
    : null

  return (
    <button
      type="button"
      class={cls}
      style={style}
      aria-label={entryAriaLabel(tt, entry, spanEnd)}
      onClick={(e) => { e.stopPropagation(); onOpen(entry) }}
    >
      {empty ? (
        <span class="sh-timetable-block__plus" aria-hidden="true">+</span>
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
            {!picture && !hideMeta && !isBreak && meta && (
              <span class="sh-timetable-block__meta">{meta}</span>
            )}
            {!hideTime && !picture && (
              <span class="sh-timetable-block__time">{formatRange(entry.start, spanEnd ?? entry.end)}</span>
            )}
          </span>
        </>
      )}
    </button>
  )
}
