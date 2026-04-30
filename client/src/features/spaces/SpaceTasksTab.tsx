/**
 * SpaceTasksTab — per-space task lists, mounted inside SpaceFeedPage's
 * ``activeTab === 'tasks'`` branch.
 *
 * Mirrors the household :mod:`features/tasks/TaskPage` shape — a list
 * roster on the side, the active list's tasks rendered inside a
 * tape-style ``.sh-list-card`` — but talks to ``/api/spaces/{id}/...``
 * routes so listings stay scoped to the space's membership.
 *
 * Tasks render with the same multi-row grid as the household: title on
 * row 1, monospace assignees / added-by byline on row 2, due-date pill
 * on the right. Status cycle on click; assignees + creator visible at
 * a glance.
 */
import { useEffect, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { householdUsers } from '@/store/householdUsers'
import { currentUser } from '@/store/auth'
import type { TaskItem } from '@/types'

interface SpaceTaskList { id: string; name: string; created_by: string }

const STATUS_LABEL: Record<'todo' | 'in_progress' | 'done', string> = {
  todo:        'To do',
  in_progress: 'In progress',
  done:        'Done',
}

// Module-level signals so route revisits keep the same list selected
// between switches but reset cleanly on space change (the parent calls
// ``resetSpaceTasks`` from its space-id effect).
const lists = signal<SpaceTaskList[]>([])
const tasks = signal<TaskItem[]>([])
const activeList = signal<string | null>(null)
const loading = signal(true)

export function resetSpaceTasks() {
  lists.value = []
  tasks.value = []
  activeList.value = null
  loading.value = true
}

function dueLabel(due: string): { text: string; modifier: 'due' | 'overdue' | null } {
  const d = new Date(due)
  if (Number.isNaN(d.getTime())) return { text: due, modifier: null }
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const dueDay = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const days = Math.round((dueDay.getTime() - today.getTime()) / 86400000)
  if (days < 0) return { text: 'overdue', modifier: 'overdue' }
  if (days === 0) return { text: 'today', modifier: 'due' }
  if (days === 1) return { text: 'tomorrow', modifier: 'due' }
  if (days <= 7) return { text: 'this week', modifier: 'due' }
  return { text: due, modifier: null }
}

interface Props { spaceId: string }

export function SpaceTasksTab({ spaceId }: Props) {
  const [newListName, setNewListName] = useState('')
  const [newTaskTitle, setNewTaskTitle] = useState('')

  useEffect(() => {
    void (async () => {
      try {
        const rows = await api.get(
          `/api/spaces/${spaceId}/tasks/lists`,
        ) as SpaceTaskList[]
        lists.value = rows
        if (rows.length > 0) {
          activeList.value = rows[0].id
          await loadTasksForList(rows[0].id)
        }
      } finally {
        loading.value = false
      }
    })()
  }, [spaceId])

  const loadTasksForList = async (listId: string) => {
    activeList.value = listId
    try {
      tasks.value = await api.get(
        `/api/spaces/${spaceId}/tasks/lists/${listId}/tasks`,
      ) as TaskItem[]
    } catch (err: unknown) {
      showToast(`Could not load tasks: ${(err as Error).message ?? err}`, 'error')
      tasks.value = []
    }
  }

  const submitList = async (e: Event) => {
    e.preventDefault()
    const name = newListName.trim()
    if (!name) return
    try {
      const lst = await api.post(
        `/api/spaces/${spaceId}/tasks/lists`, { name },
      ) as SpaceTaskList
      lists.value = [...lists.value, lst]
      setNewListName('')
      await loadTasksForList(lst.id)
    } catch (err: unknown) {
      showToast(`Could not create list: ${(err as Error).message ?? err}`, 'error')
    }
  }

  const submitTask = async (e: Event) => {
    e.preventDefault()
    const title = newTaskTitle.trim()
    if (!title || !activeList.value) return
    try {
      const t = await api.post(
        `/api/spaces/${spaceId}/tasks/lists/${activeList.value}/tasks`,
        { title },
      ) as TaskItem
      tasks.value = [...tasks.value, t]
      setNewTaskTitle('')
    } catch (err: unknown) {
      showToast(`Could not add task: ${(err as Error).message ?? err}`, 'error')
    }
  }

  const cycleStatus = async (t: TaskItem) => {
    const next: 'todo' | 'in_progress' | 'done' =
      t.status === 'todo' ? 'in_progress'
        : t.status === 'in_progress' ? 'done'
          : 'todo'
    try {
      const updated = await api.patch(
        `/api/spaces/${spaceId}/tasks/${t.id}`, { status: next },
      ) as TaskItem
      tasks.value = tasks.value.map(x => x.id === t.id ? updated : x)
    } catch (err: unknown) {
      showToast(`Update failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  const deleteTask = async (t: TaskItem) => {
    if (!confirm(`Delete "${t.title}"?`)) return
    try {
      await api.delete(`/api/spaces/${spaceId}/tasks/${t.id}`)
      tasks.value = tasks.value.filter(x => x.id !== t.id)
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  if (loading.value) return <Spinner />

  const me = currentUser.value
  const userNameById = (uid: string): string => {
    if (me?.user_id === uid) return 'you'
    const found = householdUsers.value.get(uid)
    return found?.display_name || found?.username || uid.slice(0, 6)
  }

  const visible = tasks.value
    .filter(t => t.list_id === activeList.value)
    .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))

  return (
    <div class="sh-tasks">
      <aside class="sh-tasks-sidebar">
        <h3 style={{ margin: 0, padding: '0 0.5rem' }}>Lists</h3>
        {lists.value.map(l => (
          <div key={l.id} class="sh-task-list-row">
            <button
              type="button"
              class={
                activeList.value === l.id
                  ? 'sh-task-list-btn sh-task-list-btn--active'
                  : 'sh-task-list-btn'
              }
              onClick={() => void loadTasksForList(l.id)}
            >
              {l.name}
            </button>
          </div>
        ))}
        <form class="sh-form-row" onSubmit={submitList}
              style={{ marginTop: '0.5rem' }}>
          <input
            type="text"
            value={newListName}
            placeholder="+ New list"
            onInput={(e) => setNewListName((e.target as HTMLInputElement).value)}
            aria-label="New list name"
          />
        </form>
      </aside>

      <div class="sh-tasks-content">
        <div class="sh-page-header">
          {activeList.value && (
            <span class="sh-muted">
              {visible.filter(t => t.status !== 'done').length} open ·{' '}
              {visible.filter(t => t.status === 'done').length} done
            </span>
          )}
        </div>

        {activeList.value && (
          <form class="sh-form-row sh-composer" onSubmit={submitTask}
                style={{ marginBottom: 0, padding: '0.5rem 0.75rem' }}>
            <input
              type="text"
              value={newTaskTitle}
              placeholder="Add a task and press Enter…"
              onInput={(e) => setNewTaskTitle((e.target as HTMLInputElement).value)}
              aria-label="New task title"
            />
            <Button type="submit" disabled={!newTaskTitle.trim()}>Add</Button>
          </form>
        )}

        {visible.length > 0 && (
          <ul class="sh-list-card" style={{ listStyle: 'none', margin: 0 }}>
            {visible.map(t => {
              const due = t.due_date ? dueLabel(t.due_date) : null
              const owners = (t.assignees ?? [])
                .map(uid => userNameById(uid))
                .filter(Boolean)
              const ownerLine =
                owners.length > 0
                  ? owners.join(' · ')
                  : t.created_by ? `+ ${userNameById(t.created_by)}` : ''
              return (
                <li key={t.id}
                    class={`sh-task-row ${t.status === 'done' ? 'sh-task--done' : ''}`}>
                  <input type="checkbox" checked={t.status === 'done'}
                    onChange={() => void cycleStatus({
                      ...t,
                      status: t.status === 'done' ? 'todo' : 'done',
                    })}
                    aria-label={`Toggle ${t.title}`} />
                  <span class="sh-task-title">{t.title}</span>
                  <div class="sh-task-meta">
                    {ownerLine && <span class="sh-byline">{ownerLine}</span>}
                    {due && <span class="sh-byline">· {due.text}</span>}
                    <button type="button"
                      class={`sh-task-status sh-task-status--${t.status}`}
                      onClick={() => void cycleStatus(t)}
                      aria-label={`Status: ${STATUS_LABEL[t.status]}`}>
                      {STATUS_LABEL[t.status]}
                    </button>
                  </div>
                  {due?.modifier === 'overdue' && (
                    <span class="sh-task-pin sh-task-pin--overdue">overdue</span>
                  )}
                  {due?.modifier === 'due' && (
                    <span class="sh-task-pin sh-task-pin--due">due</span>
                  )}
                  <button type="button" class="sh-icon-btn"
                          aria-label={`Delete ${t.title}`}
                          onClick={() => void deleteTask(t)}>🗑️</button>
                </li>
              )
            })}
          </ul>
        )}

        {visible.length === 0 && activeList.value && (
          <div class="sh-empty-state">
            <div style={{ fontSize: '2rem' }}>✅</div>
            <h3>All caught up</h3>
            <p>No tasks in this list. Type above to add one.</p>
          </div>
        )}
        {!activeList.value && (
          <div class="sh-empty-state">
            <div style={{ fontSize: '2rem' }}>📋</div>
            <h3>No task lists yet</h3>
            <p>Create your first list in the sidebar to get started.</p>
          </div>
        )}
      </div>
    </div>
  )
}
