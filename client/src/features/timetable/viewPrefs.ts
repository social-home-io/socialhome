/**
 * Per-timetable view preferences — layout override, Picture view, List
 * view — remembered in ``localStorage`` (a per-viewer convenience: a
 * missing / blocked storage just falls back to the defaults).
 */
import { useEffect, useState } from 'preact/hooks'

export type LayoutChoice = 'auto' | 'periods' | 'timeline'

export interface ViewPrefs {
  layout: LayoutChoice
  picture: boolean
  list: boolean
}

export const DEFAULT_VIEW_PREFS: ViewPrefs = { layout: 'auto', picture: false, list: false }

const key = (id: string) => `sh-timetable-view:${id}`

export function loadViewPrefs(id: string): ViewPrefs {
  try {
    const raw = localStorage.getItem(key(id))
    if (!raw) return DEFAULT_VIEW_PREFS
    const v = JSON.parse(raw) as Partial<ViewPrefs>
    return {
      layout: v.layout === 'periods' || v.layout === 'timeline' ? v.layout : 'auto',
      picture: v.picture === true,
      list: v.list === true,
    }
  } catch {
    return DEFAULT_VIEW_PREFS
  }
}

export function saveViewPrefs(id: string, prefs: ViewPrefs): void {
  try {
    localStorage.setItem(key(id), JSON.stringify(prefs))
  } catch {
    // Storage full / blocked — the choice just lasts for this visit.
  }
}

/** ``[prefs, update]`` for timetable ``id``; re-reads when ``id`` changes. */
export function useViewPrefs(id: string): [ViewPrefs, (patch: Partial<ViewPrefs>) => void] {
  const [prefs, setPrefs] = useState<ViewPrefs>(() => loadViewPrefs(id))
  useEffect(() => { setPrefs(loadViewPrefs(id)) }, [id])
  const update = (patch: Partial<ViewPrefs>) => {
    setPrefs(prev => {
      const next = { ...prev, ...patch }
      saveViewPrefs(id, next)
      return next
    })
  }
  return [prefs, update]
}
