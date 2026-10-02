/**
 * TaskDetailDialog — a task's details, for the board and the list view.
 *
 * Editable: status (the keyboard way to do what a drag does), name,
 * description, a clearable due date, priority (incl. "None"), labels
 * (chips + suggestions from the list) and assignees (the scope's
 * people). Save sends only what changed against the task as the dialog
 * opened — a field someone else changed meanwhile isn't sent back —
 * and ``null`` / ``[]`` clear.
 *
 * Not editable: the same facts as plain labelled text with the reason,
 * nothing that looks typeable.
 */
import { useRef, useState } from 'preact/hooks'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { openReport } from '@/components/ReportDialog'
import { ChipRadioGroup } from '@/components/ChipRadioGroup'
import { PeoplePicker, type Person } from '@/components/PeoplePicker'
import { currentUser } from '@/store/auth'
import { locale, t } from '@/i18n/i18n'
import type { TaskPatch, TaskStatus } from '@/store/tasks'
import type { TaskItem, TaskPriority } from '@/types'
import { useAutofocus } from '@/features/timetable/useAutofocus'
import { parseDueDate } from '../dueLabel'
import { BOARD_COLUMNS } from './moves'
import { PriorityIcon, PRIORITIES, priorityLabel } from './priority'
import { labelColorClass } from './labels'
import { LabelsInput } from './LabelsInput'
import { joinNames, statusLabel } from './TaskCard'

/** The backend's assignee cap. */
const MAX_ASSIGNEES = 10

type PriorityChoice = TaskPriority | 'none'

function sameSet(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false
  const s = new Set(a)
  return b.every(x => s.has(x))
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i])
}

export interface TaskDetailDialogProps {
  /** The row as it was when the dialog opened. */
  task: TaskItem
  editable: boolean
  readOnlyReason: string
  people: readonly Person[]
  nameOf: (uid: string) => string
  /** Labels used elsewhere in the list (suggestions). */
  labelSuggestions: readonly string[]
  onClose: () => void
  onSave: (patch: TaskPatch) => Promise<void>
  /** The space the task lives in — offers "Report" (to its moderators)
   *  on someone else's task. */
  spaceId?: string | null
}

