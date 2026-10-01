/**
 * Board moves as pure functions — what a drop or a keyboard move does
 * to a list's order, before anything is sent.
 *
 * Positions are per column: the reorder call gives each id of ONE
 * column its index. Columns sort by ``position``, then age — the order
 * the server lists them in and checks a reorder against (only the
 * moved card may change its place relative to the others). A plan
 * therefore only ever takes the moved card out and puts it back in;
 * every other card keeps its relative order.
 */
import type { TaskItem } from '@/types'
import type { TaskStatus } from '@/store/tasks'

export const BOARD_COLUMNS: readonly TaskStatus[] = ['todo', 'in_progress', 'done']

/** Position, then creation time (older first); a full tie keeps the
 *  input order (the sort is stable). */
export function compareTasks(a: TaskItem, b: TaskItem): number {
  const d = (a.position ?? 0) - (b.position ?? 0)
  if (d !== 0) return d
  const ca = a.created_at ?? ''
  const cb = b.created_at ?? ''
  if (ca !== cb) return ca < cb ? -1 : 1
  return 0
}

export function sortTasks(tasks: readonly TaskItem[]): TaskItem[] {
  return [...tasks].sort(compareTasks)
}

/** One column's tasks, top to bottom. */
export function columnTasks(tasks: readonly TaskItem[], status: TaskStatus): TaskItem[] {
  return sortTasks(tasks.filter(x => x.status === status))
}

export interface MovePlan {
  id: string
  status: TaskStatus
  /** The target column's ids, top to bottom, after the move. */
  order: string[]
  statusChanged: boolean
  /** 0-based place of the card in the target column. */
  index: number
  total: number
}

/**
 * Drop ``id`` into ``status`` before the visible card at
 * ``visibleIndex`` (an index among the column's cards that are shown,
 * without the dragged one; past the end → after the last shown card).
 * ``visible`` is the set of shown ids when a filter hides some — the
 * hidden ones keep their places. ``null`` when nothing would change.
 */
export function planMove(
  tasks: readonly TaskItem[],
  id: string,
  status: TaskStatus,
  visibleIndex: number,
  visible?: ReadonlySet<string>,
): MovePlan | null {
  const moved = tasks.find(x => x.id === id)
  if (!moved) return null
  const column = columnTasks(tasks, status)
  const dest = column.filter(x => x.id !== id)
  const shown = visible ? dest.filter(x => visible.has(x.id)) : dest
  let at: number
  if (visibleIndex < shown.length) {
    at = dest.indexOf(shown[Math.max(0, visibleIndex)])
  } else if (shown.length > 0) {
    at = dest.indexOf(shown[shown.length - 1]) + 1
  } else {
    at = dest.length
  }
  const order = dest.map(x => x.id)
  order.splice(at, 0, id)
  const statusChanged = moved.status !== status
  if (!statusChanged && order.every((tid, i) => column[i]?.id === tid)) return null
  return { id, status, order, statusChanged, index: at, total: order.length }
}

export type StepDir = 'up' | 'down' | 'left' | 'right'

/** A keyboard move by one step: up / down within the column (among the
 *  shown cards), left / right to the bottom of the next column. */
export function planStep(
  tasks: readonly TaskItem[],
  id: string,
  dir: StepDir,
  visible?: ReadonlySet<string>,
): MovePlan | null {
  const moved = tasks.find(x => x.id === id)
  if (!moved) return null
  if (dir === 'left' || dir === 'right') {
    const col = BOARD_COLUMNS.indexOf(moved.status)
    const next = BOARD_COLUMNS[col + (dir === 'left' ? -1 : 1)]
    if (!next) return null
    return planMove(tasks, id, next, Number.POSITIVE_INFINITY, visible)
  }
  const shown = columnTasks(tasks, moved.status)
    .filter(x => x.id === id || !visible || visible.has(x.id))
  const i = shown.findIndex(x => x.id === id)
  const target = dir === 'up' ? i - 1 : i + 1
  if (target < 0 || target >= shown.length) return null
  return planMove(tasks, id, moved.status, target, visible)
}

/** The board after a plan, without waiting for the server (for tests
 *  and for computing "2 of 5"). */
export function applyPlan(tasks: readonly TaskItem[], plan: MovePlan): TaskItem[] {
  const pos = new Map(plan.order.map((tid, i) => [tid, i]))
  return tasks.map((x) => {
    const p = pos.get(x.id)
    if (p === undefined) return x
    return { ...x, position: p, ...(x.id === plan.id ? { status: plan.status } : {}) }
  })
}
