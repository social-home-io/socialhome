/**
 * Task scope — which task lists the shared task components work on,
 * who can be assigned, and what the viewer may change.
 *
 * The household Tasks tab and a space's Tasks tab render the same
 * board / list (``TasksView``); they differ only in the store
 * (``householdTaskStore`` or ``spaceTaskStore(id)``), the people
 * (household users, or the space's members incl. other households')
 * and the edit rule: household tasks are changed by their creator, an
 * assignee or an admin; space tasks by any writable member (a
 * subscriber, or anyone in an archived space, only reads). The default
 * is the household — the pattern of ``features/timetable/scope.ts``.
 */
import { createContext } from 'preact'
import { useContext } from 'preact/hooks'
import { householdTaskStore, canEditTask, type TaskStore } from '@/store/tasks'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'
import { spaceMembers, loadSpaceMembers } from '@/store/spaceMembers'
import { currentUser } from '@/store/auth'
import { t } from '@/i18n/i18n'
import type { Person } from '@/components/PeoplePicker'
import type { TaskItem } from '@/types'

export interface TaskScope {
  store: TaskStore
  /** Key for per-scope session state (filters). */
  key: string
  /** Everyone a task here can be assigned to (reads signals). */
  people: () => Person[]
  /** A user's name here — "You" for the viewer (reads signals). */
  nameOf: (uid: string) => string
  /** Make sure ``people`` is loaded. */
  loadPeople: () => void
  /** May the viewer change this task? */
  canEdit: (task: Pick<TaskItem, 'created_by' | 'assignees'>) => boolean
  /** May the viewer add tasks / lists here at all? */
  canWrite: boolean
  /** Why a task can't be changed (lock tooltip, read-only dialog). */
  readOnlyReason: () => string
}

export function householdPeople(): Person[] {
  return Array.from(householdUsers.value.values()).map(u => ({
    user_id: u.user_id,
    name: u.display_name || u.username,
    picture_url: u.picture_url,
  }))
}

export const HOUSEHOLD_TASK_SCOPE: TaskScope = {
  store: householdTaskStore,
  key: 'household',
  people: householdPeople,
  nameOf: uid => personName(householdPeople(), uid),
  loadPeople: () => { void loadHouseholdUsers() },
  canEdit: task => canEditTask(task, currentUser.value, null),
  canWrite: true,
  readOnlyReason: () => t('tasks.error.forbidden'),
}

/** A space's members as people: their name in the space (or the
 *  viewer's alias for them), with the household of a remote member. */
export function spacePeople(spaceId: string, { subscribers = false } = {}): Person[] {
  const roster = spaceMembers.value[spaceId]
  if (!roster) return []
  return Array.from(roster.values())
    // Subscribers only read — they aren't offered as assignees.
    .filter(m => subscribers || (m.role as string) !== 'subscriber')
    .map(m => ({
      user_id: m.user_id,
      name: m.personal_alias || m.space_display_name || m.display_name || m.user_id.slice(0, 8),
      picture_url: m.picture_url,
      detail: m.instance_id ? m.household_name ?? null : null,
    }))
}

export interface SpaceTaskScopeOpts {
  spaceId: string
  store: TaskStore
  /** Owner / admin / member of a live space. */
  writable: boolean
  archived: boolean
}

export function spaceTaskScope({ spaceId, store, writable, archived }: SpaceTaskScopeOpts): TaskScope {
  const canWrite = writable && !archived
  return {
    store,
    key: `space:${spaceId}`,
    people: () => spacePeople(spaceId),
    nameOf: uid => personName(spacePeople(spaceId, { subscribers: true }), uid),
    loadPeople: () => { void loadSpaceMembers(spaceId) },
    canEdit: () => canWrite,
    canWrite,
    readOnlyReason: () => t(archived ? 'tasks.board.read_only_archived' : 'tasks.board.read_only_space'),
  }
}

export const TaskScopeContext = createContext<TaskScope>(HOUSEHOLD_TASK_SCOPE)

export function useTaskScope(): TaskScope {
  return useContext(TaskScopeContext)
}

/** "You" for the viewer, else the person's name, else a short id. */
export function personName(people: readonly Person[], uid: string): string {
  if (currentUser.value?.user_id === uid) return t('tasks.you')
  const p = people.find(x => x.user_id === uid)
  if (p) return p.name
  const u = householdUsers.value.get(uid)
  return u?.display_name || u?.username || uid.slice(0, 6)
}
