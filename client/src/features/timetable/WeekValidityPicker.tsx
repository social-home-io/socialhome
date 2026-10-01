/**
 * WeekValidityPicker — the "School weeks & holidays" dialog.
 *
 * "Valid from … until …" (each end can be open-ended) and the weeks as
 * one row of chips per month (a week belongs to the month of its anchor
 * day). The list is the school year — the months from the start date
 * through the end date, across the year boundary — or, open-ended, the
 * 12 months from now (or from the start date); ‹ › shift it by 12
 * months and "Back to school year" returns. Editing the dates moves it
 * live. A chip reads "W41" for a Monday-start
 * timetable (ISO week) or "Oct 4" for a Sunday-start one; its tooltip
 * and accessible name give the full range and state. States: school
 * week, holiday (muted, struck through, 🏖 and a hatch — never colour
 * alone), outside the valid range (disabled) and the current week
 * (outlined).
 *
 * Click toggles a week, Shift+click the range from the last clicked
 * chip, and a pointer drag paints the state it gave the first chip onto
 * every chip it crosses (captured once it reaches a second chip, so a
 * plain click still lands on the chip; a horizontal finger drag works on
 * touch, vertical still scrolls the sheet). Keyboard: one
 * Tab stop for the list (roving tabindex), arrows move (↑ / ↓ by month
 * row), Space toggles, Shift+Arrow extends. Quick actions — all school
 * weeks, all holidays, invert, every other week (A/B) — apply to the
 * weeks shown within the valid range; the summary counts those.
 *
 * Save is ONE ``PUT …/validity`` based on the version the dialog
 * opened with. Cancel discards; closing with unsaved changes asks.
 */
import { signal } from '@preact/signals'
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { confirmDialog } from '@/components/confirm'
import { t, locale } from '@/i18n/i18n'
import type { Timetable } from '@/types'
import { daysBetween, isIsoDate, parseIsoDate, todayIn, weekAnchor } from './dates'
import {
  anchorsOfWindow, chipAria, chipLabel, monthRows, weekInRange, weekWindow, type ChipState,
} from './yearWeeks'
import { useTimetableScope } from './scope'

const weeksFor = signal<string | null>(null)

export function openWeeksDialog(id: string): void {
  weeksFor.value = id
}

export function closeWeeksDialog(): void {
  weeksFor.value = null
}

export function WeekValidityPicker({ now }: { now?: Date }) {
  const { store } = useTimetableScope()
  const tt = store.timetables.value.find(x => x.id === weeksFor.value)
  const dirty = useRef(false)
  const setDirty = useCallback((d: boolean) => { dirty.current = d }, [])
  if (!tt) return null
  const onClose = async () => {
    if (dirty.current) {
      const ok = await confirmDialog(t('timetable.weeks.discard'), {
        title: t('timetable.weeks.discard_title'),
        confirmLabel: t('timetable.weeks.discard_confirm'),
        cancelLabel: t('timetable.weeks.keep_editing'),
        destructive: true,
      })
      if (!ok) return
    }
    dirty.current = false
    closeWeeksDialog()
  }
  return (
    <Modal open onClose={() => void onClose()} title={t('timetable.header.weeks')}>
      <WeeksForm key={tt.id} tt={tt} now={now}
                 onDirty={setDirty}
                 onDone={() => { dirty.current = false; closeWeeksDialog() }} />
    </Modal>
  )
}

function monthFmt(opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  try {
    return new Intl.DateTimeFormat(locale.value, opts)
  } catch {
    return new Intl.DateTimeFormat('en', opts)
  }
}
const firstOf = (m: string) => parseIsoDate(`${m}-01`)

/** "Sep 2026" for the first row and where the year changes, else "Oct". */
function monthLabel(m: string, withYear: boolean): string {
  return monthFmt(withYear ? { month: 'short', year: 'numeric' } : { month: 'short' }).format(firstOf(m))
}

