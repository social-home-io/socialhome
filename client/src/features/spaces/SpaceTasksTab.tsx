/**
 * SpaceTasksTab — a space's task lists, inside SpaceFeedPage's
 * ``activeTab === 'tasks'`` branch.
 *
 * The same board / list as the household Tasks tab (``TasksView``),
 * under a space task scope: ``spaceTaskStore(spaceId)`` (the
 * ``/api/spaces/{id}/tasks/...`` routes, live through the ``task.*``
 * WS frames that carry this ``space_id``), the space's members as
 * people (incl. members from other households, by their name in the
 * space) and collaborative edit rights — any writable member may
 * change any task; a subscriber, anyone while the space is archived,
 * and a member or moderator of an ADMIN_ONLY tasks feature only read.
 */
import { useMemo } from 'preact/hooks'
import { ListSkeleton } from '@/components/Skeleton'
import { t } from '@/i18n/i18n'
import { spaceTaskStore } from '@/store/tasks'
import { TasksView } from '@/features/tasks/TaskPage'
import { TaskScopeContext, spaceTaskScope } from '@/features/tasks/scope'

interface Props {
  spaceId: string
  /** Owner / admin / member — a subscriber (or no role) reads only.
   *  ``undefined`` while the viewer's role is still loading. */
  writable: boolean | undefined
  /** An archived space is read-only for everyone. */
  archived?: boolean
  /** The space keeps tasks to its admins (``tasks_access`` ADMIN_ONLY,
   *  §4.3) — the read-only reason says so. */
  adminOnly?: boolean
}

export function SpaceTasksTab({ spaceId, writable, archived = false, adminOnly = false }: Props) {
  const scope = useMemo(
    () => spaceTaskScope({
      spaceId, store: spaceTaskStore(spaceId), writable: !!writable, archived, adminOnly,
    }),
    [spaceId, writable, archived, adminOnly],
  )
  if (writable === undefined) {
    return <ListSkeleton variant="board" rows={3} label={t('tasks.loading')} />
  }
  return (
    <TaskScopeContext.Provider value={scope}>
      <div class="sh-space-tasks">
        <TasksView />
      </div>
    </TaskScopeContext.Provider>
  )
}
