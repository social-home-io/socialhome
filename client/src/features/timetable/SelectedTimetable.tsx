/**
 * SelectedTimetable — the chosen timetable: its header, the Regular |
 * This week switch, and the grid.
 *
 * Regular mode edits the plan every week follows (EntryDialog, day
 * builder, brush mode, keyboard editing) and shows a one-time hint to
 * mark holidays. Week mode (``week`` = a date in the URL) fetches the
 * resolved week (``GET …/weeks/{date}``) and renders it through the
 * same grid inside ``WeekContext``: cancelled / changed / extra
 * lessons, a "3 changes this week · Clear all" banner (with Undo), and
 * for a holiday week a "Not active this week" banner with *Activate
 * this week*. The week is refetched whenever the timetable's version
 * moves — our own override edits and the ``timetable.changed`` WS
 * frames of other household members alike.
 */
import { useEffect, useMemo, useState } from 'preact/hooks'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { clearWeek, fetchWeek, setValidity } from '@/store/timetables'
import type { ResolvedWeek, Timetable, TimetableEntry } from '@/types'
import { BrushBar } from './BrushBar'
import { brushOn, startBrush, stopBrush } from './brush'
import { openCopyDay } from './CopyDayDialog'
import {
  dateRange, editableFrom, overridesInWeek, todayIn, validitySummary, weekAnchor, weekLabel,
} from './dates'
import { openDayBuilder } from './DayBuilder'
import { openEntryDialog } from './EntryDialog'
import { focusGrid } from './focus'
import type { EntryPrefill } from './layout'
import { TimetableGrid } from './TimetableGrid'
import { TimetableHeader } from './TimetableHeader'
import { printTimetable } from './TimetablePrint'
import { openSettingsDialog } from './TimetableSettingsDialog'
import { useViewPrefs } from './viewPrefs'
import { WeekBar } from './WeekBar'
import { openWeeksDialog } from './WeekValidityPicker'
import { WeekContext, buildWeekView, isLocked } from './weekView'

interface Props {
  tt: Timetable
  narrow: boolean
  /** A date of the week shown in week mode; ``null`` = regular. */
  week: string | null
  onWeek: (date: string | null) => void
  onDuplicate: () => void
  onNew: () => void
  onDelete: () => void
  now?: Date
}

const hintKey = (id: string) => `sh-timetable-weeks-hint:${id}`

function hintDismissed(id: string): boolean {
  try {
    return localStorage.getItem(hintKey(id)) === '1'
  } catch {
    return false
  }
}

function dismissHint(id: string): void {
  try {
    localStorage.setItem(hintKey(id), '1')
  } catch { /* storage blocked — the hint just comes back next visit */ }
}

/** The resolved week, refetched when the week or the timetable
 *  version changes. */
function useResolvedWeek(tt: Timetable, anchor: string | null) {
  const [week, setWeek] = useState<ResolvedWeek | null>(null)
  const [failed, setFailed] = useState(false)
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    if (!anchor) { setWeek(null); return }
    let live = true
    setFailed(false)
    fetchWeek(tt.id, anchor)
      .then(w => { if (live) setWeek(w) })
      .catch(() => { if (live) setFailed(true) })
    return () => { live = false }
  }, [tt.id, anchor, tt.version, nonce])
  const current = week && week.anchor === anchor ? week : null
  return { week: current, failed, retry: () => setNonce(n => n + 1) }
}

