/**
 * PeoplePicker — pick people from a roster (household users, or a
 * space's members including ones from other households). Each person
 * is an avatar chip with ``aria-pressed``; ``value`` keeps the order
 * they were picked in. With ``max`` set, unpicked chips disable once
 * that many are chosen (the hint says why).
 *
 * Used by the timetable's ``AssigneePicker`` and the task dialog.
 */
import { Avatar } from '@/components/Avatar'

export interface Person {
  user_id: string
  name: string
  picture_url?: string | null
  /** Secondary text, e.g. a remote member's household. */
  detail?: string | null
}

interface Props {
  people: readonly Person[]
  value: readonly string[]
  onChange: (ids: string[]) => void
  legend: string
  hint?: string
  /** Most people that may be picked. */
  max?: number
  /** Shown at ``max`` (e.g. "Up to 10 people"). */
  maxHint?: string
  /** Shown when the roster is empty. */
  emptyText?: string
  class?: string
}

export function PeoplePicker({
  people, value, onChange, legend, hint, max, maxHint, emptyText, class: extra,
}: Props) {
  const full = max !== undefined && value.length >= max
  const toggle = (id: string) => onChange(
    value.includes(id) ? value.filter(x => x !== id) : [...value, id],
  )
  return (
    <fieldset class={extra ? `sh-people-picker ${extra}` : 'sh-people-picker'}>
      <legend>{legend}</legend>
      {hint && <p class="sh-form-hint">{hint}</p>}
      {people.length === 0 && emptyText && <p class="sh-form-hint">{emptyText}</p>}
      <div class="sh-people-picker__row">
        {people.map(p => {
          const on = value.includes(p.user_id)
          return (
            <button key={p.user_id} type="button" aria-pressed={on}
                    disabled={!on && full}
                    class={`sh-people-picker__person${on ? ' is-on' : ''}`}
                    onClick={() => toggle(p.user_id)}>
              <span aria-hidden="true" class="sh-people-picker__avatar">
                <Avatar src={p.picture_url ?? null} name={p.name} size={24} />
              </span>
              <span class="sh-people-picker__name">{p.name}</span>
              {p.detail && <span class="sh-people-picker__detail">{p.detail}</span>}
            </button>
          )
        })}
      </div>
      {full && maxHint && <p class="sh-form-hint" role="status">{maxHint}</p>}
    </fieldset>
  )
}
