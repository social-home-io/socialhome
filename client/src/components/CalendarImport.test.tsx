import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { apiMock, platformMock } = vi.hoisted(() => ({
  apiMock: { post: vi.fn(), postRaw: vi.fn() },
  platformMock: { ai: false },
}))

vi.mock('@/api', async () => {
  const real = await vi.importActual<typeof import('@/api')>('@/api')
  return { api: apiMock, ApiError: real.ApiError }
})
vi.mock('@/platform', () => ({ supportsAi: () => platformMock.ai }))

import { ApiError } from '@/api'
import { currentUser } from '@/store/auth'
import {
  CalendarImport, MAX_UPLOAD_BYTES, closeCalendarImport, openCalendarImport, importErrorMessage, type ImportCalendarOption,
} from './CalendarImport'

const CALS: ImportCalendarOption[] = [
  { id: 'cal-me', name: 'Calendar', owner_username: 'pascal' },
  { id: 'cal-maria', name: 'Calendar', owner_username: 'maria' },
]
const ICS = 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nSUMMARY:Dentist\r\n'
  + 'DTSTART:20261001T100000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'

function icsFile(text = ICS, name = 'family.ics'): File {
  return new File([text], name, { type: 'text/calendar' })
}

function setup(overrides: Partial<Parameters<typeof CalendarImport>[0]> = {}) {
  const onImported = vi.fn()
  const ensureCalendar = vi.fn().mockResolvedValue('cal-new')
  openCalendarImport()
  const utils = render(
    <CalendarImport
      calendars={CALS}
      defaultCalendarId="cal-me"
      ensureCalendar={ensureCalendar}
      onImported={onImported}
      {...overrides}
    />,
  )
  return { ...utils, onImported, ensureCalendar }
}

function fileInput(): HTMLInputElement {
  return document.body.querySelector('input[type="file"]') as HTMLInputElement
}

async function pick(_container: Element, file: File) {
  const input = fileInput()
  Object.defineProperty(input, 'files', { value: [file], configurable: true })
  fireEvent.change(input)
}

beforeEach(() => {
  apiMock.post.mockReset()
  apiMock.postRaw.mockReset()
  platformMock.ai = false
  closeCalendarImport()
  currentUser.value = { username: 'pascal' } as typeof currentUser.value
})

