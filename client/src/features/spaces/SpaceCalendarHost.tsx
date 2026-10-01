/**
 * SpaceCalendarHost — the space Calendar tab: the space's events, its
 * shared timetables, or both behind an **Events | Timetable** switch
 * (the household Calendar page's Calendar | Timetable twin).
 *
 * Which ones show follows the space's ``calendar`` / ``timetable``
 * features (``calendarModes``). The choice is remembered per space for
 * the session, so leaving the tab and coming back lands where the
 * viewer was; a remembered Timetable falls back to Events when the
 * feature is turned off.
 */
import type { ComponentChildren } from 'preact'
import { signal } from '@preact/signals'
import { t } from '@/i18n/i18n'
import { SpaceTimetableTab } from './SpaceTimetableTab'
import { calendarModes, type CalendarMode, type SpaceTabFeatures } from './spaceTabs'

const modes = signal<Record<string, CalendarMode>>({})

/** Forget every remembered choice (tests). */
export function resetCalendarModes(): void {
  modes.value = {}
}

interface Props {
  spaceId: string
  features: SpaceTabFeatures | undefined
  /** The viewer edits the space timetables (owner / admin). */
  canEdit: boolean
  /** The events agenda (rendered only while it is shown). */
  events: () => ComponentChildren
}

export function SpaceCalendarHost({ spaceId, features, canEdit, events }: Props) {
  const available = calendarModes(features)
  const wanted = modes.value[spaceId]
  const mode: CalendarMode = wanted && available.includes(wanted) ? wanted : (available[0] ?? 'events')
  const pick = (m: CalendarMode) => { modes.value = { ...modes.value, [spaceId]: m } }
  const label = (m: CalendarMode) => (m === 'events' ? t('timetable.space.events') : t('nav.timetable'))

  return (
    <div class="sh-space-calendar-host">
      {available.length > 1 && (
        <div class="sh-timetable-seg sh-space-calendar-host__switch" role="group"
             aria-label={t('timetable.space.view_aria')}>
          {available.map(m => (
            <button key={m} type="button"
                    class={`sh-timetable-seg__btn${mode === m ? ' is-on' : ''}`}
                    aria-pressed={mode === m} onClick={() => pick(m)}>
              {label(m)}
            </button>
          ))}
        </div>
      )}
      {mode === 'timetable'
        ? <SpaceTimetableTab spaceId={spaceId} canEdit={canEdit} />
        : events()}
    </div>
  )
}
