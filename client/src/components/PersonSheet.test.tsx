import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act, cleanup, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})

const routeSpy = vi.fn()
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: routeSpy }),
}))

const toastSpy = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: toastSpy }))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-me', username: 'pascal', display_name: 'Pascal' } },
}))

const aliasSpy = vi.fn()
vi.mock('@/components/AliasDialog', () => ({
  openAliasDialog: aliasSpy,
  AliasDialog: () => null,
}))

const reportSpy = vi.fn()
vi.mock('@/components/ReportDialog', () => ({ openReport: reportSpy }))

const callPickerSpy = vi.fn()
vi.mock('@/components/CallTypePickerDialog', () => ({
  openCallTypePicker: callPickerSpy,
}))

const { api } = await import('@/api')
const { PersonSheet, openPerson, closePerson } = await import('./PersonSheet')
const blocks = await import('@/store/blocks')
const follows = await import('@/store/follows')

const apiMock = api as unknown as Record<'get' | 'post' | 'delete', ReturnType<typeof vi.fn>>

const OKAFOR = { instance_id: 'p-okafor', display_name: 'Okafor family', is_own: false }
const VIZELI = { instance_id: 'us', display_name: 'Vizeli', is_own: true }

function chidi(over: Record<string, unknown> = {}) {
  return {
    display_name: 'Chidi Okafor',
    personal_alias: 'Chidi (football coach)',
    picture_url: null,
    is_local: false,
    is_online: true,
    is_idle: false,
    status: { emoji: '🌞', text: 'Holiday in Ticino', expires_at: null },
    bio: 'Loves hiking and board games.',
    household: OKAFOR,
    ...over,
  }
}

function oma(over: Record<string, unknown> = {}) {
  return {
    display_name: 'Grandma Ruth',
    personal_alias: null,
    username: 'ruth',
    picture_url: null,
    is_local: true,
    is_online: false,
    status: null,
    bio: null,
    household: VIZELI,
    ...over,
  }
}

/** Default GETs: empty block + follow lists. */
function apiDefaults(extra?: (path: string) => unknown) {
  apiMock.get.mockImplementation(async (path: string) => {
    if (path === '/api/blocks') return { blocks: [] }
    if (path === '/api/moments/follows') return { follows: [] }
    const r = extra?.(path)
    if (r instanceof Error) throw r
    if (r !== undefined) return r
    throw new Error('404')
  })
}

const q = (el: Element | Document, id: string) =>
  el.querySelector(`[data-testid="${id}"]`) as HTMLElement | null

beforeEach(() => {
  for (const f of Object.values(apiMock)) f.mockReset()
  routeSpy.mockReset()
  toastSpy.mockReset()
  aliasSpy.mockReset()
  reportSpy.mockReset()
  callPickerSpy.mockReset()
  blocks._resetBlocksForTest()
  follows._resetFollowsForTest()
  apiDefaults()
})

afterEach(() => {
  act(() => { closePerson() })
  cleanup()
})