describe('CalendarImport', () => {
  it('opens a dialog with a file picker; AI sources stay hidden without the ai capability', () => {
    const { getByRole, getByLabelText, queryByRole } = setup()
    expect(getByRole('dialog')).toBeTruthy()
    expect(getByLabelText(/Calendar file/)).toBeTruthy()
    expect(queryByRole('radiogroup')).toBeNull()
    // Import stays disabled until a file is picked.
    const submit = document.querySelector('form.sh-cal-import button[type="submit"]') as HTMLButtonElement
    expect(submit.disabled).toBe(true)
  })

  it('uploads the picked file raw as text/calendar to the chosen calendar and summarises the result', async () => {
    apiMock.postRaw.mockResolvedValueOnce({
      events: [
        { id: 'e1', summary: 'Dentist', start: '2026-10-01T10:00:00+00:00' },
        { id: 'e2', summary: 'Sports day', start: '2026-10-02T00:00:00+00:00', all_day: true },
      ],
    })
    const { container, getByLabelText, findByText, onImported } = setup()
    fireEvent.change(getByLabelText('Add to'), { target: { value: 'cal-maria' } })
    const f = icsFile()
    await pick(container, f)
    await findByText(/family\.ics/)
    const submit = document.querySelector('form.sh-cal-import button[type="submit"]') as HTMLButtonElement
    await waitFor(() => expect(submit.disabled).toBe(false))
    fireEvent.click(submit)
    await findByText('Added 2 events to maria\'s calendar.')
    expect(apiMock.postRaw).toHaveBeenCalledWith(
      '/api/calendars/cal-maria/import_ics', f, 'text/calendar',
    )
    expect(onImported).toHaveBeenCalledWith('cal-maria', expect.any(Array))
    expect(document.body.textContent).toContain('Dentist')
    expect(document.body.textContent).toContain(
      'Re-importing the same file updates its events; events removed from the file stay on the calendar.',
    )
    expect(document.body.textContent).not.toContain('Duplicates aren\'t detected')
  })

  it('reports added and updated counts when a re-import updated existing events', async () => {
    apiMock.postRaw.mockResolvedValueOnce({
      events: [
        { id: 'e1', summary: 'Dentist (moved)', start: '2026-10-01T11:00:00+00:00' },
        { id: 'e2', summary: 'Piano', start: '2026-10-02T15:00:00+00:00' },
        { id: 'e3', summary: 'Swimming', start: '2026-10-03T09:00:00+00:00' },
      ],
      created: 1,
      updated: 2,
    })
    const { container, findByText } = setup()
    await pick(container, icsFile())
    await findByText(/family\.ics/)
    fireEvent.submit(document.querySelector('form.sh-cal-import')!)
    await findByText('Imported 3 events into your calendar: 1 added, 2 updated.')
  })

  it('says "0 added" when re-importing an unchanged file only updated events', async () => {
    apiMock.postRaw.mockResolvedValueOnce({
      events: [{ id: 'e1', summary: 'Dentist', start: '2026-10-01T10:00:00+00:00' }],
      created: 0,
      updated: 1,
    })
    const { container, findByText } = setup()
    await pick(container, icsFile())
    await findByText(/family\.ics/)
    fireEvent.submit(document.querySelector('form.sh-cal-import')!)
    await findByText('Imported 1 event into your calendar: 0 added, 1 updated.')
  })

  it('defaults to the caller\'s own calendar and says "your calendar"', async () => {
    apiMock.postRaw.mockResolvedValueOnce({
      events: [{ id: 'e1', summary: 'Dentist', start: '2026-10-01T10:00:00+00:00' }],
    })
    const { container, findByText } = setup()
    await pick(container, icsFile())
    await findByText(/family\.ics/)
    fireEvent.submit(document.querySelector('form.sh-cal-import')!)
    await findByText('Added 1 event to your calendar.')
    expect(apiMock.postRaw.mock.calls[0][0]).toBe('/api/calendars/cal-me/import_ics')
  })

  it('creates the caller\'s calendar first when they have none', async () => {
    apiMock.postRaw.mockResolvedValueOnce({ events: [] })
    const { container, findByText, ensureCalendar } = setup({
      calendars: [], defaultCalendarId: null,
    })
    await pick(container, icsFile())
    await findByText(/family\.ics/)
    fireEvent.submit(document.querySelector('form.sh-cal-import')!)
    await waitFor(() => expect(ensureCalendar).toHaveBeenCalled())
    expect(apiMock.postRaw.mock.calls[0][0]).toBe('/api/calendars/cal-new/import_ics')
  })

  it('rejects a non-calendar file before uploading', async () => {
    const { container, findByRole } = setup()
    await pick(container, icsFile('%PDF-1.4 not a calendar', 'scan.pdf'))
    expect((await findByRole('alert')).textContent).toMatch(/doesn't look like a calendar file/)
    expect(apiMock.postRaw).not.toHaveBeenCalled()
  })

  it('rejects a file over the 1 MB request limit before uploading', async () => {
    const { container, findByRole } = setup()
    const big = icsFile(ICS + 'X'.repeat(MAX_UPLOAD_BYTES))
    await pick(container, big)
    expect((await findByRole('alert')).textContent).toMatch(/the limit is 1 MB/)
    expect(apiMock.postRaw).not.toHaveBeenCalled()
  })

  it('shows the parser reason when the server rejects the file (nothing imported)', async () => {
    apiMock.postRaw.mockRejectedValueOnce(
      new ApiError(422, '/api/calendars/cal-me/import_ics', {
        code: 'ICS_PARSE_ERROR', detail: 'VEVENT missing SUMMARY',
      }),
    )
    const { container, findByText, findByRole } = setup()
    await pick(container, icsFile())
    await findByText(/family\.ics/)
    fireEvent.submit(document.querySelector('form.sh-cal-import')!)
    const alert = await findByRole('alert')
    expect(alert.textContent).toContain('VEVENT missing SUMMARY')
    expect(alert.textContent).toContain('Nothing was imported')
  })

  it('with the ai capability, a description is sent as JSON {prompt}', async () => {
    platformMock.ai = true
    apiMock.post.mockResolvedValueOnce({
      events: [{ id: 'e1', summary: 'Dentist', start: '2026-10-06T10:00:00+00:00' }],
    })
    const { getByLabelText, findByText } = setup()
    fireEvent.click(getByLabelText('Description (AI)'))
    fireEvent.input(getByLabelText('Describe the event(s)'), {
      target: { value: ' dentist next Tuesday 10am ' },
    })
    fireEvent.submit(document.querySelector('form.sh-cal-import')!)
    await findByText('Added 1 event to your calendar.')
    expect(apiMock.post).toHaveBeenCalledWith(
      '/api/calendars/cal-me/import_prompt', { prompt: 'dentist next Tuesday 10am' },
    )
  })

  it('with the ai capability, a photo is sent raw with its type and the note as ?caption=', async () => {
    platformMock.ai = true
    apiMock.postRaw.mockResolvedValueOnce({ events: [] })
    const { container, getByLabelText } = setup()
    fireEvent.click(getByLabelText('Photo (AI)'))
    const img = new File([new Uint8Array(10)], 'flyer.png', { type: 'image/png' })
    await pick(container, img)
    fireEvent.input(getByLabelText('Note for the AI (optional)'), {
      target: { value: 'only swimming' },
    })
    fireEvent.submit(document.querySelector('form.sh-cal-import')!)
    await waitFor(() => expect(apiMock.postRaw).toHaveBeenCalled())
    expect(apiMock.postRaw).toHaveBeenCalledWith(
      '/api/calendars/cal-me/import_image?caption=only+swimming', img, 'image/png',
    )
  })
})

describe('importErrorMessage', () => {
  it('maps the backend failure shapes to actionable copy', () => {
    expect(importErrorMessage(new ApiError(413, 'p', null), 'file')).toMatch(/too big/)
    expect(importErrorMessage(
      new ApiError(503, 'p', { code: 'AI_AGENT_UNAVAILABLE', detail: 'x' }), 'text',
    )).toMatch(/AI import isn't available/)
    expect(importErrorMessage(
      new ApiError(422, 'p', { code: 'AI_PARSE_ERROR', detail: 'x' }), 'photo',
    )).toMatch(/this photo/)
    expect(importErrorMessage(new TypeError('Failed to fetch'), 'file')).toMatch(/Couldn't reach/)
  })
})
