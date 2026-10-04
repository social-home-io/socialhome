import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, waitFor, cleanup } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn().mockResolvedValue({ links: [] }), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { setLocale } from '@/i18n/i18n'
import { SpaceLinksTab } from './SpaceLinksTab'

describe('SpaceLinksTab', () => {
  afterEach(async () => {
    cleanup()
    await setLocale('en')
  })

  it('module exports exist', async () => {
    const mod = await import('./SpaceLinksTab')
    expect(mod).toBeTruthy()
    expect(typeof mod.SpaceLinksTab).toBe('function')
  })

  it('shows the heading, empty state and add form in English', async () => {
    const view = render(<SpaceLinksTab spaceId="s-1" />)
    await waitFor(() => expect(view.getByText('No links yet. Add one below.')).toBeTruthy())
    expect(view.getByRole('heading', { name: 'Quick links' })).toBeTruthy()
    expect(view.getByPlaceholderText('Name (for example Family wiki)')).toBeTruthy()
    expect(view.getByRole('button', { name: 'Add link' })).toBeTruthy()
  })

  it('speaks German when the UI language is German', async () => {
    await setLocale('de')
    const view = render(<SpaceLinksTab spaceId="s-1" />)
    await waitFor(() => expect(view.getByText('Noch keine Links. Füge unten einen hinzu.')).toBeTruthy())
    expect(view.getByRole('heading', { name: 'Schnelllinks' })).toBeTruthy()
    expect(view.getByRole('button', { name: 'Link hinzufügen' })).toBeTruthy()
  })
})
