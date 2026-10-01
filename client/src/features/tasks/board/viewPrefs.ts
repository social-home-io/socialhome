/**
 * Board | List, remembered per task list in ``localStorage``
 * (``sh-tasks-view:<listId>``) — a per-viewer convenience: blocked or
 * empty storage falls back to the board.
 */
import { useState } from 'preact/hooks'

export type TaskView = 'board' | 'list'

export const DEFAULT_TASK_VIEW: TaskView = 'board'

const key = (listId: string) => `sh-tasks-view:${listId}`

export function loadTaskView(listId: string): TaskView {
  try {
    return localStorage.getItem(key(listId)) === 'list' ? 'list' : DEFAULT_TASK_VIEW
  } catch {
    return DEFAULT_TASK_VIEW
  }
}

export function saveTaskView(listId: string, view: TaskView): void {
  try {
    localStorage.setItem(key(listId), view)
  } catch {
    // Storage blocked — the choice lasts for this visit only.
  }
}

/** ``[view, setView]`` for ``listId``. A list switched to is read in
 *  that same render (no frame of the previous list's view). */
export function useTaskView(listId: string | null): [TaskView, (v: TaskView) => void] {
  const read = (id: string | null) => (id ? loadTaskView(id) : DEFAULT_TASK_VIEW)
  const [state, setState] = useState<{ id: string | null; view: TaskView }>(() => ({ id: listId, view: read(listId) }))
  const view = state.id === listId ? state.view : read(listId)
  const setView = (v: TaskView) => {
    setState({ id: listId, view: v })
    if (listId) saveTaskView(listId, v)
  }
  return [view, setView]
}