function monthLong(m: string): string {
  return monthFmt({ month: 'long', year: 'numeric' }).format(firstOf(m))
}

/** "Sep 2026 – Aug 2027" */
function monthRange(a: string, b: string): string {
  const f = monthFmt({ month: 'short', year: 'numeric' }) as Intl.DateTimeFormat & {
    formatRange?: (x: Date, y: Date) => string
  }
  return f.formatRange ? f.formatRange(firstOf(a), firstOf(b)) : `${f.format(firstOf(a))} – ${f.format(firstOf(b))}`
}

interface Drag {
  start: string
  holiday: boolean
  moved: boolean
}

function WeeksForm({ tt: live, now, onDirty, onDone }: {
  tt: Timetable
  now?: Date
  onDirty: (dirty: boolean) => void
  onDone: () => void
}) {
  const { store } = useTimetableScope()
  const [tt, setTt] = useState(live)
  const ws = tt.week_start
  const today = todayIn(tt.tz, now)
  const thisWeek = weekAnchor(today, ws)
  const v = tt.validity
  const [from, setFrom] = useState(v.valid_from ?? '')
  const [fromOpen, setFromOpen] = useState(v.valid_from === null)
  const [until, setUntil] = useState(v.valid_until ?? '')
  const [untilOpen, setUntilOpen] = useState(v.valid_until === null)
  const [excluded, setExcluded] = useState<ReadonlySet<string>>(() => new Set(v.excluded_weeks))
  // 12-month steps away from the default window (the school year).
  const [offset, setOffset] = useState(0)
  const [focus, setFocus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const last = useRef<string | null>(null)
  const drag = useRef<Drag | null>(null)
  const swallow = useRef(false)
  const monthsRef = useRef<HTMLDivElement | null>(null)

  const fromVal = fromOpen || !isIsoDate(from) ? null : from
  const untilVal = untilOpen || !isIsoDate(until) ? null : until
  const enabled = (a: string) => weekInRange(a, fromVal, untilVal)
  // The window follows the dates as they are edited. The default window
  // hides months without a week in range; a shifted one shows them all
  // (disabled), so it is never an empty list.
  const win = weekWindow(fromVal, untilVal, today, ws, offset)
  const allAnchors = anchorsOfWindow(win, ws)
  // Chip texts only depend on the anchor, the week start and the UI
  // language — not on the state, so they survive every toggle.
  const texts = useMemo(() => new Map(allAnchors.map(a => [a, {
    label: chipLabel(a, ws),
    aria: { school: chipAria(a, ws, 'school'), holiday: chipAria(a, ws, 'holiday'),
      outside: chipAria(a, ws, 'outside') } as Record<ChipState, string>,
  }])), [allAnchors.join(','), ws, locale.value]) // eslint-disable-line react-hooks/exhaustive-deps
  const rows = monthRows(allAnchors)
    .filter(r => offset !== 0 || r.anchors.some(enabled))
  const anchors = rows.flatMap(r => r.anchors)
  const stateOf = (a: string): ChipState =>
    !enabled(a) ? 'outside' : excluded.has(a) ? 'holiday' : 'school'

  const initial = JSON.stringify([v.valid_from, v.valid_until, [...v.excluded_weeks].sort()])
  const current = JSON.stringify([fromVal, untilVal, [...excluded].sort()])
  const dirty = initial !== current
  useEffect(() => { onDirty(dirty) }, [dirty, onDirty])

  const setMany = (list: readonly string[], holiday: boolean) => {
    setExcluded(prev => {
      const next = new Set(prev)
      for (const a of list) {
        if (!enabled(a)) continue
        if (holiday) next.add(a)
        else next.delete(a)
      }
      return next
    })
  }

  const between = (a: string, b: string) => {
    const [lo, hi] = a < b ? [a, b] : [b, a]
    return anchors.filter(x => x >= lo && x <= hi)
  }

  const onChipClick = (a: string, e: MouseEvent) => {
    if (swallow.current) { swallow.current = false; return }
    if (!enabled(a)) return
    const holiday = !excluded.has(a)
    if (e.shiftKey && last.current && anchors.includes(last.current)) {
      setMany(between(last.current, a), holiday)
    } else {
      setMany([a], holiday)
    }
    last.current = a
    setFocus(a)
  }

  // Drag painting — the first chip's new state onto every chip crossed.
  const chipAt = (x: number, y: number) => {
    const el = document.elementFromPoint?.(x, y)?.closest<HTMLElement>('[data-week]')
    return el?.dataset.week ?? null
  }
  const onPointerDown = (e: PointerEvent) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>('[data-week]')
    if (!el || e.button !== 0) return
    const a = el.dataset.week!
    if (!enabled(a)) return
    drag.current = { start: a, holiday: !excluded.has(a), moved: false }
    // No pointer capture yet: capturing on press would retarget the
    // mouse click to the container and a plain click would never toggle
    // the chip. Capture only once a drag reaches a second chip (below).
  }
  const onPointerMove = (e: PointerEvent) => {
    const d = drag.current
    if (!d) return
    const a = chipAt(e.clientX, e.clientY)
    if (!a || (a === d.start && !d.moved)) return
    if (!d.moved) {
      d.moved = true
      setMany([d.start], d.holiday)
      // Now it is a drag: keep receiving the moves even past the chips.
      // (Touch already has implicit capture on the pressed chip; the
      // moves bubble here and ``chipAt`` reads the coordinates.)
      try { monthsRef.current?.setPointerCapture?.(e.pointerId) } catch { /* not capturable */ }
    }
    setMany([a], d.holiday)
  }
  const onPointerUp = () => {
    const d = drag.current
    drag.current = null
    if (d?.moved) {
      swallow.current = true
      last.current = d.start
      setTimeout(() => { swallow.current = false }, 0)
    }
  }

  // Open on the current week (school years straddle two calendar
  // years, so the months before it are mostly out of range).
  useEffect(() => {
    monthsRef.current?.querySelector<HTMLElement>(`[data-week="${thisWeek}"]`)
      ?.scrollIntoView?.({ block: 'center' })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Roving tabindex over the whole window.
  const tabStop = focus && anchors.includes(focus) ? focus
    : anchors.includes(thisWeek) ? thisWeek : anchors[0]
  const focusChip = (a: string) => {
    setFocus(a)
    setTimeout(() => monthsRef.current?.querySelector<HTMLElement>(`[data-week="${a}"]`)?.focus(), 0)
  }
  const onChipKey = (a: string, e: KeyboardEvent) => {
    const i = anchors.indexOf(a)
    let to: string | undefined
    if (e.key === 'ArrowRight') to = anchors[i + 1]
    else if (e.key === 'ArrowLeft') to = anchors[i - 1]
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      const m = rows.findIndex(r => r.anchors.includes(a))
      const col = rows[m].anchors.indexOf(a)
      const next = rows[m + (e.key === 'ArrowDown' ? 1 : -1)]
      if (next) to = next.anchors[Math.min(col, next.anchors.length - 1)]
    } else if (e.key === 'Home') to = anchors[0]
    else if (e.key === 'End') to = anchors[anchors.length - 1]
    else return
    e.preventDefault()
    if (!to) return
    if (e.shiftKey && enabled(a)) {
      setMany(between(a, to), excluded.has(a))
      last.current = to
    }
    focusChip(to)
  }

  const shownEnabled = anchors.filter(enabled)
  const quick = {
    school: () => setMany(shownEnabled, false),
    holidays: () => setMany(shownEnabled, true),
    invert: () => setExcluded(prev => {
      const next = new Set(prev)
      for (const a of shownEnabled) {
        if (next.has(a)) next.delete(a)
        else next.add(a)
      }
      return next
    }),
    alternate: () => {
      const ref = weekAnchor(fromVal ?? today, ws)
      setExcluded(prev => {
        const next = new Set(prev)
        for (const a of shownEnabled) {
          const odd = Math.abs(Math.round(daysBetween(ref, a) / 7)) % 2 === 1
          if (odd) next.add(a)
          else next.delete(a)
        }
        return next
      })
    },
  }
  const holidays = shownEnabled.filter(a => excluded.has(a)).length
  const school = shownEnabled.length - holidays

  const save = async (ev: Event) => {
    ev.preventDefault()
    if (!fromOpen && !isIsoDate(from)) { setError(t('timetable.weeks.err_from')); return }
    if (!untilOpen && !isIsoDate(until)) { setError(t('timetable.weeks.err_until')); return }
    if (fromVal && untilVal && fromVal > untilVal) { setError(t('timetable.weeks.err_order')); return }
    setError(null)
    setSaving(true)
    try {
      const out = await store.setValidity(tt.id, {
        valid_from: fromVal, valid_until: untilVal,
        // Holidays outside the valid range no longer mean anything.
        excluded_weeks: [...excluded].filter(enabled).sort(),
      }, { baseVersion: tt.version })
      if (out) onDone()
      else setTt(store.timetables.value.find(x => x.id === tt.id) ?? tt)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const idp = `sh-tt-weeks-${tt.id}`
  return (
    <form class="sh-form sh-timetable-weeks" onSubmit={save} noValidate>
      <fieldset class="sh-timetable-weeks__range">
        <legend>{t('timetable.weeks.valid')}</legend>
        <div class="sh-timetable-weeks__end">
          <label for={`${idp}-from`}>{t('timetable.weeks.from')}</label>
          <input id={`${idp}-from`} type="date" value={from} disabled={fromOpen}
                 onInput={(e) => setFrom((e.target as HTMLInputElement).value)} />
          <label class="sh-timetable-check">
            <input type="checkbox" checked={fromOpen}
                   onChange={(e) => {
                     const on = (e.target as HTMLInputElement).checked
                     setFromOpen(on)
                     if (!on && !from) setFrom(today)
                   }} />
            <span>{t('timetable.weeks.open_start')}</span>
          </label>
        </div>
        <div class="sh-timetable-weeks__end">
          <label for={`${idp}-until`}>{t('timetable.weeks.until')}</label>
          <input id={`${idp}-until`} type="date" value={until} disabled={untilOpen}
                 min={fromVal ?? undefined}
                 onInput={(e) => setUntil((e.target as HTMLInputElement).value)} />
          <label class="sh-timetable-check">
            <input type="checkbox" checked={untilOpen}
                   onChange={(e) => {
                     const on = (e.target as HTMLInputElement).checked
                     setUntilOpen(on)
                     if (!on && !until) setUntil(`${Number(today.slice(0, 4)) + 1}-07-31`)
                   }} />
            <span>{t('timetable.weeks.open_end')}</span>
          </label>
        </div>
      </fieldset>

      <div class="sh-timetable-weeks__yearbar">
        <button type="button" class="sh-timetable-weeks__nav" onClick={() => setOffset(offset - 1)}
                aria-label={t('timetable.weeks.prev_window')} title={t('timetable.weeks.prev_window')}>
          <span aria-hidden="true">‹</span>
        </button>
        <h3 class="sh-timetable-weeks__year" aria-live="polite">{monthRange(win.start, win.end)}</h3>
        <button type="button" class="sh-timetable-weeks__nav" onClick={() => setOffset(offset + 1)}
                aria-label={t('timetable.weeks.next_window')} title={t('timetable.weeks.next_window')}>
          <span aria-hidden="true">›</span>
        </button>
        {offset !== 0 && (
          <button type="button" class="sh-chip sh-timetable-chip" onClick={() => setOffset(0)}>
            {t(fromVal && untilVal ? 'timetable.weeks.back_school_year' : 'timetable.weeks.back_now')}
          </button>
        )}
      </div>

      <div class="sh-timetable-weeks__quick" role="group" aria-label={t('timetable.weeks.quick')}>
        <button type="button" class="sh-chip sh-timetable-chip" onClick={quick.school}>{t('timetable.weeks.all_school')}</button>
        <button type="button" class="sh-chip sh-timetable-chip" onClick={quick.holidays}>{t('timetable.weeks.all_holidays')}</button>
        <button type="button" class="sh-chip sh-timetable-chip" onClick={quick.invert}>{t('timetable.weeks.invert')}</button>
        <button type="button" class="sh-chip sh-timetable-chip" onClick={quick.alternate}
                title={t('timetable.weeks.alternate_hint')}>{t('timetable.weeks.alternate')}</button>
      </div>

      <div ref={monthsRef} class="sh-timetable-weeks__months"
           onPointerDown={onPointerDown} onPointerMove={onPointerMove}
           onPointerUp={onPointerUp} onPointerCancel={() => { drag.current = null }}>
        {rows.length === 0 && <p class="sh-form-hint">{t('timetable.weeks.none_shown')}</p>}
        {rows.map((row, i) => (
          <div key={row.month} class="sh-timetable-weeks__month">
            <span class="sh-timetable-weeks__mname" aria-hidden="true">
              {monthLabel(row.month, i === 0 || rows[i - 1].month.slice(0, 4) !== row.month.slice(0, 4))}
            </span>
            <div class="sh-timetable-weeks__chips" role="group"
                 aria-label={monthLong(row.month)}>
              {row.anchors.map(a => {
                const state = stateOf(a)
                const aria = texts.get(a)!.aria[state]
                return (
                  <button key={a} type="button" data-week={a}
                          class={`sh-timetable-week sh-timetable-week--${state}${a === thisWeek ? ' is-current' : ''}`}
                          aria-pressed={state === 'school'}
                          aria-disabled={state === 'outside' ? 'true' : undefined}
                          aria-label={aria + (a === thisWeek ? `, ${t('timetable.weeks.this_week')}` : '')}
                          title={aria}
                          tabIndex={a === tabStop ? 0 : -1}
                          onClick={(e) => onChipClick(a, e)}
                          onKeyDown={(e) => onChipKey(a, e)}
                          onFocus={() => setFocus(a)}>
                    {state === 'holiday' && <span class="sh-timetable-week__mark" aria-hidden="true">🏖</span>}
                    <span aria-hidden="true">{texts.get(a)!.label}</span>
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </div>

      <p class="sh-timetable-weeks__legend" aria-hidden="true">
        <span class="sh-timetable-week sh-timetable-week--school sh-timetable-week--key" />{t('timetable.weeks.state_school')}
        <span class="sh-timetable-week sh-timetable-week--holiday sh-timetable-week--key">🏖</span>{t('timetable.weeks.state_holiday')}
        <span class="sh-timetable-week sh-timetable-week--outside sh-timetable-week--key" />{t('timetable.weeks.state_outside')}
        <span class="sh-timetable-week sh-timetable-week--school sh-timetable-week--key is-current" />{t('timetable.weeks.this_week')}
      </p>
      <p class="sh-timetable-weeks__summary" aria-live="polite">
        {t('timetable.weeks.summary', {
          school: t(school === 1 ? 'timetable.weeks.n_school_one' : 'timetable.weeks.n_school', { n: String(school) }),
          holidays: t(holidays === 1 ? 'timetable.weeks.n_holidays_one' : 'timetable.weeks.n_holidays', { n: String(holidays) }),
        })}
      </p>

      <FormError id={`${idp}-err`} message={error} />
      <div class="sh-form-actions sh-timetable-entry__actions">
        <span class="sh-timetable-entry__spacer" />
        <Button type="button" variant="secondary" onClick={onDone}>{t('timetable.cancel')}</Button>
        <Button type="submit" loading={saving}>{t('timetable.save')}</Button>
      </div>
    </form>
  )
}
