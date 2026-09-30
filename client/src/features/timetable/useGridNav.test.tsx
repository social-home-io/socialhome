import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

const apiPatch = vi.fn()
const apiDelete = vi.fn()
const apiPut = vi.fn()
vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return {
    ApiError: actual.ApiError,
    api: {
      get: vi.fn(), post: vi.fn(),
      patch: (...a: unknown[]) => apiPatch(...a),
      put: (...a: unknown[]) => apiPut(...a),
      delete: (...a: unknown[]) => apiDelete(...a),
    },
  }
})
vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))

import { timetables } from '@/store/timetables'
import { TimetableGrid, copiedBlock } from './TimetableGrid'
import { DEFAULT_VIEW_PREFS, type ViewPrefs } from './viewPrefs'
import { entry, schoolWeek, timetable } from './testUtils'
import type { Timetable, TimetableEntry } from '@/types'

const WED = new Date(2026, 8, 30, 10, 0)

function renderGrid(tt: Timetable, prefs: Partial<ViewPrefs> = {}) {
  timetables.value = [tt]
  const onEdit = vi.fn<(e: TimetableEntry, group?: string[]) => void>()
  const utils = render(
    <TimetableGrid tt={tt} prefs={{ ...DEFAULT_VIEW_PREFS, ...prefs }} onPrefs={vi.fn()}
                   onEdit={onEdit} onAdd={vi.fn()} now={WED} />,
  )
  const nav = () => Array.from(utils.container.querySelectorAll<HTMLElement>(
    '.sh-timetable-visual [data-tt-nav]'))
  const byId = (id: string) =>
    utils.container.querySelector<HTMLElement>(`.sh-timetable-visual [data-entry-id="${id}"]`)!
  return { ...utils, onEdit, nav, byId }
}

beforeEach(() => {
  copiedBlock.value = null
  for (const f of [apiPatch, apiDelete, apiPut, showToast]) f.mockReset()
})
afterEach(() => cleanup())

// Timeline: Monday 08:00 / 09:00 / 10:00, Tuesday 07:15 / 09:10, Wednesday 08:00.
function timelineTt() {
  const m1 = entry(0, '08:00', '08:45', { title: 'Mathe', room: '204', icon: '🔢' })
  const m2 = entry(0, '09:00', '09:45', { title: 'Deutsch' })
  const m3 = entry(0, '10:00', '10:20', { kind: 'break', title: 'Pause' })
  const t1 = entry(1, '07:15', '08:00', { title: 'Musik' })
  const t2 = entry(1, '09:10', '09:55')
  const w1 = entry(2, '08:00', '08:45', { title: 'Sport' })
  return { tt: timetable({ days: [0, 1, 2], entries: [m1, m2, m3, t1, t2, w1] }), m1, m2, m3, t1, t2, w1 }
}