export function SelectedTimetable({
  tt, narrow, week: weekDate, onWeek, onDuplicate, onNew, onDelete, now,
}: Props) {
  const [prefs, setPrefs] = useViewPrefs(tt.id)
  const anchor = weekDate ? weekAnchor(weekDate, tt.week_start) : null
  const { week, failed, retry } = useResolvedWeek(tt, anchor)
  // Regular mode's "Changes this week: N" counts the resolved current
  // week, exactly like week mode's banner (only fetched when the week
  // has overrides at all).
  const currentAnchor = weekAnchor(todayIn(tt.tz, now), tt.week_start)
  const { week: current } = useResolvedWeek(tt,
    !anchor && overridesInWeek(tt, currentAnchor).length > 0 ? currentAnchor : null)
  const pending = current ? changedLessons(current) : 0
  const built = useMemo(
    () => (week ? buildWeekView(tt, week, editableFrom(tt, now)) : null),
    [tt, week], // eslint-disable-line react-hooks/exhaustive-deps
  )
  const [hint, setHint] = useState(() => !hintDismissed(tt.id))
  const brushing = brushOn(tt.id)

  // Brush mode belongs to the regular plan of this timetable only.
  useEffect(() => () => stopBrush(), [tt.id])
  useEffect(() => { if (anchor) stopBrush() }, [anchor])
  useEffect(() => {
    if (!brushing) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      if (document.querySelector('.sh-modal, [role="menu"]')) return
      stopBrush()
      focusGrid(tt.id)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [brushing, tt.id])

  const onBrush = (on: boolean) => {
    if (!on) { stopBrush(); return }
    if (prefs.list) setPrefs({ list: false })
    startBrush(tt)
  }

  // Regular mode edits the plan…
  const onEditRegular = (entry: TimetableEntry, group?: string[]) =>
    openEntryDialog({ timetableId: tt.id, entry, group })
  const onAddRegular = (prefill: EntryPrefill) =>
    openEntryDialog({ timetableId: tt.id, entry: null, prefill })
  // …week mode one date of it.
  const onEditWeek = (entry: TimetableEntry, _group?: string[], runIds?: string[]) => {
    if (!built) return
    const lesson = built.info.lessons.get(entry.id)
    const date = built.info.dates[entry.weekday]
    if (!lesson || !date || isLocked(built.info, entry.weekday)) return
    // A merged double lesson: its dialog acts on every half.
    const run = runIds?.map(id => built.info.lessons.get(id))
      .filter((l): l is NonNullable<typeof l> => !!l)
    openEntryDialog({ timetableId: tt.id, entry: null, week: { date, lesson, run } })
  }
  const onAddWeek = (prefill: EntryPrefill) => {
    if (!built) return
    const date = built.info.dates[prefill.weekday]
    if (!date || isLocked(built.info, prefill.weekday) || !built.info.valid[prefill.weekday]) return
    openEntryDialog({ timetableId: tt.id, entry: null, prefill, week: { date, lesson: null } })
  }

  const header = (
    <TimetableHeader
      tt={tt}
      picture={prefs.picture}
      onPicture={(picture) => setPrefs({ picture })}
      list={prefs.list}
      onList={(list) => { if (list) stopBrush(); setPrefs({ list }) }}
      onSettings={() => openSettingsDialog(tt.id)}
      onDuplicate={onDuplicate}
      onNew={onNew}
      onDelete={onDelete}
      onWeeks={() => openWeeksDialog(tt.id)}
      onPrint={printTimetable}
      brush={brushing}
      onBrush={anchor ? undefined : onBrush}
    />
  )
  const bar = <WeekBar tt={tt} anchor={anchor} onWeek={onWeek} pending={pending} now={now} />

  if (!anchor) {
    const v = tt.validity
    const showHint = hint && tt.entries.length > 0 && !v.valid_from && !v.valid_until
      && v.excluded_weeks.length === 0
    return (
      <article class="sh-timetable-selected">
        {header}
        {showHint && (
          <div class="sh-timetable-banner" role="note">
            <span class="sh-timetable-banner__icon" aria-hidden="true">🏖</span>
            <span class="sh-timetable-banner__text">{t('timetable.weeks.hint')}</span>
            <span class="sh-timetable-banner__actions">
              <Button onClick={() => { dismissHint(tt.id); setHint(false); openWeeksDialog(tt.id) }}>
                {t('timetable.weeks.hint_mark')}
              </Button>
              <Button variant="ghost" onClick={() => { dismissHint(tt.id); setHint(false) }}>
                {t('timetable.weeks.hint_later')}
              </Button>
            </span>
          </div>
        )}
        {bar}
        {brushing && <BrushBar tt={tt} narrow={narrow} />}
        <TimetableGrid tt={tt} prefs={prefs} onPrefs={setPrefs} onEdit={onEditRegular}
                       onAdd={onAddRegular} narrow={narrow} now={now}
                       onSetupDay={(weekday) => openDayBuilder({ timetableId: tt.id, weekday })}
                       onCopyDay={(weekday) => openCopyDay({ timetableId: tt.id, weekday })} />
      </article>
    )
  }

  return (
    <article class="sh-timetable-selected">
      {header}
      {bar}
      {failed && !built ? (
        <div class="sh-empty-state" role="alert">
          <div aria-hidden="true">⚠️</div>
          <h3>{t('timetable.week.load_failed')}</h3>
          <div class="sh-empty-state__cta-row">
            <Button onClick={retry}>{t('timetable.retry')}</Button>
          </div>
        </div>
      ) : !built ? (
        <div class="sh-timetable-loading"><Spinner /></div>
      ) : (
        <WeekMode tt={tt} week={week!} built={built} anchor={anchor} prefs={prefs} setPrefs={setPrefs}
                  narrow={narrow} now={now} onEdit={onEditWeek} onAdd={onAddWeek} />
      )}
    </article>
  )
}

/** Lessons of ``week`` that differ from the regular plan. */
function changedLessons(week: ResolvedWeek): number {
  return week.days.reduce((n, d) => n + d.lessons.filter(l => l.status !== 'normal').length, 0)
}

function WeekMode({ tt, week, built, anchor, prefs, setPrefs, narrow, now, onEdit, onAdd }: {
  tt: Timetable
  week: ResolvedWeek
  built: NonNullable<ReturnType<typeof buildWeekView>>
  anchor: string
  prefs: ReturnType<typeof useViewPrefs>[0]
  setPrefs: ReturnType<typeof useViewPrefs>[1]
  narrow: boolean
  now?: Date
  onEdit: (entry: TimetableEntry, group?: string[], run?: string[]) => void
  onAdd: (prefill: EntryPrefill) => void
}) {
  const [busy, setBusy] = useState(false)
  // What the week shows as changed — the resolved lessons, not the raw
  // override list, so the count always matches the badges on screen.
  const count = changedLessons(week)
  const holiday = tt.validity.excluded_weeks.includes(anchor)
  const locked = Object.keys(built.info.dates).every(d => isLocked(built.info, Number(d)))
  const label = weekLabel(anchor, tt.week_start)

  const act = async (call: () => Promise<unknown>) => {
    setBusy(true)
    try {
      await call()
    } catch (e) {
      showToast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }
  const onClearAll = () => void act(() => clearWeek(tt.id, anchor, {
    undo: { message: t('timetable.week.cleared', { week: label }), onUndone: () => focusGrid(tt.id) },
  }))
  const onActivate = () => void act(async () => {
    const out = await setValidity(tt.id, {
      ...tt.validity,
      excluded_weeks: tt.validity.excluded_weeks.filter(w => w !== anchor),
    })
    if (out) showToast(t('timetable.week.activated'), 'success')
  })

  if (!Object.values(built.info.valid).some(Boolean)) {
    const v = tt.validity
    return (
      <div class="sh-timetable-banner sh-timetable-banner--off" role="status">
        <span class="sh-timetable-banner__icon" aria-hidden="true">{holiday ? '🏖' : '📅'}</span>
        <span class="sh-timetable-banner__text">
          <strong>{t(holiday ? 'timetable.week.holiday' : 'timetable.week.outside')}</strong>
          {!holiday && (
            <span class="sh-form-hint">
              {v.valid_from && v.valid_until
                ? dateRange(v.valid_from, v.valid_until, true)
                : validitySummary(v)}
            </span>
          )}
        </span>
        <span class="sh-timetable-banner__actions">
          {holiday ? (
            <Button onClick={onActivate} loading={busy}>{t('timetable.week.activate')}</Button>
          ) : (
            <Button variant="secondary" onClick={() => openWeeksDialog(tt.id)}>
              {t('timetable.week.change_dates')}
            </Button>
          )}
        </span>
      </div>
    )
  }

  return (
    <>
      {count > 0 ? (
        <div class="sh-timetable-banner sh-timetable-banner--changes" role="status">
          <span class="sh-timetable-banner__icon" aria-hidden="true">🔁</span>
          <span class="sh-timetable-banner__text">
            {t(count === 1 ? 'timetable.week.changes_one' : 'timetable.week.changes', { n: String(count) })}
          </span>
          <span class="sh-timetable-banner__actions">
            <Button variant="secondary" onClick={onClearAll} loading={busy} disabled={locked}
                    title={locked ? t('timetable.week.locked') : undefined}>
              {t('timetable.week.clear_all')}
            </Button>
          </span>
        </div>
      ) : (
        <p class="sh-timetable-weekhint">
          {t(locked ? 'timetable.week.locked' : 'timetable.week.hint')}
        </p>
      )}
      <WeekContext.Provider value={built.info}>
        <TimetableGrid tt={built.view} prefs={prefs} onPrefs={setPrefs} onEdit={onEdit}
                       onAdd={onAdd} narrow={narrow} now={now} printWeek={label} />
      </WeekContext.Provider>
    </>
  )
}
