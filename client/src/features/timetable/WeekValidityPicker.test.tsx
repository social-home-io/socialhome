import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { render, fireEvent, waitFor, within, cleanup } from '@testing-library/preact'

const apiPut = vi.fn()
vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return {
    ApiError: actual.ApiError,
    api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(), put: (...a: unknown[]) => apiPut(...a) },
  }
})
vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
const confirmDialog = vi.fn()
vi.mock('@/components/confirm', () => ({ confirmDialog: (...a: unknown[]) => confirmDialog(...a) }))

import { timetables } from '@/store/timetables'
import { WeekValidityPicker, closeWeeksDialog, openWeeksDialog } from './WeekValidityPicker'
import { anchorsOfWindow, monthRows, weekInRange, weekWindow } from './yearWeeks'
import { timetable } from './testUtils'
import type { Timetable, TimetableValidity } from '@/types'

// Wed 30 Sep 2026.
const NOW = new Date(2026, 8, 30, 10, 0)

function setup(validity: Partial<TimetableValidity> = {}, extra: Partial<Timetable> = {}) {
  const tt = timetable({
    version: 4,
    validity: { valid_from: '2026-09-07', valid_until: '2027-07-30', excluded_weeks: [], ...validity },
    ...extra,
  })
  timetables.value = [tt]
  openWeeksDialog(tt.id)
  const utils = render(<WeekValidityPicker now={NOW} />)
  const chip = (week: string) =>
    utils.container.ownerDocument.querySelector(`[data-week="${week}"]`) as HTMLButtonElement
  return { ...utils, tt, chip }
}