describe('grid keyboard navigation', () => {
  it('is one Tab stop (roving tabindex) over the blocks', () => {
    const { tt } = timelineTt()
    const { nav } = renderGrid(tt)
    const stops = nav().filter(el => el.tabIndex === 0)
    expect(stops).toHaveLength(1)
    expect(nav().length).toBeGreaterThan(5)
  })

  it('↑/↓ move within the day, ←/→ to the nearest start on the next day, Home/End to the ends', () => {
    const { tt, m1, m2, m3, t2, w1 } = timelineTt()
    const { byId } = renderGrid(tt)
    byId(m1.id).focus()
    fireEvent.keyDown(byId(m1.id), { key: 'ArrowDown' })
    expect(document.activeElement).toBe(byId(m2.id))
    expect(byId(m2.id).tabIndex).toBe(0)
    expect(byId(m1.id).tabIndex).toBe(-1)
    fireEvent.keyDown(byId(m2.id), { key: 'ArrowRight' }) // 09:00 → Tue 09:10
    expect(document.activeElement).toBe(byId(t2.id))
    fireEvent.keyDown(byId(t2.id), { key: 'ArrowRight' }) // → Wed 08:00 (only one)
    expect(document.activeElement).toBe(byId(w1.id))
    fireEvent.keyDown(byId(w1.id), { key: 'ArrowRight' }) // last day: stays
    expect(document.activeElement).toBe(byId(w1.id))
    fireEvent.keyDown(byId(w1.id), { key: 'ArrowLeft' })
    fireEvent.keyDown(byId(t2.id), { key: 'ArrowLeft' })
    expect(document.activeElement).toBe(byId(m2.id))
    fireEvent.keyDown(byId(m2.id), { key: 'End' })
    expect(document.activeElement).toBe(byId(m3.id))
    fireEvent.keyDown(byId(m3.id), { key: 'Home' })
    expect(document.activeElement).toBe(byId(m1.id))
  })

  it('reaches a Periods break band and keeps the column for ←/→', () => {
    const tt = timetable({ entries: schoolWeek([0, 1, 2]), days: [0, 1, 2] })
    const { container, byId } = renderGrid(tt)
    const tue = tt.entries.filter(e => e.weekday === 1)
    const wed = tt.entries.filter(e => e.weekday === 2)
    byId(tue[1].id).focus() // Tue 2nd lesson (08:50)
    fireEvent.keyDown(byId(tue[1].id), { key: 'ArrowDown' })
    const band = container.querySelector<HTMLElement>('.sh-timetable-visual .sh-timetable-band')!
    expect(document.activeElement).toBe(band)
    fireEvent.keyDown(band, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(byId(tue[3].id)) // Tue 09:55
    fireEvent.keyDown(byId(tue[3].id), { key: 'ArrowRight' })
    expect(document.activeElement).toBe(byId(wed[3].id))
  })

  it('Enter opens (the block\'s own click)', () => {
    const { tt, m1 } = timelineTt()
    const { byId, onEdit } = renderGrid(tt)
    byId(m1.id).focus()
    fireEvent.click(document.activeElement!)
    expect(onEdit).toHaveBeenCalledWith(expect.objectContaining({ id: m1.id }))
  })

  it('Delete removes a lesson with an Undo toast — but never a break', async () => {
    const { tt, m1, m3 } = timelineTt()
    apiDelete.mockResolvedValue({ timetable: { ...tt, version: 2, entries: tt.entries.filter(e => e.id !== m1.id) } })
    const { byId } = renderGrid(tt)
    byId(m3.id).focus()
    fireEvent.keyDown(byId(m3.id), { key: 'Delete' })
    expect(apiDelete).not.toHaveBeenCalled()
    byId(m1.id).focus()
    fireEvent.keyDown(byId(m1.id), { key: 'Delete' })
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith(`/api/timetables/tt1/entries/${m1.id}?version=1`))
    await waitFor(() => expect(showToast).toHaveBeenCalled())
    const [msg, , opts] = showToast.mock.calls[0]
    expect(msg).toBe('Lesson deleted')
    apiPut.mockResolvedValue({ timetable: { ...tt, version: 3 } })
    opts.action.onClick()
    await waitFor(() => expect(apiPut).toHaveBeenCalledWith('/api/timetables/tt1/entries',
      { version: 2, entries: tt.entries }))
  })

  it('Ctrl+C copies a block, Ctrl+V pastes its subject onto another lesson (PATCH + Undo), not onto a break', async () => {
    const { tt, m1, m3, t2 } = timelineTt()
    apiPatch.mockResolvedValue({ timetable: { ...tt, version: 2 } })
    const { byId } = renderGrid(tt)
    byId(m1.id).focus()
    fireEvent.keyDown(byId(m1.id), { key: 'c', ctrlKey: true })
    expect(copiedBlock.value).toMatchObject({ title: 'Mathe', icon: '🔢', room: '204' })
    expect(showToast.mock.calls[0][0]).toMatch(/^Copied “Mathe”/)
    byId(m3.id).focus()
    fireEvent.keyDown(byId(m3.id), { key: 'v', metaKey: true })
    expect(apiPatch).not.toHaveBeenCalled()
    byId(t2.id).focus()
    fireEvent.keyDown(byId(t2.id), { key: 'v', ctrlKey: true })
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith(`/api/timetables/tt1/entries/${t2.id}`, {
      version: 1, title: 'Mathe', icon: '🔢', color: null, room: '204', teacher: null,
    }))
    await waitFor(() => expect(showToast).toHaveBeenCalledTimes(2))
    expect(showToast.mock.calls[1][0]).toBe('Pasted “Mathe”')
    expect(showToast.mock.calls[1][2].action.label).toBe('Undo')
  })
})
