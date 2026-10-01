import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor, fireEvent, screen } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m, _mock: m }
})

import {
  StickyDialog,
  STICKY_COLORS,
  openCreateStickyDialog,
  openEditStickyDialog,
} from './StickyDialog'
import { api } from '@/api'
import { householdStickyStore, spaceStickyStore, resetStickies } from '@/store/stickies'
import { toasts } from './Toast'
import { resetPendingDeletes, pendingDeletes } from '@/utils/undoableDelete'
import { confirmDialog } from './confirm'

vi.mock('./confirm', () => ({ confirmDialog: vi.fn().mockResolvedValue(true) }))

const apiMock = api as unknown as {
  get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn>;
  patch: ReturnType<typeof vi.fn>; delete: ReturnType<typeof vi.fn>;
}

function fakeSticky(over: Partial<{ id: string; content: string; color: string; space_id: string | null }> = {}) {
  return {
    id: over.id ?? 's-1',
    author: 'a-id',
    content: over.content ?? 'Buy milk',
    color: over.color ?? STICKY_COLORS[0].hex,
    position_x: 100,
    position_y: 100,
    created_at: '2026-05-02T12:00:00Z',
    updated_at: '2026-05-02T12:00:00Z',
    space_id: over.space_id ?? null,
  }
}

async function seed(rows = [fakeSticky()]) {
  apiMock.get.mockResolvedValueOnce(rows)
  await householdStickyStore.load({ force: true })
}

