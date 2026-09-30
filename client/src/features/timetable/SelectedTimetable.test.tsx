/**
 * Week mode ("This week"): URL state, the navigator, the rendering of
 * cancelled / changed / extra lessons, the override actions, clear all
 * + undo, the holiday banner, locked past days and the WS refetch.
 */
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { render, fireEvent, waitFor, within, cleanup } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiPut = vi.fn()
const apiDelete = vi.fn()
vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return {
    ApiError: actual.ApiError,
    api: {
      get: (...a: unknown[]) => apiGet(...a),
      post: (...a: unknown[]) => apiPost(...a),
      patch: (...a: unknown[]) => apiPatch(...a),
      put: (...a: unknown[]) => apiPut(...a),
      delete: (...a: unknown[]) => apiDelete(...a),
    },
  }
})
const wsHandlers: Record<string, (e: { type: string; data: Record<string, unknown> }) => void> = {}
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { type: string; data: Record<string, unknown> }) => void) => {
      wsHandlers[type] = h
      return () => {}
    },
  },
}))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))
vi.mock('@/components/confirm', () => ({ confirmDialog: vi.fn() }))
vi.mock('@/store/pageTitle', () => ({ useTitle: vi.fn() }))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map() },
  loadHouseholdUsers: vi.fn(),
  householdDisplayName: (id: string) => id,
  householdPictureUrl: () => null,
}))
vi.mock('@/store/auth', () => ({ currentUser: { value: { user_id: 'u1', username: 'p', display_name: 'P' } } }))

import { loaded, selectedId, timetables, wireTimetablesWs } from '@/store/timetables'
import { EntryDialog, entryDialog } from './EntryDialog'
import { SelectedTimetable } from './SelectedTimetable'
import TimetablePage from './TimetablePage'
import { WeekBar } from './WeekBar'
import { entry, timetable } from './testUtils'
import type { EffectiveLesson, ResolvedWeek, Timetable, TimetableEntry } from '@/types'

// Wed 7 Oct 2026 — the week of Mon 5 Oct.
const NOW = new Date(2026, 9, 7, 10, 0)

let tt: Timetable
let mathe: TimetableEntry
let deutsch: TimetableEntry
let sport: TimetableEntry

function lesson(e: TimetableEntry, date: string, extra: Partial<EffectiveLesson> = {}): EffectiveLesson {
  const { id, weekday: _wd, ...rest } = e
  void _wd
  return { source_id: id, date, status: 'normal', override_id: null, original: null, ...rest, ...extra }
}

function week(lessons: EffectiveLesson[], extra: Partial<ResolvedWeek> = {}): ResolvedWeek {
  const days = ['2026-10-05', '2026-10-06']
  return {
    anchor: '2026-10-05', valid: true,
    days: days.map(date => ({ date, valid: true, lessons: lessons.filter(l => l.date === date) })),
    ...extra,
  }
}

beforeEach(() => {
  mathe = entry(0, '08:00', '08:45', { title: 'Mathe', room: '204' })
  deutsch = entry(0, '08:50', '09:35', { title: 'Deutsch' })
  sport = entry(1, '08:00', '08:45', { title: 'Sport' })
  tt = timetable({ version: 3, days: [0, 1], entries: [mathe, deutsch, sport] })
  timetables.value = [tt]
  loaded.value = false
  selectedId.value = null
  entryDialog.value = null
  localStorage.clear()
  for (const f of [apiGet, apiPost, apiPatch, apiPut, apiDelete, showToast]) f.mockReset()
})
afterEach(() => cleanup())

function renderWeek(w: ResolvedWeek, date = '2026-10-07') {
  apiGet.mockResolvedValue({ week: w })
  const onWeek = vi.fn()
  const utils = render(
    <>
      <SelectedTimetable tt={timetables.value[0]} narrow={false} week={date} onWeek={onWeek}
                         onDuplicate={vi.fn()} onNew={vi.fn()} onDelete={vi.fn()} now={NOW} />
      <EntryDialog />
    </>,
  )
  return { ...utils, onWeek }
}

