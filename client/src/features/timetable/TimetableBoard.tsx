/**
 * TimetableBoard — the body of a Timetable view, for any scope (the
 * household page, a space's tab): loading / failed / empty states, one
 * card per timetable, the selected timetable and the dialogs.
 *
 * The scope (``TimetableScopeContext``) decides the store and whether
 * the viewer edits: a view-only scope gets no create buttons, no
 * "+ New" card, no Duplicate / Delete, and only the read-only lesson
 * details dialog. Where the selection and the week live is the host's
 * business (the household keeps them in the URL, a space tab locally):
 * the board reads ``store.selectedId`` and asks ``select`` / ``onWeek``
 * to change them.
 */
import type { ComponentChildren } from 'preact'
import { useEffect, useState } from 'preact/hooks'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { confirmDialog } from '@/components/confirm'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { loadHouseholdUsers } from '@/store/householdUsers'
import type { Timetable } from '@/types'
import { CopyDayDialog } from './CopyDayDialog'
import { DayBuilder, openDayBuilder } from './DayBuilder'
import { EntryDialog } from './EntryDialog'
import { LessonInfoDialog } from './LessonInfoDialog'
import { useTimetableScope } from './scope'
import { SelectedTimetable } from './SelectedTimetable'
import { TimetableCard } from './TimetableCard'
import { TimetableCreateDialog, openCreateDialog } from './TimetableCreateDialog'
import { TimetableSettingsDialog } from './TimetableSettingsDialog'
import { orderedDays } from './time'
import { useNarrow } from './useNarrow'
import { WeekValidityPicker } from './WeekValidityPicker'

interface Props {
  /** Change the selection (``null`` = none). */
  select: (id: string | null) => void
  /** Week mode's date for the selected timetable; ``null`` = regular. */
  week: string | null
  onWeek: (id: string, date: string | null) => void
  /** The empty state's heading and line. */
  emptyTitle: string
  emptyBody: string
  /** Extra header toggles for the selected timetable (e.g. the Home pin). */
  headerExtra?: (tt: Timetable) => ComponentChildren
  /** A caption under the selected timetable's name. */
  headerCaption?: string
  now?: Date
}

export function TimetableBoard({
  select, week, onWeek, emptyTitle, emptyBody, headerExtra, headerCaption, now,
}: Props) {
  const { store, editable, assignees } = useTimetableScope()
  const [failed, setFailed] = useState(false)
  const narrow = useNarrow()

  const load = () => {
    setFailed(false)
    store.loadTimetables().catch(() => setFailed(true))
  }
  useEffect(() => {
    if (assignees) void loadHouseholdUsers()
    load()
  }, [store]) // eslint-disable-line react-hooks/exhaustive-deps

  const list = store.timetables.value
  const onlyId = list.length === 1 ? list[0].id : null
  const selected = list.find(x => x.id === store.selectedId.value) ?? (onlyId ? list[0] : null)
  const isLoaded = store.loaded.value

  // Exactly one timetable → select it (the household puts it in the URL).
  useEffect(() => {
    if (isLoaded && onlyId && store.selectedId.value !== onlyId) select(onlyId)
  }, [isLoaded, onlyId, select]) // eslint-disable-line react-hooks/exhaustive-deps

  const onDuplicate = async (tt: Timetable) => {
    try {
      const copy = await store.duplicateTimetable(tt.id)
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
      await store.deleteTimetable(tt.id)
      showToast(t('timetable.deleted'), 'success')
      select(null)
    } catch (e) {
      showToast((e as Error).message, 'error')
    }
  }

  const dialogs = editable ? (
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
  ) : (
    <LessonInfoDialog />
  )

  if (!isLoaded) {
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
          <h3>{emptyTitle}</h3>
          <p>{emptyBody}</p>
          {editable && (
            <div class="sh-empty-state__cta-row">
              <Button onClick={() => openCreateDialog('school')}>
                {t('timetable.empty.create_school')}
              </Button>
              <Button variant="secondary" onClick={() => openCreateDialog('empty')}>
                {t('timetable.empty.start_empty')}
              </Button>
            </div>
          )}
        </div>
        {dialogs}
      </div>
    )
  }

  return (
    <div class="sh-timetable-page">
      {/* View only with a single timetable: nothing to pick, no strip. */}
      {(editable || list.length > 1) && (
      <div class="sh-timetable-cards" role="group" aria-label={t('timetable.list_aria')}>
        {list.map(tt => (
          <TimetableCard key={tt.id} tt={tt} selected={tt.id === selected?.id}
                         onSelect={() => select(tt.id)} />
        ))}
        {editable && (
          <button type="button" class="sh-timetable-newcard"
                  aria-label={t('timetable.new')} onClick={() => openCreateDialog('school')}>
            <span aria-hidden="true">{t('timetable.new_short')}</span>
          </button>
        )}
      </div>
      )}
      {selected && (
        <SelectedTimetable key={selected.id} tt={selected} narrow={narrow}
                           week={week} onWeek={(date) => onWeek(selected.id, date)}
                           onDuplicate={() => void onDuplicate(selected)}
                           onNew={() => openCreateDialog('school')}
                           onDelete={() => void onDelete(selected)}
                           headerExtra={headerExtra?.(selected)}
                           headerCaption={headerCaption}
                           now={now} />
      )}
      {dialogs}
    </div>
  )
}
