import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, screen } from '@testing-library/preact'
import type { DashboardDisplay, DashboardState } from './dashboardTypes'

vi.mock('@/api', async (importOriginal) => {
  const real = await importOriginal<typeof import('@/api')>()
  return {
    ...real,
    api: {
      get: vi.fn(),
      put: vi.fn(),
      post: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn().mockResolvedValue(null),
    },
  }
})
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/components/confirm', () => ({ confirmDialog: vi.fn().mockResolvedValue(true) }))

import { api, ApiError } from '@/api'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { DashboardPanelsCard, formatPairingCode } from './DashboardPanelsCard'

const mPost = vi.mocked(api.post)
const mPatch = vi.mocked(api.patch)
const mDelete = vi.mocked(api.delete)

const KITCHEN: DashboardDisplay = {
  id: 'd1',
  name: 'Kitchen',
  model: 'esp32-s3-box',
  firmware: '0.1.0',
  created_at: '2026-10-13T08:00:00+00:00',
  last_seen_at: null,
  locked: false,
}

function state(over: Partial<DashboardState> = {}): DashboardState {
  return {
    enabled: true,
    running: true,
    running_port: 8100,
    port: 8100,
    port_fixed: false,
    password_set: true,
    password_set_at: '2026-10-11T08:00:00+00:00',
    pin_set: false,
    address: null,
    url: 'http://homeassistant.local:8100',
    url_host_allowed: true,
    locale: 'en',
    idle_return_minutes: 3,
    night_start: '22:00',
    night_end: '06:00',
    keep_screen_on: true,
    show_status_text: false,
    show_household_messages: false,
    mention_sound: true,
    household_chat_enabled: true,
    last_seen_at: null,
    people: [],
    displays: [],
    ...over,
  }
}

let reload: ReturnType<typeof vi.fn<() => Promise<void>>>

function mount(s: DashboardState) {
  return render(<DashboardPanelsCard state={s} reload={reload} />)
}

async function openAdd() {
  fireEvent.click(screen.getByRole('button', { name: 'Add a panel' }))
  await screen.findByRole('dialog')
}

beforeEach(() => {
  reload = vi.fn<() => Promise<void>>().mockResolvedValue(undefined)
  vi.stubGlobal('fetch', vi.fn())
})

