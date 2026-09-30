/**
 * TimetablePrint — what "Print" (··· menu → ``window.print()``) puts on
 * paper. Hidden on screen; the print stylesheet hides the app chrome
 * and the interactive grid and shows this instead, on landscape A4.
 *
 * A header (name, days, validity — or, in week mode, which week), the
 * Periods table when every day has the same slots (the proportional
 * Timeline prints badly), else the List view's tables, and a
 * "Printed <date>" footer. The colour stripe stays as each block's
 * left border, icons stay, breaks keep their hatch — readable in black
 * and white.
 */
import { computed, signal } from '@preact/signals'
import { locale, t } from '@/i18n/i18n'
import type { Timetable } from '@/types'
import { validitySummary } from './dates'
import { isPeriodsEligible } from './layout'
import { TimetableList } from './TimetableList'
import { TimetablePeriods } from './TimetablePeriods'
import { daysSummary } from './time'

interface Props {
  tt: Timetable
  days: number[]
  /** Week mode: the week shown ("W41 · Oct 5 – 11"). */
  week?: string
  now?: Date
}

const noop = () => {}

/** True while the page is being printed — the twin is only rendered
 *  then, so the screen DOM carries one grid, not two. Set by the
 *  browser's ``beforeprint`` / ``afterprint`` (Ctrl+P, the browser
 *  menu) and ``matchMedia('print')``, and by ``printTimetable``. */
const printEvent = signal(false)
const printMedia = signal(false)
export const printing = computed(() => printEvent.value || printMedia.value)

let wired = false
export function wirePrinting(): void {
  if (wired || typeof window === 'undefined') return
  wired = true
  window.addEventListener('beforeprint', () => { printEvent.value = true })
  window.addEventListener('afterprint', () => { printEvent.value = false })
  const mq = typeof window.matchMedia === 'function' ? window.matchMedia('print') : null
  if (mq) {
    printMedia.value = mq.matches
    mq.addEventListener?.('change', (e) => { printMedia.value = e.matches })
  }
}

// Wired at import, before any grid renders — an effect would miss a
// Ctrl+P that comes before its first commit.
wirePrinting()

/** The ··· menu's "Print": render the twin, let it commit, then print. */
export function printTimetable(): void {
  wirePrinting()
  printEvent.value = true
  setTimeout(() => window.print(), 0)
}

export function TimetablePrint({ tt, days, week, now = new Date() }: Props) {
  let printed: string
  try {
    printed = new Intl.DateTimeFormat(locale.value, { dateStyle: 'medium' }).format(now)
  } catch {
    printed = now.toDateString()
  }
  return (
    <div class="sh-timetable-print sh-timetable-print-only" aria-hidden="true" lang={locale.value}>
      <header class="sh-timetable-print__head">
        <h2 class="sh-timetable-print__name">{tt.name}</h2>
        <p class="sh-timetable-print__meta">
          {daysSummary(tt.days, tt.week_start)}
          {' · '}
          {week ? t('timetable.print.week', { week }) : validitySummary(tt.validity)}
        </p>
      </header>
      {isPeriodsEligible(tt, days) ? (
        <TimetablePeriods tt={tt} days={days} today={null} picture={false}
                          onEdit={noop} onAddAt={noop} onAddDay={noop} printCopy />
      ) : (
        <TimetableList tt={tt} days={days} />
      )}
      <footer class="sh-timetable-print__foot">{t('timetable.print.printed', { date: printed })}</footer>
    </div>
  )
}
