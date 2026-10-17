import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, act, cleanup, type RenderResult } from '@testing-library/preact'
import { signal } from '@preact/signals'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { User } from '@/types'

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const handlers = new Map<string, Set<Handler>>()
const emit = (type: string, data: Record<string, unknown>) =>
  handlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))

const baseUser: User = {
  user_id: 'u1', username: 'anna', display_name: 'Anna', is_admin: false,
  picture_url: null, picture_hash: null, bio: null, is_new_member: false,
  handle: 'anna_h',
  status: { emoji: null, text: null, expires_at: null },
}

const FUTURE = '2099-01-01T15:30:00+00:00'
const PAST = '2000-01-01T00:00:00+00:00'

describe('MeButton', () => {
  const currentUser = signal<User | null>(null)
  const toast = vi.fn()
  const route = vi.fn()

  beforeEach(() => {
    vi.resetModules()
    handlers.clear()
    toast.mockReset()
    route.mockReset()
    currentUser.value = { ...baseUser }
    vi.doMock('@/store/auth', () => ({ currentUser }))
    vi.doMock('./Toast', () => ({ showToast: toast }))
    vi.doMock('preact-iso', () => ({ useLocation: () => ({ route, url: '/', path: '/' }) }))
    vi.doMock('@/ws', () => ({
      ws: {
        on: (type: string, h: Handler) => {
          if (!handlers.has(type)) handlers.set(type, new Set())
          handlers.get(type)!.add(h)
          return () => { handlers.get(type)?.delete(h) }
        },
      },
    }))
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  async function mount(patch: ReturnType<typeof vi.fn> = vi.fn()) {
    vi.doMock('@/api', async () => {
      const real = await vi.importActual<typeof import('@/api')>('@/api')
      return { ApiError: real.ApiError, api: { patch } }
    })
    const { MeButton } = await import('./MeButton')
    const se = await import('./StatusEditor')
    const view = render(<MeButton />)
    return { view, patch, statusEditorOpen: se.statusEditorOpen }
  }

  const trigger = (view: RenderResult) =>
    view.container.querySelector<HTMLButtonElement>('button.sh-me-btn')!
  const badge = (view: RenderResult) =>
    view.container.querySelector('[data-testid="me-status-badge"]')
  const itemNames = (view: RenderResult) =>
    view.getAllByRole('menuitem').map(el => el.textContent)

  it('renders nothing until currentUser is loaded', async () => {
    currentUser.value = null
    const { view } = await mount()
    expect(view.container.innerHTML).toBe('')
  })

  it('no status: no badge, aria-label names only you', async () => {
    const { view } = await mount()
    const btn = trigger(view)
    expect(btn.getAttribute('aria-label')).toBe('You: Anna')
    expect(btn.getAttribute('aria-haspopup')).toBe('menu')
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    expect(badge(view)).toBeNull()
  })

  it('a live status shows the emoji badge (aria-hidden) and is in the aria-label', async () => {
    currentUser.value = { ...baseUser, status: { emoji: '🌴', text: 'On leave', expires_at: FUTURE } }
    const { view } = await mount()
    expect(trigger(view).getAttribute('aria-label')).toBe('You: Anna. Status: 🌴 On leave')
    expect(badge(view)?.textContent).toBe('🌴')
    expect(badge(view)?.getAttribute('aria-hidden')).toBe('true')
  })

  it('an expired status shows no badge', async () => {
    currentUser.value = { ...baseUser, status: { emoji: '🌴', text: 'On leave', expires_at: PAST } }
    const { view } = await mount()
    expect(badge(view)).toBeNull()
    expect(trigger(view).getAttribute('aria-label')).toBe('You: Anna')
  })

  it('a text-only status gets a dot badge', async () => {
    currentUser.value = { ...baseUser, status: { emoji: null, text: 'Busy', expires_at: null } }
    const { view } = await mount()
    expect(badge(view)?.classList.contains('sh-me-btn__badge--dot')).toBe(true)
    expect(trigger(view).getAttribute('aria-label')).toBe('You: Anna. Status: Busy')
  })

  it('the badge leaves on time when the deadline passes', async () => {
    vi.useFakeTimers()
    const soon = new Date(Date.now() + 60_000).toISOString()
    currentUser.value = { ...baseUser, status: { emoji: '🎧', text: 'Focus', expires_at: soon } }
    const { view } = await mount()
    expect(badge(view)).not.toBeNull()
    await act(async () => { vi.advanceTimersByTime(61_000) })
    expect(badge(view)).toBeNull()
  })

  it('menu without a status: header says "No status set"; Set a status… and Profile & settings', async () => {
    const { view } = await mount()
    fireEvent.click(trigger(view))
    expect(trigger(view).getAttribute('aria-expanded')).toBe('true')
    const menu = view.getByRole('menu')
    const header = menu.querySelector('.sh-post-menu__header')!
    expect(header.getAttribute('role')).toBe('none')
    expect(header.textContent).toContain('Anna')
    expect(header.textContent).toContain('@anna_h')
    expect(header.textContent).toContain('No status set')
    expect(itemNames(view)).toEqual(['Set a status…', 'Profile & settings'])
    // The header is not a stop for the arrow keys: focus opens on the first item.
    expect(document.activeElement?.textContent).toBe('Set a status…')
  })

  it('menu with a status: header shows it and when it clears; Edit, Clear and settings', async () => {
    currentUser.value = { ...baseUser, status: { emoji: '🌴', text: 'On leave', expires_at: FUTURE } }
    const { view } = await mount()
    fireEvent.click(trigger(view))
    const header = view.getByRole('menu').querySelector('.sh-post-menu__header')!
    expect(header.textContent).toContain('🌴 On leave')
    expect(header.textContent).toMatch(/Until /)
    expect(itemNames(view)).toEqual(['Edit status…', 'Clear status', 'Profile & settings'])
  })

  it('Set a status… opens the status dialog', async () => {
    const { view, statusEditorOpen } = await mount()
    statusEditorOpen.value = false
    fireEvent.click(trigger(view))
    fireEvent.click(view.getByRole('menuitem', { name: 'Set a status…' }))
    expect(statusEditorOpen.value).toBe(true)
    expect(view.queryByRole('menu')).toBeNull()
    statusEditorOpen.value = false
  })

  it('Clear status sends one PATCH with nulls, updates currentUser and toasts', async () => {
    currentUser.value = { ...baseUser, status: { emoji: '🌴', text: 'On leave', expires_at: null } }
    const patch = vi.fn(async () => ({ ...baseUser }))
    const { view } = await mount(patch)
    fireEvent.click(trigger(view))
    fireEvent.click(view.getByRole('menuitem', { name: 'Clear status' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/me', {
      status_emoji: null, status_text: null,
    }))
    await waitFor(() => expect(badge(view)).toBeNull())
    expect(currentUser.value?.status?.emoji).toBeNull()
    expect(toast).toHaveBeenCalledWith('Status cleared', 'info')
  })

  it('Profile & settings routes to /settings (the router adds the ingress prefix)', async () => {
    const { view } = await mount()
    fireEvent.click(trigger(view))
    fireEvent.click(view.getByRole('menuitem', { name: 'Profile & settings' }))
    expect(route).toHaveBeenCalledWith('/settings')
  })

  it('Escape closes the menu and returns focus to the trigger', async () => {
    const { view } = await mount()
    fireEvent.click(trigger(view))
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(view.queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(trigger(view))
  })

  it('our own user.status_changed frame updates currentUser and the badge', async () => {
    const { view } = await mount()
    act(() => {
      emit('user.status_changed', { user_id: 'u1', status: { emoji: '🍽️', text: 'Lunch', expires_at: null } })
    })
    expect(currentUser.value?.status?.text).toBe('Lunch')
    await waitFor(() => expect(badge(view)?.textContent).toBe('🍽️'))
    act(() => { emit('user.status_changed', { user_id: 'u1', status: null }) })
    await waitFor(() => expect(badge(view)).toBeNull())
    expect(currentUser.value?.status).toEqual({ emoji: null, text: null, expires_at: null })
  })

  it("someone else's frame leaves currentUser alone", async () => {
    await mount()
    act(() => {
      emit('user.status_changed', { user_id: 'u2', status: { emoji: '🎧', text: 'Focus', expires_at: null } })
    })
    expect(currentUser.value?.status?.text).toBeNull()
  })

  it('renders in haos mode too (no token, ingress auth)', async () => {
    const { instanceConfig } = await import('@/store/instance')
    instanceConfig.value = {
      mode: 'haos', instance_name: 'Hearth', instance_id: null,
      capabilities: ['ingress', 'ha_person_directory'], setup_required: false,
    }
    const { view } = await mount()
    expect(trigger(view).getAttribute('aria-label')).toBe('You: Anna')
    instanceConfig.value = null
  })

  it('the App top bar mounts it right after the notification bell, and the status dialog once', () => {
    const app = readFileSync(join(__dirname, '..', 'App.tsx'), 'utf8')
    expect(app).toMatch(/<NotificationBell \/>\s*<MeButton \/>/)
    expect(app.match(/<StatusEditorHost \/>/g)?.length).toBe(1)
  })
})
