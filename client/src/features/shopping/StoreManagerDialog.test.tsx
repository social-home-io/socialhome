import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiPut = vi.fn()
const apiDelete = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: (...args: unknown[]) => apiPatch(...args),
    put: (...args: unknown[]) => apiPut(...args),
    delete: (...args: unknown[]) => apiDelete(...args),
  },
}))

vi.mock('@/ws', () => ({
  ws: { on: vi.fn(() => () => {}) },
}))

vi.mock('@/components/confirm', () => ({
  confirmDialog: vi.fn().mockResolvedValue(true),
}))

vi.mock('@/components/Toast', () => ({
  showToast: vi.fn(),
}))

interface Fixture {
  items?: { id: string; text: string; store: string | null; completed: boolean }[]
  stores?: { name: string; sort_order: number }[]
}

/** Seed the shopping signals the dialog renders from. Returns the
 *  freshly-imported module registry entries so each test drives the
 *  same instances the component sees. */
async function setup(f: Fixture = {}) {
  const shopping = await import('@/store/shopping')
  shopping.items.value = (f.items ?? []) as never
  shopping.stores.value = (f.stores ?? []) as never
  const { StoreManagerDialog } = await import('./StoreManagerDialog')
  const { render, waitFor, fireEvent } = await import('@testing-library/preact')
  const { showToast } = await import('@/components/Toast')
  const { confirmDialog } = await import('@/components/confirm')
  const onClose = vi.fn()
  const utils = render(<StoreManagerDialog open={true} onClose={onClose} />)
  return { ...utils, waitFor, fireEvent, showToast, confirmDialog, onClose, shopping }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiPut.mockReset()
  apiDelete.mockReset()
  apiPost.mockResolvedValue({})
  apiPatch.mockResolvedValue({})
  apiPut.mockResolvedValue([])
  apiDelete.mockResolvedValue(undefined)
})

const TWO_STORES = [
  { name: 'Aldi', sort_order: 0 },
  { name: 'Migros', sort_order: 1 },
]

const THREE_ITEMS = [
  { id: 'i1', text: 'Eggs', store: 'Aldi', completed: false },
  { id: 'i2', text: 'Milk', store: 'Aldi', completed: false },
  { id: 'i3', text: 'Bread', store: 'Aldi', completed: true },
  { id: 'i4', text: 'Cheese', store: 'Migros', completed: false },
  { id: 'i5', text: 'Salt', store: null, completed: false },
]

