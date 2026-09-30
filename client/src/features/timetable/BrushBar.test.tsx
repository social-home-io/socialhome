import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, within, cleanup } from '@testing-library/preact'

const apiPut = vi.fn()
const apiGet = vi.fn()
vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return {
    ApiError: actual.ApiError,
    api: {
      get: (...a: unknown[]) => apiGet(...a), post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      put: (...a: unknown[]) => apiPut(...a),
    },
  }
})
vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))
vi.mock('@/components/confirm', () => ({ confirmDialog: vi.fn() }))

import { timetables } from '@/store/timetables'
import { brushState, stopBrush } from './brush'
import { entryDialog } from './EntryDialog'
import { SelectedTimetable } from './SelectedTimetable'
import { entry, timetable } from './testUtils'
import type { Timetable, TimetableEntry } from '@/types'

let tt: Timetable
let mathe: TimetableEntry
let empty1: TimetableEntry
let empty2: TimetableEntry
let pause: TimetableEntry
let deutsch: TimetableEntry

beforeEach(() => {
  mathe = entry(0, '08:00', '08:45', { title: 'Mathe', icon: '🔢', room: '204' })
  empty1 = entry(0, '08:50', '09:35')
  pause = entry(0, '09:35', '09:55', { kind: 'break', title: 'Pause' })
  empty2 = entry(0, '09:55', '10:40', { room: 'B1' })
  deutsch = entry(1, '08:00', '08:45', { title: 'Deutsch', color: 'rose' })
  tt = timetable({ version: 5, days: [0, 1], entries: [mathe, empty1, pause, empty2, deutsch] })
  timetables.value = [tt]
  entryDialog.value = null
  localStorage.clear()
  for (const f of [apiPut, apiGet, showToast]) f.mockReset()
  apiPut.mockImplementation((_p: string, body: { entries: TimetableEntry[] }) =>
    Promise.resolve({ timetable: { ...timetables.value[0], version: timetables.value[0].version + 1,
      entries: body.entries } }))
})
afterEach(() => { cleanup(); stopBrush() })

function renderPage() {
  const utils = render(
    <SelectedTimetable tt={timetables.value[0]} narrow={false} week={null} onWeek={vi.fn()}
                       onDuplicate={vi.fn()} onNew={vi.fn()} onDelete={vi.fn()} />,
  )
  fireEvent.click(utils.getByRole('button', { name: 'Fill' }))
  return utils
}

const block = (c: Element, id: string) =>
  c.querySelector(`.sh-timetable-visual [data-entry-id="${id}"]`) as HTMLElement

const putEntries = (call = 0) =>
  Object.fromEntries((apiPut.mock.calls[call][1].entries as TimetableEntry[]).map(e => [e.id, e]))

