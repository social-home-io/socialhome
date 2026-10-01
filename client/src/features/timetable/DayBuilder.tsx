/**
 * DayBuilder — the fast way to set up a day's times ("Set up Monday").
 *
 * A start time, the lesson length and the gap between lessons, then an
 * editable list of slots (lesson / break, minutes, optional title) with
 * a live start–end preview per row: changing one length ripples through
 * the rows after it (maths in ``builder.ts``). "+ Lesson" / "+ Break"
 * append, "+ Add before" puts an early "0." lesson in front; rows are
 * reordered with ↑ / ↓ (no drag and drop) and removed with ✕.
 *
 * Save is ONE ``POST …/days/{wd}/generate`` (a day that already has
 * lessons asks "Replace N lessons on Monday?" via the store), keeping
 * the subjects of the entries the rows came from. "Also use these
 * times on" copies the saved day onto other days in the same go; a
 * single Undo puts every touched day back.
 *
 * Opened from a day heading's ⋯ menu, an empty day's "Set up Monday",
 * the phone day view, and right after creating an *Empty* timetable.
 * Like the EntryDialog it snapshots the timetable at mount and bases
 * its save on that version.
 */
import { signal } from '@preact/signals'
import { useRef, useState } from 'preact/hooks'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { t } from '@/i18n/i18n'
import type { Timetable, TimetableEntryKind } from '@/types'
import {
  DEFAULT_BREAK_MINUTES, addBefore, initialState, moveRow, newRow, timeRows, toSlots,
  type BuilderRow, type TimedRow,
} from './builder'
import { CopyTargets } from './CopyTargets'
import { focusGrid } from './focus'
import { dayEntries } from './layout'
import { formatRange, fromMinutes, toMinutes, weekdayName } from './time'
import { useAutofocus } from './useAutofocus'
import { useTimetableScope } from './scope'

interface BuilderTarget {
  timetableId: string
  weekday: number
}

export const dayBuilder = signal<BuilderTarget | null>(null)

export function openDayBuilder(target: BuilderTarget): void {
  dayBuilder.value = target
}

export function closeDayBuilder(): void {
  dayBuilder.value = null
}

export function DayBuilder() {
  const { store } = useTimetableScope()
  const target = dayBuilder.value
  const tt = target ? store.timetables.value.find(x => x.id === target.timetableId) : undefined
  if (!target || !tt || !tt.days.includes(target.weekday)) return null
  return (
    <Modal open onClose={closeDayBuilder}
           title={t('timetable.builder.title', { day: weekdayName(target.weekday, 'long') })}>
      <BuilderForm key={`${tt.id}:${target.weekday}`} tt={tt} weekday={target.weekday} />
    </Modal>
  )
}

/** ``"45"`` → 45; blank / fractional / out of range → ``null``. */
function intIn(raw: string, min: number, max: number): number | null {
  if (raw.trim() === '') return null
  const n = Number(raw)
  return Number.isInteger(n) && n >= min && n <= max ? n : null
}

const ERR_KEY: Record<NonNullable<TimedRow['error']>, string> = {
  midnight: 'timetable.builder.err_midnight',
  short: 'timetable.builder.err_short',
  overlap: 'timetable.builder.err_overlap',
}

function slotName(r: TimedRow): string {
  return r.kind === 'lesson'
    ? t('timetable.builder.lesson_n', { n: r.label ?? '' })
    : t('timetable.builder.break_at', { time: fromMinutes(r.start) })
}

