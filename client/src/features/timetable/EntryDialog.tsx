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
 */
import { signal } from '@preact/signals'
import { useRef, useState } from 'preact/hooks'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { t } from '@/i18n/i18n'
import {
  addEntry, deleteEntry, patchEntry, replaceEntries, timetables, type EntryInput,
} from '@/store/timetables'
import type { Timetable, TimetableColor, TimetableEntry, TimetableEntryKind } from '@/types'
import { ColorSwatches } from './ColorSwatches'
import { normalizeSubject } from './colors'
import { IconPicker } from './IconPicker'
import { suggestIcon } from './icons'
import { dayEntries, type EntryPrefill } from './layout'
import { fromMinutes, orderedDays, toMinutes, weekdayName } from './time'
import { focusGrid } from './focus'
import { useAutofocus } from './useAutofocus'

interface DialogState {
  timetableId: string
  entry: TimetableEntry | null
  prefill?: EntryPrefill
  /** A Periods break band: the ids of the same break on every day. */
  group?: string[]
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
  const [kind, setKind] = useState<TimetableEntryKind>(initial.kind)
  const [title, setTitle] = useState(initial.title ?? '')
  const [icon, setIcon] = useState<string | null>(initial.icon)
  const [iconManual, setIconManual] = useState(entry?.icon != null)
  const [start, setStart] = useState(initial.start)
  const [end, setEnd] = useState(initial.end)
  const [weekday, setWeekday] = useState(initial.weekday)
  const [room, setRoom] = useState(initial.room ?? '')
  const [teacher, setTeacher] = useState(initial.teacher ?? '')
  const [note, setNote] = useState(initial.note ?? '')
  const [color, setColor] = useState<TimetableColor | null>(initial.color)
  const [shift, setShift] = useState(true)
  const [applyAllChoice, setApplyAllChoice] = useState<boolean | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const titleRef = useRef<HTMLInputElement | null>(null)
  useAutofocus(titleRef, true)

  const startMin = start ? toMinutes(start) : NaN
  const endMin = end ? toMinutes(end) : NaN
  const length = endMin - startMin

  // Titles already used in this timetable — the datalist autocomplete.
  const knownTitles = [...new Set(tt.entries.map(e => e.title?.trim()).filter(Boolean))] as string[]

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

  const onTitle = (v: string) => {
    setTitle(v)
    if (!iconManual) setIcon(suggestIcon(v))
  }
  const onPickIcon = (v: string | null) => {
    setIcon(v)
    setIconManual(true)
  }
  const onStart = (v: string) => {
    // Moving the start keeps the length.
    if (v && start && end) setEnd(fromMinutes(toMinutes(v) + (toMinutes(end) - toMinutes(start))))
    setStart(v)
  }
  const setLength = (minutes: number) => {
    if (start && minutes > 0) setEnd(fromMinutes(toMinutes(start) + minutes))
  }

  const validate = (): string | null => {
    if (!start || !end) return t('timetable.entry.err_time')
    if (endMin <= startMin) return t('timetable.entry.err_order')
    if (length < 5) return t('timetable.entry.err_short')
    return null
  }

  const fields: Fields = {
    kind,
    title: clean(title),
    icon,
    start,
    end,
    room: clean(room),
    teacher: clean(teacher),
    note: clean(note),
    color,
  }

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
    const invalid = validate()
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
      <fieldset class="sh-timetable-kind">
        <legend class="sr-only">{t('timetable.kind.legend')}</legend>
        {(['lesson', 'break'] as const).map(k => (
          <label key={k} class={`sh-timetable-kind__opt${kind === k ? ' is-on' : ''}`}>
            <input type="radio" name={`${idp}-kind`} class="sh-timetable-kind__input"
                   checked={kind === k} onChange={() => setKind(k)} />
            {t(`timetable.kind.${k}`)}
          </label>
        ))}
      </fieldset>

      <div>
        <label for={`${idp}-title`}>{t('timetable.entry.title')}</label>
        <input
          ref={titleRef}
          id={`${idp}-title`}
          value={title}
          maxLength={60}
          list={`${idp}-titles`}
          autocomplete="off"
          placeholder={t(kind === 'break' ? 'timetable.entry.break_placeholder' : 'timetable.entry.title_placeholder')}
          onInput={(e) => onTitle((e.target as HTMLInputElement).value)}
        />
        <datalist id={`${idp}-titles`}>
          {knownTitles.map(s => <option key={s} value={s} />)}
        </datalist>
      </div>

      <IconPicker value={icon} onChange={onPickIcon} id={idp} />

      <div class="sh-timetable-entry__times">
        <div>
          <label for={`${idp}-start`}>{t('timetable.entry.start')}</label>
          <input id={`${idp}-start`} type="time" step={300} value={start}
                 onInput={(e) => onStart((e.target as HTMLInputElement).value)} />
        </div>
        <div>
          <label for={`${idp}-end`}>{t('timetable.entry.end')}</label>
          <input id={`${idp}-end`} type="time" step={300} value={end}
                 onInput={(e) => setEnd((e.target as HTMLInputElement).value)} />
        </div>
      </div>
      <fieldset class="sh-timetable-entry__length">
        <legend>{t('timetable.entry.duration')}</legend>
        <div class="sh-timetable-entry__chips">
          {DURATIONS.map(n => (
            <button key={n} type="button" aria-pressed={length === n}
                    class={`sh-chip sh-timetable-chip${length === n ? ' sh-chip--active' : ''}`}
                    onClick={() => setLength(n)}>
              {t('timetable.entry.minutes', { n: String(n) })}
            </button>
          ))}
          <label class="sh-timetable-entry__custom">
            <span class="sr-only">{t('timetable.entry.custom_minutes')}</span>
            <input type="number" min={5} max={600} step={5} inputMode="numeric"
                   value={Number.isFinite(length) && length > 0 ? String(length) : ''}
                   onInput={(e) => setLength(Number((e.target as HTMLInputElement).value))} />
            <span aria-hidden="true">{t('timetable.entry.min_suffix')}</span>
          </label>
        </div>
        {end && <p class="sh-form-hint" aria-live="polite">{t('timetable.entry.ends_at', { time: end })}</p>}
      </fieldset>

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

      <ColorSwatches name={`${idp}-color`} value={color} onChange={setColor}
                     noneLabel={t('timetable.color.auto')} />

      <details class="sh-timetable-entry__more" open={!!(initial.room || initial.teacher || initial.note)}>
        <summary>{t('timetable.entry.more')}</summary>
        <div class="sh-timetable-entry__more-body">
          <div>
            <label for={`${idp}-room`}>{t('timetable.entry.room')}</label>
            <input id={`${idp}-room`} value={room} maxLength={30}
                   onInput={(e) => setRoom((e.target as HTMLInputElement).value)} />
          </div>
          <div>
            <label for={`${idp}-teacher`}>{t('timetable.entry.teacher')}</label>
            <input id={`${idp}-teacher`} value={teacher} maxLength={60}
                   onInput={(e) => setTeacher((e.target as HTMLInputElement).value)} />
          </div>
          <div>
            <label for={`${idp}-note`}>{t('timetable.entry.note')}</label>
            <textarea id={`${idp}-note`} value={note} maxLength={200} rows={2}
                      onInput={(e) => setNote((e.target as HTMLTextAreaElement).value)} />
          </div>
        </div>
      </details>

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