describe('brush mode', () => {
  it('shows a radiogroup of the subjects (deduped) plus the eraser; the first is active', () => {
    const { getByRole } = renderPage()
    const group = getByRole('radiogroup', { name: 'Subject to fill in' })
    const radios = within(group).getAllByRole('radio')
    expect(radios.map(r => r.textContent)).toEqual(['🔢Mathe', 'Deutsch', '🧽 Clear'])
    expect(radios[0].getAttribute('aria-checked')).toBe('true')
    fireEvent.keyDown(radios[0], { key: 'ArrowRight' })
    expect(brushState.value?.active).toMatchObject({ kind: 'paint', title: 'Deutsch' })
  })

  it('a click applies the brush (title, icon, colour; room kept) and never opens the dialog', async () => {
    const { container } = renderPage()
    fireEvent.click(block(container, empty2.id))
    await waitFor(() => expect(apiPut).toHaveBeenCalledTimes(1))
    expect(entryDialog.value).toBeNull()
    expect(apiPut.mock.calls[0][1].version).toBe(5)
    expect(putEntries()[empty2.id]).toMatchObject({ title: 'Mathe', icon: '🔢', color: null, room: 'B1' })
    expect(putEntries()[empty1.id].title).toBeNull()
    const [msg, , opts] = showToast.mock.calls[0]
    expect(msg).toBe('Filled with Mathe')
    expect(opts.action.label).toBe('Undo')
  })

  it('an empty lesson takes the room of the brush; the label says what a press does', async () => {
    const { container } = renderPage()
    expect(block(container, empty1.id).getAttribute('aria-label'))
      .toMatch(/^Fill with Mathe: Monday, 2nd lesson/)
    // Breaks are inert: no prefix, aria-disabled, and no PUT.
    const br = block(container, pause.id)
    expect(br.getAttribute('aria-disabled')).toBe('true')
    expect(br.getAttribute('aria-label')).not.toMatch(/^Fill/)
    fireEvent.click(br)
    fireEvent.click(block(container, empty1.id))
    await waitFor(() => expect(apiPut).toHaveBeenCalledTimes(1))
    expect(putEntries()[empty1.id]).toMatchObject({ title: 'Mathe', room: '204' })
    expect(putEntries()[pause.id]).toEqual(pause)
  })

  it('a pointer drag across blocks commits ONE PUT on pointerup, skipping breaks', async () => {
    const { container } = renderPage()
    fireEvent.pointerDown(block(container, empty1.id), { clientX: 10, clientY: 10 })
    fireEvent.pointerEnter(block(container, pause.id))
    fireEvent.pointerEnter(block(container, empty2.id))
    expect(block(container, empty2.id).classList.contains('is-stroke')).toBe(true)
    fireEvent.pointerUp(document, { clientX: 10, clientY: 200 })
    // The click the browser sends after pointerup is swallowed.
    fireEvent.click(block(container, empty2.id))
    await waitFor(() => expect(apiPut).toHaveBeenCalledTimes(1))
    const byId = putEntries()
    expect(byId[empty1.id].title).toBe('Mathe')
    expect(byId[empty2.id].title).toBe('Mathe')
    expect(byId[pause.id]).toEqual(pause)
    expect(showToast.mock.calls[0][0]).toBe('2 lessons filled with Mathe')
  })

  it('the eraser clears title, icon, colour, room and teacher of lessons', async () => {
    const { container, getByRole } = renderPage()
    fireEvent.click(getByRole('radio', { name: /Clear/ }))
    expect(block(container, mathe.id).getAttribute('aria-label')).toMatch(/^Clear: /)
    fireEvent.click(block(container, mathe.id))
    await waitFor(() => expect(apiPut).toHaveBeenCalled())
    expect(putEntries()[mathe.id]).toMatchObject({ title: null, icon: null, color: null, room: null, teacher: null })
  })

  it('keyboard activation of a focused block (Enter / Space → click) applies the brush', async () => {
    const { container } = renderPage()
    const b = block(container, empty1.id)
    b.focus()
    fireEvent.click(b, { detail: 0 })
    await waitFor(() => expect(apiPut).toHaveBeenCalledTimes(1))
  })

  it('+ New subject makes a brush from title, icon and colour', async () => {
    const { container, getByRole, getByLabelText } = renderPage()
    fireEvent.click(getByRole('button', { name: '+ New subject' }))
    const dialog = getByRole('dialog', { name: 'New subject' })
    fireEvent.input(within(dialog).getByLabelText('Title'), { target: { value: 'Sachkunde' } })
    fireEvent.click(getByLabelText('Moss'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Use this subject' }))
    expect(brushState.value?.active).toMatchObject({ title: 'Sachkunde', icon: '🌱', color: 'moss' })
    expect(getByRole('radio', { name: /Sachkunde/ }).getAttribute('aria-checked')).toBe('true')
    fireEvent.click(block(container, empty1.id))
    await waitFor(() => expect(apiPut).toHaveBeenCalled())
    expect(putEntries()[empty1.id]).toMatchObject({ title: 'Sachkunde', icon: '🌱', color: 'moss' })
  })

  it('Esc, Done or the toggle leave brush mode; blocks open the dialog again', () => {
    const { container, getByRole, queryByRole } = renderPage()
    expect(getByRole('button', { name: 'Fill' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(brushState.value).toBeNull()
    expect(queryByRole('radiogroup')).toBeNull()
    fireEvent.click(block(container, mathe.id))
    expect(entryDialog.value?.entry?.id).toBe(mathe.id)
    entryDialog.value = null
    fireEvent.click(getByRole('button', { name: 'Fill' }))
    fireEvent.click(getByRole('button', { name: 'Done' }))
    expect(brushState.value).toBeNull()
  })

  it('a merged double lesson is brushed as a whole — both halves, one PUT', async () => {
    const d1 = entry(0, '08:00', '08:45', { title: 'Mathe' })
    const d2 = entry(0, '08:50', '09:35', { title: 'Mathe' })
    const other = entry(0, '09:55', '10:40', { title: 'Deutsch' })
    timetables.value = [timetable({ version: 2, days: [0], entries: [d1, d2, other] })]
    const { container, getByRole } = renderPage()
    expect(container.querySelector('.sh-timetable-visual td[rowspan="2"]')).not.toBeNull()
    fireEvent.click(getByRole('radio', { name: /Deutsch/ }))
    fireEvent.click(block(container, d1.id))
    await waitFor(() => expect(apiPut).toHaveBeenCalledTimes(1))
    expect(putEntries()[d1.id].title).toBe('Deutsch')
    expect(putEntries()[d2.id].title).toBe('Deutsch')
    expect(showToast.mock.calls[0][0]).toBe('2 lessons filled with Deutsch')
  })

  it('the day-heading "+" and empty-cell "+" do not open the dialog in brush mode', () => {
    const d1 = entry(0, '08:00', '08:45', { title: 'Mathe' })
    const t1 = entry(1, '08:00', '08:45', { title: 'Deutsch' })
    const t2 = entry(1, '08:50', '09:35', { title: 'Sport' })
    timetables.value = [timetable({ version: 2, days: [0, 1], entries: [d1, t1, t2] })]
    const { getByRole, container } = renderPage()
    fireEvent.click(getByRole('button', { name: 'Periods' }))
    const head = getByRole('button', { name: /^Add a lesson on Monday — Brush mode is on/ })
    expect(head.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(head)
    const cell = container.querySelector<HTMLElement>('.sh-timetable-visual .sh-timetable-cell-add')!
    expect(cell.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(cell)
    expect(entryDialog.value).toBeNull()
  })
})