function BuilderForm({ tt: live, weekday }: { tt: Timetable; weekday: number }) {
  const { store } = useTimetableScope()
  const [tt, setTt] = useState(live)
  const breakTitle = t('timetable.builder.break_title')
  const [init] = useState(() => initialState(live, weekday, breakTitle))
  const [start, setStart] = useState(init.start)
  const [lessonRaw, setLessonRaw] = useState(String(init.lessonMinutes))
  const [gapRaw, setGapRaw] = useState(String(init.gap))
  const [rows, setRows] = useState<BuilderRow[]>(init.rows)
  const [targets, setTargets] = useState<number[]>([])
  const [withSubjects, setWithSubjects] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const startRef = useRef<HTMLInputElement | null>(null)
  const listRef = useRef<HTMLOListElement | null>(null)
  useAutofocus(startRef, true)

  const lessonMinutes = intIn(lessonRaw, 5, 240)
  const gap = intIn(gapRaw, 0, 120) ?? 0
  const startMin = start ? toMinutes(start) : NaN
  const timed = Number.isFinite(startMin) ? timeRows(rows, startMin, gap) : []
  const lessons = timed.filter(r => r.kind === 'lesson').length
  const idp = `sh-tt-builder-${tt.id}-${weekday}`

  const onLessonMinutes = (raw: string) => {
    setLessonRaw(raw)
    const n = intIn(raw, 5, 240)
    if (n !== null) setRows(rs => rs.map(r => (r.kind === 'lesson' ? { ...r, minutes: n } : r)))
  }
  const patchRow = (key: string, patch: Partial<BuilderRow>) =>
    setRows(rs => rs.map(r => (r.key === key ? { ...r, ...patch } : r)))
  const setKind = (key: string, kind: TimetableEntryKind) => patchRow(key, {
    kind,
    early: false,
    minutes: kind === 'break' ? DEFAULT_BREAK_MINUTES : (lessonMinutes ?? tt.defaults.lesson_minutes),
    title: kind === 'break' ? breakTitle : '',
  })
  // Keep focus on the moved row's button so repeated ↑ / ↓ keep moving it.
  const move = (index: number, dir: -1 | 1, which: 'up' | 'down') => {
    const key = rows[index].key
    setRows(rs => moveRow(rs, index, dir))
    setTimeout(() => listRef.current
      ?.querySelector<HTMLElement>(`[data-row="${key}"] [data-move="${which}"]:not(:disabled)`)
      ?.focus(), 0)
  }
  const remove = (index: number) => {
    setRows(rs => rs.filter((_, i) => i !== index))
    setTimeout(() => {
      const items = listRef.current?.querySelectorAll<HTMLElement>('[data-remove]')
      const next = items?.[Math.min(index, (items?.length ?? 1) - 1)]
      ;(next ?? startRef.current)?.focus()
    }, 0)
  }
  const add = (kind: TimetableEntryKind) => setRows(rs => [...rs, kind === 'lesson'
    ? newRow('lesson', lessonMinutes ?? tt.defaults.lesson_minutes)
    : newRow('break', DEFAULT_BREAK_MINUTES, breakTitle)])
  const onAddBefore = () => {
    const next = addBefore({ start, lessonMinutes: lessonMinutes ?? tt.defaults.lesson_minutes, gap, rows })
    if (!next) return
    if (toMinutes(next.start) < 0 || !Number.isFinite(toMinutes(next.start))) {
      setError(t('timetable.builder.err_before'))
      return
    }
    setStart(next.start)
    setRows(next.rows)
  }
  const canAddBefore = !rows[0]?.early && Number.isFinite(startMin)
    && startMin - (lessonMinutes ?? tt.defaults.lesson_minutes)
      - (rows[0]?.kind === 'lesson' ? gap : 0) >= 0

  const validate = (): string | null => {
    if (!start || !Number.isFinite(startMin)) return t('timetable.entry.err_time')
    if (lessonMinutes === null) return t('timetable.settings.err_lesson')
    if (intIn(gapRaw, 0, 120) === null) return t('timetable.settings.err_gap')
    if (rows.length === 0) return t('timetable.builder.err_empty')
    if (timed.some(r => r.error)) return t('timetable.builder.err_rows')
    return null
  }

  const save = async (ev: Event) => {
    ev.preventDefault()
    const invalid = validate()
    setError(invalid)
    if (invalid) return
    setSaving(true)
    try {
      const previous = tt.entries
      const slots = toSlots(timed, dayEntries(tt, weekday))
      const out = await store.generateDay(tt.id, weekday, slots, { baseVersion: tt.version })
      if (!out) {
        // Declined the replace, or a 409 reloaded — rebase and stay open.
        setTt(store.timetables.value.find(x => x.id === tt.id) ?? tt)
        return
      }
      let final = out
      let copied: number[] = []
      if (targets.length > 0) {
        // The day is saved already: a failing copy must not strand it
        // without its Undo, nor keep a dialog based on a stale version.
        try {
          const res = await store.copyDay(tt.id, weekday, targets, withSubjects, { baseVersion: out.version })
          if (res) { final = res; copied = targets }
        } catch (e) {
          showToast((e as Error).message, 'error')
        }
      }
      const day = weekdayName(weekday, 'long')
      const message = copied.length
        ? t('timetable.builder.saved_copied', {
            days: [weekday, ...copied].map(d => weekdayName(d, 'short')).join(', '),
          })
        : t('timetable.builder.saved', { day })
      store.offerEntriesUndo(tt.id, previous, final.version, { message, onUndone: () => focusGrid(tt.id) })
      closeDayBuilder()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const first = timed[0]
  const last = timed[timed.length - 1]

  return (
    <form class="sh-form sh-timetable-builder" onSubmit={save} noValidate>
      <div class="sh-timetable-builder__params">
        <div>
          <label for={`${idp}-start`}>{t('timetable.builder.start')}</label>
          <input ref={startRef} id={`${idp}-start`} type="time" step={300} value={start}
                 onInput={(e) => setStart((e.target as HTMLInputElement).value)} />
        </div>
        <div>
          <label for={`${idp}-lesson`}>{t('timetable.builder.lesson_minutes')}</label>
          <input id={`${idp}-lesson`} type="number" min={5} max={240} step={5} inputMode="numeric"
                 value={lessonRaw}
                 onInput={(e) => onLessonMinutes((e.target as HTMLInputElement).value)} />
        </div>
        <div>
          <label for={`${idp}-gap`}>{t('timetable.builder.gap_minutes')}</label>
          <input id={`${idp}-gap`} type="number" min={0} max={120} step={5} inputMode="numeric"
                 value={gapRaw} onInput={(e) => setGapRaw((e.target as HTMLInputElement).value)} />
        </div>
      </div>

      <fieldset class="sh-timetable-builder__slots">
        <legend>{t('timetable.builder.slots')}</legend>
        {first && last && (
          <p class="sh-form-hint" aria-live="polite">
            {t(lessons === 1 ? 'timetable.builder.summary_one' : 'timetable.builder.summary', {
              n: String(lessons), range: formatRange(fromMinutes(first.start), fromMinutes(last.end)),
            })}
          </p>
        )}
        {canAddBefore && (
          <button type="button" class="sh-timetable-builder__before" onClick={onAddBefore}
                  title={t('timetable.builder.add_before_hint')}>
            {t('timetable.builder.add_before')}
          </button>
        )}
        <ol ref={listRef} class="sh-timetable-builder__list">
          {timed.map((r, i) => {
            const name = slotName(r)
            const errId = `${idp}-${r.key}-err`
            return (
              <li key={r.key} data-row={r.key}
                  class={`sh-timetable-builder__row sh-timetable-builder__row--${r.kind}${r.error ? ' is-invalid' : ''}`}>
                <span class="sh-timetable-builder__num" aria-hidden="true">{r.label ?? ''}</span>
                <span class="sh-timetable-builder__time">
                  {formatRange(fromMinutes(r.start), fromMinutes(r.end))}
                </span>
                <select class="sh-timetable-builder__kind" value={r.kind}
                        aria-label={t('timetable.builder.kind_of', { slot: name })}
                        onChange={(e) => setKind(r.key, (e.target as HTMLSelectElement).value as TimetableEntryKind)}>
                  <option value="lesson">{t('timetable.kind.lesson')}</option>
                  <option value="break">{t('timetable.kind.break')}</option>
                </select>
                <span class="sh-timetable-builder__min">
                  <input type="number" min={5} max={600} step={5} inputMode="numeric"
                         value={Number.isFinite(r.minutes) ? String(r.minutes) : ''}
                         aria-label={t('timetable.builder.minutes_of', { slot: name })}
                         aria-invalid={r.error ? 'true' : undefined}
                         aria-describedby={r.error ? errId : undefined}
                         onInput={(e) => {
                           const v = (e.target as HTMLInputElement).value
                           patchRow(r.key, { minutes: v === '' ? NaN : Number(v) })
                         }} />
                  <span aria-hidden="true">{t('timetable.entry.min_suffix')}</span>
                </span>
                <input class="sh-timetable-builder__title" value={r.title} maxLength={60}
                       aria-label={t('timetable.builder.title_of', { slot: name })}
                       placeholder={t(r.kind === 'break'
                         ? 'timetable.entry.break_placeholder' : 'timetable.builder.title_placeholder')}
                       onInput={(e) => patchRow(r.key, { title: (e.target as HTMLInputElement).value })} />
                <span class="sh-timetable-builder__actions">
                  <button type="button" class="sh-timetable-builder__icon" data-move="up"
                          disabled={i === 0} aria-label={t('timetable.builder.move_up', { slot: name })}
                          title={t('timetable.builder.move_up', { slot: name })}
                          onClick={() => move(i, -1, 'up')}>
                    <span aria-hidden="true">↑</span>
                  </button>
                  <button type="button" class="sh-timetable-builder__icon" data-move="down"
                          disabled={i === timed.length - 1}
                          aria-label={t('timetable.builder.move_down', { slot: name })}
                          title={t('timetable.builder.move_down', { slot: name })}
                          onClick={() => move(i, 1, 'down')}>
                    <span aria-hidden="true">↓</span>
                  </button>
                  <button type="button" class="sh-timetable-builder__icon" data-remove
                          aria-label={t('timetable.builder.remove', { slot: name })}
                          title={t('timetable.builder.remove', { slot: name })}
                          onClick={() => remove(i)}>
                    <span aria-hidden="true">✕</span>
                  </button>
                </span>
                {r.error && (
                  <p id={errId} class="sh-timetable-builder__err">{t(ERR_KEY[r.error])}</p>
                )}
              </li>
            )
          })}
        </ol>
        {rows.length === 0 && <p class="sh-form-hint">{t('timetable.builder.err_empty')}</p>}
        <div class="sh-timetable-builder__add">
          <button type="button" class="sh-chip sh-timetable-chip" onClick={() => add('lesson')}>
            {t('timetable.builder.add_lesson')}
          </button>
          <button type="button" class="sh-chip sh-timetable-chip" onClick={() => add('break')}>
            {t('timetable.builder.add_break')}
          </button>
        </div>
      </fieldset>

      <CopyTargets tt={tt} source={weekday} value={targets} onChange={setTargets}
                   withSubjects={withSubjects} onWithSubjects={setWithSubjects}
                   legend={t('timetable.builder.copy_legend')} id={idp} />

      <FormError id={`${idp}-err`} message={error} />

      <div class="sh-form-actions sh-timetable-entry__actions">
        <span class="sh-timetable-entry__spacer" />
        <Button type="button" variant="secondary" onClick={closeDayBuilder}>
          {t('timetable.cancel')}
        </Button>
        <Button type="submit" loading={saving}>{t('timetable.save')}</Button>
      </div>
    </form>
  )
}
