/**
 * EntryDialog — add or edit one lesson / break.
 *
 * Opened imperatively (``openEntryDialog``) by the grid, the day view
 * and the list. Beyond the plain fields it carries the two shortcuts
 * that make filling a Stundenplan quick:
 *
 *  * **Shift the following lessons** — when an edit moves the end time
 *    and later slots exist that day, they move by the same amount.
 *  * **Apply colour & icon to all "‹title›" lessons**.
 *
 * Both are built client-side into ONE atomic ``PUT …/entries`` (no
 * partial state if the server refuses; one Undo covers everything).
 * Opened from a Periods break band (``group``), the dialog edits that
 * break on every day at once, again as one PUT.
 *
 * The form snapshots the timetable at mount: it diffs against that
 * snapshot, sends only what changed, and passes the snapshot's version
 * as ``baseVersion`` — a change that landed while it was open 409s
 * (refetch + "reloaded") instead of being overwritten.
 *
 * The icon follows the title (``suggestIcon``) until the user picks
 * one by hand. Obvious mistakes are caught client-side; the server's
 * 422 message is shown inline under the form.
 *
 * **Week mode** (``week`` in the state — the Vertretungsplan): the
 * same fields (``useLessonFields`` / ``LessonFields``) edit ONE date.
 * Titled "Mathe · Mon, Oct 5", it offers *Cancel this lesson* (a
 * ``cancel`` override), *Change for this week* (a ``replace`` override
 * carrying only the fields that differ from the regular lesson — POST,
 * or PATCH when one exists) and *Restore regular* (DELETE the
 * override). With no lesson it adds an extra one for that date (an
 * ``add`` override). Each of those offers an Undo.
 */
import type { ComponentChildren, RefObject } from 'preact'
import { signal } from '@preact/signals'
import { useRef, useState } from 'preact/hooks'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { t } from '@/i18n/i18n'
import {
  addEntry, addOverride, applyOverrides, deleteEntry, deleteOverride, patchEntry, patchOverride,
  replaceEntries, timetables, type EntryInput, type OverrideStep, type UndoOpts,
} from '@/store/timetables'
import type {
  EffectiveLesson, Timetable, TimetableColor, TimetableEntry, TimetableEntryKind,
} from '@/types'
import { ColorSwatches } from './ColorSwatches'
import { normalizeSubject } from './colors'
import { weekdayDate } from './dates'
import { IconPicker } from './IconPicker'
import { suggestIcon } from './icons'
import { displayTitle } from './labels'
import { dayEntries, type EntryPrefill } from './layout'
import { fromMinutes, orderedDays, toMinutes, weekdayName } from './time'
import { focusGrid } from './focus'
import { useAutofocus } from './useAutofocus'

/** Week mode: the date being changed, and the effective lesson there
 *  (``null`` = add an extra lesson on that date). */
export interface WeekTarget {
  date: string
  lesson: EffectiveLesson | null
  /** A merged double lesson: every lesson of the run (``lesson`` is the
   *  first). Cancel / Change / Restore act on each, one override per
   *  lesson, under one Undo. */
  run?: EffectiveLesson[]
}

interface DialogState {
  timetableId: string
  entry: TimetableEntry | null
  prefill?: EntryPrefill
  /** A Periods break band: the ids of the same break on every day. */
  group?: string[]
  week?: WeekTarget
}

export const entryDialog = signal<DialogState | null>(null)

export function openEntryDialog(state: DialogState): void {
  entryDialog.value = state
}

export function closeEntryDialog(): void {
  entryDialog.value = null
}

const DURATIONS = [30, 45, 60, 90] as const

