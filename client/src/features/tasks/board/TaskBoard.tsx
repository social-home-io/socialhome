/**
 * TaskBoard — one task list as fixed columns: To do · In progress ·
 * Done, each with its name and count.
 *
 * - **Add:** "+ Add task" at the foot of To do and In progress files a
 *   task straight into that column (one request; it lands at the
 *   bottom). Done ends with "N done · Clear all" (instant, with Undo;
 *   only the tasks the viewer may delete).
 * - **Move:** drag a card (``useBoardDrag``: mouse after a few px,
 *   touch after a long-press) — a ghost follows, a line marks where it
 *   lands, the page scrolls at the edges. Or from the keyboard:
 *   Alt+←/→ to the next column, Alt+↑/↓ within one, or the card's ⋯
 *   menu. Every move is optimistic (``store.moveTask``), announced in
 *   an ``aria-live`` line ("Moved 'X' to In progress, 2 of 5") and
 *   rolled back with a toast if the server refuses.
 * - **Filter:** ``BoardFilterBar`` above the columns; hidden cards keep
 *   their places when the others move.
 * - **Phones (< 640 px):** one column at a time, picked with a chip row
 *   that shows the counts; while dragging, the chips are drop targets
 *   ("to the bottom of that column").
 *
 * Who may do what comes from the task scope (``useTaskScope``).
 */
import { useRef, useState } from 'preact/hooks'
import { ChipRadioGroup } from '@/components/ChipRadioGroup'
import { QuickAddBar } from '@/components/QuickAddBar'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import { locale, t } from '@/i18n/i18n'
import { isOne, type TaskStatus } from '@/store/tasks'
import type { TaskItem } from '@/types'
import { ArchiveDivider } from '@/features/organize/shared/ArchiveDivider'
import { useNarrow } from '@/features/timetable/useNarrow'
import { pendingDeletes } from '@/utils/undoableDelete'
import { pendingTargetIds } from '@/store/moderationMine'
import { useTaskScope } from '../scope'
import { BOARD_COLUMNS, columnTasks, planMove, planStep, type MovePlan, type StepDir } from './moves'
import { TaskCard, statusLabel } from './TaskCard'
import { useBoardDrag, type DragState, type DropTarget } from './useBoardDrag'
import { BoardFilterBar } from './BoardFilterBar'
import { TouchDragHint } from './TouchDragHint'
import { useBoardFits } from './useBoardFits'
import { filtersActive, getFilters, matchesFilters, setFilters } from './filters'
import { collectLabels } from './labels'

/** The backend's title cap. */
const TITLE_MAX = 200
/** Touch ghost: at most this wide, this far above the finger. */
const TOUCH_GHOST_W = 220
const TOUCH_GHOST_LIFT = 72

function errText(err: unknown): string {
  return String((err as Error)?.message ?? err)
}

/** Where the ghost goes. A finger would hide a ghost under it (and the
 *  chip it is over), so on touch it is narrower and floats above the
 *  finger, kept on screen. */
function ghostStyle(d: DragState): Record<string, string> {
  if (d.pointerType !== 'touch') {
    return { left: `${d.x - d.offsetX}px`, top: `${d.y - d.offsetY}px`, width: `${d.width}px` }
  }
  const width = Math.min(d.width, TOUCH_GHOST_W)
  const vw = typeof window !== 'undefined' ? window.innerWidth : width
  const left = Math.max(8, Math.min(d.x - 24, vw - width - 8))
  return { left: `${left}px`, top: `${d.y - TOUCH_GHOST_LIFT}px`, width: `${width}px` }
}

function focusCard(id: string) {
  requestAnimationFrame(() => {
    const card = Array.from(document.querySelectorAll<HTMLElement>('[data-board-card]'))
      .find(el => el.dataset.taskId === id)
    card?.querySelector<HTMLElement>('.sh-board-card__title')?.focus()
  })
}

interface Props {
  listId: string
  /** The list's tasks (pending deletes already left out). */
  tasks: readonly TaskItem[]
  onOpen: (task: TaskItem) => void
}

