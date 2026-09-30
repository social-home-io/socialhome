/**
 * TimetablePage — the Calendar page's "Timetable" tab: the household's
 * school timetables (Stundenplan).
 *
 * No timetables → an empty state with the two ways to start. One →
 * it opens straight away. Several → a card per timetable; the chosen
 * one is kept in the URL (``?tab=timetable&tt=<id>``) so a link or a
 * reload lands on it. The selected timetable shows its header (inline
 * rename, Picture view, Settings / Duplicate / Delete) above the
 * grid — or, below 640 px, the one-day-at-a-time view.
 */
import { useCallback, useEffect, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { confirmDialog } from '@/components/confirm'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { loadHouseholdUsers } from '@/store/householdUsers'
import { useTitle } from '@/store/pageTitle'
import {
  deleteTimetable, duplicateTimetable, loaded, loadTimetables, selectedId, timetables,
} from '@/store/timetables'
import type { Timetable } from '@/types'
import { CopyDayDialog } from './CopyDayDialog'
import { isIsoDate } from './dates'
import { DayBuilder, openDayBuilder } from './DayBuilder'
import { EntryDialog } from './EntryDialog'
import { SelectedTimetable } from './SelectedTimetable'
import { TimetableCard } from './TimetableCard'
import { TimetableCreateDialog, openCreateDialog } from './TimetableCreateDialog'
import { TimetableSettingsDialog } from './TimetableSettingsDialog'
import { orderedDays } from './time'
import { useNarrow } from './useNarrow'
import { WeekValidityPicker } from './WeekValidityPicker'

const BASE = '/calendar?tab=timetable'

const query = (url: string | undefined) => new URLSearchParams((url ?? '').split('?')[1] ?? '')

export function ttFromUrl(url: string | undefined): string | null {
  return query(url).get('tt')
}

/** ``&week=YYYY-MM-DD`` — its presence means week mode. */
export function weekFromUrl(url: string | undefined): string | null {
  const w = query(url).get('week')
  return isIsoDate(w) ? w : null
}

function pageUrl(tt: string | null, week: string | null): string {
  let url = BASE
  if (tt) url += `&tt=${encodeURIComponent(tt)}`
  if (tt && week) url += `&week=${week}`
  return url
}

export default function TimetablePage() {
  useTitle('Timetable')
  const loc = useLocation()
  const [failed, setFailed] = useState(false)
  const narrow = useNarrow()

  const load = () => {
    setFailed(false)
    loadTimetables().catch(() => setFailed(true))
  }
  useEffect(() => {
    void loadHouseholdUsers()
    load()
  }, [])

  useEffect(() => { selectedId.value = ttFromUrl(loc.url) }, [loc.url])

  const week = weekFromUrl(loc.url)
  // Switching timetables keeps week mode (compare two children's weeks).
  const select = useCallback((id: string | null) => {
    selectedId.value = id
    const next = pageUrl(id, weekFromUrl(loc.url))
    if (loc.url !== next) loc.route?.(next, true)
  }, [loc])

  const list = timetables.value
  const onlyId = list.length === 1 ? list[0].id : null
  const selected = list.find(x => x.id === selectedId.value) ?? (onlyId ? list[0] : null)
  const isLoaded = loaded.value
  const setWeek = (date: string | null) => {
    const next = pageUrl(selected?.id ?? null, date)
    if (loc.url !== next) loc.route?.(next, true)
  }

  // Exactly one timetable → select it so its id lands in the URL.
  useEffect(() => {
    if (isLoaded && onlyId && selectedId.value !== onlyId) select(onlyId)
  }, [isLoaded, onlyId, select])

  const onDuplicate = async (tt: Timetable) => {
    try {
      const copy = await duplicateTimetable(tt.id)
      showToast(t('timetable.duplicated'), 'success')
      select(copy.id)
    } catch (e) {
      showToast((e as Error).message, 'error')
    }
  }
  const onDelete = async (tt: Timetable) => {
    const ok = await confirmDialog(t('timetable.delete_confirm', { name: tt.name }), {
      title: t('timetable.delete_title'),
      confirmLabel: t('timetable.header.delete'),
      destructive: true,
    })
    if (!ok) return
    try {
      await deleteTimetable(tt.id)
      showToast(t('timetable.deleted'), 'success')
      select(null)
    } catch (e) {
      showToast((e as Error).message, 'error')
    }
  }

  const dialogs = (
    <>
      <TimetableCreateDialog onCreated={(tt) => {
        select(tt.id)
        // An *Empty* timetable starts with its first day's times.
        if (tt.entries.length === 0 && tt.days.length > 0) {
          openDayBuilder({ timetableId: tt.id, weekday: orderedDays(tt.days, tt.week_start)[0] })
        }
      }} />
      <TimetableSettingsDialog />
      <EntryDialog />
      <DayBuilder />
      <CopyDayDialog />
      <WeekValidityPicker />
    </>
  )

  if (!loaded.value) {
    return (
      <div class="sh-timetable-page">
        {failed ? (
          <div class="sh-empty-state" role="alert">
            <div aria-hidden="true">⚠️</div>
            <h3>{t('timetable.load_failed')}</h3>
            <div class="sh-empty-state__cta-row">
              <Button onClick={load}>{t('timetable.retry')}</Button>
            </div>
          </div>
        ) : (
          <div class="sh-timetable-loading"><Spinner /></div>
        )}
      </div>
    )
  }

  if (list.length === 0) {
    return (
      <div class="sh-timetable-page">
        <div class="sh-empty-state">
          <div aria-hidden="true">🏫</div>
          <h3>{t('timetable.empty.title')}</h3>
          <p>{t('timetable.empty.body')}</p>
          <div class="sh-empty-state__cta-row">
            <Button onClick={() => openCreateDialog('school')}>
              {t('timetable.empty.create_school')}
            </Button>
            <Button variant="secondary" onClick={() => openCreateDialog('empty')}>
              {t('timetable.empty.start_empty')}
            </Button>
          </div>
        </div>
        {dialogs}
      </div>
    )
  }

  return (
    <div class="sh-timetable-page">
      <div class="sh-timetable-cards" role="group" aria-label={t('timetable.list_aria')}>
        {list.map(tt => (
          <TimetableCard key={tt.id} tt={tt} selected={tt.id === selected?.id}
                         onSelect={() => select(tt.id)} />
        ))}
        <button type="button" class="sh-timetable-newcard"
                aria-label={t('timetable.new')} onClick={() => openCreateDialog('school')}>
          <span aria-hidden="true">{t('timetable.new_short')}</span>
        </button>
      </div>
      {selected && (
        <SelectedTimetable key={selected.id} tt={selected} narrow={narrow}
                           week={week} onWeek={setWeek}
                           onDuplicate={() => void onDuplicate(selected)}
                           onNew={() => openCreateDialog('school')}
                           onDelete={() => void onDelete(selected)} />
      )}
      {dialogs}
    </div>
  )
}