export function EntryDialog() {
  const state = entryDialog.value
  const tt = state ? timetables.value.find(x => x.id === state.timetableId) : undefined
  if (!state || !tt) return null
  if (state.week) {
    const { date, lesson } = state.week
    const what = lesson
      ? displayTitle(lesson) || t('timetable.aria.empty_slot')
      : t('timetable.week.new_title')
    return (
      <Modal open onClose={closeEntryDialog} title={`${what} · ${weekdayDate(date)}`}>
        <WeekEntryForm key={`${tt.id}:${date}:${lesson?.source_id ?? 'new'}:${state.prefill?.start}`}
                       tt={tt} target={state.week} prefill={state.prefill} />
      </Modal>
    )
  }
  const title = state.group
    ? t('timetable.entry.edit_band_title')
    : state.entry
      ? t(state.entry.kind === 'break' ? 'timetable.entry.edit_break_title' : 'timetable.entry.edit_title')
      : t('timetable.entry.new_title')
  return (
    <Modal open onClose={closeEntryDialog} title={title}>
      <EntryForm
        key={`${tt.id}:${state.entry?.id ?? 'new'}:${state.prefill?.weekday}:${state.prefill?.start}`}
        tt={tt}
        entry={state.entry}
        prefill={state.prefill}
        group={state.group}
      />
    </Modal>
  )
}

const clean = (s: string) => (s.trim() === '' ? null : s.trim())

type Fields = Pick<TimetableEntry,
  'kind' | 'title' | 'icon' | 'start' | 'end' | 'room' | 'teacher' | 'note' | 'color'>

/** The fields of ``next`` that differ from ``prev``. */
function changedFields(next: Fields, prev: TimetableEntry): Partial<Fields> {
  const out: Partial<Fields> = {}
  for (const k of Object.keys(next) as (keyof Fields)[]) {
    if (next[k] !== prev[k]) (out as Record<string, unknown>)[k] = next[k]
  }
  return out
}

const shiftBy = (hhmm: string, minutes: number) => fromMinutes(toMinutes(hhmm) + minutes)