export function TaskBoard({ listId, tasks, onOpen }: Props) {
  const scope = useTaskScope()
  const { store } = scope
  const boardRef = useRef<HTMLDivElement | null>(null)
  // One column at a time on phones, and wherever three don't fit.
  const fits = useBoardFits(boardRef)
  const narrow = useNarrow() || !fits
  const [mobileCol, setMobileCol] = useState<TaskStatus>('todo')
  const [announce, setAnnounce] = useState('')
  const [adding, setAdding] = useState<TaskStatus | null>(null)
  const [draft, setDraft] = useState('')
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const addBtnRefs = useRef<Partial<Record<TaskStatus, HTMLButtonElement | null>>>({})
  const filterKey = `${scope.key}:${listId}`
  const filters = getFilters(filterKey)
  const people = scope.people()
  const me = currentUser.value?.user_id ?? null


  const active = filtersActive(filters)
  const shownTasks = active ? tasks.filter(x => matchesFilters(x, filters, me)) : tasks
  const labels = collectLabels(tasks, locale.value)
  const columns = Object.fromEntries(
    BOARD_COLUMNS.map(s => [s, columnTasks(shownTasks, s)]),
  ) as Record<TaskStatus, TaskItem[]>
  const editable = (x: TaskItem) => scope.canEdit(x)
  // The viewer's own changes waiting for a moderator (§4.3).
  const inReview = store.spaceId ? pendingTargetIds(store.spaceId, 'tasks') : new Set<string>()
  const readOnlyReason = scope.readOnlyReason()

  /** The list as the store has it now (minus pending deletes) and the
   *  ids the current filters show — read when a queued move runs. */
  const current = (): { rows: TaskItem[]; shown: Set<string> | undefined } => {
    const hiddenIds = pendingDeletes.value
    const rows = (store.tasksByList.value[listId] ?? []).filter(x => !hiddenIds.has(x.id))
    const f = getFilters(filterKey)
    const shown = filtersActive(f)
      ? new Set(rows.filter(x => matchesFilters(x, f, currentUser.value?.user_id ?? null)).map(x => x.id))
      : undefined
    return { rows, shown }
  }

  /** Queue a move: it is planned (``makePlan``) when its turn comes, from
   *  the state the previous move left, so quick moves never overlap. */
  function run(
    makePlan: (rows: readonly TaskItem[], shown: Set<string> | undefined) => MovePlan | null,
    focus: boolean,
  ): Promise<void> {
    return store.serial(listId, async () => {
      const { rows, shown } = current()
      const plan = makePlan(rows, shown)
      if (!plan) return
      const task = rows.find(x => x.id === plan.id)
      if (!task) return
      const original = task.status
      // "2 of 5" among the cards the viewer sees.
      const seen = shown ? plan.order.filter(id => shown.has(id)) : plan.order
      setAnnounce(t('tasks.board.moved', {
        title: task.title, column: statusLabel(plan.status),
        n: String(seen.indexOf(plan.id) + 1), total: String(seen.length),
      }))
      if (narrow) setMobileCol(plan.status)
      const pending = store.moveTask(plan.id, plan.status, plan.order)
      if (focus) focusCard(plan.id)
      try {
        if (await pending === 'queued') {
          // Held for review (someone else's task in a "Reviewed" space):
          // the card went back; the store toasted.
          setAnnounce(t('tasks.board.move_queued', { title: task.title }))
          if (narrow) setMobileCol(original)
          if (focus) focusCard(plan.id)
          return
        }
        // A late re-render may have taken focus from the moved card.
        if (focus && document.activeElement === document.body) focusCard(plan.id)
      } catch (err) {
        if ((err as { partial?: unknown } | null)?.partial) {
          // The status was saved; only the place in the column wasn't.
          const msg = t('tasks.board.move_partial', { column: statusLabel(plan.status) })
          showToast(msg, 'error')
          setAnnounce(msg)
          void store.loadList(listId, { force: true }).catch(() => {})
          return
        }
        const forbidden = (err as { status?: unknown } | null)?.status === 403
        showToast(forbidden ? t('tasks.error.forbidden') : t('tasks.board.move_failed', { error: errText(err) }), 'error')
        setAnnounce(t('tasks.board.move_reverted', { title: task.title }))
        if (narrow) setMobileCol(original)
        if (focus) focusCard(plan.id)
      }
    })
  }

  const drag = useBoardDrag({
    canDrag: (id) => {
      const x = tasks.find(r => r.id === id)
      return !!x && editable(x)
    },
    onDrop: (id: string, target: DropTarget) => {
      // The drop index counts the cards shown now; the plan is made
      // from the store's state when the move's turn comes.
      void run((rows, shown) => planMove(rows, id, target.status, target.index, shown), false)
    },
    scrollRef,
  })

  const step = (id: string, dir: StepDir) => {
    void run((rows, shown) => planStep(rows, id, dir, shown), true)
  }
  const moveTo = (id: string, status: TaskStatus) => {
    void run((rows, shown) => planMove(rows, id, status, Number.POSITIVE_INFINITY, shown), true)
  }

  const addTask = async (status: TaskStatus) => {
    const title = draft.trim()
    if (!title) return
    try {
      await store.createTask(listId, title, { status })
      setDraft('')
    } catch (err) {
      showToast(t('tasks.error.add', { error: errText(err) }), 'error')
    }
  }
  const closeAdd = (status: TaskStatus) => {
    setAdding(null)
    setDraft('')
    requestAnimationFrame(() => addBtnRefs.current[status]?.focus())
  }

  const deleteTask = (task: TaskItem) => {
    const col = columns[task.status]
    const i = col.findIndex(x => x.id === task.id)
    const neighbour = col[i + 1] ?? col[i - 1]
    store.removeTask(task, { onUndone: () => focusCard(task.id) })
    if (neighbour) focusCard(neighbour.id)
  }

  const allDone = tasks.filter(x => x.status === 'done')
  const clearable = allDone.filter(editable)
  const clearDone = () => {
    const first = clearable[0]?.id
    store.clearDone(listId, {
      canDelete: editable,
      onUndone: () => { if (first) focusCard(first) },
    })
  }

  const hintId = `sh-board-hint-${listId}`
  const dragged = drag.drag ? tasks.find(x => x.id === drag.drag!.id) ?? null : null
  const shownColumns = narrow ? [mobileCol] : BOARD_COLUMNS

  const renderColumn = (status: TaskStatus) => {
    const rows = columns[status]
    const over = drag.over?.status === status ? drag.over : null
    const lineAt = over?.kind === 'column' ? over.index : null
    let k = 0
    const items = rows.map((x) => {
      const isDragged = drag.drag?.id === x.id
      const line = !isDragged && lineAt === k
      if (!isDragged) k++
      const i = rows.indexOf(x)
      const canEdit = editable(x)
      return (
        <li key={x.id} class="sh-board-column__item">
          {line && <div class="sh-board-insert" aria-hidden="true" />}
          <TaskCard
            task={x}
            editable={canEdit && scope.canWrite}
            readOnlyReason={readOnlyReason}
            showLock={scope.canWrite}
            nameOf={scope.nameOf}
            people={people}
            hintId={hintId}
            dragProps={drag.cardProps(x.id)}
            dragging={isDragged}
            pressing={drag.pressingId === x.id}
            canMoveUp={i > 0}
            canMoveDown={i < rows.length - 1}
            pendingReview={inReview.has(x.id)}
            onOpen={() => onOpen(x)}
            onMoveTo={s => moveTo(x.id, s)}
            onStep={d => step(x.id, d)}
            onDelete={() => deleteTask(x)}
          />
        </li>
      )
    })
    const lineAtEnd = lineAt !== null && lineAt >= k
    const headingId = `sh-board-col-${listId}-${status}`
    return (
      <section
        key={status}
        class={`sh-board-column sh-board-column--${status}${over ? ' sh-board-column--over' : ''}`}
        aria-labelledby={headingId}
        data-board-drop={status}
      >
        <header class="sh-board-column__header">
          <span class={`sh-task-group__dot sh-task-group__dot--${status}`} aria-hidden="true" />
          <h3 class="sh-board-column__name" id={headingId}>{statusLabel(status)}</h3>
          <span class="sh-board-column__count">
            {/* For ``i18n:check``: t('tasks.board.count_sr') t('tasks.board.count_sr_one') */}
            <span class="sr-only">
              {t(isOne(rows.length) ? 'tasks.board.count_sr_one' : 'tasks.board.count_sr', { n: String(rows.length) })}
            </span>
            <span aria-hidden="true">{rows.length}</span>
          </span>
        </header>
        <ol class="sh-board-column__cards">
          {items}
          {lineAtEnd && <li class="sh-board-column__item" aria-hidden="true"><div class="sh-board-insert" /></li>}
        </ol>
        {rows.length === 0 && (
          <p class="sh-board-column__empty">
            {active ? t('tasks.board.no_matches') : drag.drag ? t('tasks.drop_into', { name: statusLabel(status) }) : t('tasks.board.column_empty')}
          </p>
        )}
        {status !== 'done' && scope.canWrite && (
          adding === status ? (
            <QuickAddBar
              class="sh-board-column__add-bar"
              value={draft}
              placeholder={t('tasks.board.add_placeholder')}
              inputLabel={t('tasks.board.add_label', { name: statusLabel(status) })}
              submitLabel={t('tasks.add')}
              onValueChange={setDraft}
              onSubmit={() => { void addTask(status) }}
              inputProps={{
                maxLength: TITLE_MAX,
                autoFocus: true,
                onKeyDown: (e: KeyboardEvent) => {
                  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeAdd(status) }
                },
                onBlur: (e: FocusEvent) => {
                  // Leaving an empty field closes it; a typed title stays.
                  const next = e.relatedTarget as Node | null
                  const bar = (e.currentTarget as HTMLElement).closest('.sh-board-column__add-bar')
                  if (!draft.trim() && !(next && bar?.contains(next))) { setAdding(null); setDraft('') }
                },
              }}
            />
          ) : (
            <button
              type="button"
              ref={(el) => { addBtnRefs.current[status] = el }}
              class="sh-board-column__add"
              onClick={() => { setDraft(''); setAdding(status) }}
            >
              <span aria-hidden="true">+</span>{t('tasks.board.add_task')}
              <span class="sr-only"> — {statusLabel(status)}</span>
            </button>
          )
        )}
        {status === 'done' && allDone.length > 0 && (
          <ArchiveDivider
            class="sh-board-column__archive"
            label={t('tasks.board.done_summary', { n: String(allDone.length) })}
            actionLabel={scope.canWrite && clearable.length > 0 ? t('tasks.clear_all') : undefined}
            actionAriaLabel={t('tasks.clear_all_label')}
            onAction={scope.canWrite && clearable.length > 0 ? clearDone : undefined}
          />
        )}
      </section>
    )
  }

  return (
    <div class={`sh-board${drag.drag ? ' sh-board--dragging' : ''}`} ref={boardRef}>
      <BoardFilterBar
        value={filters}
        onChange={next => setFilters(filterKey, next)}
        people={people}
        labels={labels}
        shown={shownTasks.length}
        total={tasks.length}
        canBeAssigned={!!me && people.some(p => p.user_id === me)}
      />
      <p class="sr-only" id={hintId}>{t('tasks.board.keyboard_hint')}</p>
      {scope.canWrite && <TouchDragHint />}
      {narrow && (
        <ChipRadioGroup<TaskStatus>
          class="sh-board-switcher"
          ariaLabel={t('tasks.board.columns')}
          value={mobileCol}
          onChange={setMobileCol}
          options={BOARD_COLUMNS.map(s => ({
            value: s,
            label: t('tasks.board.column_chip', { name: statusLabel(s), n: String(columns[s].length) }),
            attrs: {
              'data-board-drop': s,
              'data-board-drop-kind': 'chip',
              ...(drag.over?.kind === 'chip' && drag.over.status === s ? { 'data-over': 'true' } : {}),
            },
          }))}
        />
      )}
      <div class="sh-board__scroll" ref={scrollRef}>
        <div class={`sh-board__columns${narrow ? ' sh-board__columns--one' : ''}`}>
          {shownColumns.map(renderColumn)}
        </div>
      </div>
      <p class="sr-only" role="status" aria-live="polite">{announce}</p>
      {drag.drag && dragged && (
        <div
          class="sh-board-ghost"
          aria-hidden="true"
          style={ghostStyle(drag.drag)}
        >
          <TaskCard
            task={dragged}
            editable={true}
            readOnlyReason=""
            nameOf={scope.nameOf}
            people={people}
            ghost
            onOpen={() => {}}
          />
        </div>
      )}
    </div>
  )
}