afterEach(() => {
  // Every call goes through the api client.
  expect(fetch).not.toHaveBeenCalled()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('formatPairingCode', () => {
  it('upper-cases, drops separators and look-alikes, caps at 6', () => {
    expect(formatPairingCode('abc-def')).toBe('ABCDEF')
    expect(formatPairingCode(' 7k2 m9q ')).toBe('7K2M9Q')
    expect(formatPairingCode('O0I1LUV')).toBe('')
    expect(formatPairingCode('ABCDEFGH')).toBe('ABCDEF')
  })
})

describe('DashboardPanelsCard — list', () => {
  it('says when no panel is paired', () => {
    mount(state())
    expect(screen.getByRole('heading', { name: 'Wall panels' })).toBeTruthy()
    expect(screen.getByText('No wall panels paired yet.')).toBeTruthy()
  })

  it('lists name, model · firmware and when it was last seen', () => {
    mount(state({
      displays: [
        KITCHEN,
        { ...KITCHEN, id: 'd2', name: 'Hallway', model: null, firmware: null,
          last_seen_at: new Date(Date.now() - 5 * 60_000).toISOString() },
      ],
    }))
    expect(screen.getByText('Kitchen')).toBeTruthy()
    expect(screen.getByText('esp32-s3-box · 0.1.0')).toBeTruthy()
    expect(screen.getByText('Not connected yet')).toBeTruthy()
    expect(screen.getByText(/^Last seen /)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Rename Hallway' })).toBeTruthy()
  })

  it('greys "Add a panel" and says why while the dashboard is off', () => {
    mount(state({ enabled: false, running: false, running_port: null }))
    const add = screen.getByRole('button', { name: 'Add a panel' }) as HTMLButtonElement
    expect(add.disabled).toBe(true)
    expect(add.getAttribute('aria-describedby')).toBe('sh-dash-panels-off')
    expect(screen.getByText('Turn the dashboard on to pair a panel.')).toBeTruthy()
  })
})

describe('DashboardPanelsCard — add', () => {
  it('formats the code as typed and pairs the panel', async () => {
    mPost.mockResolvedValue({ ...KITCHEN })
    mount(state())
    await openAdd()
    const code = screen.getByLabelText('Code on the panel') as HTMLInputElement
    fireEvent.input(code, { target: { value: 'k7m-2qx' } })
    expect(code.value).toBe('K7M2QX')
    fireEvent.input(screen.getByLabelText('Name'), { target: { value: '  Kitchen ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Pair' }))
    await waitFor(() => expect(mPost).toHaveBeenCalledWith(
      '/api/admin/dashboard/displays', { code: 'K7M2QX', name: 'Kitchen' },
    ))
    await waitFor(() => expect(reload).toHaveBeenCalled())
    expect(showToast).toHaveBeenCalledWith('Kitchen is paired', 'success')
  })

  it('needs all 6 characters and a name before calling the server', async () => {
    mount(state())
    await openAdd()
    fireEvent.input(screen.getByLabelText('Code on the panel'), { target: { value: 'K7M' } })
    fireEvent.click(screen.getByRole('button', { name: 'Pair' }))
    expect((await screen.findByRole('alert')).textContent).toBe('Enter all 6 characters of the code.')
    fireEvent.input(screen.getByLabelText('Code on the panel'), { target: { value: 'K7M2QX' } })
    fireEvent.click(screen.getByRole('button', { name: 'Pair' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('Give the panel a name.'))
    expect(mPost).not.toHaveBeenCalled()
  })

  it('shows a wrong code inline and keeps the dialog open', async () => {
    mPost.mockRejectedValue(new ApiError(404, '/api/admin/dashboard/displays', {
      code: 'DASHBOARD_PAIRING_CODE_INVALID', detail: 'x',
    }))
    mount(state())
    await openAdd()
    fireEvent.input(screen.getByLabelText('Code on the panel'), { target: { value: 'K7M2QX' } })
    fireEvent.input(screen.getByLabelText('Name'), { target: { value: 'Kitchen' } })
    fireEvent.click(screen.getByRole('button', { name: 'Pair' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/^No panel is waiting with that code\./)
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(reload).not.toHaveBeenCalled()
  })

  it('starts empty every time it opens', async () => {
    mount(state())
    await openAdd()
    fireEvent.input(screen.getByLabelText('Code on the panel'), { target: { value: 'K7M2QX' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await openAdd()
    expect((screen.getByLabelText('Code on the panel') as HTMLInputElement).value).toBe('')
  })
})

describe('DashboardPanelsCard — rename / remove', () => {
  it('renames a panel', async () => {
    mPatch.mockResolvedValue({ ...KITCHEN, name: 'Hallway' })
    mount(state({ displays: [KITCHEN] }))
    fireEvent.click(screen.getByRole('button', { name: 'Rename Kitchen' }))
    const input = await screen.findByLabelText('Name') as HTMLInputElement
    expect(input.value).toBe('Kitchen')
    fireEvent.input(input, { target: { value: 'Hallway' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(mPatch).toHaveBeenCalledWith(
      '/api/admin/dashboard/displays/d1', { name: 'Hallway' },
    ))
    await waitFor(() => expect(reload).toHaveBeenCalled())
  })

  it('removes a panel after confirming', async () => {
    mount(state({ displays: [KITCHEN] }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove Kitchen' }))
    await waitFor(() => expect(mDelete).toHaveBeenCalledWith('/api/admin/dashboard/displays/d1'))
    expect(confirmDialog).toHaveBeenCalledWith(
      expect.stringMatching(/new code/),
      expect.objectContaining({ title: 'Remove Kitchen?', destructive: true }),
    )
    await waitFor(() => expect(reload).toHaveBeenCalled())
    expect(showToast).toHaveBeenCalledWith('Kitchen removed', 'success')
  })

  it('keeps the panel when the confirm is cancelled', async () => {
    vi.mocked(confirmDialog).mockResolvedValueOnce(false)
    mount(state({ displays: [KITCHEN] }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove Kitchen' }))
    await waitFor(() => expect(confirmDialog).toHaveBeenCalled())
    expect(mDelete).not.toHaveBeenCalled()
  })

  it('a panel already removed elsewhere just refreshes, no error toast', async () => {
    mDelete.mockRejectedValueOnce(new ApiError(404, '/api/admin/dashboard/displays/d1', {
      code: 'DASHBOARD_DISPLAY_NOT_FOUND', detail: 'x',
    }))
    mount(state({ displays: [KITCHEN] }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove Kitchen' }))
    await waitFor(() => expect(reload).toHaveBeenCalled())
    expect(showToast).not.toHaveBeenCalled()
  })
})