const block = (c: Element, id: string) =>
  c.ownerDocument.querySelector(`.sh-timetable-visual [data-entry-id="${id}"]`) as HTMLElement

describe('week mode — URL state', () => {
  function renderAt(url: string) {
    window.history.replaceState(null, '', url)
    apiGet.mockImplementation((p: string) => Promise.resolve(p === '/api/timetables'
      ? { timetables: [tt] }
      : { week: week([lesson(mathe, '2026-10-05')]) }))
    return render(<LocationProvider><TimetablePage /></LocationProvider>)
  }

  it('the presence of &week= means week mode; Regular drops it, This week sets it', async () => {
    const { findByRole, getByRole } = renderAt('/calendar?tab=timetable&tt=tt1&week=2026-10-07')
    const regular = await findByRole('button', { name: 'Regular' })
    expect(getByRole('button', { name: 'This week', pressed: true })).toBeTruthy()
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/timetables/tt1/weeks/2026-10-05'))
    fireEvent.click(regular)
    await waitFor(() => expect(window.location.search).toBe('?tab=timetable&tt=tt1'))
    fireEvent.click(getByRole('button', { name: 'This week', pressed: false }))
    await waitFor(() => expect(window.location.search).toMatch(/^\?tab=timetable&tt=tt1&week=\d{4}-\d{2}-\d{2}$/))
  })

  it('defaults to Regular', async () => {
    const { findByRole } = renderAt('/calendar?tab=timetable&tt=tt1')
    expect((await findByRole('button', { name: 'Regular' })).getAttribute('aria-pressed')).toBe('true')
    expect(apiGet).not.toHaveBeenCalledWith(expect.stringContaining('/weeks/'))
  })
})

describe('week mode — navigator', () => {
  it('steps by a week from the Sunday anchor and jumps back to this week', () => {
    const sun = timetable({ week_start: 6, days: [0, 1, 2, 3, 4] })
    const onWeek = vi.fn()
    const { getByRole, getByText } = render(
      <WeekBar tt={sun} anchor="2026-10-11" onWeek={onWeek} now={NOW} />)
    expect(getByText((s) => s.replace(/\s/g, ' ') === 'Oct 11 – 17')).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Previous week' }))
    expect(onWeek).toHaveBeenLastCalledWith('2026-10-04')
    fireEvent.click(getByRole('button', { name: 'Next week' }))
    expect(onWeek).toHaveBeenLastCalledWith('2026-10-18')
    fireEvent.click(getByRole('button', { name: 'Today' }))
    expect(onWeek).toHaveBeenLastCalledWith('2026-10-04')
  })

  it('labels a Monday-start week with its ISO number; regular mode links to this week\'s changes', () => {
    const onWeek = vi.fn()
    const withOv = timetable({ overrides: [
      { id: 'o1', date: '2026-10-06', kind: 'cancel', entry_id: 'x', start: null, end: null,
        entry_kind: 'lesson', label: null, title: null, room: null, teacher: null, note: null,
        color: null, icon: null },
    ] })
    const a = render(<WeekBar tt={withOv} anchor="2026-10-05" onWeek={onWeek} now={NOW} />)
    expect(a.getByText((s) => s.replace(/\s/g, ' ') === 'W41 · Oct 5 – 11')).toBeTruthy()
    expect((a.getByRole('button', { name: 'Today' }) as HTMLButtonElement).disabled)
      .toBe(true)
    cleanup()
    const b = render(<WeekBar tt={withOv} anchor={null} onWeek={onWeek} pending={1} now={NOW} />)
    fireEvent.click(b.getByRole('button', { name: 'Changes this week: 1' }))
    expect(onWeek).toHaveBeenLastCalledWith('2026-10-05')
  })
})

