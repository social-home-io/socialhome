import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

describe('HaUsersPanel', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('renders HA users with synced toggles', async () => {
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => ([
          {
            username: 'alice', display_name: 'Alice',
            picture_url: null, is_admin: false, synced: true,
          },
          {
            username: 'kid', display_name: 'Kid',
            picture_url: null, is_admin: false, synced: false,
          },
        ])),
        post: vi.fn(),
        delete: vi.fn(),
      },
    }))
    const { HaUsersPanel } = await import('./HaUsersPanel')
    const { findByText } = render(<HaUsersPanel />)
    expect(await findByText('Alice')).toBeTruthy()
    expect(await findByText('Kid')).toBeTruthy()
    expect(await findByText('Active')).toBeTruthy()
    expect(await findByText('Not added')).toBeTruthy()
  })

  it('each person has a named switch that toggles from the switch or its status word', async () => {
    const post = vi.fn(async () => ({ synced: true }))
    const del = vi.fn(async () => ({}))
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => ([
          { username: 'alice', display_name: 'Alice', picture_url: null, is_admin: false, synced: true },
          { username: 'kid', display_name: 'Kid', picture_url: null, is_admin: false, synced: false },
        ])),
        post,
        delete: del,
      },
    }))
    vi.doMock('@/store/instance', () => ({
      instanceConfig: { value: { mode: 'haos', capabilities: ['ingress'], setup_required: false, instance_name: 'Home' } },
    }))
    const { HaUsersPanel } = await import('./HaUsersPanel')
    const { findByRole } = render(<HaUsersPanel />)
    const kid = await findByRole('switch', { name: 'Sync Kid' }) as HTMLInputElement
    expect(kid.checked).toBe(false)
    fireEvent.click(kid)
    await new Promise((r) => setTimeout(r, 20))
    expect(post).toHaveBeenCalledTimes(1)
    expect(kid.checked).toBe(true)
    // Kid's own status word, now "Active", is part of the tap target.
    const word = kid.parentElement!.querySelector('span')!
    expect(word.textContent).toBe('Active')
    fireEvent.click(word)
    await new Promise((r) => setTimeout(r, 20))
    expect(del).toHaveBeenCalledTimes(1)
  })

  it('flips the toggle and calls POST /provision for a new sync (haos)', async () => {
    const post = vi.fn(async () => ({ synced: true }))
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => ([{
          username: 'kid', display_name: 'Kid',
          picture_url: null, is_admin: false, synced: false,
        }])),
        post,
        delete: vi.fn(),
      },
    }))
    // haos mode — provision sends no body (Ingress is the auth path).
    vi.doMock('@/store/instance', () => ({
      instanceConfig: { value: { mode: 'haos', capabilities: ['ingress'], setup_required: false, instance_name: 'Home' } },
    }))
    const { HaUsersPanel } = await import('./HaUsersPanel')
    const { findByLabelText } = render(<HaUsersPanel />)
    const toggle = (await findByLabelText('Sync Kid')) as HTMLInputElement
    toggle.click()
    await new Promise((r) => setTimeout(r, 20))
    expect(post).toHaveBeenCalledWith(
      '/api/admin/ha-users/kid/provision',
      undefined,
    )
  })

  it('prompts for a password in ha mode and forwards it to provision', async () => {
    const post = vi.fn(async () => ({ synced: true }))
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => ([{
          username: 'kid', display_name: 'Kid',
          picture_url: null, is_admin: false, synced: false,
        }])),
        post,
        delete: vi.fn(),
      },
    }))
    vi.doMock('@/store/instance', () => ({
      instanceConfig: { value: { mode: 'ha', capabilities: ['password_auth'], setup_required: false, instance_name: 'Home' } },
    }))
    const { HaUsersPanel } = await import('./HaUsersPanel')
    const { findByLabelText, findByText, container } = render(<HaUsersPanel />)
    const toggle = (await findByLabelText('Sync Kid')) as HTMLInputElement
    fireEvent.click(toggle)
    // Modal opens — fill the two password fields and submit.
    await findByText('Set a Social Home password')
    const pwInputs = container.querySelectorAll<HTMLInputElement>('input[type="password"]')
    expect(pwInputs.length).toBe(2)
    fireEvent.input(pwInputs[0], { target: { value: 'strong-pw-1' } })
    fireEvent.input(pwInputs[1], { target: { value: 'strong-pw-1' } })
    const submit = await findByText(/^Add Kid$/) as HTMLButtonElement
    fireEvent.click(submit)
    await new Promise((r) => setTimeout(r, 20))
    expect(post).toHaveBeenCalledWith(
      '/api/admin/ha-users/kid/provision',
      { password: 'strong-pw-1' },
    )
  })
})
