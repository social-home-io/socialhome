import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import type { User } from '@/types'

const apiPost = vi.fn()
vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return { ApiError: actual.ApiError, api: { get: vi.fn(), post: (...a: unknown[]) => apiPost(...a) } }
})
vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

const users = vi.hoisted(() => ({ value: new Map<string, unknown>() }))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: users, loadHouseholdUsers: vi.fn(),
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-dad', username: 'pascal', display_name: 'Pascal Vizeli' } },
}))
vi.mock('@/utils/week', async () => {
  const actual = await vi.importActual<typeof import('@/utils/week')>('@/utils/week')
  return { ...actual, currentWeekStart: () => 0 }
})

import { TimetableCreateDialog, openCreateDialog, closeCreateDialog, defaultName } from './TimetableCreateDialog'
import { timetable } from './testUtils'

function user(user_id: string, display_name: string): User {
  return { user_id, username: display_name.toLowerCase(), display_name, is_admin: false,
    picture_url: null, picture_hash: null, bio: null } as User
}

beforeEach(() => {
  users.value = new Map([
    ['u-dad', user('u-dad', 'Pascal Vizeli')],
    ['u-emma', user('u-emma', 'Emma')],
    ['u-leo', user('u-leo', 'Leo Vizeli')],
  ])
  apiPost.mockReset()
  closeCreateDialog()
})

const nameField = (getByLabelText: (s: string) => HTMLElement) =>
  getByLabelText('Name') as HTMLInputElement

describe('TimetableCreateDialog', () => {
  it('prefills the name from me, focused and selected so typing replaces it', async () => {
    openCreateDialog()
    const { getByLabelText } = render(<TimetableCreateDialog onCreated={vi.fn()} />)
    const name = nameField(getByLabelText)
    expect(name.value).toBe("Pascal's timetable")
    await waitFor(() => expect(document.activeElement).toBe(name))
    expect(name.selectionStart).toBe(0)
    expect(name.selectionEnd).toBe(name.value.length)
    expect(name.placeholder).toBe('e.g. Emma – Class 4b')
  })

  it('on touch (no autofocus) the prefilled name is selected on first focus', () => {
    const mm = window.matchMedia
    window.matchMedia = ((q: string) => ({ matches: q === '(pointer: coarse)', media: q,
      addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
    try {
      openCreateDialog()
      const { getByLabelText } = render(<TimetableCreateDialog onCreated={vi.fn()} />)
      const name = nameField(getByLabelText)
      name.setSelectionRange(0, 0)
      fireEvent.focus(name)
      expect(name.selectionStart).toBe(0)
      expect(name.selectionEnd).toBe(name.value.length)
      // …only the first time: a second focus keeps the caret where the user put it.
      name.setSelectionRange(2, 2)
      fireEvent.focus(name)
      expect(name.selectionStart).toBe(2)
    } finally {
      window.matchMedia = mm
    }
  })

  it('follows the first assignee until the name is edited', () => {
    openCreateDialog()
    const { getByLabelText, getByRole } = render(<TimetableCreateDialog onCreated={vi.fn()} />)
    fireEvent.click(getByRole('button', { name: 'Pascal Vizeli' })) // unassign me
    expect(nameField(getByLabelText).value).toBe('Timetable')
    fireEvent.click(getByRole('button', { name: 'Emma' }))
    expect(nameField(getByLabelText).value).toBe("Emma's timetable")
    // A second assignee doesn't change the first.
    fireEvent.click(getByRole('button', { name: 'Leo Vizeli' }))
    expect(nameField(getByLabelText).value).toBe("Emma's timetable")
    fireEvent.input(nameField(getByLabelText), { target: { value: 'Emma – 4b' } })
    fireEvent.click(getByRole('button', { name: 'Emma' }))
    expect(nameField(getByLabelText).value).toBe('Emma – 4b')
  })

  it('defaultName uses the first name only', () => {
    expect(defaultName(['u-leo'])).toBe("Leo's timetable")
    expect(defaultName([])).toBe('Timetable')
  })

  it('creates with template, days, week start and assignees', async () => {
    const created = timetable({ id: 'new' })
    apiPost.mockResolvedValue({ timetable: created })
    const onCreated = vi.fn()
    openCreateDialog('empty')
    const { getByRole, getByLabelText } = render(<TimetableCreateDialog onCreated={onCreated} />)
    expect((getByLabelText(/Add every lesson yourself/) as HTMLInputElement).checked).toBe(true)
    fireEvent.click(getByRole('button', { name: 'Mon–Sat' }))
    fireEvent.click(getByLabelText('Sunday'))
    fireEvent.click(getByRole('button', { name: 'Create timetable' }))
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ id: 'new' })))
    expect(apiPost).toHaveBeenCalledWith('/api/timetables', {
      name: "Pascal's timetable", template: 'empty', days: [0, 1, 2, 3, 4, 5],
      week_start: 6, assignees: ['u-dad'],
    })
  })

  it('shows the Sunday hint when the week start differs from mine', () => {
    openCreateDialog()
    const { getByLabelText, queryByText } = render(<TimetableCreateDialog onCreated={vi.fn()} />)
    expect(queryByText('Weeks start on Sunday for this timetable')).toBeNull()
    fireEvent.click(getByLabelText('Sunday'))
    expect(queryByText('Weeks start on Sunday for this timetable')).not.toBeNull()
  })

  it('refuses an empty name inline', async () => {
    openCreateDialog()
    const { getByLabelText, getByRole, findByRole } = render(<TimetableCreateDialog onCreated={vi.fn()} />)
    fireEvent.input(nameField(getByLabelText), { target: { value: '   ' } })
    fireEvent.click(getByRole('button', { name: 'Create timetable' }))
    expect((await findByRole('alert')).textContent).toBe("Name can't be empty")
    expect(apiPost).not.toHaveBeenCalled()
  })
})
