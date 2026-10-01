import { describe, it, expect, beforeEach } from 'vitest'
import { loadTaskView, saveTaskView, DEFAULT_TASK_VIEW } from './viewPrefs'

describe('task view prefs', () => {
  beforeEach(() => localStorage.clear())
  it('defaults to the board', () => {
    expect(DEFAULT_TASK_VIEW).toBe('board')
    expect(loadTaskView('l1')).toBe('board')
  })
  it('remembers the choice per list under sh-tasks-view:<id>', () => {
    saveTaskView('l1', 'list')
    expect(localStorage.getItem('sh-tasks-view:l1')).toBe('list')
    expect(loadTaskView('l1')).toBe('list')
    expect(loadTaskView('l2')).toBe('board')
  })
  it('falls back to the board when storage throws', () => {
    const orig = Storage.prototype.getItem
    Storage.prototype.getItem = () => { throw new Error('blocked') }
    try {
      expect(loadTaskView('l1')).toBe('board')
    } finally {
      Storage.prototype.getItem = orig
    }
  })
})

describe('useTaskView', () => {
  it('reads the new list\'s view in the same render it switches (no flash)', async () => {
    const { render } = await import('@testing-library/preact')
    const { useTaskView } = await import('./viewPrefs')
    localStorage.clear()
    saveTaskView('l2', 'list')
    const seen: string[] = []
    function Probe({ id }: { id: string }) {
      const [view] = useTaskView(id)
      seen.push(`${id}:${view}`)
      return null
    }
    const r = render(<Probe id="l1" />)
    r.rerender(<Probe id="l2" />)
    expect(seen).toEqual(['l1:board', 'l2:list'])
  })
})
