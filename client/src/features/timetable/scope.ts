/**
 * Timetable scope — which timetables the shared timetable components
 * work on, and what the viewer may do with them.
 *
 * The household Timetable page and a space's Timetable tab render the
 * same components (grid, day view, dialogs, brush, week mode, print);
 * they differ only in the store they read and write (the household's
 * ``/api/timetables`` or a space's ``/api/spaces/{id}/timetables``),
 * whether the viewer may edit (space members read, owners / admins
 * edit) and whether timetables have assignees (household only). The
 * default is the household, fully editable — so a component rendered
 * without a provider behaves exactly as before scopes existed.
 */
import { createContext } from 'preact'
import { useContext } from 'preact/hooks'
import { householdTimetableStore, type TimetableStore } from '@/store/timetables'

export interface TimetableScope {
  store: TimetableStore
  /** ``false`` = view only: every editing affordance is hidden. */
  editable: boolean
  /** Timetables of this scope belong to household members. */
  assignees: boolean
}

export const HOUSEHOLD_SCOPE: TimetableScope = {
  store: householdTimetableStore,
  editable: true,
  assignees: true,
}

export const TimetableScopeContext = createContext<TimetableScope>(HOUSEHOLD_SCOPE)

export function useTimetableScope(): TimetableScope {
  return useContext(TimetableScopeContext)
}
