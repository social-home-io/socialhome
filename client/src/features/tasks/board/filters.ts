/**
 * Board filters — client-side, for the session (kept per scope + list
 * while the app is open, gone on reload). Text matches the title,
 * description and labels; "Assigned to me", an assignee, a label and a
 * priority (or "no priority") narrow further; all of them combine.
 */
import { signal } from '@preact/signals'
import type { TaskItem, TaskPriority } from '@/types'
import { labelKey } from './labels'

export interface BoardFilters {
  text: string
  mine: boolean
  assignee: string | null
  label: string | null
  priority: TaskPriority | 'none' | null
}

export const EMPTY_FILTERS: BoardFilters = {
  text: '', mine: false, assignee: null, label: null, priority: null,
}

export function filtersActive(f: BoardFilters): boolean {
  return !!f.text.trim() || f.mine || f.assignee !== null || f.label !== null || f.priority !== null
}

export function matchesFilters(task: TaskItem, f: BoardFilters, me: string | null): boolean {
  const assignees = task.assignees ?? []
  if (f.mine && (!me || !assignees.includes(me))) return false
  if (f.assignee !== null && !assignees.includes(f.assignee)) return false
  if (f.label !== null) {
    const want = labelKey(f.label)
    if (!(task.labels ?? []).some(l => labelKey(l) === want)) return false
  }
  if (f.priority === 'none' && task.priority) return false
  if (f.priority !== null && f.priority !== 'none' && task.priority !== f.priority) return false
  const q = f.text.trim().toLocaleLowerCase()
  if (q) {
    const hay = [task.title, task.description ?? '', ...(task.labels ?? [])].join('\n').toLocaleLowerCase()
    if (!hay.includes(q)) return false
  }
  return true
}

/** Session memory: scope + list → filters. */
const remembered = signal<Readonly<Record<string, BoardFilters>>>({})

export function getFilters(key: string): BoardFilters {
  return remembered.value[key] ?? EMPTY_FILTERS
}

export function setFilters(key: string, next: BoardFilters): void {
  remembered.value = { ...remembered.value, [key]: next }
}

/** Forget every remembered filter (logout, tests). */
export function resetFilters(): void {
  remembered.value = {}
}
