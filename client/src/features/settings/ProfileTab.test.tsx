import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup, act } from '@testing-library/preact'

const { apiMock, FakeApiError, confirmMock } = vi.hoisted(() => {
  class FakeApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  }
  return {
    apiMock: {
      get: vi.fn(),
      post: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
      upload: vi.fn(),
    },
    FakeApiError,
    confirmMock: vi.fn(),
  }
})
vi.mock('@/api', () => ({ api: apiMock, ApiError: FakeApiError }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirmMock }))

import { currentUser } from '@/store/auth'
import type { User } from '@/types'
import { confirmLeave, shouldConfirmLeave } from '@/router/navigationGuard'
import {
  ProfileTab, profileErrors, profileIsDirty, resetProfileDraft,
  useProfileLeaveGuard,
} from './ProfileTab'

function me(extra: Partial<User> = {}): User {
  return {
    user_id: 'u1', username: 'pascal', handle: 'pascal', display_name: 'Pascal Vizeli',
    is_admin: true, picture_url: '/api/users/u1/picture?v=1', picture_hash: 'h1',
    bio: 'Fixes the garden gate.', is_new_member: false, source: 'manual',
    status: { emoji: '🛠', text: 'Fixing the gate', expires_at: null },
    ...extra,
  }
}

/** ``PATCH /api/me`` answers with the whole profile, as the route does. */
function echoPatch() {
  apiMock.patch.mockImplementation(async (_path: string, body: Record<string, unknown>) => {
    const u = { ...(currentUser.value as User) }
    if ('display_name' in body) u.display_name = body.display_name as string
    if ('bio' in body) u.bio = body.bio as string | null
    return u
  })
}

function Guarded() {
  useProfileLeaveGuard('/settings')
  return <ProfileTab />
}

const input = (el: Element, value: string) => fireEvent.input(el, { target: { value } })

beforeEach(() => {
  for (const fn of Object.values(apiMock)) fn.mockReset()
  confirmMock.mockReset()
  currentUser.value = me()
  resetProfileDraft()
  echoPatch()
  apiMock.post.mockImplementation(async (_p: string, body: { handle: string }) => ({ handle: body.handle }))
})
afterEach(() => {
  cleanup()
  currentUser.value = null
  resetProfileDraft()
})

describe('ProfileTab — how others see you', () => {
  it('leads with the saved name, @handle and bio', () => {
    const { getByRole, getByText } = render(<ProfileTab />)
    expect(getByText('How others see you')).toBeTruthy()
    expect(getByRole('heading', { level: 2, name: 'Pascal Vizeli' })).toBeTruthy()
    expect(getByText('@pascal')).toBeTruthy()
    expect(getByText('Fixes the garden gate.', { selector: '.sh-profile-glance__bio' })).toBeTruthy()
  })

  it('status is not part of the profile: no status line, no status fields', () => {
    // The fixture user HAS a status — it lives in the top bar's "You" menu now.
    const { container, queryByLabelText, queryByRole } = render(<ProfileTab />)
    expect(container.textContent).not.toContain('Fixing the gate')
    expect(queryByLabelText('Status emoji')).toBeNull()
    expect(queryByLabelText('Status text')).toBeNull()
    expect(queryByRole('button', { name: 'Clear status' })).toBeNull()
  })

  it('offers Change photo as a button and Remove as the quiet action (P6)', () => {
    const { getByRole } = render(<ProfileTab />)
    const change = getByRole('button', { name: 'Change photo' })
    expect(change.className).toContain('sh-btn')
    const remove = getByRole('button', { name: 'Remove photo' })
    expect(remove.className).toBe('sh-profile-glance__quiet')
  })

  it('hides Remove photo when there is no picture', () => {
    currentUser.value = me({ picture_url: null })
    const { queryByRole } = render(<ProfileTab />)
    expect(queryByRole('button', { name: 'Remove photo' })).toBeNull()
  })

  it('drops the "Edited here" chip outside HA mode (P4)', () => {
    const { container, queryByText } = render(<ProfileTab />)
    expect(queryByText(/Edited here/)).toBeNull()
    expect(container.querySelector('.sh-profile-source')).toBeNull()
  })

  it('keeps the HA source chip and the "Use Home Assistant picture" action for HA users', () => {
    currentUser.value = me({ source: 'ha' })
    const { getByText, getByRole } = render(<ProfileTab />)
    expect(getByText(/Synced from Home Assistant/)).toBeTruthy()
    expect(getByRole('button', { name: 'Use Home Assistant picture' })).toBeTruthy()
  })

  it('removing the photo asks first, then deletes it', async () => {
    confirmMock.mockResolvedValue(true)
    apiMock.delete.mockResolvedValue(undefined)
    apiMock.get.mockResolvedValue(me({ picture_url: null }))
    const { getByRole, queryByRole } = render(<ProfileTab />)
    fireEvent.click(getByRole('button', { name: 'Remove photo' }))
    await waitFor(() => expect(apiMock.delete).toHaveBeenCalledWith('/api/me/picture'))
    await waitFor(() => expect(queryByRole('button', { name: 'Remove photo' })).toBeNull())
  })
})

