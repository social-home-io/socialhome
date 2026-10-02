/**
 * TaskCard — one task on the board.
 *
 * Title (two lines at most) is the card's button: it opens the detail
 * dialog, and its hit area stretches over the whole card. Around it:
 * the priority icon (none without a priority), up to three label chips
 * (+N) coloured by the label's name, one due chip (overdue / today /
 * the date; none once done), up to three assignee avatars (+N) and ≡
 * when there are notes. Everything the eye reads is also in the
 * title's description, so a screen reader hears one card as one item.
 *
 * A card the viewer may change has a ⋯ menu ("Move to …", "Move up /
 * down", Edit, Delete), answers Alt+arrow keys on its title and can be
 * dragged; one they can't shows 🔒 with the reason and does none of it.
 */
import { Avatar } from '@/components/Avatar'
import { OverflowMenu, type MenuItem } from '@/components/OverflowMenu'
import { t } from '@/i18n/i18n'
import type { TaskItem } from '@/types'
import type { TaskStatus } from '@/store/tasks'
import type { Person } from '@/components/PeoplePicker'
import { dueLabel } from '../dueLabel'
import { PriorityIcon } from './priority'
import { labelColorClass } from './labels'
import { BOARD_COLUMNS, type StepDir } from './moves'
import type { CardDragProps } from './useBoardDrag'

const MAX_LABELS_SHOWN = 3
const MAX_AVATARS_SHOWN = 3

/** Status names. For ``i18n:check``:
 *  t('tasks.status.todo') t('tasks.status.in_progress') t('tasks.status.done') */
export function statusLabel(s: TaskStatus): string {
  return t(`tasks.status.${s}`)
}

/** "Lena, Max & you" in the UI language. */
export function joinNames(names: string[], lang?: string): string {
  try {
    return new Intl.ListFormat(lang || undefined, { style: 'short', type: 'conjunction' }).format(names)
  } catch {
    return names.join(', ')
  }
}

export interface TaskCardProps {
  task: TaskItem
  editable: boolean
  readOnlyReason: string
  nameOf: (uid: string) => string
  people: readonly Person[]
  /** Id of the board's "how to move a card" hint. */
  hintId?: string
  dragProps?: CardDragProps
  /** This card is the one being dragged (its slot stays, dimmed). */
  dragging?: boolean
  /** A touch long-press is under way on it. */
  pressing?: boolean
  /** The copy that follows the pointer: no controls, hidden from AT. */
  ghost?: boolean
  /** Show 🔒 on a card the viewer can't change. Off when the whole
   *  board is read-only — the page says so once. Default on. */
  showLock?: boolean
  canMoveUp?: boolean
  canMoveDown?: boolean
  /** The viewer's change to this task waits for a moderator (§4.3
   *  "Reviewed") — shows a "Pending review" chip. */
  pendingReview?: boolean
  onOpen: () => void
  onMoveTo?: (status: TaskStatus) => void
  onStep?: (dir: StepDir) => void
  onDelete?: () => void
}

