import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const apiPatch = vi.fn()
const apiGet = vi.fn()
vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return { ApiError: actual.ApiError, api: { get: (...a: unknown[]) => apiGet(...a), patch: (...a: unknown[]) => apiPatch(...a) } }
})
vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))
const confirmDialog = vi.fn()
vi.mock('@/components/confirm', () => ({ confirmDialog: (...a: unknown[]) => confirmDialog(...a) }))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map() }, loadHouseholdUsers: vi.fn(),
}))

vi.mock('@/utils/week', async () => {
  const actual = await vi.importActual<typeof import('@/utils/week')>('@/utils/week')
  return { ...actual, currentWeekStart: () => 0 }
})

import { ApiError } from '@/api'
import { timetables } from '@/store/timetables'
import { TimetableSettingsDialog, openSettingsDialog, closeSettingsDialog } from './TimetableSettingsDialog'
import { entry, timetable } from './testUtils'

beforeEach(() => {
  apiPatch.mockReset()
  apiGet.mockReset()
  showToast.mockReset()
  confirmDialog.mockReset()
  timetables.value = [timetable({ version: 3, days: [0, 1, 2, 3, 4, 5],
    entries: [entry(5, '08:00', '08:45', { title: 'AG' })] })]
  closeSettingsDialog()
})

describe('TimetableSettingsDialog', () => {
  it('sends only the changed fields', async () => {
    apiPatch.mockResolvedValue({ timetable: { ...timetables.value[0], version: 4 } })
    openSettingsDialog('tt1')
    const { getByLabelText, getByRole } = render(<TimetableSettingsDialog />)
    fireEvent.input(getByLabelText('Name'), { target: { value: 'Emma 4b' } })
    fireEvent.click(getByLabelText('Teal'))
    fireEvent.input(getByLabelText('Lesson length (min)'), { target: { value: '50' } })
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalled())
    expect(apiPatch).toHaveBeenCalledWith('/api/timetables/tt1', {
      version: 3, name: 'Emma 4b', color: 'teal',
      defaults: { lesson_minutes: 50, gap_minutes: 5, day_start: '08:00' },
    })
  })

  it('removing a day with lessons confirms, then retries with drop_orphans', async () => {
    apiPatch
      .mockRejectedValueOnce(new ApiError(409, '/x', { code: 'DAYS_ORPHAN_ENTRIES', detail: '', count: 1 }))
      .mockResolvedValueOnce({ timetable: { ...timetables.value[0], version: 4, days: [0, 1, 2, 3, 4] } })
    confirmDialog.mockResolvedValue(true)
    openSettingsDialog('tt1')
    const { getByRole } = render(<TimetableSettingsDialog />)
    fireEvent.click(getByRole('button', { name: 'Mon–Fri' }))
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledTimes(2))
    expect(confirmDialog.mock.calls[0][0]).toBe('Remove 1 lesson on Saturday?')
    expect(apiPatch).toHaveBeenLastCalledWith('/api/timetables/tt1',
      { version: 3, days: [0, 1, 2, 3, 4], drop_orphans: true })
  })

  it('shows the Sunday hint and keeps the dialog open on a server error', async () => {
    apiPatch.mockRejectedValue(new ApiError(422, '/x', { code: 'UNPROCESSABLE', detail: 'unknown tz' }))
    openSettingsDialog('tt1')
    const { getByLabelText, getByRole, getByText } = render(<TimetableSettingsDialog />)
    fireEvent.click(getByLabelText('Sunday'))
    expect(getByText('Weeks start on Sunday for this timetable')).toBeTruthy()
    fireEvent.input(getByLabelText('Time zone'), { target: { value: 'Mars/Olympus' } })
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(getByRole('alert').textContent).toBe('unknown tz'))
    expect(apiPatch).toHaveBeenCalledWith('/api/timetables/tt1',
      { version: 3, week_start: 6, tz: 'Mars/Olympus' })
  })

  it('diffs against the snapshot and sends its version, so a change made meanwhile 409s', async () => {
    openSettingsDialog('tt1')
    const { getByLabelText, getByRole } = render(<TimetableSettingsDialog />)
    // A WS frame renames it to "Theirs" (v4) while the dialog is open.
    timetables.value = [{ ...timetables.value[0], version: 4, name: 'Theirs' }]
    apiPatch.mockRejectedValue(new ApiError(409, '/x',
      { code: 'TIMETABLE_CONFLICT', detail: '', current_version: 4 }))
    apiGet.mockResolvedValue({ timetable: timetables.value[0] })
    fireEvent.click(getByLabelText('Teal'))
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'Someone else just changed this timetable — reloaded', 'info'))
    // Only the colour changed in the form — the name isn't sent back.
    expect(apiPatch).toHaveBeenCalledWith('/api/timetables/tt1', { version: 3, color: 'teal' })
    expect(getByRole('dialog')).toBeTruthy()
  })

  it('validates the lesson length and gap inline and never sends 0 for an empty field', async () => {
    openSettingsDialog('tt1')
    const { getByLabelText, getByRole, findByRole } = render(<TimetableSettingsDialog />)
    fireEvent.input(getByLabelText('Lesson length (min)'), { target: { value: '' } })
    fireEvent.click(getByRole('button', { name: 'Save' }))
    expect((await findByRole('alert')).textContent).toBe('Lesson length must be 5–240 minutes.')
    fireEvent.input(getByLabelText('Lesson length (min)'), { target: { value: '45' } })
    fireEvent.input(getByLabelText('Gap between lessons (min)'), { target: { value: '121' } })
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(getByRole('alert').textContent).toBe('The gap must be 0–120 minutes.'))
    expect(apiPatch).not.toHaveBeenCalled()
  })
})
