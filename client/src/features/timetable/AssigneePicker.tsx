/**
 * AssigneePicker — which household members a timetable belongs to.
 * Members render as avatar chips with ``aria-pressed``; the order of
 * ``value`` is the order they were picked (the first one names the
 * timetable in the create dialog).
 */
import { useEffect } from 'preact/hooks'
import { Avatar } from '@/components/Avatar'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'

interface Props {
  value: string[]
  onChange: (ids: string[]) => void
  legend: string
  hint?: string
}

export function AssigneePicker({ value, onChange, legend, hint }: Props) {
  useEffect(() => { void loadHouseholdUsers() }, [])
  const users = Array.from(householdUsers.value.values())
  const toggle = (id: string) => onChange(
    value.includes(id) ? value.filter(x => x !== id) : [...value, id],
  )
  return (
    <fieldset class="sh-timetable-assignees">
      <legend>{legend}</legend>
      {hint && <p class="sh-form-hint">{hint}</p>}
      <div class="sh-timetable-assignees__row">
        {users.map(u => {
          const on = value.includes(u.user_id)
          const name = u.display_name || u.username
          return (
            <button key={u.user_id} type="button" aria-pressed={on}
                    class={`sh-timetable-person${on ? ' is-on' : ''}`}
                    onClick={() => toggle(u.user_id)}>
              <span aria-hidden="true" class="sh-timetable-person__avatar">
                <Avatar src={u.picture_url} name={name} size={24} />
              </span>
              <span>{name}</span>
            </button>
          )
        })}
      </div>
    </fieldset>
  )
}