beforeEach(() => {
  closeWeeksDialog()
  apiPut.mockReset()
  confirmDialog.mockReset()
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('year weeks', () => {
  it('lists the anchors of a window, grouped by the anchor day\'s month', () => {
    const mon = anchorsOfWindow({ start: '2026-01', end: '2026-12' }, 0)
    expect(mon[0]).toBe('2026-01-05')
    expect(mon[mon.length - 1]).toBe('2026-12-28')
    expect(mon).toHaveLength(52)
    const sun = anchorsOfWindow({ start: '2026-01', end: '2026-12' }, 6)
    expect(sun[0]).toBe('2026-01-04')
    expect(monthRows(mon)[9]).toEqual({ month: '2026-10',
      anchors: ['2026-10-05', '2026-10-12', '2026-10-19', '2026-10-26'] })
    expect(weekInRange('2026-08-31', '2026-09-06', null)).toBe(true) // Sunday 6th is in
    expect(weekInRange('2026-08-24', '2026-09-06', null)).toBe(false)
  })

  it('the window is the school year when both ends are set, else 12 months', () => {
    expect(weekWindow('2026-09-07', '2027-07-30', '2026-09-30', 0, 0))
      .toEqual({ start: '2026-09', end: '2027-07', school: true })
    // A range starting mid-week keeps the week that holds the start date.
    expect(weekWindow('2026-09-01', '2027-07-30', '2026-09-30', 0, 0).start).toBe('2026-08')
    expect(weekWindow(null, null, '2026-09-30', 0, 0))
      .toEqual({ start: '2026-09', end: '2027-08', school: false })
    expect(weekWindow('2027-02-10', null, '2026-09-30', 0, 0).start).toBe('2027-02')
    // A multi-year range is capped at 18 months at a time.
    expect(weekWindow('2026-09-07', '2029-07-30', '2026-09-30', 0, 0).end).toBe('2028-02')
    expect(weekWindow('2026-09-07', '2027-07-30', '2026-09-30', 0, 1))
      .toEqual({ start: '2027-09', end: '2028-08', school: false })
  })
})

describe('WeekValidityPicker', () => {
  it('labels chips "W41" for a Monday start with the full range as name and tooltip', () => {
    const { chip } = setup()
    const c = chip('2026-10-05')
    expect(c.textContent).toBe('W41')
    expect(c.getAttribute('aria-label')!.replace(/\s/g, ' ')).toBe('Week 41, Oct 5 – 11, 2026, school week')
    expect(c.getAttribute('title')).toBe(c.getAttribute('aria-label'))
    expect(c.getAttribute('aria-pressed')).toBe('true')
    // The current week is outlined and says so.
    expect(chip('2026-09-28').classList.contains('is-current')).toBe(true)
    expect(chip('2026-09-28').getAttribute('aria-label')).toMatch(/, this week$/)
  })

  it('labels chips by their Sunday date for a Sunday start', () => {
    const { chip } = setup({}, { week_start: 6 })
    expect(chip('2026-10-04').textContent).toBe('Oct 4')
    expect(chip('2026-10-04').getAttribute('aria-label')!.replace(/\s/g, ' '))
      .toBe('Week of Oct 4 – 10, 2026, school week')
  })

  it('shows the school year Sep 2026 – Jul 2027 as one list, labelled with the year when it changes', () => {
    const { container, chip, getByText } = setup()
    const labels = Array.from(container.ownerDocument.querySelectorAll('.sh-timetable-weeks__mname'))
      .map(e => e.textContent)
    expect(labels).toEqual(['Sep 2026', 'Oct', 'Nov', 'Dec', 'Jan 2027', 'Feb', 'Mar', 'Apr',
      'May', 'Jun', 'Jul'])
    // Months outside the range are hidden, not greyed.
    expect(chip('2026-08-24')).toBeNull()
    expect(chip('2027-08-02')).toBeNull()
    expect(chip('2027-04-05')).not.toBeNull() // Easter, same list — no year switch
    expect(getByText((s) => s.replace(/\s/g, ' ') === 'Sep 2026 – Jul 2027')).toBeTruthy()
    // 47 weeks overlap 7 Sep – 30 Jul.
    expect(getByText('47 school weeks · 0 holiday weeks')).toBeTruthy()
  })

  it('an open-ended timetable shows 12 months from the current month; weeks past the end are disabled', () => {
    const { container, chip } = setup({ valid_from: null, valid_until: '2027-03-31' })
    const labels = Array.from(container.ownerDocument.querySelectorAll('.sh-timetable-weeks__mname'))
      .map(e => e.textContent)
    expect(labels[0]).toBe('Sep 2026')
    expect(labels).toContain('Mar')
    // Months after the end hold no week in range → hidden.
    expect(labels).not.toContain('Apr')
    expect(chip('2026-09-07').getAttribute('aria-disabled')).toBeNull()
  })

  it('shifts the window by 12 months and back to the school year; out-of-range weeks are disabled there', () => {
    const { getByRole, chip, getByText, queryByRole } = setup()
    expect(queryByRole('button', { name: 'Back to school year' })).toBeNull()
    fireEvent.click(getByRole('button', { name: 'Later 12 months' }))
    expect(getByText((s) => s.replace(/\s/g, ' ') === 'Sep 2027 – Aug 2028')).toBeTruthy()
    const c = chip('2027-09-06')
    expect(c.getAttribute('aria-disabled')).toBe('true')
    expect(c.getAttribute('aria-label')).toMatch(/outside the valid dates/)
    fireEvent.click(c)
    expect(c.classList.contains('sh-timetable-week--outside')).toBe(true)
    fireEvent.click(getByRole('button', { name: 'Back to school year' }))
    expect(chip('2026-10-05')).not.toBeNull()
    fireEvent.click(getByRole('button', { name: 'Earlier 12 months' }))
    expect(getByText((s) => s.replace(/\s/g, ' ') === 'Sep 2025 – Aug 2026')).toBeTruthy()
  })

  it('changing the dates in the dialog moves the window live', () => {
    const { getByLabelText, chip, container } = setup()
    fireEvent.input(getByLabelText('Until'), { target: { value: '2026-12-18' } })
    const labels = Array.from(container.ownerDocument.querySelectorAll('.sh-timetable-weeks__mname'))
      .map(e => e.textContent)
    expect(labels).toEqual(['Sep 2026', 'Oct', 'Nov', 'Dec'])
    expect(chip('2027-01-04')).toBeNull()
  })

  it('click toggles one week; shift+click toggles the range from the last click', () => {
    const { chip, getByText } = setup()
    fireEvent.click(chip('2026-10-12'))
    expect(chip('2026-10-12').getAttribute('aria-pressed')).toBe('false')
    expect(chip('2026-10-12').textContent).toBe('🏖W42')
    fireEvent.click(chip('2026-10-26'), { shiftKey: true })
    for (const w of ['2026-10-12', '2026-10-19', '2026-10-26']) {
      expect(chip(w).classList.contains('sh-timetable-week--holiday')).toBe(true)
    }
    expect(chip('2026-10-05').classList.contains('sh-timetable-week--school')).toBe(true)
    expect(getByText('44 school weeks · 3 holiday weeks')).toBeTruthy()
  })

  it('a pointer drag paints the first chip\'s new state across the chips it crosses', () => {
    const { chip, container } = setup()
    const months = container.ownerDocument.querySelector('.sh-timetable-weeks__months')!
    const at: Record<number, HTMLElement> = {
      1: chip('2026-11-02'), 2: chip('2026-11-09'), 3: chip('2026-11-16'),
    }
    // jsdom has no elementFromPoint; stub it for this test only.
    const original = document.elementFromPoint
    document.elementFromPoint = ((x: number) => at[x] ?? null) as typeof document.elementFromPoint
    onTestFinished(() => { document.elementFromPoint = original })
    fireEvent.pointerDown(at[1], { clientX: 1, clientY: 0, pointerId: 1, pointerType: 'touch' })
    fireEvent.pointerMove(months, { clientX: 2, clientY: 0, pointerId: 1 })
    fireEvent.pointerMove(months, { clientX: 3, clientY: 0, pointerId: 1 })
    fireEvent.pointerUp(months, { clientX: 3, clientY: 0, pointerId: 1 })
    fireEvent.click(at[3]) // the click after the drag is swallowed
    for (const w of ['2026-11-02', '2026-11-09', '2026-11-16']) {
      expect(chip(w).classList.contains('sh-timetable-week--holiday')).toBe(true)
    }
    expect(chip('2026-11-23').classList.contains('sh-timetable-week--school')).toBe(true)
  })

  it('arrow keys rove across the window; Shift+Arrow extends the state', async () => {
    const { chip } = setup({ excluded_weeks: ['2026-10-05'] })
    expect(chip('2026-09-28').tabIndex).toBe(0)
    expect(chip('2026-10-05').tabIndex).toBe(-1)
    chip('2026-10-05').focus()
    fireEvent.keyDown(chip('2026-10-05'), { key: 'ArrowRight', shiftKey: true })
    await waitFor(() => expect(document.activeElement).toBe(chip('2026-10-12')))
    expect(chip('2026-10-12').classList.contains('sh-timetable-week--holiday')).toBe(true)
    fireEvent.keyDown(chip('2026-10-12'), { key: 'ArrowDown' })
    await waitFor(() => expect(document.activeElement).toBe(chip('2026-11-09')))
  })

  it('quick actions apply to the weeks shown within the valid range', async () => {
    apiPut.mockResolvedValue({ timetable: { ...timetable(), version: 5 } })
    // Open-ended: the window is Sep 2026 – Aug 2027; a holiday further out stays untouched.
    const { getByRole, chip, getByText } = setup({
      valid_from: '2026-09-07', valid_until: null, excluded_weeks: ['2027-10-04'],
    })
    const quick = getByRole('group', { name: 'Quick actions for the weeks shown' })
    fireEvent.click(within(quick).getByRole('button', { name: 'All holidays' }))
    expect(chip('2026-09-07').classList.contains('sh-timetable-week--holiday')).toBe(true)
    expect(getByText('0 school weeks · 52 holiday weeks')).toBeTruthy()
    fireEvent.click(within(quick).getByRole('button', { name: 'Invert' }))
    expect(chip('2026-09-07').classList.contains('sh-timetable-week--school')).toBe(true)
    fireEvent.click(within(quick).getByRole('button', { name: 'Every other week' }))
    // Keeps the week of the valid-from date (7 Sep), pauses the next.
    expect(chip('2026-09-07').classList.contains('sh-timetable-week--school')).toBe(true)
    expect(chip('2026-09-14').classList.contains('sh-timetable-week--holiday')).toBe(true)
    expect(chip('2026-09-21').classList.contains('sh-timetable-week--school')).toBe(true)
    fireEvent.click(within(quick).getByRole('button', { name: 'All school weeks' }))
    expect(chip('2026-09-14').classList.contains('sh-timetable-week--school')).toBe(true)
    fireEvent.click(within(quick).getByRole('button', { name: 'All holidays' }))
    // The next window was not touched by any of it.
    fireEvent.click(getByRole('button', { name: 'Later 12 months' }))
    expect(chip('2027-09-06').classList.contains('sh-timetable-week--school')).toBe(true)
    expect(chip('2027-10-04').classList.contains('sh-timetable-week--holiday')).toBe(true)
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPut).toHaveBeenCalled())
    const sent: string[] = apiPut.mock.calls[0][1].excluded_weeks
    expect(sent).toHaveLength(53)
    expect(sent[0]).toBe('2026-09-07')
    expect(sent[sent.length - 1]).toBe('2027-10-04')
  })

  it('saves one PUT with the sorted anchors and the version it opened with', async () => {
    apiPut.mockResolvedValue({ timetable: { ...timetable(), version: 5 } })
    const { chip, getByRole, getByLabelText } = setup({ excluded_weeks: ['2026-12-21'] })
    fireEvent.click(chip('2026-10-26'))
    fireEvent.click(chip('2026-10-19'))
    fireEvent.click(getByLabelText('Open-ended'))
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPut).toHaveBeenCalledWith('/api/timetables/tt1/validity', {
      version: 4, valid_from: '2026-09-07', valid_until: null,
      excluded_weeks: ['2026-10-19', '2026-10-26', '2026-12-21'],
    }))
  })

  it('drops holidays outside the valid range on save', async () => {
    apiPut.mockResolvedValue({ timetable: { ...timetable(), version: 5 } })
    const { getByRole } = setup({ excluded_weeks: ['2025-12-22', '2026-10-12', '2027-08-09'] })
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPut).toHaveBeenCalled())
    expect(apiPut.mock.calls[0][1].excluded_weeks).toEqual(['2026-10-12'])
  })

  it('a plain mouse click toggles without capturing the pointer; a drag captures once it reaches a second chip', () => {
    const { chip, container } = setup()
    const months = container.ownerDocument.querySelector<HTMLElement>('.sh-timetable-weeks__months')!
    const capture = vi.fn()
    months.setPointerCapture = capture
    const c = chip('2026-11-02')
    fireEvent.pointerDown(c, { clientX: 1, clientY: 0, pointerId: 7, pointerType: 'mouse' })
    fireEvent.pointerUp(c, { clientX: 1, clientY: 0, pointerId: 7, pointerType: 'mouse' })
    fireEvent.click(c)
    expect(capture).not.toHaveBeenCalled()
    expect(c.classList.contains('sh-timetable-week--holiday')).toBe(true)
    const original = document.elementFromPoint
    document.elementFromPoint = (() => chip('2026-11-09')) as typeof document.elementFromPoint
    onTestFinished(() => { document.elementFromPoint = original })
    fireEvent.pointerDown(c, { clientX: 1, clientY: 0, pointerId: 8, pointerType: 'mouse' })
    fireEvent.pointerMove(months, { clientX: 2, clientY: 0, pointerId: 8, pointerType: 'mouse' })
    expect(capture).toHaveBeenCalledWith(8)
  })

  it('refuses a start after the end, inline', () => {
    const { getByLabelText, getByRole } = setup()
    fireEvent.input(getByLabelText('From'), { target: { value: '2027-09-01' } })
    fireEvent.click(getByRole('button', { name: 'Save' }))
    expect(getByRole('alert').textContent).toBe('The start date must be before the end date.')
    expect(apiPut).not.toHaveBeenCalled()
  })

  it('closing with unsaved changes asks first; Cancel discards', async () => {
    const { chip, getByRole, queryByRole } = setup()
    fireEvent.click(getByRole('button', { name: 'Close dialog' }))
    await waitFor(() => expect(queryByRole('dialog')).toBeNull()) // nothing changed → no prompt
    expect(confirmDialog).not.toHaveBeenCalled()

    openWeeksDialog('tt1')
    await waitFor(() => expect(getByRole('dialog')).toBeTruthy())
    fireEvent.click(chip('2026-10-12'))
    confirmDialog.mockResolvedValueOnce(false)
    fireEvent.click(getByRole('button', { name: 'Close dialog' }))
    await waitFor(() => expect(confirmDialog).toHaveBeenCalledWith(
      "Your changes to the school weeks aren't saved yet.", expect.objectContaining({ title: 'Discard changes?' })))
    expect(getByRole('dialog')).toBeTruthy()
    confirmDialog.mockResolvedValueOnce(true)
    fireEvent.click(getByRole('button', { name: 'Close dialog' }))
    await waitFor(() => expect(queryByRole('dialog')).toBeNull())
  })
})