function EntryForm({ tt: live, entry, prefill, group }: {
  tt: Timetable
  entry: TimetableEntry | null
  prefill?: EntryPrefill
  group?: string[]
}) {
  // Everything below reads the snapshot taken at mount, not the live
  // store copy — see the module docstring (CAS on open dialogs).
  const [tt, setTt] = useState(live)
  const initial = entry ?? {
    weekday: prefill?.weekday ?? orderedDays(tt.days, tt.week_start)[0],
    start: prefill?.start ?? tt.defaults.day_start,
    end: prefill?.end ?? fromMinutes(toMinutes(tt.defaults.day_start) + tt.defaults.lesson_minutes),
    kind: 'lesson' as TimetableEntryKind,
    title: null, room: null, teacher: null, note: null, color: null, icon: null,
  }
  const f = useLessonFields(initial, entry?.icon != null)
  const { title, icon, end, color, endMin } = f
  const [weekday, setWeekday] = useState(initial.weekday)
  const [shift, setShift] = useState(true)
  const [applyAllChoice, setApplyAllChoice] = useState<boolean | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const titleRef = useRef<HTMLInputElement | null>(null)
  useAutofocus(titleRef, true)

  // "Shift the following lessons": an edit whose end moved, with later
  // slots on the same day.
  const shiftDelta = entry && end ? endMin - toMinutes(entry.end) : 0
  const later = entry
    ? dayEntries(tt, entry.weekday).filter(e => e.id !== entry.id
        && toMinutes(e.start) >= toMinutes(entry.end))
    : []
  const showShift = !group && entry !== null && shiftDelta !== 0 && later.length > 0

  // "Apply colour & icon to all ‹title› lessons".
  const subject = normalizeSubject(title)
  const sameSubject = subject
    ? tt.entries.filter(e => e.id !== entry?.id && normalizeSubject(e.title ?? '') === subject)
    : []
  const styleChanged = color !== (entry?.color ?? null) || icon !== (entry?.icon ?? null)
  const applyAll = !group && sameSubject.length > 0 && (applyAllChoice ?? styleChanged)

  const fields = f.fields()

  const opts = { baseVersion: tt.version }
  const onUndone = () => focusGrid(tt.id)
  // ``null`` = a 409 was reloaded: rebase onto the fresh copy so the
  // next Save applies these edits to it (the user keeps their input).
  const done = (ok: unknown) => {
    if (ok) closeEntryDialog()
    else setTt(timetables.value.find(x => x.id === tt.id) ?? tt)
  }

  const save = async (ev: Event) => {
    ev.preventDefault()
    const invalid = f.validate()
    setError(invalid)
    if (invalid) return
    const changes = entry ? changedFields(fields, entry) : fields
    if (entry && Object.keys(changes).length === 0) { closeEntryDialog(); return }
    setSaving(true)
    try {
      const doShift = showShift && shift
      if (group) {
        const ids = new Set(group)
        const list: EntryInput[] = tt.entries.map(e => ids.has(e.id) ? { ...e, ...changes } : e)
        done(await replaceEntries(tt.id, list, {
          ...opts, undo: { message: t('timetable.entry.band_updated'), onUndone },
        }))
      } else if (doShift || applyAll) {
        const laterIds = new Set(later.map(e => e.id))
        const subjectIds = new Set(sameSubject.map(e => e.id))
        const list: EntryInput[] = tt.entries.map(e => {
          if (entry && e.id === entry.id) return { ...e, ...fields }
          let out = e
          if (applyAll && subjectIds.has(e.id)) out = { ...out, color, icon }
          if (doShift && laterIds.has(e.id)) {
            out = { ...out, start: shiftBy(e.start, shiftDelta), end: shiftBy(e.end, shiftDelta) }
          }
          return out
        })
        if (!entry) list.push({ weekday, label: null, ...fields })
        const message = doShift
          ? t('timetable.entry.shifted')
          : t('timetable.entry.applied_all', { title: title.trim() })
        done(await replaceEntries(tt.id, list, { ...opts, undo: { message, onUndone } }))
      } else if (entry) {
        done(await patchEntry(tt.id, entry.id, changes, opts))
      } else {
        done(await addEntry(tt.id, { weekday, ...fields }, opts))
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!entry) return
    setSaving(true)
    try {
      const out = group
        ? await replaceEntries(tt.id, tt.entries.filter(e => !group.includes(e.id)), {
            ...opts, undo: { message: t('timetable.entry.band_deleted'), onUndone },
          })
        : await deleteEntry(tt.id, entry.id, { ...opts, onUndone })
      if (out) {
        closeEntryDialog()
        focusGrid(tt.id)
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const idp = `sh-tt-entry-${entry?.id ?? 'new'}`
  const errId = `${idp}-err`

  return (
    <form class="sh-form sh-timetable-entry" onSubmit={save} noValidate>
      <LessonFields f={f} idp={idp} titleRef={titleRef} knownTitles={knownTitles(tt)}
                    openMore={!!(initial.room || initial.teacher || initial.note)}>
        {!entry && (
          <div>
            <label for={`${idp}-day`}>{t('timetable.entry.weekday')}</label>
            <select id={`${idp}-day`} value={String(weekday)}
                    onChange={(e) => setWeekday(Number((e.target as HTMLSelectElement).value))}>
              {orderedDays(tt.days, tt.week_start).map(d => (
                <option key={d} value={String(d)}>{weekdayName(d, 'long')}</option>
              ))}
            </select>
          </div>
        )}
      </LessonFields>

      {showShift && (
        <label class="sh-timetable-check">
          <input type="checkbox" checked={shift}
                 onChange={(e) => setShift((e.target as HTMLInputElement).checked)} />
          <span>{t('timetable.entry.shift_following', {
            n: `${shiftDelta > 0 ? '+' : '−'}${Math.abs(shiftDelta)}`,
          })}</span>
        </label>
      )}
      {sameSubject.length > 0 && (
        <label class="sh-timetable-check">
          <input type="checkbox" checked={applyAll}
                 onChange={(e) => setApplyAllChoice((e.target as HTMLInputElement).checked)} />
          <span>{t('timetable.entry.apply_all', { title: title.trim() })}</span>
        </label>
      )}

      <FormError id={errId} message={error} />

      <div class="sh-form-actions sh-timetable-entry__actions">
        {entry && (
          <Button type="button" variant="danger" onClick={remove} disabled={saving}>
            {t('timetable.entry.delete')}
          </Button>
        )}
        <span class="sh-timetable-entry__spacer" />
        <Button type="button" variant="secondary" onClick={closeEntryDialog}>
          {t('timetable.cancel')}
        </Button>
        <Button type="submit" loading={saving}>{t('timetable.save')}</Button>
      </div>
    </form>
  )
}

// ─── Shared lesson fields (regular + week mode) ──────────────────────

/** Titles already used in this timetable — the datalist autocomplete. */
function knownTitles(tt: Timetable): string[] {
  return [...new Set(tt.entries.map(e => e.title?.trim()).filter(Boolean))] as string[]
}

type Initial = Pick<TimetableEntry,
  'kind' | 'title' | 'icon' | 'start' | 'end' | 'room' | 'teacher' | 'note' | 'color'>

/** State of the lesson fields both forms share, with the small
 *  behaviours that come with them: the icon follows the title until
 *  picked by hand, moving the start keeps the length, duration chips
 *  set the end. */
function useLessonFields(initial: Initial, iconPicked: boolean) {
  const [kind, setKind] = useState<TimetableEntryKind>(initial.kind)
  const [title, setTitle] = useState(initial.title ?? '')
  const [icon, setIcon] = useState<string | null>(initial.icon)
  const [iconManual, setIconManual] = useState(iconPicked)
  const [start, setStart] = useState(initial.start)
  const [end, setEnd] = useState(initial.end)
  const [room, setRoom] = useState(initial.room ?? '')
  const [teacher, setTeacher] = useState(initial.teacher ?? '')
  const [note, setNote] = useState(initial.note ?? '')
  const [color, setColor] = useState<TimetableColor | null>(initial.color)
  const startMin = start ? toMinutes(start) : NaN
  const endMin = end ? toMinutes(end) : NaN
  const length = endMin - startMin
  return {
    kind, setKind, title, icon, start, end, room, setRoom, teacher, setTeacher,
    note, setNote, color, setColor, setEnd, startMin, endMin, length,
    onTitle: (v: string) => {
      setTitle(v)
      if (!iconManual) setIcon(suggestIcon(v))
    },
    onPickIcon: (v: string | null) => {
      setIcon(v)
      setIconManual(true)
    },
    onStart: (v: string) => {
      if (v && start && end) setEnd(fromMinutes(toMinutes(v) + (toMinutes(end) - toMinutes(start))))
      setStart(v)
    },
    setLength: (minutes: number) => {
      if (start && minutes > 0) setEnd(fromMinutes(toMinutes(start) + minutes))
    },
    validate: (): string | null => {
      if (!start || !end) return t('timetable.entry.err_time')
      if (endMin <= startMin) return t('timetable.entry.err_order')
      if (length < 5) return t('timetable.entry.err_short')
      return null
    },
    fields: (): Fields => ({
      kind,
      title: clean(title),
      icon,
      start,
      end,
      room: clean(room),
      teacher: clean(teacher),
      note: clean(note),
      color,
    }),
  }
}

type LessonFieldState = ReturnType<typeof useLessonFields>

type TextField = 'title' | 'room' | 'teacher' | 'note'
/** Week mode: the regular values, shown where a field is left empty. */
type Placeholders = Partial<Record<TextField, string>>

function LessonFields({
  f, idp, titleRef, knownTitles, openMore, showKind = true, placeholders, children,
}: {
  f: LessonFieldState
  idp: string
  titleRef: RefObject<HTMLInputElement>
  knownTitles: string[]
  openMore: boolean
  showKind?: boolean
  placeholders?: Placeholders
  /** Rendered after the times (the regular form's weekday picker). */
  children?: ComponentChildren
}) {
  const { kind, length, end } = f
  return (
    <>
      {showKind && (
        <fieldset class="sh-timetable-kind">
          <legend class="sr-only">{t('timetable.kind.legend')}</legend>
          {(['lesson', 'break'] as const).map(k => (
            <label key={k} class={`sh-timetable-kind__opt${kind === k ? ' is-on' : ''}`}>
              <input type="radio" name={`${idp}-kind`} class="sh-timetable-kind__input"
                     checked={kind === k} onChange={() => f.setKind(k)} />
              {t(`timetable.kind.${k}`)}
            </label>
          ))}
        </fieldset>
      )}

      <div>
        <label for={`${idp}-title`}>{t('timetable.entry.title')}</label>
        <input
          ref={titleRef}
          id={`${idp}-title`}
          value={f.title}
          maxLength={60}
          list={`${idp}-titles`}
          autocomplete="off"
          placeholder={placeholders?.title
            ?? t(kind === 'break' ? 'timetable.entry.break_placeholder' : 'timetable.entry.title_placeholder')}
          onInput={(e) => f.onTitle((e.target as HTMLInputElement).value)}
        />
        <datalist id={`${idp}-titles`}>
          {knownTitles.map(s => <option key={s} value={s} />)}
        </datalist>
      </div>

      <IconPicker value={f.icon} onChange={f.onPickIcon} id={idp} />

      <div class="sh-timetable-entry__times">
        <div>
          <label for={`${idp}-start`}>{t('timetable.entry.start')}</label>
          <input id={`${idp}-start`} type="time" step={300} value={f.start}
                 onInput={(e) => f.onStart((e.target as HTMLInputElement).value)} />
        </div>
        <div>
          <label for={`${idp}-end`}>{t('timetable.entry.end')}</label>
          <input id={`${idp}-end`} type="time" step={300} value={end}
                 onInput={(e) => f.setEnd((e.target as HTMLInputElement).value)} />
        </div>
      </div>
      <fieldset class="sh-timetable-entry__length">
        <legend>{t('timetable.entry.duration')}</legend>
        <div class="sh-timetable-entry__chips">
          {DURATIONS.map(n => (
            <button key={n} type="button" aria-pressed={length === n}
                    class={`sh-chip sh-timetable-chip${length === n ? ' sh-chip--active' : ''}`}
                    onClick={() => f.setLength(n)}>
              {t('timetable.entry.minutes', { n: String(n) })}
            </button>
          ))}
          <label class="sh-timetable-entry__custom">
            <span class="sr-only">{t('timetable.entry.custom_minutes')}</span>
            <input type="number" min={5} max={600} step={5} inputMode="numeric"
                   value={Number.isFinite(length) && length > 0 ? String(length) : ''}
                   onInput={(e) => f.setLength(Number((e.target as HTMLInputElement).value))} />
            <span aria-hidden="true">{t('timetable.entry.min_suffix')}</span>
          </label>
        </div>
        {end && <p class="sh-form-hint" aria-live="polite">{t('timetable.entry.ends_at', { time: end })}</p>}
      </fieldset>

      {children}

      <ColorSwatches name={`${idp}-color`} value={f.color} onChange={f.setColor}
                     noneLabel={t('timetable.color.auto')} />

      <details class="sh-timetable-entry__more" open={openMore}>
        <summary>{t('timetable.entry.more')}</summary>
        <div class="sh-timetable-entry__more-body">
          <div>
            <label for={`${idp}-room`}>{t('timetable.entry.room')}</label>
            <input id={`${idp}-room`} value={f.room} maxLength={30} placeholder={placeholders?.room}
                   onInput={(e) => f.setRoom((e.target as HTMLInputElement).value)} />
          </div>
          <div>
            <label for={`${idp}-teacher`}>{t('timetable.entry.teacher')}</label>
            <input id={`${idp}-teacher`} value={f.teacher} maxLength={60} placeholder={placeholders?.teacher}
                   onInput={(e) => f.setTeacher((e.target as HTMLInputElement).value)} />
          </div>
          <div>
            <label for={`${idp}-note`}>{t('timetable.entry.note')}</label>
            <textarea id={`${idp}-note`} value={f.note} maxLength={200} rows={2} placeholder={placeholders?.note}
                      onInput={(e) => f.setNote((e.target as HTMLTextAreaElement).value)} />
          </div>
        </div>
      </details>
    </>
  )
}

// ─── Week mode ───────────────────────────────────────────────────────

/** The fields a ``replace`` override may carry (``label`` aside). */
const REPLACEABLE = ['title', 'icon', 'start', 'end', 'room', 'teacher', 'note', 'color'] as const
type Replaceable = typeof REPLACEABLE[number]

/** Fields of ``next`` that differ from the regular ``base`` entry. An
 *  empty field (``null``) means "as usual" — the form shows the regular
 *  value as its placeholder — so it is never sent. */
// Known limit — colour: a ``replace`` override's ``color: null`` means
// "as usual", so in week mode the "Automatic" swatch keeps the regular
// colour; it can't switch a lesson with an explicit colour back to its
// automatic subject colour for one week (pick another swatch instead).
// Same for icon ``null`` ("No icon" keeps the regular icon).
function overrideDiff(next: Fields, base: TimetableEntry): Partial<Pick<Fields, Replaceable>> {
  const out: Partial<Record<Replaceable, unknown>> = {}
  for (const k of REPLACEABLE) {
    if (next[k] !== null && next[k] !== base[k]) out[k] = next[k]
  }
  return out as Partial<Pick<Fields, Replaceable>>
}

const allNull = Object.fromEntries(REPLACEABLE.map(k => [k, null])) as Record<Replaceable, null>

function WeekEntryForm({ tt: live, target, prefill }: {
  tt: Timetable
  target: WeekTarget
  prefill?: EntryPrefill
}) {
  const [tt, setTt] = useState(live)
  const { date, lesson } = target
  const runLessons = target.run && target.run.length > 1 ? target.run : null
  const regularOf = (l: EffectiveLesson) => tt.entries.find(e => e.id === l.source_id) ?? l.original
  const status = lesson?.status ?? null
  const overrideId = lesson?.override_id ?? null
  // The regular slot this date's lesson comes from (none for an extra).
  const regular = lesson && status !== 'added'
    ? (tt.entries.find(e => e.id === lesson.source_id) ?? lesson.original)
    : null
  // A replace override can't blank a field (null = "as usual"), so the
  // text fields of a regular lesson start empty with the regular value
  // as the placeholder, and only what is typed in is sent. A value the
  // override already changed shows as the value.
  const own = (k: TextField) => (regular && lesson && lesson[k] === regular[k] ? null : lesson?.[k] ?? null)
  const initial: Initial = lesson
    ? {
        kind: lesson.kind, title: own('title'), icon: lesson.icon, start: lesson.start,
        end: runLessons ? runLessons[runLessons.length - 1].end : lesson.end, room: own('room'), teacher: own('teacher'), note: own('note'),
        color: lesson.color,
      }
    : {
        kind: 'lesson', title: null, icon: null, color: null, room: null, teacher: null, note: null,
        start: prefill?.start ?? tt.defaults.day_start,
        end: prefill?.end ?? fromMinutes(toMinutes(tt.defaults.day_start) + tt.defaults.lesson_minutes),
      }
  // The icon of an existing lesson only changes when picked by hand.
  const f = useLessonFields(initial, lesson !== null)
  const usual = (v: string | null | undefined) => (v ? t('timetable.week.as_usual', { value: v }) : undefined)
  const placeholders: Placeholders | undefined = regular ? {
    title: usual(regular.title),
    room: usual(regular.room ? `${t('timetable.entry.room')} ${regular.room}` : null),
    teacher: usual(regular.teacher),
    note: usual(regular.note),
  } : undefined
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const titleRef = useRef<HTMLInputElement | null>(null)
  useAutofocus(titleRef, true)
  const day = weekdayDate(date)
  const opts = { baseVersion: tt.version }
  const undo = (message: string): UndoOpts => ({ message, onUndone: () => focusGrid(tt.id) })

  const run = async (call: () => Promise<Timetable | null>) => {
    setSaving(true)
    setError(null)
    try {
      const out = await call()
      if (out) closeEntryDialog()
      else setTt(timetables.value.find(x => x.id === tt.id) ?? tt)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const save = (ev: Event) => {
    ev.preventDefault()
    const invalid = f.validate()
    setError(invalid)
    if (invalid) return
    const fields = f.fields()
    if (!lesson) {
      const { kind, ...rest } = fields
      void run(() => addOverride(tt.id, { date, kind: 'add', entry_kind: kind, ...rest },
        { ...opts, undo: undo(t('timetable.week.added', { date: day })) }))
      return
    }
    if (status === 'added' && overrideId) {
      const { kind, ...rest } = fields
      void run(() => patchOverride(tt.id, overrideId, { entry_kind: kind, ...rest },
        { ...opts, undo: undo(t('timetable.week.changed_toast', { date: day })) }))
      return
    }
    if (runLessons) {
      // Each half against its own regular slot; a new start applies to
      // the first half, a new end to the last.
      const last = runLessons.length - 1
      const ops: OverrideStep[] = runLessons.flatMap((l, i): OverrideStep[] => {
        const reg = regularOf(l)
        if (!reg) return []
        // Times the half doesn't own compare equal, so they drop out.
        const d = overrideDiff({ ...fields, start: i === 0 ? fields.start : reg.start,
          end: i === last ? fields.end : reg.end }, reg)
        const any = Object.keys(d).length > 0
        if (l.override_id) {
          return any ? [{ op: 'patch', overrideId: l.override_id, fields: { kind: 'replace', ...allNull, ...d } }] : []
        }
        return any ? [{ op: 'add', fields: { date, kind: 'replace', entry_id: reg.id, ...d } }] : []
      })
      if (ops.length === 0) { closeEntryDialog(); return }
      void run(() => applyOverrides(tt.id, ops,
        { ...opts, undo: undo(t('timetable.week.changed_toast', { date: day })) }))
      return
    }
    if (!regular) return
    const diff = overrideDiff(fields, regular)
    const changed = Object.keys(diff).length > 0
    if (!overrideId) {
      if (!changed) { closeEntryDialog(); return }
      void run(() => addOverride(tt.id, { date, kind: 'replace', entry_id: regular.id, ...diff },
        { ...opts, undo: undo(t('timetable.week.changed_toast', { date: day })) }))
      return
    }
    // A changed lesson edited back to its regular fields: drop the
    // override. (A cancelled one stays cancelled — "Restore regular"
    // is the explicit way back.)
    if (!changed) {
      if (status === 'changed') restore()
      else closeEntryDialog()
      return
    }
    void run(() => patchOverride(tt.id, overrideId, { kind: 'replace', ...allNull, ...diff },
      { ...opts, undo: undo(t('timetable.week.changed_toast', { date: day })) }))
  }

  const cancelLesson = () => {
    if (!regular) return
    const message = t('timetable.week.cancelled_toast', { date: day })
    if (runLessons) {
      const ops = runLessons.flatMap((l): OverrideStep[] => {
        const reg = regularOf(l)
        if (!reg || l.status === 'cancelled') return []
        return [l.override_id
          ? { op: 'patch', overrideId: l.override_id, fields: { kind: 'cancel', ...allNull, label: null } }
          : { op: 'add', fields: { date, kind: 'cancel', entry_id: reg.id } }]
      })
      void run(() => applyOverrides(tt.id, ops, { ...opts, undo: undo(message) }))
      return
    }
    void run(() => overrideId
      ? patchOverride(tt.id, overrideId, { kind: 'cancel', ...allNull, label: null },
          { ...opts, undo: undo(message) })
      : addOverride(tt.id, { date, kind: 'cancel', entry_id: regular.id },
          { ...opts, undo: undo(message) }))
  }

  const restore = () => {
    if (!overrideId) return
    const message = status === 'added'
      ? t('timetable.week.removed_toast', { date: day })
      : t('timetable.week.restored_toast', { date: day })
    if (runLessons) {
      const ops = runLessons.flatMap((l): OverrideStep[] =>
        l.override_id ? [{ op: 'delete', overrideId: l.override_id }] : [])
      void run(() => applyOverrides(tt.id, ops, { ...opts, undo: undo(message) }))
      return
    }
    void run(() => deleteOverride(tt.id, overrideId, { ...opts, undo: undo(message) }))
  }

  const idp = `sh-tt-week-${lesson?.source_id ?? 'new'}-${date}`
  const regularText = regular
    ? `${displayTitle(regular) || t('timetable.aria.empty_slot')} · ${regular.start}–${regular.end}`
      + (regular.room ? ` · ${t('timetable.aria.room', { room: regular.room })}` : '')
    : null

  return (
    <form class="sh-form sh-timetable-entry sh-timetable-entry--week" onSubmit={save} noValidate>
      {status === 'cancelled' && (
        <p class="sh-timetable-week-note sh-timetable-week-note--cancelled" role="status">
          {t('timetable.week.is_cancelled')}
        </p>
      )}
      {regularText && status !== 'normal' && (
        <p class="sh-form-hint">{t('timetable.week.usually', { what: regularText })}</p>
      )}
      {/* The one-tap actions first: "sick today / lesson cancelled" is
       *  the common case, a field-by-field change the rarer one. */}
      {(overrideId || (lesson && status !== 'cancelled')) && (
        <div class="sh-timetable-week-actions">
          {lesson && status !== 'added' && status !== 'cancelled' && (
            <Button type="button" variant="danger" onClick={cancelLesson} disabled={saving}>
              {t('timetable.week.cancel_lesson')}
            </Button>
          )}
          {overrideId && (
            <Button type="button" variant={status === 'added' ? 'danger' : 'secondary'}
                    onClick={restore} disabled={saving}>
              {t(status === 'added' ? 'timetable.week.remove_extra' : 'timetable.week.restore')}
            </Button>
          )}
        </div>
      )}
      {lesson && status !== 'added' && (
        <h3 class="sh-timetable-week-change">{t('timetable.week.or_change')}</h3>
      )}
      <LessonFields f={f} idp={idp} titleRef={titleRef} knownTitles={knownTitles(tt)}
                    openMore={!!(initial.room || initial.teacher || initial.note
                      || regular?.room || regular?.teacher || regular?.note)}
                    showKind={!lesson || status === 'added'} placeholders={placeholders} />
      {placeholders && <p class="sh-form-hint">{t('timetable.week.empty_keeps')}</p>}
      <p class="sh-form-hint">{t(lesson ? 'timetable.week.only_this_week' : 'timetable.week.extra_hint',
        { date: day })}</p>

      <FormError id={`${idp}-err`} message={error} />

      <div class="sh-form-actions sh-timetable-entry__actions">
        <span class="sh-timetable-entry__spacer" />
        <Button type="button" variant="secondary" onClick={closeEntryDialog}>
          {t('timetable.close')}
        </Button>
        <Button type="submit" loading={saving}>
          {t(!lesson ? 'timetable.week.add_submit'
            : status === 'added' ? 'timetable.save' : 'timetable.week.change_submit')}
        </Button>
      </div>
    </form>
  )
}