describe('PersonSheet', () => {
  it('renders nothing until openPerson is called', () => {
    render(<PersonSheet />)
    expect(q(document, 'person-sheet')).toBeNull()
  })

  it('remote person: alias heading, real name, status pill, bio, Message + Follow, Report + Block', async () => {
    render(<PersonSheet />)
    await act(async () => { openPerson('ru-chidi', { person: chidi() }) })
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement
    expect(dialog).not.toBeNull()
    const heading = dialog.querySelector('h2') as HTMLElement
    expect(heading.textContent).toBe('Chidi (football coach)')
    // The dialog is named by the heading — the name shows once.
    expect(dialog.getAttribute('aria-labelledby')).toBe(heading.id)
    expect(q(dialog, 'person-real-name')?.textContent)
      .toBe('Their name: Chidi Okafor · nickname only you see')
    expect(q(dialog, 'person-status')?.textContent).toContain('Holiday in Ticino')
    expect(dialog.textContent).toContain('Loves hiking and board games.')
    expect(q(dialog, 'person-household')?.textContent).toContain('Okafor family')
    expect(q(dialog, 'person-message')).not.toBeNull()
    expect(q(dialog, 'person-call')).toBeNull()
    expect(q(dialog, 'person-follow')?.getAttribute('aria-pressed')).toBe('false')
    expect(q(dialog, 'person-nickname')?.textContent).toContain('Edit nickname')
    expect(q(dialog, 'person-report')).not.toBeNull()
    expect(q(dialog, 'person-block')).not.toBeNull()
    // Remote presence is shown when known.
    expect(dialog.querySelector('.sh-avatar-status-dot--online')).not.toBeNull()
    // A complete hint never fetches the person.
    expect(apiMock.get).not.toHaveBeenCalledWith('/api/friends/ru-chidi')
  })

  it('own household: Message + Call, follow as a row, no Report, household marked as yours', async () => {
    render(<PersonSheet />)
    await act(async () => { openPerson('u-oma', { person: oma() }) })
    expect(q(document, 'person-message')).not.toBeNull()
    expect(q(document, 'person-call')).not.toBeNull()
    expect(q(document, 'person-follow')?.classList.contains('sh-person-sheet__row--btn')).toBe(true)
    expect(q(document, 'person-report')).toBeNull()
    expect(q(document, 'person-block')).not.toBeNull()
    expect(q(document, 'person-household')?.textContent).toBe('HouseholdVizeli · your household')
    expect(q(document, 'person-real-name')).toBeNull()
    expect(q(document, 'person-nickname')?.textContent).toContain('Set nickname')
  })

  it('emoji-only status becomes an avatar badge, not a pill', async () => {
    render(<PersonSheet />)
    await act(async () => {
      openPerson('u-oma', {
        person: oma({ status: { emoji: '🍝', text: null, expires_at: null } }),
      })
    })
    const badge = q(document, 'person-status-badge')
    expect(badge?.textContent).toBe('🍝')
    expect(badge?.getAttribute('aria-label')).toBe('Status: 🍝')
    expect(q(document, 'person-status')).toBeNull()
  })

  it('status with an expiry today reads "until HH:MM"', async () => {
    const later = new Date()
    later.setHours(23, 59, 0, 0)
    render(<PersonSheet />)
    await act(async () => {
      openPerson('u-oma', {
        person: oma({ status: { emoji: '☕', text: 'Coffee at 3', expires_at: later.toISOString() } }),
      })
    })
    expect(q(document, 'person-status')?.textContent).toMatch(/until 23:59|until 11:59/)
  })

  it('fetches GET /api/friends/{id} when only a name is known, then fills in', async () => {
    apiDefaults(path => (path === '/api/friends/ru-chidi' ? { user_id: 'ru-chidi', ...chidi() } : undefined))
    render(<PersonSheet />)
    await act(async () => {
      openPerson('ru-chidi', { person: { display_name: 'Chidi Okafor' } })
    })
    await waitFor(() => {
      expect(q(document, 'person-message')).not.toBeNull()
    })
    expect(apiMock.get).toHaveBeenCalledWith('/api/friends/ru-chidi')
    expect(document.querySelector('h2')?.textContent).toBe('Chidi (football coach)')
    expect(q(document, 'person-household')?.textContent).toContain('Okafor family')
  })

  it('ignores a hint name that is just the raw id', async () => {
    let resolve: (v: unknown) => void = () => {}
    apiMock.get.mockImplementation((path: string) => {
      if (path === '/api/friends/ru-x') return new Promise(r => { resolve = r })
      return Promise.resolve(path === '/api/blocks' ? { blocks: [] } : { follows: [] })
    })
    render(<PersonSheet />)
    await act(async () => { openPerson('ru-x', { person: { display_name: 'ru-x' } }) })
    expect(document.querySelector('h2')?.textContent).toBe('Someone')
    // Actions wait for the fetch instead of guessing local vs remote.
    expect(q(document, 'person-message')).toBeNull()
    expect(q(document, 'person-block')).toBeNull()
    expect(q(document, 'person-sheet')?.getAttribute('aria-busy')).toBe('true')
    await act(async () => { resolve({ user_id: 'ru-x', ...chidi({ personal_alias: null }) }) })
    expect(document.querySelector('h2')?.textContent).toBe('Chidi Okafor')
  })

  it('unknown / not-connected person (404) gets a minimal sheet from the hint', async () => {
    render(<PersonSheet />)
    await act(async () => {
      openPerson('ru-space', { person: { display_name: 'Spacey' } })
    })
    await waitFor(() => { expect(q(document, 'person-limited')).not.toBeNull() })
    expect(document.querySelector('h2')?.textContent).toBe('Spacey')
    expect(q(document, 'person-message')).toBeNull()
    expect(q(document, 'person-household')).toBeNull()
    expect(q(document, 'person-follow')).not.toBeNull()
    expect(q(document, 'person-report')).not.toBeNull()
    expect(q(document, 'person-block')).not.toBeNull()
  })

  it('a 404 never offers Message, even when the opener hinted how to address them', async () => {
    // A space roster / chat hint may carry is_local + username (or a
    // remote id); once the household says "not someone you're connected
    // to" (guardian-separated, space-only), Message would only fail.
    render(<PersonSheet />)
    await act(async () => {
      openPerson('u-kid', { person: { display_name: 'Kid', is_local: true, username: 'kid' } })
    })
    await waitFor(() => { expect(q(document, 'person-limited')).not.toBeNull() })
    expect(q(document, 'person-message')).toBeNull()
    expect(q(document, 'person-call')).toBeNull()
    await act(async () => {
      openPerson('ru-x', { person: { display_name: 'Remote', is_local: false } })
    })
    await waitFor(() => { expect(q(document, 'person-limited')).not.toBeNull() })
    expect(q(document, 'person-message')).toBeNull()
    // A space roster hint carries the household (never is_local) — it
    // still shows after the 404: useful context for a stranger.
    await act(async () => {
      openPerson('ru-y', {
        person: {
          display_name: 'Spacey',
          household: { instance_id: 'p-x', display_name: 'Far away', is_own: false },
        },
      })
    })
    await waitFor(() => { expect(q(document, 'person-limited')).not.toBeNull() })
    expect(q(document, 'person-message')).toBeNull()
    expect(q(document, 'person-household')?.textContent).toContain('Far away')
    expect(q(document, 'person-report')).not.toBeNull()
  })

  it('a stale fetch never paints over the person opened after it', async () => {
    let resolveFirst: (v: unknown) => void = () => {}
    apiMock.get.mockImplementation((path: string) => {
      if (path === '/api/friends/ru-a') return new Promise(r => { resolveFirst = r })
      if (path === '/api/blocks') return Promise.resolve({ blocks: [] })
      return Promise.resolve({ follows: [] })
    })
    render(<PersonSheet />)
    await act(async () => { openPerson('ru-a') })
    await act(async () => { openPerson('u-oma', { person: oma() }) })
    await act(async () => { resolveFirst({ user_id: 'ru-a', ...chidi() }) })
    expect(document.querySelector('h2')?.textContent).toBe('Grandma Ruth')
  })

  it('blocked person: note + Unblock only, name from the block list', async () => {
    apiMock.get.mockImplementation(async (path: string) => {
      if (path === '/api/blocks') {
        return { blocks: [{ user_id: 'ru-bad', display_name: 'Bad Actor', blocked_at: '2026-01-01' }] }
      }
      if (path === '/api/moments/follows') return { follows: [] }
      throw new Error('404')
    })
    await blocks.loadBlocks(true)
    render(<PersonSheet />)
    await act(async () => { openPerson('ru-bad') })
    await waitFor(() => { expect(q(document, 'person-unblock')).not.toBeNull() })
    expect(document.querySelector('h2')?.textContent).toBe('Bad Actor')
    expect(q(document, 'person-limited')?.textContent).toContain('You blocked Bad Actor')
    for (const id of ['person-message', 'person-follow', 'person-nickname', 'person-block', 'person-report']) {
      expect(q(document, id)).toBeNull()
    }
    apiMock.delete.mockResolvedValueOnce({})
    await act(async () => { fireEvent.click(q(document, 'person-unblock')!) })
    expect(apiMock.delete).toHaveBeenCalledWith('/api/blocks/ru-bad')
    expect(blocks.isBlocked('ru-bad')).toBe(false)
    await waitFor(() => { expect(q(document, 'person-sheet')).toBeNull() })
  })

  it('Message on a remote person POSTs {user_id} and routes into the thread', async () => {
    apiMock.post.mockResolvedValueOnce({ id: 'cdm-cross' })
    render(<PersonSheet />)
    await act(async () => { openPerson('ru-chidi', { person: chidi() }) })
    await act(async () => { fireEvent.click(q(document, 'person-message')!) })
    await waitFor(() => { expect(routeSpy).toHaveBeenCalledWith('/dms/cdm-cross') })
    expect(apiMock.post).toHaveBeenCalledWith('/api/conversations/dm', { user_id: 'ru-chidi' })
    expect(q(document, 'person-sheet')).toBeNull()
  })

  it('Message on a household member POSTs {username}', async () => {
    apiMock.post.mockResolvedValueOnce({ id: 'cdm-local' })
    render(<PersonSheet />)
    await act(async () => { openPerson('u-oma', { person: oma() }) })
    await act(async () => { fireEvent.click(q(document, 'person-message')!) })
    await waitFor(() => { expect(routeSpy).toHaveBeenCalledWith('/dms/cdm-local') })
    expect(apiMock.post).toHaveBeenCalledWith('/api/conversations/dm', { username: 'ruth' })
  })

  it('a failed DM start keeps the sheet open and toasts', async () => {
    apiMock.post.mockRejectedValueOnce(new Error('nope'))
    render(<PersonSheet />)
    await act(async () => { openPerson('ru-chidi', { person: chidi() }) })
    await act(async () => { fireEvent.click(q(document, 'person-message')!) })
    await waitFor(() => { expect(toastSpy).toHaveBeenCalledWith('nope', 'error') })
    expect(q(document, 'person-sheet')).not.toBeNull()
    expect(routeSpy).not.toHaveBeenCalled()
  })

  it('Call opens the DM then the audio/video picker', async () => {
    apiMock.post.mockResolvedValueOnce({ id: 'cdm-local' })
    render(<PersonSheet />)
    await act(async () => { openPerson('u-oma', { person: oma() }) })
    await act(async () => { fireEvent.click(q(document, 'person-call')!) })
    await waitFor(() => { expect(callPickerSpy).toHaveBeenCalledWith('cdm-local') })
    expect(q(document, 'person-sheet')).toBeNull()
  })

  it('Follow toggles in place and flips aria-pressed', async () => {
    apiMock.post.mockResolvedValueOnce({})
    render(<PersonSheet />)
    await act(async () => { openPerson('ru-chidi', { person: chidi() }) })
    await act(async () => { fireEvent.click(q(document, 'person-follow')!) })
    expect(apiMock.post).toHaveBeenCalledWith('/api/moments/follows', { user_id: 'ru-chidi' })
    await waitFor(() => {
      expect(q(document, 'person-follow')?.getAttribute('aria-pressed')).toBe('true')
    })
    expect(q(document, 'person-follow')?.textContent).toContain('Following')
    apiMock.delete.mockResolvedValueOnce({})
    await act(async () => { fireEvent.click(q(document, 'person-follow')!) })
    expect(apiMock.delete).toHaveBeenCalledWith('/api/moments/follows/ru-chidi')
    await waitFor(() => {
      expect(q(document, 'person-follow')?.getAttribute('aria-pressed')).toBe('false')
    })
  })

  it('Edit nickname closes the sheet and opens the alias dialog; saving notifies the opener', async () => {
    const onAliasChanged = vi.fn()
    render(<PersonSheet />)
    await act(async () => { openPerson('ru-chidi', { person: chidi(), onAliasChanged }) })
    await act(async () => { fireEvent.click(q(document, 'person-nickname')!) })
    expect(aliasSpy).toHaveBeenCalledWith(expect.objectContaining({
      targetUserId: 'ru-chidi',
      globalDisplayName: 'Chidi Okafor',
      currentAlias: 'Chidi (football coach)',
    }))
    expect(q(document, 'person-sheet')).toBeNull()
    aliasSpy.mock.calls[0][0].onSave('Coach')
    expect(onAliasChanged).toHaveBeenCalledWith('ru-chidi', 'Coach')
  })

  it('Report closes the sheet and opens the user report flow', async () => {
    render(<PersonSheet />)
    await act(async () => { openPerson('ru-chidi', { person: chidi() }) })
    await act(async () => { fireEvent.click(q(document, 'person-report')!) })
    expect(reportSpy).toHaveBeenCalledWith('user', 'ru-chidi')
    expect(q(document, 'person-sheet')).toBeNull()
  })

  it('Block asks for confirmation first, then blocks and closes', async () => {
    render(<PersonSheet />)
    await act(async () => { openPerson('ru-chidi', { person: chidi() }) })
    await act(async () => { fireEvent.click(q(document, 'person-block')!) })
    expect(apiMock.post).not.toHaveBeenCalled()
    const dialogs = document.querySelectorAll('[role="dialog"]')
    expect(dialogs.length).toBe(2)
    const confirm = dialogs[dialogs.length - 1] as HTMLElement
    expect(confirm?.textContent).toContain('Block Chidi (football coach)?')
    apiMock.post.mockResolvedValueOnce({})
    const confirmBtn = Array.from(confirm.querySelectorAll('button'))
      .find(b => b.textContent === 'Block') as HTMLButtonElement
    await act(async () => { fireEvent.click(confirmBtn) })
    expect(apiMock.post).toHaveBeenCalledWith('/api/blocks', { user_id: 'ru-chidi' })
    expect(blocks.isBlocked('ru-chidi')).toBe(true)
    await waitFor(() => { expect(q(document, 'person-sheet')).toBeNull() })
  })

  it('yourself: no person actions, an Edit your profile button instead', async () => {
    render(<PersonSheet />)
    await act(async () => {
      openPerson('u-me', { person: oma({ display_name: 'Pascal', username: 'pascal' }) })
    })
    expect(q(document, 'person-message')).toBeNull()
    expect(q(document, 'person-block')).toBeNull()
    expect(q(document, 'person-nickname')).toBeNull()
    await act(async () => { fireEvent.click(q(document, 'person-edit-profile')!) })
    expect(routeSpy).toHaveBeenCalledWith('/settings')
  })

  it('a11y: Escape closes and focus returns to the opener; Tab stays inside', async () => {
    const opener = document.createElement('button')
    opener.textContent = 'open'
    document.body.appendChild(opener)
    opener.focus()
    render(<PersonSheet />)
    await act(async () => { openPerson('ru-chidi', { person: chidi() }) })
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.contains(document.activeElement)).toBe(true)
    // Shift+Tab from the first focusable wraps to the last (focus trap).
    const focusables = dialog.querySelectorAll<HTMLElement>('button:not([disabled])')
    focusables[0].focus()
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(focusables[focusables.length - 1])
    await act(async () => { fireEvent.keyDown(document, { key: 'Escape' }) })
    expect(q(document, 'person-sheet')).toBeNull()
    expect(document.activeElement).toBe(opener)
    opener.remove()
  })
})
