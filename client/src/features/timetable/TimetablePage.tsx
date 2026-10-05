/**
 * TimetablePage — the Calendar page's "Timetable" tab: the household's
 * school timetables (Stundenplan).
 *
 * No timetables → an empty state with the two ways to start. One →
 * it opens straight away. Several → a card per timetable; the chosen
 * one is kept in the URL (``?tab=timetable&tt=<id>``) so a link or a
 * reload lands on it. The selected timetable shows its header (inline
 * rename, Picture view, Settings / Duplicate / Delete) above the
 * grid — or, below 640 px, the one-day-at-a-time view. The body is the
 * scope-agnostic ``TimetableBoard``; this page adds the URL state.
 */
import { useCallback, useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { selectedId } from '@/store/timetables'
import { isIsoDate } from './dates'
import { TimetableBoard } from './TimetableBoard'

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
  useTitle(t('nav.timetable'))
  const loc = useLocation()

  useEffect(() => { selectedId.value = ttFromUrl(loc.url) }, [loc.url])

  // Switching timetables keeps week mode (compare two children's weeks).
  const select = useCallback((id: string | null) => {
    selectedId.value = id
    const next = pageUrl(id, weekFromUrl(loc.url))
    if (loc.url !== next) loc.route?.(next, true)
  }, [loc])
  const setWeek = (id: string, date: string | null) => {
    const next = pageUrl(id, date)
    if (loc.url !== next) loc.route?.(next, true)
  }

  return (
    <TimetableBoard select={select} week={weekFromUrl(loc.url)} onWeek={setWeek}
                    emptyTitle={t('timetable.empty.title')} emptyBody={t('timetable.empty.body')} />
  )
}
