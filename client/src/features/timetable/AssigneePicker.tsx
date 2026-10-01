/**
 * AssigneePicker — which household members a timetable belongs to: the
 * shared ``PeoplePicker`` over the household users. The order of
 * ``value`` is the order they were picked (the first one names the
 * timetable in the create dialog).
 */
import { useEffect } from 'preact/hooks'
import { PeoplePicker } from '@/components/PeoplePicker'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'

interface Props {
  value: string[]
  onChange: (ids: string[]) => void
  legend: string
  hint?: string
}

export function AssigneePicker({ value, onChange, legend, hint }: Props) {
  useEffect(() => { void loadHouseholdUsers() }, [])
  const people = Array.from(householdUsers.value.values()).map(u => ({
    user_id: u.user_id, name: u.display_name || u.username, picture_url: u.picture_url,
  }))
  return (
    <PeoplePicker class="sh-timetable-assignees" people={people} value={value}
                  onChange={onChange} legend={legend} hint={hint} />
  )
}
