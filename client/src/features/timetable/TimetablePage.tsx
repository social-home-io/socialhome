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
import type { Timetable, TimetableEntry } from '@/types'
import { EntryDialog, openEntryDialog } from './EntryDialog'
import { TimetableCard } from './TimetableCard'
import { TimetableCreateDialog, openCreateDialog } from './TimetableCreateDialog'
import { TimetableGrid } from './TimetableGrid'
import type { EntryPrefill } from './layout'
import { TimetableHeader } from './TimetableHeader'
import { TimetableSettingsDialog, openSettingsDialog } from './TimetableSettingsDialog'
import { useNarrow } from './useNarrow'
import { useViewPrefs } from './viewPrefs'

const BASE = '/calendar?tab=timetable'

export function ttFromUrl(url: string | undefined): string | null {
  const q = (url ?? '').split('?')[1] ?? ''
  return new URLSearchParams(q).get('tt')
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

  const select = useCallback((id: string | null) => {
    selectedId.value = id
    const next = id ? `${BASE}&tt=${encodeURIComponent(id)}` : BASE
    if (loc.url !== next) loc.route?.(next, true)
  }, [loc])

  const list = timetables.value
  const onlyId = list.length === 1 ? list[0].id : null
  const selected = list.find(x => x.id === selectedId.value) ?? (onlyId ? list[0] : null)
  const isLoaded = loaded.value

  // Exactly one timetable → select it so its id lands in the URL.
  useEffect(() => {
    if (isLoaded && onlyId && selectedId.value !== onlyId) select(onlyId)
  }, [isLoaded, onlyId, select])

  const dialogs = (
    <>
      <TimetableCreateDialog onCreated={(tt) => select(tt.id)} />
      <TimetableSettingsDialog />
      <EntryDialog />
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
                           onSelect={select} />
      )}
      {dialogs}
    </div>
  )
}

function SelectedTimetable({ tt, narrow, onSelect }: {
  tt: Timetable
  narrow: boolean
  onSelect: (id: string | null) => void
}) {
  const [prefs, setPrefs] = useViewPrefs(tt.id)
  const onEdit = (entry: TimetableEntry, group?: string[]) =>
    openEntryDialog({ timetableId: tt.id, entry, group })
  const onAdd = (prefill: EntryPrefill) => openEntryDialog({ timetableId: tt.id, entry: null, prefill })

  const onDuplicate = async () => {
    try {
      const copy = await duplicateTimetable(tt.id)
      showToast(t('timetable.duplicated'), 'success')
      onSelect(copy.id)
    } catch (e) {
      showToast((e as Error).message, 'error')
    }
  }
  const onDelete = async () => {
    const ok = await confirmDialog(t('timetable.delete_confirm', { name: tt.name }), {
      title: t('timetable.delete_title'),
      confirmLabel: t('timetable.header.delete'),
      destructive: true,
    })
    if (!ok) return
    try {
      await deleteTimetable(tt.id)
      showToast(t('timetable.deleted'), 'success')
      onSelect(null)
    } catch (e) {
      showToast((e as Error).message, 'error')
    }
  }

  return (
    <article class="sh-timetable-selected">
      <TimetableHeader
        tt={tt}
        picture={prefs.picture}
        onPicture={(picture) => setPrefs({ picture })}
        list={prefs.list}
        onList={(list) => setPrefs({ list })}
        onSettings={() => openSettingsDialog(tt.id)}
        onDuplicate={() => void onDuplicate()}
        onNew={() => openCreateDialog('school')}
        onDelete={() => void onDelete()}
      />
      <TimetableGrid tt={tt} prefs={prefs} onPrefs={setPrefs} onEdit={onEdit} onAdd={onAdd}
                     narrow={narrow} />
    </article>
  )
}