describe('StoreManagerDialog', () => {
  it('renders nothing when closed', async () => {
    const shopping = await import('@/store/shopping')
    shopping.stores.value = TWO_STORES
    const { StoreManagerDialog } = await import('./StoreManagerDialog')
    const { render } = await import('@testing-library/preact')
    const { container } = render(
      <StoreManagerDialog open={false} onClose={() => {}} />,
    )
    expect(container.querySelector('.sh-store-manager')).toBeNull()
  })

  it('lists the catalogue in trip order with per-store item counts', async () => {
    const { container } = await setup({
      stores: [
        { name: 'Migros', sort_order: 1 },
        { name: 'Aldi', sort_order: 0 },
      ],
      items: THREE_ITEMS,
    })
    const rows = Array.from(container.querySelectorAll('.sh-store-manager__row'))
    expect(rows.length).toBe(2)
    // Trip order (sort_order), not the order the array arrived in.
    expect(rows[0].textContent).toContain('Aldi')
    expect(rows[1].textContent).toContain('Migros')
    // Counts include completed items — the catalogue is about the
    // store, not about what's still to buy.
    expect(rows[0].querySelector('.sh-store-manager__count')!.textContent)
      .toContain('3')
    expect(rows[1].querySelector('.sh-store-manager__count')!.textContent)
      .toContain('1')
  })

  it('shows an empty state with an add affordance when there are no stores', async () => {
    const { container, getByRole } = await setup({ stores: [], items: [] })
    expect(container.querySelector('.sh-store-manager__empty')).not.toBeNull()
    expect(getByRole('button', { name: /add store/i })).toBeTruthy()
  })

  it('▼ moves a store down and PUTs the full ordered list', async () => {
    const { getByLabelText, waitFor } = await setup({
      stores: [
        { name: 'Aldi', sort_order: 0 },
        { name: 'Migros', sort_order: 1 },
        { name: 'Coop', sort_order: 2 },
      ],
    })
    const { fireEvent } = await import('@testing-library/preact')
    fireEvent.click(getByLabelText('Move Aldi down'))
    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledWith('/api/shopping/stores/order', {
        order: ['Migros', 'Aldi', 'Coop'],
      })
    })
  })

  it('▲ moves a store up and is disabled on the first row', async () => {
    const { getByLabelText, waitFor } = await setup({ stores: TWO_STORES })
    const { fireEvent } = await import('@testing-library/preact')
    expect((getByLabelText('Move Aldi up') as HTMLButtonElement).disabled).toBe(true)
    expect((getByLabelText('Move Migros down') as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(getByLabelText('Move Migros up'))
    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledWith('/api/shopping/stores/order', {
        order: ['Migros', 'Aldi'],
      })
    })
  })

  it('renames a store inline via PATCH and toasts the surviving name', async () => {
    apiPatch.mockResolvedValue({
      old_name: 'Aldi', new_name: 'Coop', merged: false, moved_items: 0,
    })
    const { getByLabelText, container, waitFor, showToast } = await setup({
      stores: TWO_STORES,
      items: THREE_ITEMS,
    })
    const { fireEvent } = await import('@testing-library/preact')
    fireEvent.click(getByLabelText('Rename Aldi'))
    await waitFor(() => {
      expect(container.querySelector('.sh-store-manager__input')).not.toBeNull()
    })
    const input = container.querySelector('.sh-store-manager__input') as HTMLInputElement
    fireEvent.input(input, { target: { value: 'Coop' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith('/api/shopping/stores/Aldi', {
        name: 'Coop',
      })
    })
    await waitFor(() => {
      expect(vi.mocked(showToast).mock.calls.some(
        c => /renamed/i.test(String(c[0])) && String(c[0]).includes('Coop'),
      )).toBe(true)
    })
  })

  it('confirms before a rename that would merge, then toasts the merge', async () => {
    apiPatch.mockResolvedValue({
      old_name: 'Aldi', new_name: 'Migros', merged: true, moved_items: 3,
    })
    const { getByLabelText, container, waitFor, showToast, confirmDialog } =
      await setup({ stores: TWO_STORES, items: THREE_ITEMS })
    vi.mocked(confirmDialog).mockResolvedValue(true)
    const { fireEvent } = await import('@testing-library/preact')
    fireEvent.click(getByLabelText('Rename Aldi'))
    await waitFor(() => {
      expect(container.querySelector('.sh-store-manager__input')).not.toBeNull()
    })
    const input = container.querySelector('.sh-store-manager__input') as HTMLInputElement
    // Different casing than the existing "Migros" — still a merge.
    fireEvent.input(input, { target: { value: 'migros' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => {
      expect(confirmDialog).toHaveBeenCalled()
    })
    const msg = String(vi.mocked(confirmDialog).mock.calls.at(-1)![0])
    expect(msg).toContain('Migros')
    expect(msg).toContain('already exists')
    expect(msg).toContain('3')

    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith('/api/shopping/stores/Aldi', {
        name: 'migros',
      })
    })
    await waitFor(() => {
      // The SURVIVING spelling is the target's casing, not what was typed.
      expect(vi.mocked(showToast).mock.calls.some(
        c => /merged/i.test(String(c[0]))
          && String(c[0]).includes('Migros')
          && String(c[0]).includes('3'),
      )).toBe(true)
    })
  })

  it('cancelling the merge confirm leaves the catalogue untouched', async () => {
    const { getByLabelText, container, waitFor, confirmDialog } = await setup({
      stores: TWO_STORES, items: THREE_ITEMS,
    })
    vi.mocked(confirmDialog).mockResolvedValue(false)
    const { fireEvent } = await import('@testing-library/preact')
    fireEvent.click(getByLabelText('Rename Aldi'))
    await waitFor(() => {
      expect(container.querySelector('.sh-store-manager__input')).not.toBeNull()
    })
    const input = container.querySelector('.sh-store-manager__input') as HTMLInputElement
    fireEvent.input(input, { target: { value: 'Migros' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => {
      expect(confirmDialog).toHaveBeenCalled()
    })
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('deletes a store behind a destructive confirm mentioning "No store"', async () => {
    const { getByLabelText, waitFor, confirmDialog } = await setup({
      stores: TWO_STORES, items: THREE_ITEMS,
    })
    vi.mocked(confirmDialog).mockResolvedValue(true)
    const { fireEvent } = await import('@testing-library/preact')
    fireEvent.click(getByLabelText('Delete Aldi'))
    await waitFor(() => {
      expect(confirmDialog).toHaveBeenCalled()
    })
    const [msg, opts] = vi.mocked(confirmDialog).mock.calls.at(-1)!
    expect(String(msg)).toContain('No store')
    expect(opts).toMatchObject({ destructive: true })
    await waitFor(() => {
      expect(apiDelete).toHaveBeenCalledWith('/api/shopping/stores/Aldi')
    })
  })

  it('does not delete when the confirm is declined', async () => {
    const { getByLabelText, waitFor, confirmDialog } = await setup({
      stores: TWO_STORES,
    })
    vi.mocked(confirmDialog).mockResolvedValue(false)
    const { fireEvent } = await import('@testing-library/preact')
    fireEvent.click(getByLabelText('Delete Aldi'))
    await waitFor(() => {
      expect(confirmDialog).toHaveBeenCalled()
    })
    expect(apiDelete).not.toHaveBeenCalled()
  })

  it('adds a store via POST /api/shopping/stores', async () => {
    apiPost.mockResolvedValue({ name: 'Coop', sort_order: 2 })
    const { getByRole, container, waitFor } = await setup({ stores: TWO_STORES })
    const { fireEvent } = await import('@testing-library/preact')
    fireEvent.click(getByRole('button', { name: /add store/i }))
    await waitFor(() => {
      expect(container.querySelector('.sh-store-manager__add-input')).not.toBeNull()
    })
    const input = container.querySelector('.sh-store-manager__add-input') as HTMLInputElement
    fireEvent.input(input, { target: { value: 'Coop' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith('/api/shopping/stores', { name: 'Coop' })
    })
  })

  it('says so when the added store was already in the catalogue', async () => {
    // The endpoint is idempotent case-insensitively — adding "aldi"
    // must not look like a no-op.
    apiPost.mockResolvedValue({ name: 'Aldi', sort_order: 0 })
    const { getByRole, container, waitFor, showToast } = await setup({
      stores: TWO_STORES,
    })
    const { fireEvent } = await import('@testing-library/preact')
    fireEvent.click(getByRole('button', { name: /add store/i }))
    await waitFor(() => {
      expect(container.querySelector('.sh-store-manager__add-input')).not.toBeNull()
    })
    const input = container.querySelector('.sh-store-manager__add-input') as HTMLInputElement
    fireEvent.input(input, { target: { value: 'aldi' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => {
      expect(vi.mocked(showToast).mock.calls.some(
        c => /already/i.test(String(c[0])),
      )).toBe(true)
    })
  })

  it('surfaces a failing call as an error toast', async () => {
    apiDelete.mockRejectedValue(new Error('nope'))
    const { getByLabelText, waitFor, showToast, confirmDialog } = await setup({
      stores: TWO_STORES,
    })
    vi.mocked(confirmDialog).mockResolvedValue(true)
    const { fireEvent } = await import('@testing-library/preact')
    fireEvent.click(getByLabelText('Delete Aldi'))
    await waitFor(() => {
      expect(vi.mocked(showToast).mock.calls.some(c => c[1] === 'error')).toBe(true)
    })
  })
})
