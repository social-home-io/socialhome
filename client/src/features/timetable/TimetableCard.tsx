/**
 * TimetableCard — one timetable in the picker: colour stripe, name,
 * days ("Mon–Fri"), who it's for, and whether it runs this week.
 */
import { Avatar } from '@/components/Avatar'
import { t } from '@/i18n/i18n'
import { householdDisplayName, householdPictureUrl } from '@/store/householdUsers'
import type { Timetable } from '@/types'
import { colorClass } from './colors'
import { daysSummary } from './time'

interface Props {
  tt: Timetable
  selected: boolean
  onSelect: () => void
}

export function TimetableCard({ tt, selected, onSelect }: Props) {
  const names = tt.assignees.map(householdDisplayName)
  const active = tt.active_this_week !== false
  return (
    <button
      type="button"
      class={`sh-timetable-card ${colorClass(tt.color)}${selected ? ' is-on' : ''}`}
      aria-pressed={selected}
      onClick={onSelect}
    >
      <span class="sh-timetable-card__stripe" aria-hidden="true" />
      <span class="sh-timetable-card__body">
        <span class="sh-timetable-card__name">{tt.name}</span>
        <span class="sh-timetable-card__days">{daysSummary(tt.days, tt.week_start)}</span>
        <span class="sh-timetable-card__foot">
          {names.length > 0 && (
            <span class="sh-timetable-card__people"
                  aria-label={t('timetable.assignees_aria', { names: names.join(', ') })}>
              {tt.assignees.slice(0, 4).map((id, i) => (
                <span key={id} aria-hidden="true" class="sh-timetable-card__avatar">
                  <Avatar src={householdPictureUrl(id)} name={names[i]} size={22} />
                </span>
              ))}
            </span>
          )}
          <span class={`sh-timetable-status${active ? ' sh-timetable-status--on' : ''}`}>
            {t(active ? 'timetable.status.active' : 'timetable.status.inactive')}
          </span>
        </span>
      </span>
    </button>
  )
}
