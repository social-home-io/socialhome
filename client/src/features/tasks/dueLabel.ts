/**
 * The one due-date chip of a task row (and, next, a board card):
 *
 * - past due → ``"Overdue · Sep 28"``, tone ``overdue`` (danger)
 * - due today → ``"Today"``, tone ``today`` (amber)
 * - otherwise → the date, no tone
 *
 * Dates are formatted in the UI language (``locale``); the year shows
 * only when it isn't the current one. ``due_date`` is a calendar day
 * (``YYYY-MM-DD``), read as a LOCAL date — ``new Date('2026-10-03')``
 * would parse it as UTC midnight and show the day before west of UTC.
 */
import { locale, t } from '@/i18n/i18n'

export interface DueLabel {
  text: string
  /** Compact text for narrow rows (the date alone; tone carries the rest). */
  short: string
  tone: 'overdue' | 'today' | null
  /** Full date for the tooltip. */
  title: string
}

/** ``YYYY-MM-DD`` (or an ISO timestamp) → a local Date at midnight. */
export function parseDueDate(due: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(due)
  if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
  const d = new Date(due)
  if (Number.isNaN(d.getTime())) return null
  return new Date(d.getFullYear(), d.getMonth(), d.getDate())
}

export function dueLabel(due: string, now: Date = new Date()): DueLabel {
  const day = parseDueDate(due)
  if (!day) return { text: due, short: due, tone: null, title: due }
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const lang = locale.value || undefined
  const date = day.toLocaleDateString(lang, day.getFullYear() === today.getFullYear()
    ? { day: 'numeric', month: 'short' }
    : { day: 'numeric', month: 'short', year: 'numeric' })
  const title = day.toLocaleDateString(lang, { dateStyle: 'full' })
  const diff = day.getTime() - today.getTime()
  if (diff < 0) return { text: t('tasks.due.overdue', { date }), short: date, tone: 'overdue', title }
  if (diff === 0) return { text: t('tasks.due.today'), short: t('tasks.due.today'), tone: 'today', title }
  return { text: date, short: date, tone: null, title }
}
