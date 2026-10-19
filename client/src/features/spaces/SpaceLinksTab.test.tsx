import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, waitFor, cleanup, fireEvent } from '@testing-library/preact'

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

  it('reports every staged change to the page and calls nothing itself', async () => {
    const { api } = await import('@/api')
    ;(api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ links: [
      { id: 'a', label: 'School', url: 'https://school.example', position: 0 },
      { id: 'b', label: 'Wiki', url: 'https://wiki.example', position: 1 },
    ] })
    const onDraft = vi.fn()
    const view = render(<SpaceLinksTab spaceId="s-1" onDraft={onDraft} />)
    fireEvent.click(await view.findByRole('button', { name: 'Move Wiki up' }))
    await waitFor(() => expect(onDraft).toHaveBeenLastCalledWith(expect.objectContaining({ count: 1, invalid: 0 })))
    const labels = [...view.container.querySelectorAll('.sh-space-links-editor__label')]
      .map(i => (i as HTMLInputElement).value)
    expect(labels).toEqual(['Wiki', 'School'])
    // An emptied name is flagged on the row and blocks Save.
    fireEvent.input(view.getAllByLabelText('Link name')[0], { target: { value: '' } })
    expect(await view.findByText('Enter a name and an address.')).toBeTruthy()
    await waitFor(() => expect(onDraft).toHaveBeenLastCalledWith(expect.objectContaining({ invalid: 1 })))
    expect(api.patch).not.toHaveBeenCalled()
    expect(api.post).not.toHaveBeenCalled()
    expect(api.delete).not.toHaveBeenCalled()
  })

  it('the add form refuses an address that is not a full web address', async () => {
    const view = render(<SpaceLinksTab spaceId="s-1" />)
    await view.findByText('No links yet. Add one below.')
    const form = view.container.querySelector('form')!
    const [label, url] = [...form.querySelectorAll('input')]
    fireEvent.input(label, { target: { value: 'Wiki' } })
    fireEvent.input(url, { target: { value: 'javascript:alert(1)' } })
    fireEvent.submit(form)
    expect((await view.findByRole('alert')).textContent).toBe('Use a full web address, starting with https://')
    expect(view.queryByText('No links yet. Add one below.')).toBeTruthy()
  })

  it('a failed load offers Try again instead of an empty list', async () => {
    const { api } = await import('@/api')
    ;(api.get as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('offline'))
    const view = render(<SpaceLinksTab spaceId="s-1" />)
    expect((await view.findByRole('alert')).textContent).toContain("Couldn't load the links: offline")
    expect(view.queryByText('No links yet. Add one below.')).toBeNull()
    fireEvent.click(view.getByRole('button', { name: 'Try again' }))
    await view.findByText('No links yet. Add one below.')
  })

  it('speaks German when the UI language is German', async () => {
    await setLocale('de')
    const view = render(<SpaceLinksTab spaceId="s-1" />)
    await waitFor(() => expect(view.getByText('Noch keine Links. Füge unten einen hinzu.')).toBeTruthy())
    expect(view.getByRole('heading', { name: 'Schnelllinks' })).toBeTruthy()
    expect(view.getByRole('button', { name: 'Link hinzufügen' })).toBeTruthy()
  })
})
