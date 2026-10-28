import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { apiMock } = vi.hoisted(() => ({ apiMock: { patch: vi.fn() } }))
vi.mock('@/api', () => ({ api: apiMock }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { currentUser } from '@/store/auth'
import { instanceConfig } from '@/store/instance'
import { showToast } from '@/components/Toast'
import type { User } from '@/types'
import { HaAutomationsSwitch } from './HaAutomationsSwitch'

function me(extra: Partial<User> = {}): User {
  return {
    user_id: 'u-anna', username: 'anna', display_name: 'Anna', is_admin: false,
    picture_url: null, picture_hash: null, bio: null, is_new_member: false,
    preferences_json: '{}', restrictions: [],
    ...extra,
  }
}

function platform(capabilities: string[]) {
  instanceConfig.value = {
    mode: 'haos', instance_name: 'Hearth', instance_id: null,
    capabilities, setup_required: false,
  }
}

beforeEach(() => {
  apiMock.patch.mockReset()
  vi.mocked(showToast).mockReset()
})
afterEach(() => {
  currentUser.value = null
  instanceConfig.value = null
})

describe('HaAutomationsSwitch (D14)', () => {
  it('is hidden when the platform has no automation events (standalone)', () => {
    platform([])
    currentUser.value = me()
    const { container } = render(<HaAutomationsSwitch />)
    expect(container.innerHTML).toBe('')
  })

  it('is hidden for a protected account (the server refuses it too)', () => {
    platform(['automation_events'])
    currentUser.value = me({ protected: true, restrictions: ['ha_automations'] })
    const { container } = render(<HaAutomationsSwitch />)
    expect(container.innerHTML).toBe('')
  })

  it('is off by default and saves the opt-in', async () => {
    platform(['automation_events'])
    currentUser.value = me()
    apiMock.patch.mockResolvedValueOnce({})
    const { getByRole, getByText } = render(<HaAutomationsSwitch />)
    const box = getByRole('checkbox', {
      name: 'Send my notifications and activity to Home Assistant automations',
    }) as HTMLInputElement
    expect(box.checked).toBe(false)
    expect(getByText(/never names, titles or what anything says/)).toBeTruthy()
    fireEvent.click(box)
    await waitFor(() => expect(apiMock.patch).toHaveBeenCalledWith(
      '/api/me', { preferences: { ha_automation_events: true } },
    ))
    await waitFor(() => expect(box.checked).toBe(true))
  })

  it('turns an existing opt-in off', async () => {
    platform(['automation_events'])
    currentUser.value = me({ preferences_json: '{"ha_automation_events":true}' })
    apiMock.patch.mockResolvedValueOnce({})
    const { getByRole } = render(<HaAutomationsSwitch />)
    const box = getByRole('checkbox') as HTMLInputElement
    expect(box.checked).toBe(true)
    fireEvent.click(box)
    await waitFor(() => expect(apiMock.patch).toHaveBeenCalledWith(
      '/api/me', { preferences: { ha_automation_events: false } },
    ))
    await waitFor(() => expect(box.checked).toBe(false))
  })

  it('keeps the old state and says so when saving fails', async () => {
    platform(['automation_events'])
    currentUser.value = me()
    apiMock.patch.mockRejectedValueOnce(new Error(''))
    const { getByRole } = render(<HaAutomationsSwitch />)
    const box = getByRole('checkbox') as HTMLInputElement
    fireEvent.click(box)
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      "Couldn't save. Try again.", 'error',
    ))
    expect(box.checked).toBe(false)
  })
})