describe('StickyDialog', () => {
  beforeEach(() => {
    apiMock.get.mockReset()
    apiMock.post.mockReset()
    apiMock.patch.mockReset()
    apiMock.delete.mockReset()
    resetStickies()
    resetPendingDeletes()
    toasts.value = []
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
  })

  it('renders nothing when closed', () => {
    const { container } = render(<StickyDialog />)
    expect(container.querySelector('.sh-sticky-dialog')).toBeNull()
  })

  it('colour choices are a radiogroup of named colours; the seeded one is checked', async () => {
    render(<StickyDialog />)
    openCreateStickyDialog(null, { x: 100, y: 100 }, STICKY_COLORS[2].hex)
    const group = await screen.findByRole('radiogroup', { name: 'Colour' })
    const radios = group.querySelectorAll('[role="radio"]')
    expect(radios).toHaveLength(STICKY_COLORS.length)
    expect([...radios].map(r => r.textContent)).toEqual(
      ['Yellow', 'Coral', 'Mint', 'Sky', 'Lilac', 'Peach'])
    expect(screen.getByRole('radio', { name: 'Mint' }).getAttribute('aria-checked')).toBe('true')
  })

  it('a colour outside the palette shows as "Custom" so the group keeps a checked option', async () => {
    render(<StickyDialog />)
    openEditStickyDialog(fakeSticky({ color: '#123456' }), null)
    const custom = await screen.findByRole('radio', { name: 'Custom' })
    expect(custom.getAttribute('aria-checked')).toBe('true')
  })

  it('after picking a palette colour the original custom colour is still offered', async () => {
    await seed([fakeSticky({ color: '#123456' })])
    apiMock.patch.mockResolvedValueOnce(fakeSticky({ color: '#123456' }))
    const { container } = render(<StickyDialog />)
    openEditStickyDialog(fakeSticky({ color: '#123456' }), null)
    fireEvent.click(await screen.findByRole('radio', { name: 'Yellow' }))
    const custom = screen.getByRole('radio', { name: 'Custom' })
    expect(custom.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(custom)
    expect(screen.getByRole('radio', { name: 'Custom' }).getAttribute('aria-checked')).toBe('true')
    fireEvent.submit(container.querySelector('form')!)
    await waitFor(() => expect(apiMock.patch).toHaveBeenCalled())
    expect(apiMock.patch.mock.calls[0][1].color).toBe('#123456')
  })

  it('the preview switches to the light ink on a dark colour', async () => {
    const { container } = render(<StickyDialog />)
    openEditStickyDialog(fakeSticky({ color: '#123456' }), null)
    await waitFor(() => expect(container.querySelector('textarea')).not.toBeNull())
    const ta = container.querySelector('textarea')!
    expect(ta.classList.contains('sh-ink-light')).toBe(true)
    fireEvent.click(screen.getByRole('radio', { name: 'Yellow' }))
    expect(container.querySelector('textarea')!.classList.contains('sh-ink-light')).toBe(false)
  })

  it('opens in edit mode pre-filled with sticky content', async () => {
    const { container } = render(<StickyDialog />)
    openEditStickyDialog(fakeSticky({ content: 'Pickup laundry' }), null)
    await waitFor(() => {
      expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull()
    })
    const ta = container.querySelector('textarea') as HTMLTextAreaElement
    expect(ta.value).toBe('Pickup laundry')
  })

  it('create submit POSTs to household endpoint and adds to the household store', async () => {
    await seed([])
    apiMock.post.mockResolvedValueOnce(fakeSticky({ id: 's-new', content: 'Hi' }))
    const { container } = render(<StickyDialog />)
    openCreateStickyDialog(null, { x: 200, y: 100 })
    await waitFor(() => expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull())
    fireEvent.input(container.querySelector('textarea')!, { target: { value: 'Hi' } })
    fireEvent.submit(container.querySelector('form')!)
    await waitFor(() => expect(apiMock.post).toHaveBeenCalled())
    expect(apiMock.post.mock.calls[0][0]).toBe('/api/stickies')
    await waitFor(() => expect(householdStickyStore.find('s-new')).toBeTruthy())
  })

  it('create submit POSTs to the space-scoped endpoint when spaceId is set', async () => {
    apiMock.post.mockResolvedValueOnce(fakeSticky({ id: 's-new', content: 'Hi', space_id: 'sp-42' }))
    const { container } = render(<StickyDialog />)
    openCreateStickyDialog('sp-42', { x: 0, y: 0 })
    await waitFor(() => expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull())
    fireEvent.input(container.querySelector('textarea')!, { target: { value: 'Space note' } })
    fireEvent.submit(container.querySelector('form')!)
    await waitFor(() => expect(apiMock.post).toHaveBeenCalled())
    expect(apiMock.post.mock.calls[0][0]).toBe('/api/spaces/sp-42/stickies')
    await waitFor(() => expect(spaceStickyStore('sp-42').find('s-new')).toBeTruthy())
    expect(householdStickyStore.find('s-new')).toBeUndefined()
  })

  it('edit submit PATCHes content + colour', async () => {
    await seed()
    apiMock.patch.mockResolvedValueOnce(
      fakeSticky({ id: 's-1', content: 'New text', color: STICKY_COLORS[3].hex }),
    )
    const { container } = render(<StickyDialog />)
    openEditStickyDialog(fakeSticky(), null)
    await waitFor(() => expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull())
    fireEvent.input(container.querySelector('textarea')!, { target: { value: 'New text' } })
    fireEvent.click(screen.getByRole('radio', { name: 'Sky' }))
    fireEvent.submit(container.querySelector('form')!)
    await waitFor(() => expect(apiMock.patch).toHaveBeenCalled())
    const [url, body] = apiMock.patch.mock.calls[0]
    expect(url).toBe('/api/stickies/s-1')
    expect(body).toEqual({ content: 'New text', color: STICKY_COLORS[3].hex })
  })

  it('a failed edit keeps the dialog open and rolls the note back', async () => {
    await seed()
    apiMock.patch.mockRejectedValueOnce(new Error('offline'))
    const { container } = render(<StickyDialog />)
    openEditStickyDialog(fakeSticky(), null)
    await waitFor(() => expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull())
    fireEvent.input(container.querySelector('textarea')!, { target: { value: 'Changed' } })
    fireEvent.submit(container.querySelector('form')!)
    await waitFor(() => expect(toasts.value.some(x => x.message.includes('offline'))).toBe(true))
    expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull()
    expect(householdStickyStore.find('s-1')?.content).toBe('Buy milk')
  })

  it('Delete closes the dialog at once and offers Undo — no confirm', async () => {
    await seed()
    const { container } = render(<StickyDialog />)
    openEditStickyDialog(fakeSticky(), null)
    await waitFor(() => expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull())
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(confirmDialog).not.toHaveBeenCalled()
    await waitFor(() => expect(container.querySelector('.sh-sticky-dialog')).toBeNull())
    expect(pendingDeletes.value.has('s-1')).toBe(true)
    const toast = toasts.value.find(x => x.action)
    expect(toast?.message).toBe('Deleted note: Buy milk')
    expect(toast?.action?.label).toBe('Undo')
    expect(apiMock.delete).not.toHaveBeenCalled()
    toast!.action!.onClick()
    expect(pendingDeletes.value.has('s-1')).toBe(false)
  })

  it('no Delete button when creating', async () => {
    const { container } = render(<StickyDialog />)
    openCreateStickyDialog(null)
    await waitFor(() => expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull())
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull()
  })
})