export function TaskDetailDialog({
  task, editable, readOnlyReason, people, nameOf, labelSuggestions, onClose, onSave,
  spaceId = null,
}: TaskDetailDialogProps) {
  const snap = useRef(task)
  const base = snap.current
  const [title, setTitle] = useState(base.title)
  const [description, setDescription] = useState(base.description ?? '')
  const [dueDate, setDueDate] = useState(base.due_date ?? '')
  const [status, setStatus] = useState<TaskStatus>(base.status)
  const [priority, setPriority] = useState<PriorityChoice>(base.priority ?? 'none')
  const [labels, setLabels] = useState<string[]>(base.labels ?? [])
  const [assignees, setAssignees] = useState<string[]>(base.assignees ?? [])
  const [saving, setSaving] = useState(false)
  const [titleError, setTitleError] = useState(false)
  const nameRef = useRef<HTMLInputElement | null>(null)
  useAutofocus(nameRef, editable)
  const report = spaceId && base.created_by && base.created_by !== currentUser.value?.user_id
    ? (
      <Button variant="ghost" type="button"
              onClick={() => { onClose(); openReport('task', base.id, spaceId) }}>
        {t('report.action')}
      </Button>
    )
    : null

  const addedBy = base.created_by && (
    <p class="sh-task-edit__added">
      {base.created_by === currentUser.value?.user_id
        ? t('tasks.edit.added_by_you')
        : t('tasks.edit.added_by', { name: nameOf(base.created_by) })}
    </p>
  )

  if (!editable) {
    const due = base.due_date ? parseDueDate(base.due_date) : null
    const notes = base.description?.trim()
    const who = base.assignees ?? []
    const tags = base.labels ?? []
    return (
      <Modal open={true} onClose={onClose} title={t('tasks.edit.title_readonly')}>
        <div class="sh-task-edit">
          <p class="sh-task-edit__readonly">
            <span aria-hidden="true">🔒 </span>{readOnlyReason}
          </p>
          <dl class="sh-task-view">
            <dt>{t('tasks.edit.status')}</dt>
            <dd>
              <span class={`sh-task-edit__status-text sh-task-edit__status-text--${base.status}`}>
                {statusLabel(base.status)}
              </span>
            </dd>
            <dt>{t('tasks.edit.name')}</dt>
            <dd class="sh-task-view__name">{base.title}</dd>
            <dt>{t('tasks.edit.description')}</dt>
            {notes
              ? <dd class="sh-task-edit__notes">{base.description}</dd>
              : <dd class="sh-task-edit__empty">{t('tasks.edit.no_notes')}</dd>}
            <dt>{t('tasks.edit.due')}</dt>
            {base.due_date
              ? <dd>{due ? due.toLocaleDateString(locale.value || undefined, { dateStyle: 'full' }) : base.due_date}</dd>
              : <dd class="sh-task-edit__empty">{t('tasks.edit.no_due')}</dd>}
            <dt>{t('tasks.priority.title')}</dt>
            {base.priority
              ? <dd class="sh-task-view__priority"><PriorityIcon priority={base.priority} withText={false} /> {priorityLabel(base.priority)}</dd>
              : <dd class="sh-task-edit__empty">{t('tasks.priority.none')}</dd>}
            <dt>{t('tasks.labels.title')}</dt>
            {tags.length > 0
              ? (
                <dd class="sh-task-view__labels">
                  {tags.map(l => <span key={l} class={`sh-task-label ${labelColorClass(l)}`}>{l}</span>)}
                </dd>
              )
              : <dd class="sh-task-edit__empty">{t('tasks.labels.none')}</dd>}
            <dt>{t('tasks.edit.assignees')}</dt>
            {who.length > 0
              ? <dd>{joinNames(who.map(nameOf), locale.value)}</dd>
              : <dd class="sh-task-edit__empty">{t('tasks.edit.no_assignees')}</dd>}
          </dl>
          {addedBy}
          <div class="sh-form-actions">
            {report}
            <Button variant="secondary" onClick={onClose}>{t('common.close')}</Button>
          </div>
        </div>
      </Modal>
    )
  }

  const save = async (e: Event) => {
    e.preventDefault()
    if (!title.trim()) {
      setTitleError(true)
      nameRef.current?.focus()
      return
    }
    const patch: TaskPatch = {}
    if (title.trim() !== base.title) patch.title = title.trim()
    if ((description.trim() || null) !== (base.description?.trim() || null)) {
      patch.description = description.trim() || null
    }
    if ((dueDate || null) !== (base.due_date || null)) patch.due_date = dueDate || null
    if (status !== base.status) patch.status = status
    const nextPriority = priority === 'none' ? null : priority
    if (nextPriority !== (base.priority ?? null)) patch.priority = nextPriority
    if (!sameList(labels, base.labels ?? [])) patch.labels = labels
    if (!sameSet(assignees, base.assignees ?? [])) patch.assignees = assignees
    if (Object.keys(patch).length === 0) {
      onClose()
      return
    }
    setSaving(true)
    try {
      await onSave(patch)
    } finally {
      setSaving(false)
    }
  }

  // Someone already assigned but no longer in the roster stays pickable
  // (to unassign them), named as best we can.
  const roster: Person[] = [
    ...people,
    ...assignees.filter(uid => !people.some(p => p.user_id === uid))
      .map(uid => ({ user_id: uid, name: nameOf(uid) })),
  ]

  return (
    <Modal open={true} onClose={onClose} title={t('tasks.edit.title')}>
      <form onSubmit={save} class="sh-task-edit" noValidate>
        <span class="sh-form-label" id="sh-task-edit-status">{t('tasks.edit.status')}</span>
        <ChipRadioGroup<TaskStatus>
          variant="segmented"
          class="sh-task-edit__status"
          labelledBy="sh-task-edit-status"
          value={status}
          onChange={setStatus}
          options={BOARD_COLUMNS.map(s => ({ value: s, label: statusLabel(s) }))}
        />
        <label>
          {t('tasks.edit.name')}
          <input ref={nameRef} type="text" value={title} maxLength={200} required
            aria-invalid={titleError ? 'true' : undefined}
            aria-describedby={titleError ? 'sh-task-edit-title-err' : undefined}
            onInput={(e) => { setTitle((e.target as HTMLInputElement).value); setTitleError(false) }} />
        </label>
        {titleError && (
          <p class="sh-form-error" id="sh-task-edit-title-err" role="alert">
            {t('tasks.edit.name_required')}
          </p>
        )}
        <label>
          {t('tasks.edit.description')}
          <textarea value={description} maxLength={5000} rows={3}
            onInput={(e) => setDescription((e.target as HTMLTextAreaElement).value)} />
        </label>
        <label for="sh-task-edit-due">{t('tasks.edit.due')}</label>
        <span class="sh-task-edit__due">
          <input id="sh-task-edit-due" type="date" value={dueDate}
            onInput={(e) => setDueDate((e.target as HTMLInputElement).value)} />
          {dueDate && (
            <button type="button" class="sh-link" onClick={() => setDueDate('')}
                    aria-label={t('tasks.edit.clear_due_label')}>
              {t('tasks.edit.clear_due')}
            </button>
          )}
        </span>
        <span class="sh-form-label" id="sh-task-edit-priority">{t('tasks.priority.title')}</span>
        <ChipRadioGroup<PriorityChoice>
          class="sh-task-edit__priority"
          labelledBy="sh-task-edit-priority"
          value={priority}
          onChange={setPriority}
          options={[
            { value: 'none', label: t('tasks.priority.none') },
            ...[...PRIORITIES].reverse().map(p => ({ value: p, label: priorityLabel(p) })),
          ]}
        />
        <LabelsInput id="sh-task-edit-labels" value={labels} onChange={setLabels}
                     suggestions={labelSuggestions} />
        <PeoplePicker
          class="sh-task-edit__people"
          legend={t('tasks.edit.assignees')}
          people={roster}
          value={assignees}
          onChange={setAssignees}
          max={MAX_ASSIGNEES}
          maxHint={t('tasks.edit.assignees_full', { max: String(MAX_ASSIGNEES) })}
          emptyText={t('tasks.edit.no_people')}
        />
        {addedBy}
        <div class="sh-form-actions">
          {report}
          <Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" loading={saving}>{t('common.save')}</Button>
        </div>
      </form>
    </Modal>
  )
}