describe('ProfileTab — one form, dirty-only save bar (decision 7)', () => {
  it('shows no save bar until something changes, and hides it when the change is undone', () => {
    const { getByLabelText, queryByRole } = render(<ProfileTab />)
    expect(queryByRole('region', { name: 'Unsaved changes' })).toBeNull()
    const name = getByLabelText('Name')
    input(name, 'Pascal V.')
    expect(queryByRole('region', { name: 'Unsaved changes' })).toBeTruthy()
    expect(profileIsDirty()).toBe(true)
    input(name, 'Pascal Vizeli')
    expect(queryByRole('region', { name: 'Unsaved changes' })).toBeNull()
  })

  it('Discard puts every field back and hides the bar', () => {
    const { getByLabelText, getByRole, queryByRole } = render(<ProfileTab />)
    input(getByLabelText('Name'), 'Someone else')
    input(getByLabelText('About you'), 'New bio')
    fireEvent.click(getByRole('button', { name: 'Discard' }))
    expect((getByLabelText('Name') as HTMLInputElement).value).toBe('Pascal Vizeli')
    expect((getByLabelText('About you') as HTMLTextAreaElement).value).toBe('Fixes the garden gate.')
    expect(queryByRole('region', { name: 'Unsaved changes' })).toBeNull()
    expect(apiMock.patch).not.toHaveBeenCalled()
  })

  it('saves only the changed fields, each on its own endpoint', async () => {
    const { getByLabelText, getByRole, queryByRole } = render(<ProfileTab />)
    input(getByLabelText('Name'), '  Pascal V.  ')
    input(getByLabelText('Public @handle'), 'pascal_v')
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(queryByRole('region', { name: 'Unsaved changes' })).toBeNull())
    expect(apiMock.patch).toHaveBeenCalledTimes(1)
    expect(apiMock.patch).toHaveBeenCalledWith('/api/me', { display_name: 'Pascal V.' })
    expect(apiMock.post).toHaveBeenCalledWith('/api/me/handle', { handle: 'pascal_v' })
    expect(currentUser.value?.display_name).toBe('Pascal V.')
    expect(currentUser.value?.handle).toBe('pascal_v')
  })

  it('a bio emptied to whitespace saves as no bio', async () => {
    const { getByLabelText, getByRole } = render(<ProfileTab />)
    input(getByLabelText('About you'), '   ')
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiMock.patch).toHaveBeenCalledWith('/api/me', { bio: null }))
  })

  it('Enter in a field submits the form', async () => {
    const { getByLabelText } = render(<ProfileTab />)
    const name = getByLabelText('Name')
    input(name, 'Pascal V.')
    fireEvent.submit(name.closest('form') as HTMLFormElement)
    await waitFor(() => expect(apiMock.patch).toHaveBeenCalledWith('/api/me', { display_name: 'Pascal V.' }))
  })
})