describe('week mode — rendering', () => {
  it('shows cancelled, changed and extra lessons, with dates in the headings', async () => {
    const w = week([
      lesson(mathe, '2026-10-05', { status: 'cancelled', override_id: 'o1', original: mathe }),
      lesson(deutsch, '2026-10-05', {
        status: 'changed', override_id: 'o2', original: deutsch, title: 'Englisch', start: '09:00',
        end: '09:45', room: '112',
      }),
      { ...lesson(sport, '2026-10-06', { status: 'added', override_id: 'o3', title: 'AG Chor', start: '14:00', end: '14:45' }), source_id: 'o3' },
    ])
    const { container, findByText } = renderWeek(w)
    await waitFor(() => expect(block(container, mathe.id)).not.toBeNull())
    const cancelled = block(container, mathe.id)
    expect(cancelled.classList.contains('sh-timetable-block--cancelled')).toBe(true)
    expect(cancelled.getAttribute('aria-label')).toMatch(/Mathe.*, cancelled$/)
    const changed = block(container, deutsch.id)
    expect(changed.classList.contains('sh-timetable-block--changed')).toBe(true)
    expect(changed.querySelector('.sh-timetable-block__change')!.textContent)
      .toBe('Deutsch → Englisch · 08:50 → 09:00 · Room 112')
    const extra = block(container, 'o3')
    expect(within(extra).getByText('Extra')).toBeTruthy()
    expect(within(cancelled).getByText('Cancelled')).toBeTruthy()
    // The print / list twin (rendered while printing) carries the statuses too.
    window.dispatchEvent(new Event('beforeprint'))
    onTestFinished(() => { window.dispatchEvent(new Event('afterprint')) })
    await waitFor(() => expect(container.querySelector('.sh-timetable-print')).not.toBeNull())
    const print = container.querySelector('.sh-timetable-print')!
    expect(print.querySelector('.sh-timetable-list__row--cancelled s')!.textContent).toBe('Mathe')
    const heads = Array.from(container.querySelectorAll('.sh-timetable-visual .sh-timetable-dayhead__name > [aria-hidden]'))
    expect(heads.map(h => h.textContent)).toEqual(['Mon 5', 'Tue 6'])
    expect(await findByText('3 changes this week')).toBeTruthy()
  })
})

