/**
 * BoardFilterBar — narrow the board: text, "Assigned to me", one
 * assignee, one label, one priority (or "No priority"), and "Clear
 * filters". Client-side; the board remembers them for the session.
 * When any filter is on, a status line says how many tasks show.
 */
import { useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import type { Person } from '@/components/PeoplePicker'
import type { TaskPriority } from '@/types'
import { EMPTY_FILTERS, filtersActive, type BoardFilters } from './filters'
import { PRIORITIES, priorityLabel } from './priority'

interface Props {
  value: BoardFilters
  onChange: (next: BoardFilters) => void
  people: readonly Person[]
  labels: readonly string[]
  /** Shown / total, for the status line. */
  shown: number
  total: number
  /** The viewer can be an assignee here ("Assigned to me"). */
  canBeAssigned: boolean
}

let seq = 0

export function BoardFilterBar({
  value, onChange, people, labels, shown, total, canBeAssigned,
}: Props) {
  const [id] = useState(() => `sh-board-filters-${++seq}`)
  const [open, setOpen] = useState(false)
  const active = filtersActive(value)
  const set = (patch: Partial<BoardFilters>) => onChange({ ...value, ...patch })
  const picked = (value.mine ? 1 : 0) + [value.assignee, value.label, value.priority].filter(v => v !== null).length
  // On a narrow board (a container query on the board's width, see
  // app.css) the controls after the search fold behind "Filters"; it
  // stays open by itself while any of them is on. On a wide board the
  // button is hidden and the controls always show.
  const expanded = open || picked > 0
  return (
    <div class="sh-board-filters" role="search" aria-label={t('tasks.board.filters')}>
      <span class="sh-board-filters__search">
        <span class="sh-board-filters__search-icon" aria-hidden="true">⌕</span>
        <input
          type="search"
          aria-label={t('tasks.board.search_label')}
          value={value.text}
          placeholder={t('tasks.board.search_placeholder')}
          onInput={e => set({ text: (e.target as HTMLInputElement).value })}
        />
      </span>
      <button
        type="button"
        class={`sh-board-filters__chip sh-board-filters__toggle${picked > 0 ? ' is-on' : ''}`}
        aria-expanded={expanded}
        aria-controls={`${id}-more`}
        onClick={() => setOpen(o => !o)}
      >
        {picked > 0 ? t('tasks.board.filters_n', { n: String(picked) }) : t('tasks.board.filters_short')}
      </button>
      <div class="sh-board-filters__more" id={`${id}-more`} data-open={expanded ? 'true' : 'false'}>
      {canBeAssigned && (
        <button
          type="button"
          class={`sh-board-filters__chip${value.mine ? ' is-on' : ''}`}
          aria-pressed={value.mine}
          onClick={() => set({ mine: !value.mine })}
        >
          {t('tasks.board.mine')}
        </button>
      )}
      <label class={`sh-board-filters__select${value.assignee !== null ? ' is-on' : ''}`}>
        <span class="sr-only">{t('tasks.board.filter_assignee')}</span>
        <select
          value={value.assignee ?? ''}
          onChange={e => set({ assignee: (e.target as HTMLSelectElement).value || null })}
        >
          <option value="">{t('tasks.board.any_assignee')}</option>
          {people.map(p => <option key={p.user_id} value={p.user_id}>{p.name}</option>)}
        </select>
      </label>
      <label class={`sh-board-filters__select${value.label !== null ? ' is-on' : ''}`}>
        <span class="sr-only">{t('tasks.board.filter_label')}</span>
        <select
          value={value.label ?? ''}
          disabled={labels.length === 0 && value.label === null}
          onChange={e => set({ label: (e.target as HTMLSelectElement).value || null })}
        >
          <option value="">{t('tasks.board.any_label')}</option>
          {labels.map(l => <option key={l} value={l}>{l}</option>)}
        </select>
      </label>
      <label class={`sh-board-filters__select${value.priority !== null ? ' is-on' : ''}`}>
        <span class="sr-only">{t('tasks.board.filter_priority')}</span>
        <select
          value={value.priority ?? ''}
          onChange={(e) => {
            const v = (e.target as HTMLSelectElement).value
            set({ priority: v ? (v as TaskPriority | 'none') : null })
          }}
        >
          <option value="">{t('tasks.board.any_priority')}</option>
          {PRIORITIES.map(p => <option key={p} value={p}>{priorityLabel(p)}</option>)}
          <option value="none">{t('tasks.priority.none')}</option>
        </select>
      </label>
      </div>
      {active && (
        <button type="button" class="sh-link sh-board-filters__clear" onClick={() => onChange(EMPTY_FILTERS)}>
          {t('tasks.board.clear_filters')}
        </button>
      )}
      <p class={active ? 'sh-board-filters__status' : 'sr-only'} id={id} role="status" aria-live="polite">
        {active ? t('tasks.board.showing', { n: String(shown), total: String(total) }) : ''}
      </p>
    </div>
  )
}