describe('ProfileTab — errors land on their field', () => {
  it('a taken handle keeps the other saves and shows the error at the handle', async () => {
    apiMock.post.mockRejectedValue(new FakeApiError(422, 'That handle is taken.'))
    const { getByLabelText, getByRole, findByText } = render(<ProfileTab />)
    input(getByLabelText('Name'), 'Pascal V.')
    input(getByLabelText('Public @handle'), 'taken')
    fireEvent.click(getByRole('button', { name: 'Save' }))

    await findByText('That handle is taken.')
    // The name saved…
    expect(currentUser.value?.display_name).toBe('Pascal V.')
    expect(profileErrors.value.name).toBeUndefined()
    // …the handle did not, and says why, right at the field.
    const handle = getByLabelText('Public @handle') as HTMLInputElement
    expect(handle.value).toBe('taken')
    expect(handle.getAttribute('aria-invalid')).toBe('true')
    expect(handle.getAttribute('aria-describedby')).toContain('sh-handle-error')
    expect(document.activeElement).toBe(handle)
    // Still unsaved: the bar stays for another try.
    expect(getByRole('region', { name: 'Unsaved changes' })).toBeTruthy()
    expect(currentUser.value?.handle).toBe('pascal')
  })

  it('an empty name is caught before any request', async () => {
    const { getByLabelText, getByRole, findByText } = render(<ProfileTab />)
    input(getByLabelText('Name'), '   ')
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await findByText("Your name can't be empty.")
    expect(apiMock.patch).not.toHaveBeenCalled()
  })

  it('typing in a field clears its error', async () => {
    apiMock.post.mockRejectedValue(new FakeApiError(422, 'That handle is taken.'))
    const { getByLabelText, getByRole, findByText, queryByText } = render(<ProfileTab />)
    input(getByLabelText('Public @handle'), 'taken')
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await findByText('That handle is taken.')
    input(getByLabelText('Public @handle'), 'taken2')
    expect(queryByText('That handle is taken.')).toBeNull()
  })
})

describe('ProfileTab — unsaved-change guard', () => {
  it('asks before an in-app route change only while something is unsaved', async () => {
    const { getByLabelText } = render(<Guarded />)
    expect(shouldConfirmLeave('/feed')).toBe(false)
    input(getByLabelText('Name'), 'Pascal V.')
    expect(shouldConfirmLeave('/feed')).toBe(true)
    // Staying on Settings (a hash / query hop) is not leaving.
    expect(shouldConfirmLeave('/settings#privacy')).toBe(false)
  })

  it('"Keep editing" keeps the edits; "Discard changes" drops them', async () => {
    const { getByLabelText } = render(<Guarded />)
    input(getByLabelText('Name'), 'Pascal V.')

    confirmMock.mockResolvedValueOnce(false)
    expect(await confirmLeave()).toBe(false)
    expect(profileIsDirty()).toBe(true)
    expect(confirmMock.mock.calls[0][1]).toMatchObject({
      title: 'Leave without saving?',
      confirmLabel: 'Discard changes',
      cancelLabel: 'Keep editing',
    })

    confirmMock.mockResolvedValueOnce(true)
    expect(await confirmLeave()).toBe(true)
    expect(profileIsDirty()).toBe(false)
  })

  it('warns on reload / tab close while dirty', () => {
    const { getByLabelText } = render(<Guarded />)
    const clean = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(clean)
    expect(clean.defaultPrevented).toBe(false)

    input(getByLabelText('Name'), 'Pascal V.')
    const dirty = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(dirty)
    expect(dirty.defaultPrevented).toBe(true)
  })

  it('stops guarding once unmounted', () => {
    const { getByLabelText, unmount } = render(<Guarded />)
    input(getByLabelText('Name'), 'Pascal V.')
    act(() => { unmount() })
    expect(shouldConfirmLeave('/feed')).toBe(false)
  })
})
