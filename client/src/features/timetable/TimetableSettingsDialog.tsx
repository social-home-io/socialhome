/**
 * TimetableSettingsDialog — edit a timetable's header: name, colour,
 * assignees, days (removing a day that still has lessons asks first,
 * via the store's orphan-confirm flow), week start, the defaults new
 * lessons use, and — tucked under "Advanced" — the time zone.
 * The form snapshots the timetable at mount and sends only the fields
 * that changed against it, based on the snapshot's version — a change
 * that landed meanwhile 409s (refetch + "reloaded") instead of being
 * overwritten; the form then rebases onto the fresh copy.
 */
import { signal } from '@preact/signals'
import { useState } from 'preact/hooks'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { t } from '@/i18n/i18n'
import { patchHeader, timetables, type HeaderPatch } from '@/store/timetables'
import type { Timetable, TimetableColor } from '@/types'
import type { WeekStart } from '@/utils/week'
import { AssigneePicker } from './AssigneePicker'
import { ColorSwatches } from './ColorSwatches'
import { DaysPicker } from './DaysPicker'
import { WeekStartField } from './WeekStartField'

const settingsFor = signal<string | null>(null)

export function openSettingsDialog(id: string): void {
  settingsFor.value = id
}

export function closeSettingsDialog(): void {
  settingsFor.value = null
}

function timeZones(): string[] {
  try {
    const intl = Intl as unknown as { supportedValuesOf?: (k: string) => string[] }
    return intl.supportedValuesOf?.('timeZone') ?? []
  } catch {
    return []
  }
}

export function TimetableSettingsDialog() {
  const tt = timetables.value.find(x => x.id === settingsFor.value)
  if (!tt) return null
  return (
    <Modal open onClose={closeSettingsDialog} title={t('timetable.settings.title')}>
      <SettingsForm key={tt.id} tt={tt} />
    </Modal>
  )
}

const sameList = (a: readonly unknown[], b: readonly unknown[]) =>
  a.length === b.length && a.every((x, i) => x === b[i])

/** ``"45"`` → 45; empty / fractional / out-of-range → ``null``. */
function minutesIn(raw: string, min: number, max: number): number | null {
  if (raw.trim() === '') return null
  const n = Number(raw)
  return Number.isInteger(n) && n >= min && n <= max ? n : null
}

function SettingsForm({ tt: live }: { tt: Timetable }) {
  const [tt, setTt] = useState(live)
  const [name, setName] = useState(tt.name)
  const [color, setColor] = useState<TimetableColor | null>(tt.color)
  const [assignees, setAssignees] = useState<string[]>(tt.assignees)
  const [days, setDays] = useState<number[]>(tt.days)
  const [weekStart, setWeekStart] = useState<WeekStart>(tt.week_start)
  const [tz, setTz] = useState(tt.tz)
  const [lesson, setLesson] = useState(String(tt.defaults.lesson_minutes))
  const [gap, setGap] = useState(String(tt.defaults.gap_minutes))
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const submit = async (ev: Event) => {
    ev.preventDefault()
    if (!name.trim()) { setError(t('timetable.name_required')); return }
    const lessonMin = minutesIn(lesson, 5, 240)
    if (lessonMin === null) { setError(t('timetable.settings.err_lesson')); return }
    const gapMin = minutesIn(gap, 0, 120)
    if (gapMin === null) { setError(t('timetable.settings.err_gap')); return }
    const patch: HeaderPatch = {}
    if (name.trim() !== tt.name) patch.name = name.trim()
    if (color !== tt.color) patch.color = color
    if (!sameList(assignees, tt.assignees)) patch.assignees = assignees
    if (!sameList([...days].sort(), [...tt.days].sort())) patch.days = [...days].sort((a, b) => a - b)
    if (weekStart !== tt.week_start) patch.week_start = weekStart
    if (tz.trim() && tz.trim() !== tt.tz) patch.tz = tz.trim()
    const defaults = {
      ...tt.defaults,
      lesson_minutes: lessonMin,
      gap_minutes: gapMin,
    }
    if (defaults.lesson_minutes !== tt.defaults.lesson_minutes
        || defaults.gap_minutes !== tt.defaults.gap_minutes) patch.defaults = defaults
    if (Object.keys(patch).length === 0) { closeSettingsDialog(); return }
    setError(null)
    setSaving(true)
    try {
      if (await patchHeader(tt.id, patch, { baseVersion: tt.version })) closeSettingsDialog()
      else setTt(timetables.value.find(x => x.id === tt.id) ?? tt)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const zones = timeZones()
  return (
    <form class="sh-form sh-timetable-form" onSubmit={submit} noValidate>
      <div>
        <label for="sh-tt-set-name">{t('timetable.create.name')}</label>
        <input id="sh-tt-set-name" value={name} maxLength={60} required
               onInput={(e) => setName((e.target as HTMLInputElement).value)} />
      </div>
      <ColorSwatches name="sh-tt-set-color" value={color} onChange={setColor}
                     noneLabel={t('timetable.color.none')} />
      <AssigneePicker value={assignees} onChange={setAssignees}
                      legend={t('timetable.create.assignees')} />
      <DaysPicker value={days} onChange={setDays} weekStart={weekStart} />
      <WeekStartField name="sh-tt-set-ws" value={weekStart} onChange={setWeekStart} />
      <fieldset class="sh-timetable-defaults">
        <legend>{t('timetable.settings.defaults_hint')}</legend>
        <div class="sh-timetable-entry__times">
          <div>
            <label for="sh-tt-set-lesson">{t('timetable.settings.lesson_minutes')}</label>
            <input id="sh-tt-set-lesson" type="number" min={5} max={240} step={5}
                   inputMode="numeric" value={lesson}
                   onInput={(e) => setLesson((e.target as HTMLInputElement).value)} />
          </div>
          <div>
            <label for="sh-tt-set-gap">{t('timetable.settings.gap_minutes')}</label>
            <input id="sh-tt-set-gap" type="number" min={0} max={120} step={5}
                   inputMode="numeric" value={gap}
                   onInput={(e) => setGap((e.target as HTMLInputElement).value)} />
          </div>
        </div>
      </fieldset>
      <details class="sh-timetable-entry__more">
        <summary>{t('timetable.settings.advanced')}</summary>
        <div class="sh-timetable-entry__more-body">
          <div>
            <label for="sh-tt-set-tz">{t('timetable.settings.tz')}</label>
            <input id="sh-tt-set-tz" value={tz} list="sh-tt-set-tz-list" autocomplete="off"
                   aria-describedby="sh-tt-set-tz-hint"
                   onInput={(e) => setTz((e.target as HTMLInputElement).value)} />
            <datalist id="sh-tt-set-tz-list">
              {zones.map(z => <option key={z} value={z} />)}
            </datalist>
            <span id="sh-tt-set-tz-hint" class="sh-form-hint">{t('timetable.settings.tz_hint')}</span>
          </div>
        </div>
      </details>
      <FormError id="sh-tt-set-err" message={error} />
      <div class="sh-form-actions">
        <Button type="button" variant="secondary" onClick={closeSettingsDialog}>
          {t('timetable.cancel')}
        </Button>
        <Button type="submit" loading={saving}>{t('timetable.save')}</Button>
      </div>
    </form>
  )
}