describe('week mode — actions', () => {
  it('Cancel this lesson POSTs a cancel override (with Undo)', async () => {
    apiPost.mockResolvedValue({ timetable: { ...tt, version: 4 } })
    const { container, findByText, getByRole } = renderWeek(week([lesson(mathe, '2026-10-05')]))
    await findByText(/Tap a lesson/)
    fireEvent.click(block(container, mathe.id))
    expect(getByRole('dialog').getAttribute('aria-labelledby')).toBeTruthy()
    expect(getByRole('heading', { name: 'Mathe · Mon, Oct 5' })).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Cancel this lesson' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/timetables/tt1/overrides',
      { version: 3, date: '2026-10-05', kind: 'cancel', entry_id: mathe.id }))
    expect(showToast.mock.calls[0][0]).toBe('Lesson cancelled on Mon, Oct 5')
    expect(showToast.mock.calls[0][2].action.label).toBe('Undo')
  })

  it('shows the regular values as "(as usual)" placeholders in empty fields', async () => {
    const { container, findByText, getByRole, getByLabelText } = renderWeek(week([lesson(mathe, '2026-10-05')]))
    await findByText(/Tap a lesson/)
    fireEvent.click(block(container, mathe.id))
    const title = getByLabelText('Title') as HTMLInputElement
    const room = getByLabelText('Room') as HTMLInputElement
    expect(title.value).toBe('')
    expect(title.placeholder).toBe('Mathe (as usual)')
    expect(room.value).toBe('')
    expect(room.placeholder).toBe('Room 204 (as usual)')
    // No regular teacher → the ordinary (empty) placeholder.
    expect((getByLabelText('Teacher') as HTMLInputElement).placeholder).toBe('')
    expect(getByRole('dialog').textContent).toContain('Leave a field empty to keep the regular value.')
  })

  it('a changed lesson shows its changed value; emptying it goes back to the regular one', async () => {
    const changed = lesson(mathe, '2026-10-05', { status: 'changed', override_id: 'o1', original: mathe, room: '112' })
    apiDelete.mockResolvedValue({ timetable: { ...tt, version: 4 } })
    const { container, findByText, getByRole, getByLabelText } = renderWeek(week([changed]))
    await findByText('1 change this week')
    fireEvent.click(block(container, mathe.id))
    const room = getByLabelText('Room') as HTMLInputElement
    expect(room.value).toBe('112')
    expect((getByLabelText('Title') as HTMLInputElement).value).toBe('')
    fireEvent.input(room, { target: { value: '' } })
    fireEvent.click(getByRole('button', { name: 'Change for this week' }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/timetables/tt1/overrides/o1?version=3'))
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('cancelling a merged double lesson cancels both halves, in sequence, with one undo', async () => {
    const m1 = entry(0, '08:00', '08:45', { title: 'Mathe' })
    const m2 = entry(0, '08:50', '09:35', { title: 'Mathe' })
    tt = timetable({ version: 3, days: [0], entries: [m1, m2] })
    timetables.value = [tt]
    apiPost
      .mockResolvedValueOnce({ timetable: { ...tt, version: 4 } })
      .mockResolvedValueOnce({ timetable: { ...tt, version: 5 } })
    const w: ResolvedWeek = { anchor: '2026-10-05', valid: true,
      days: [{ date: '2026-10-05', valid: true, lessons: [lesson(m1, '2026-10-05'), lesson(m2, '2026-10-05')] }] }
    const { container, findByText, getByRole } = renderWeek(w)
    await findByText(/Tap a lesson/)
    expect(container.querySelector('.sh-timetable-visual td[rowspan="2"]')).not.toBeNull()
    fireEvent.click(block(container, m1.id))
    fireEvent.click(getByRole('button', { name: 'Cancel this lesson' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(2))
    expect(apiPost.mock.calls[0][1]).toEqual({ version: 3, date: '2026-10-05', kind: 'cancel', entry_id: m1.id })
    expect(apiPost.mock.calls[1][1]).toEqual({ version: 4, date: '2026-10-05', kind: 'cancel', entry_id: m2.id })
    expect(showToast).toHaveBeenCalledTimes(1)
    expect(showToast.mock.calls[0][2].action.label).toBe('Undo')
  })

  it('Change for this week POSTs a replace with only the fields typed into', async () => {
    apiPost.mockResolvedValue({ timetable: { ...tt, version: 4 } })
    const { container, findByText, getByRole, getByLabelText } = renderWeek(week([lesson(mathe, '2026-10-05')]))
    await findByText(/Tap a lesson/)
    fireEvent.click(block(container, mathe.id))
    // Typing the regular value (or only spaces) is no change.
    fireEvent.input(getByLabelText('Title'), { target: { value: 'Mathe' } })
    fireEvent.input(getByLabelText('Teacher'), { target: { value: '   ' } })
    fireEvent.input(getByLabelText('Room'), { target: { value: '112' } })
    fireEvent.click(getByRole('button', { name: 'Change for this week' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/timetables/tt1/overrides',
      { version: 3, date: '2026-10-05', kind: 'replace', entry_id: mathe.id, room: '112' }))
  })

  it('a changed lesson PATCHes its override, and Restore regular DELETEs it', async () => {
    const changed = lesson(mathe, '2026-10-05', { status: 'changed', override_id: 'o1', original: mathe, room: '112' })
    apiPatch.mockResolvedValue({ timetable: { ...tt, version: 4 } })
    const { container, findByText, getByRole, getByLabelText } = renderWeek(week([changed]))
    await findByText('1 change this week')
    fireEvent.click(block(container, mathe.id))
    expect(getByRole('dialog').textContent).toContain('Usually: Mathe · 08:00–08:45 · room 204')
    fireEvent.input(getByLabelText('Teacher'), { target: { value: 'Hr. Beck' } })
    fireEvent.click(getByRole('button', { name: 'Change for this week' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/timetables/tt1/overrides/o1', {
      version: 3, kind: 'replace', title: null, icon: null, start: null, end: null, color: null,
      note: null, room: '112', teacher: 'Hr. Beck',
    }))
    fireEvent.click(block(container, mathe.id))
    apiDelete.mockResolvedValue({ timetable: { ...tt, version: 5 } })
    fireEvent.click(getByRole('button', { name: 'Restore regular' }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/timetables/tt1/overrides/o1?version=4'))
  })

  it('"+" in a day heading adds an extra lesson for that date', async () => {
    apiPost.mockResolvedValue({ timetable: { ...tt, version: 4 } })
    const { findByText, getByRole, getByLabelText } = renderWeek(week([lesson(mathe, '2026-10-05')]))
    await findByText(/Tap a lesson/)
    fireEvent.click(getByRole('button', { name: 'Add an extra lesson on Tuesday' }))
    expect(getByRole('heading', { name: 'Extra lesson · Tue, Oct 6' })).toBeTruthy()
    fireEvent.input(getByLabelText('Title'), { target: { value: 'AG Chor' } })
    fireEvent.click(getByRole('button', { name: 'Add for this week' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalled())
    expect(apiPost.mock.calls[0][1]).toMatchObject({
      version: 3, date: '2026-10-06', kind: 'add', entry_kind: 'lesson', title: 'AG Chor',
      start: '08:00', end: '08:45',
    })
  })

  it('Clear all DELETEs the week\'s overrides and offers an undo', async () => {
    tt = { ...tt, overrides: [{ id: 'o1', date: '2026-10-05', kind: 'cancel', entry_id: mathe.id,
      start: null, end: null, entry_kind: 'lesson', label: null, title: null, room: null,
      teacher: null, note: null, color: null, icon: null }] }
    timetables.value = [tt]
    apiDelete.mockResolvedValue({ timetable: { ...tt, version: 4, overrides: [] } })
    const w = week([lesson(mathe, '2026-10-05', { status: 'cancelled', override_id: 'o1', original: mathe })])
    const { findByRole } = renderWeek(w)
    fireEvent.click(await findByRole('button', { name: 'Clear all' }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith(
      '/api/timetables/tt1/weeks/2026-10-05/overrides?version=3'))
    const [msg, , opts] = showToast.mock.calls[0]
    expect(msg.replace(/\s/g, ' ')).toBe('Changes cleared for W41 · Oct 5 – 11')
    apiPost.mockResolvedValue({ timetable: { ...tt, version: 5 } })
    opts.action.onClick()
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/timetables/tt1/overrides',
      expect.objectContaining({ version: 4, date: '2026-10-05', kind: 'cancel', entry_id: mathe.id })))
  })

  it('a holiday week says so and "Activate this week" drops it from the excluded weeks', async () => {
    tt = { ...tt, validity: { valid_from: null, valid_until: null, excluded_weeks: ['2026-09-28', '2026-10-05'] } }
    timetables.value = [tt]
    const w: ResolvedWeek = { anchor: '2026-10-05', valid: false,
      days: [{ date: '2026-10-05', valid: false, lessons: [] }, { date: '2026-10-06', valid: false, lessons: [] }] }
    apiPut.mockResolvedValue({ timetable: { ...tt, version: 4 } })
    const { findByText, getByRole, container } = renderWeek(w)
    expect(await findByText('Not active this week (holiday)')).toBeTruthy()
    expect(container.querySelector('.sh-timetable-visual')).toBeNull()
    fireEvent.click(getByRole('button', { name: 'Activate this week' }))
    await waitFor(() => expect(apiPut).toHaveBeenCalledWith('/api/timetables/tt1/validity', {
      version: 3, valid_from: null, valid_until: null, excluded_weeks: ['2026-09-28'],
    }))
  })

  it('days more than 14 days ago are locked, with a tooltip saying why', async () => {
    const old: ResolvedWeek = { anchor: '2026-09-14', valid: true, days: [
      { date: '2026-09-14', valid: true, lessons: [lesson(mathe, '2026-09-14')] },
      { date: '2026-09-15', valid: true, lessons: [lesson(sport, '2026-09-15')] },
    ] }
    const { container, findByText, getByRole } = renderWeek(old, '2026-09-16')
    await findByText(/can't be changed any more/)
    const b = block(container, mathe.id)
    expect(b.getAttribute('aria-disabled')).toBe('true')
    expect(b.getAttribute('title')).toBe("More than 14 days ago — can't be changed any more")
    fireEvent.click(b)
    expect(entryDialog.value).toBeNull()
    const add = getByRole('button', {
      name: "Add an extra lesson on Monday — More than 14 days ago — can't be changed any more" })
    expect(add.getAttribute('aria-disabled')).toBe('true')
    expect(add.getAttribute('title')).toBe("More than 14 days ago — can't be changed any more")
    fireEvent.click(add)
    expect(entryDialog.value).toBeNull()
  })
})

describe('regular mode — changes link', () => {
  it('counts the resolved current week, like the week banner', async () => {
    const ov = (id: string, entry_id: string) => ({ id, date: '2026-10-05', kind: 'cancel' as const, entry_id,
      start: null, end: null, entry_kind: 'lesson' as const, label: null, title: null, room: null,
      teacher: null, note: null, color: null, icon: null })
    // Two overrides, but one of them targets a lesson no longer on the plan.
    tt = { ...tt, overrides: [ov('o1', mathe.id), ov('o2', 'gone')] }
    timetables.value = [tt]
    apiGet.mockResolvedValue({ week: week([
      lesson(mathe, '2026-10-05', { status: 'cancelled', override_id: 'o1', original: mathe }),
      lesson(deutsch, '2026-10-05'),
    ]) })
    const { findByRole } = render(
      <SelectedTimetable tt={tt} narrow={false} week={null} onWeek={vi.fn()}
                         onDuplicate={vi.fn()} onNew={vi.fn()} onDelete={vi.fn()} now={NOW} />)
    expect(await findByRole('button', { name: 'Changes this week: 1' })).toBeTruthy()
    expect(apiGet).toHaveBeenCalledWith('/api/timetables/tt1/weeks/2026-10-05')
  })
})

describe('week mode — inactive days', () => {
  it('"+" on a day the timetable is not in effect is disabled and says why', async () => {
    const w: ResolvedWeek = { anchor: '2026-10-05', valid: true, days: [
      { date: '2026-10-05', valid: true, lessons: [lesson(mathe, '2026-10-05')] },
      { date: '2026-10-06', valid: false, lessons: [] },
    ] }
    const { findByText, getByRole } = renderWeek(w)
    await findByText(/Tap a lesson/)
    const add = getByRole('button', {
      name: 'Add an extra lesson on Tuesday — Not in effect this day (holiday or outside the valid dates)' })
    expect(add.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(add)
    expect(entryDialog.value).toBeNull()
    // Screen readers hear the full localised date, not the ISO one.
    expect(document.querySelector('.sh-timetable-visual .sh-timetable-dayhead__name .sr-only')!.textContent)
      .toBe('Monday, October 5, 2026')
  })
})

describe('week mode — live updates', () => {
  it('a timetable.changed frame refetches the week shown', async () => {
    wireTimetablesWs()
    const { findByText, rerender } = renderWeek(week([lesson(mathe, '2026-10-05')]))
    await findByText(/Tap a lesson/)
    expect(apiGet).toHaveBeenCalledTimes(1)
    wsHandlers['timetable.changed']({ type: 'timetable.changed',
      data: { space_id: null, timetable: { ...tt, version: 9 } } })
    rerender(
      <SelectedTimetable tt={timetables.value[0]} narrow={false} week="2026-10-07" onWeek={vi.fn()}
                         onDuplicate={vi.fn()} onNew={vi.fn()} onDelete={vi.fn()} now={NOW} />,
    )
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))
    expect(apiGet).toHaveBeenLastCalledWith('/api/timetables/tt1/weeks/2026-10-05')
  })
})