export function TaskCard({
  task, editable, readOnlyReason, nameOf, people, hintId, dragProps, dragging, pressing, ghost,
  showLock = true, canMoveUp, canMoveDown, pendingReview = false, onOpen, onMoveTo, onStep, onDelete,
}: TaskCardProps) {
  const done = task.status === 'done'
  const due = task.due_date && !done ? dueLabel(task.due_date) : null
  const labels = task.labels ?? []
  const shownLabels = labels.slice(0, MAX_LABELS_SHOWN)
  const moreLabels = labels.slice(MAX_LABELS_SHOWN)
  const assignees = task.assignees ?? []
  const shownPeople = assignees.slice(0, MAX_AVATARS_SHOWN)
  const morePeople = assignees.length - shownPeople.length
  const hasNotes = !!task.description?.trim()
  const metaId = `sh-board-card-meta-${task.id}`
  const lock = !editable && showLock && !ghost
  const review = pendingReview && !ghost
  const showMeta = !!due || hasNotes || lock || review || assignees.length > 0 || labels.length > 0
  const describedBy = [showMeta ? metaId : '', editable && hintId ? hintId : ''].filter(Boolean).join(' ')

  const onKeyDown = (e: KeyboardEvent) => {
    if (!editable || !onStep || !e.altKey || e.ctrlKey || e.metaKey) return
    const dir: StepDir | null = e.key === 'ArrowUp' ? 'up' : e.key === 'ArrowDown' ? 'down'
      : e.key === 'ArrowLeft' ? 'left' : e.key === 'ArrowRight' ? 'right' : null
    if (!dir) return
    e.preventDefault()
    onStep(dir)
  }

  const menu: MenuItem[] = editable ? [
    ...BOARD_COLUMNS.filter(s => s !== task.status).map(s => ({
      key: `move-${s}`,
      label: t('tasks.board.move_to', { name: statusLabel(s) }),
      onSelect: () => onMoveTo?.(s),
    })),
    { key: 'up', label: t('tasks.board.move_up'), disabled: !canMoveUp, onSelect: () => onStep?.('up') },
    { key: 'down', label: t('tasks.board.move_down'), disabled: !canMoveDown, onSelect: () => onStep?.('down') },
    { key: 'edit', label: t('tasks.board.edit'), onSelect: onOpen },
    ...(onDelete ? [{ key: 'delete', label: t('tasks.board.delete'), danger: true, onSelect: onDelete }] : []),
  ] : []

  return (
    <article
      class={'sh-board-card'
        + (done ? ' sh-board-card--done' : '')
        + (editable ? ' sh-board-card--draggable' : ' sh-board-card--locked')
        + (dragging ? ' sh-board-card--dragging' : '')
        + (pressing ? ' sh-board-card--pressing' : '')
        + (ghost ? ' sh-board-card--ghost' : '')}
      {...(ghost ? { 'aria-hidden': 'true' } : { 'data-board-card': '', 'data-task-id': task.id })}
      {...(editable && !ghost ? dragProps : {})}
    >
      <div class="sh-board-card__top">
        <PriorityIcon priority={task.priority} />
        <button
          type="button"
          tabIndex={ghost ? -1 : undefined}
          class="sh-board-card__title"
          aria-describedby={describedBy || undefined}
          onClick={onOpen}
          onKeyDown={onKeyDown}
        >
          {task.title}
        </button>
        {editable && !ghost && (
          <div class="sh-board-card__menu" data-no-drag>
            <OverflowMenu
              label={t('tasks.board.card_menu', { title: task.title })}
              bareTrigger
              triggerClass="sh-icon-btn sh-row-action sh-board-card__menu-btn"
              menuClass="sh-board-card__menu-list"
              items={menu}
            >
              <span aria-hidden="true">⋯</span>
            </OverflowMenu>
          </div>
        )}
      </div>
      {showMeta && (
        <div class="sh-board-card__meta" id={ghost ? undefined : metaId}>
          {review && (
            <span class="sh-badge sh-badge--pending sh-board-card__review" title={t('tasks.pending_review_hint')}>
              {t('tasks.pending_review')}
            </span>
          )}
          {labels.length > 0 && (
            <span class="sh-board-card__labels">
              <span class="sr-only">{t('tasks.labels.sr', { names: joinNames(labels) })}</span>
              {shownLabels.map(l => (
                <span key={l} class={`sh-task-label ${labelColorClass(l)}`} aria-hidden="true">{l}</span>
              ))}
              {moreLabels.length > 0 && (
                <span class="sh-task-label sh-task-label--more" aria-hidden="true" title={moreLabels.join(', ')}>
                  +{moreLabels.length}
                </span>
              )}
            </span>
          )}
          {(due || hasNotes || lock || assignees.length > 0) && (
            <span class="sh-board-card__foot">
              {/* The start slot holds due / notes / lock — empty or not,
                * so the avatars always sit at the end of this one row. */}
              <span class="sh-board-card__foot-start">
                {due && (
                  <span class={`sh-task-due${due.tone ? ` sh-task-due--${due.tone}` : ''}`} title={due.title}>
                    {due.text}
                  </span>
                )}
                {hasNotes && (
                  <span class="sh-board-card__notes" title={t('tasks.has_notes')}>
                    <span aria-hidden="true">≡</span>
                    <span class="sr-only">{t('tasks.has_notes')}</span>
                  </span>
                )}
                {lock && (
                  <span class="sh-board-card__lock" title={readOnlyReason}>
                    <span aria-hidden="true">🔒</span>
                    <span class="sr-only">{t('tasks.read_only')}: {readOnlyReason}</span>
                  </span>
                )}
              </span>
              {assignees.length > 0 && (
                <span class="sh-board-card__people" title={joinNames(assignees.map(nameOf))}>
                  <span class="sr-only">{t('tasks.assigned_to')} {joinNames(assignees.map(nameOf))}</span>
                  {shownPeople.map((uid) => {
                    const p = people.find(x => x.user_id === uid)
                    return (
                      <span key={uid} class="sh-board-card__avatar" aria-hidden="true">
                        <Avatar src={p?.picture_url ?? null} name={p?.name ?? nameOf(uid)} size={22} />
                      </span>
                    )
                  })}
                  {morePeople > 0 && (
                    <span class="sh-board-card__avatar sh-board-card__avatar--more" aria-hidden="true">
                      +{morePeople}
                    </span>
                  )}
                </span>
              )}
            </span>
          )}
        </div>
      )}
    </article>
  )
}
