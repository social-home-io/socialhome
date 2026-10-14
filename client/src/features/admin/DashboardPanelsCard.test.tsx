import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, screen } from '@testing-library/preact'
import type { DashboardDisplay, DashboardState, HaPanel, SaveFn } from './dashboardTypes'

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
import { DashboardPanelsCard, HA_POLL_MS, HA_WAIT_MS, formatPairingCode } from './DashboardPanelsCard'

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
    panel_unlock_from_ha: false,
    household_chat_enabled: true,
    last_seen_at: null,
    people: [],
    displays: [],
    ...over,
  }
}

let reload: ReturnType<typeof vi.fn<() => Promise<void>>>
let save: ReturnType<typeof vi.fn<SaveFn>>

function mount(s: DashboardState) {
  return render(<DashboardPanelsCard state={s} save={save} reload={reload} />)
}

async function openAdd() {
  fireEvent.click(screen.getByRole('button', { name: 'Add a panel' }))
  await screen.findByRole('dialog')
}

beforeEach(() => {
  reload = vi.fn<() => Promise<void>>().mockResolvedValue(undefined)
  save = vi.fn<SaveFn>().mockResolvedValue(null)
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

describe('DashboardPanelsCard — Home Assistant unlock', () => {
  const haSwitch = () => screen.getByRole('switch', {
    name: 'Home Assistant may unlock wall panels',
  }) as HTMLInputElement

  it('is off by default and explains the trade-off, even with no panel paired', () => {
    mount(state())
    expect(screen.getByText('No wall panels paired yet.')).toBeTruthy()
    const sw = haSwitch()
    expect(sw.checked).toBe(false)
    expect(sw.disabled).toBe(false)
    expect(document.getElementById(sw.getAttribute('aria-describedby')!)?.textContent).toBe(
      'Lets a Home Assistant automation unlock a panel without the PIN. Anyone who can run '
      + 'your Home Assistant automations or actions can then lift the privacy lock.')
  })

  it('reflects the stored setting', () => {
    mount(state({ panel_unlock_from_ha: true }))
    expect(haSwitch().checked).toBe(true)
  })

  it('saves the switch and greys it while the save is in flight', async () => {
    let finish: (v: Error | null) => void = () => {}
    save.mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    mount(state())
    fireEvent.click(haSwitch())
    expect(save).toHaveBeenCalledWith({ panel_unlock_from_ha: true })
    await waitFor(() => expect(haSwitch().disabled).toBe(true))
    finish(null)
    await waitFor(() => expect(haSwitch().disabled).toBe(false))
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

describe('DashboardPanelsCard — unlock', () => {
  const LOCKED: DashboardDisplay = { ...KITCHEN, id: 'd2', name: 'Hallway', locked: true }

  it('shows "Locked" and an Unlock button only for a locked panel', () => {
    mount(state({ displays: [KITCHEN, LOCKED] }))
    expect(screen.getAllByText('Locked')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Unlock Hallway' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Unlock Kitchen' })).toBeNull()
  })

  it('lifts the lock with a PATCH, toasts and reloads', async () => {
    mPatch.mockResolvedValue({ ...LOCKED, locked: false })
    mount(state({ displays: [LOCKED] }))
    fireEvent.click(screen.getByRole('button', { name: 'Unlock Hallway' }))
    await waitFor(() => expect(mPatch).toHaveBeenCalledWith(
      '/api/admin/dashboard/displays/d2', { locked: false },
    ))
    await waitFor(() => expect(reload).toHaveBeenCalled())
    expect(showToast).toHaveBeenCalledWith('Hallway is unlocked', 'success')
    expect(confirmDialog).not.toHaveBeenCalled()
  })

  it('a panel removed meanwhile just refreshes, no toast', async () => {
    mPatch.mockRejectedValueOnce(new ApiError(404, '/api/admin/dashboard/displays/d2', {
      code: 'DASHBOARD_DISPLAY_NOT_FOUND', detail: 'x',
    }))
    mount(state({ displays: [LOCKED] }))
    fireEvent.click(screen.getByRole('button', { name: 'Unlock Hallway' }))
    await waitFor(() => expect(reload).toHaveBeenCalled())
    expect(showToast).not.toHaveBeenCalled()
  })

  it('any other failure toasts the message and still reloads', async () => {
    mPatch.mockRejectedValueOnce(new Error('Server is down'))
    mount(state({ displays: [LOCKED] }))
    fireEvent.click(screen.getByRole('button', { name: 'Unlock Hallway' }))
    await waitFor(() => expect(reload).toHaveBeenCalled())
    expect(showToast).toHaveBeenCalledWith('Server is down', 'error')
  })
})

describe('DashboardPanelsCard — found in Home Assistant', () => {
  const mGet = vi.mocked(api.get)
  const READY: HaPanel = {
    id: 'dev-kitchen', name: 'Kitchen panel', sw_version: '2026.10.13 (ESPHome 2026.9.1)',
    status: 'ready', can_connect: true, display_id: null, display_name: null,
  }

  it('is hidden without the Home Assistant capability', () => {
    mount(state())
    expect(screen.queryByText('Found in Home Assistant')).toBeNull()
    expect(mGet).not.toHaveBeenCalled()
  })

  it('lists the panels with their state, and only offers Connect where it works', async () => {
    mGet.mockResolvedValue({
      panels: [
        READY,
        { ...READY, id: 'dev-hall', name: 'Hall', status: 'connected', display_id: 'd1',
          display_name: 'Hallway' },
        { ...READY, id: 'dev-off', name: 'Garage', status: 'offline', can_connect: false },
        { ...READY, id: 'dev-old', name: 'Attic', status: 'needs_update', can_connect: false },
      ],
    })
    mount(state({ ha_display_discovery: true }))
    expect(screen.getByText('Asking Home Assistant…')).toBeTruthy()
    await screen.findByText('Kitchen panel')
    expect(mGet).toHaveBeenCalledWith('/api/admin/dashboard/displays/ha')
    expect(screen.getByText('Not paired yet')).toBeTruthy()
    expect(screen.getByText('Paired here as Hallway')).toBeTruthy()
    expect(screen.getByText('Offline in Home Assistant')).toBeTruthy()
    expect(screen.getByText("Update the panel's firmware to connect it from here")).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Connect Kitchen panel' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Connect Hall again' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Connect Garage' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Connect Attic' })).toBeNull()
  })

  it('says how to adopt a panel when Home Assistant has none', async () => {
    mGet.mockResolvedValue({ panels: [] })
    mount(state({ ha_display_discovery: true }))
    expect(await screen.findByText(/No wall panels in Home Assistant yet/)).toBeTruthy()
  })

  it('shows a failed read and retries on Refresh', async () => {
    mGet.mockRejectedValueOnce(new ApiError(502, '/api/admin/dashboard/displays/ha', {
      code: 'DISPLAY_DISCOVERY_FAILED', detail: 'x',
    }))
    mount(state({ ha_display_discovery: true }))
    expect((await screen.findByRole('alert')).textContent)
      .toBe("Home Assistant didn't answer. Try again in a moment.")
    mGet.mockResolvedValue({ panels: [READY] })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await screen.findByText('Kitchen panel')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('greys Connect while the dashboard is off', async () => {
    mGet.mockResolvedValue({ panels: [READY] })
    mount(state({ ha_display_discovery: true, enabled: false, running: false, running_port: null }))
    const btn = await screen.findByRole('button', { name: 'Connect Kitchen panel' }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it('connects, waits for the panel to pair itself, then refreshes the paired list', async () => {
    mGet.mockResolvedValue({ panels: [READY] })
    mPost.mockResolvedValue({ expires_in: 600 })
    mount(state({ ha_display_discovery: true }))
    const btn = await screen.findByRole('button', { name: 'Connect Kitchen panel' })
    vi.useFakeTimers()
    try {
      fireEvent.click(btn)
      await vi.waitFor(() => expect(mPost).toHaveBeenCalledWith(
        '/api/admin/dashboard/displays/ha/dev-kitchen/connect', {},
      ))
      await vi.waitFor(() => expect(screen.getByText('Waiting for the panel to pair…')).toBeTruthy())
      expect(showToast).toHaveBeenCalledWith(
        'Sent to Kitchen panel — it pairs itself in a few seconds.', 'success',
      )
      mGet.mockResolvedValue({ panels: [{ ...READY, status: 'connected', display_id: 'd9',
        display_name: 'Kitchen panel' }] })
      await vi.advanceTimersByTimeAsync(HA_POLL_MS)
      await vi.waitFor(() => expect(reload).toHaveBeenCalled())
      expect(showToast).toHaveBeenCalledWith('Kitchen panel is connected', 'success')
      expect(screen.getByText('Paired here as Kitchen panel')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('"Connect again" waits for a new pairing, not the old one', async () => {
    const HALL: HaPanel = { ...READY, status: 'connected', display_id: 'd1', display_name: 'Hall' }
    mGet.mockResolvedValue({ panels: [HALL] })
    mPost.mockResolvedValue({ expires_in: 600 })
    mount(state({ ha_display_discovery: true }))
    const btn = await screen.findByRole('button', { name: 'Connect Kitchen panel again' })
    vi.useFakeTimers()
    try {
      fireEvent.click(btn)
      await vi.waitFor(() => expect(screen.getByText('Waiting for the panel to pair…')).toBeTruthy())
      // Still the old display: keep waiting, no "connected" toast.
      await vi.advanceTimersByTimeAsync(HA_POLL_MS)
      expect(reload).not.toHaveBeenCalled()
      expect(screen.getByText('Waiting for the panel to pair…')).toBeTruthy()
      mGet.mockResolvedValue({ panels: [{ ...HALL, display_id: 'd2' }] })
      await vi.advanceTimersByTimeAsync(HA_POLL_MS)
      await vi.waitFor(() => expect(reload).toHaveBeenCalled())
      expect(showToast).toHaveBeenCalledWith('Hall is connected', 'success')
    } finally {
      vi.useRealTimers()
    }
  })

  it('names the panel in the timeout even when Home Assistant stops listing it', async () => {
    mGet.mockResolvedValue({ panels: [READY] })
    mPost.mockResolvedValue({ expires_in: 600 })
    mount(state({ ha_display_discovery: true }))
    const btn = await screen.findByRole('button', { name: 'Connect Kitchen panel' })
    vi.useFakeTimers()
    try {
      fireEvent.click(btn)
      await vi.waitFor(() => expect(screen.getByText('Waiting for the panel to pair…')).toBeTruthy())
      mGet.mockRejectedValue(new ApiError(502, '/x', { code: 'DISPLAY_DISCOVERY_FAILED', detail: 'x' }))
      await vi.advanceTimersByTimeAsync(HA_WAIT_MS + HA_POLL_MS)
      await vi.waitFor(() => expect(screen.getByText(/^Kitchen panel didn't pair\./)).toBeTruthy())
    } finally {
      vi.useRealTimers()
    }
  })

  it('gives up waiting after a minute and says so', async () => {
    mGet.mockResolvedValue({ panels: [READY] })
    mPost.mockResolvedValue({ expires_in: 600 })
    mount(state({ ha_display_discovery: true }))
    const btn = await screen.findByRole('button', { name: 'Connect Kitchen panel' })
    vi.useFakeTimers()
    try {
      fireEvent.click(btn)
      await vi.waitFor(() => expect(screen.getByText('Waiting for the panel to pair…')).toBeTruthy())
      await vi.advanceTimersByTimeAsync(HA_WAIT_MS + HA_POLL_MS)
      await vi.waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(
        /^Kitchen panel didn't pair\./,
      ))
      expect(reload).not.toHaveBeenCalled()
      expect((screen.getByRole('button', { name: 'Connect Kitchen panel' }) as HTMLButtonElement)
        .disabled).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows why a connect was refused next to the panel', async () => {
    mGet.mockResolvedValue({ panels: [READY] })
    mPost.mockRejectedValue(new ApiError(409, '/x', {
      code: 'DASHBOARD_ADDRESS_REQUIRED', detail: 'x', params: { reason: 'no_address' },
    }))
    mount(state({ ha_display_discovery: true }))
    fireEvent.click(await screen.findByRole('button', { name: 'Connect Kitchen panel' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toBe(
      'Set the dashboard address first, so the panel can reach the dashboard.',
    )
    const btn = screen.getByRole('button', { name: 'Connect Kitchen panel' })
    expect(btn.getAttribute('aria-describedby')).toBe(alert.id)
    // Refresh starts over: the old reason goes once the admin re-reads.
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })
})
