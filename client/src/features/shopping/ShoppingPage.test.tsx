import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: (...args: unknown[]) => apiPatch(...args),
    delete: (...args: unknown[]) => apiDelete(...args),
  },
}))

vi.mock('@/ws', () => ({
  ws: { on: vi.fn(() => () => {}) },
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('@/store/householdUsers', () => ({
  householdDisplayName: (uid: string) => uid,
  loadHouseholdUsers: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('@/components/confirm', () => ({
  confirmDialog: vi.fn().mockResolvedValue(true),
}))

interface MockFixtures {
  items: unknown[]
  stores: unknown[]
}

function wireApi(f: MockFixtures): void {
  apiGet.mockImplementation(async (url: string) => {
    if (url.startsWith('/api/shopping/stores')) return f.stores
    if (url.startsWith('/api/shopping')) return f.items
    return []
  })
  apiPatch.mockResolvedValue({})
  apiPost.mockResolvedValue({})
  apiDelete.mockResolvedValue(undefined)
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDelete.mockReset()
})

/** Minimal DataTransfer polyfill for the drag-drop tests. jsdom
 *  doesn't ship one, and the page's ``onDragStart`` writes via
 *  ``setData`` / reads back via ``getData`` + ``types``. */
function makeDataTransfer(): DataTransfer {
  const store = new Map<string, string>()
  const types: string[] = []
  return {
    types,
    effectAllowed: 'all',
    setData(type: string, val: string) {
      store.set(type, val)
      if (!types.includes(type)) types.push(type)
    },
    getData(type: string) { return store.get(type) ?? '' },
  } as unknown as DataTransfer
}

describe('ShoppingPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./ShoppingPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })

  it('renders rows with a separate checkbox button and text element', async () => {
    // Regression for the "click text marks it done" bug. The new
    // row places the checkbox and the text as independent siblings
    // — neither is wrapped in the other's tap target.
    wireApi({
      items: [{
        id: 'i1', text: 'Milk', store: null, completed: false,
        created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1',
      }],
      stores: [],
    })
    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelectorAll('.sh-shopping-item').length).toBe(1)
    }, { timeout: 2000 })

    const row = container.querySelector('.sh-shopping-item')
    expect(row).not.toBeNull()
    const check = row!.querySelector('.sh-shopping-item__check')
    const text = row!.querySelector('.sh-shopping-item__text')
    expect(check).not.toBeNull()
    expect(text).not.toBeNull()
    // The checkbox must NOT contain the text (the old
    // ``<label>``-wrap pattern); they sit side by side.
    expect(check!.contains(text!)).toBe(false)
    expect(text!.contains(check!)).toBe(false)
  })

  it('toggles done when the checkbox button is clicked, not when the text is clicked', async () => {
    wireApi({
      items: [{
        id: 'i1', text: 'Milk', store: null, completed: false,
        created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1',
      }],
      stores: [],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-item')).not.toBeNull()
    })

    // Click the TEXT — must enter rename mode, not toggle done.
    const text = container.querySelector('.sh-shopping-item__text') as HTMLElement
    fireEvent.click(text)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-item--edit')).not.toBeNull()
    })
    // The toggle endpoint must NOT have been hit by the text click.
    expect(apiPatch).not.toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/shopping\/i1\/(complete|uncomplete)/),
    )
  })

  it('inline "+ New store…" creates a new store and assigns it in one go', async () => {
    // Regression for the mobile-tested complaint: there used to be
    // no way to add a new store without first picking it for an
    // item via ``window.prompt()`` (poor mobile UX). The picker
    // now swaps to an inline name-entry input on tap of "+ New
    // store…"; Save fires ``onPick(name)`` which the store layer
    // upserts into the catalogue + assigns to the item.
    wireApi({
      items: [{
        id: 'i1', text: 'Eggs', store: null, completed: false,
        created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1',
      }],
      stores: [{ name: 'Aldi', sort_order: 0 }],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-store-pill')).not.toBeNull()
    })

    // Open the picker.
    fireEvent.click(container.querySelector('.sh-shopping-store-pill') as HTMLElement)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-store-picker__menu')).not.toBeNull()
    })

    // Tap "+ New store…" → swap to ``new`` mode.
    const newBtn = Array.from(
      container.querySelectorAll('.sh-shopping-store-picker__opt'),
    ).find(b => b.textContent?.includes('New store')) as HTMLElement
    expect(newBtn).toBeTruthy()
    fireEvent.click(newBtn)

    // Inline input appears.
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-store-picker__new-input'))
        .not.toBeNull()
    })

    // Type a name + Enter → PATCH with new store.
    const input = container.querySelector(
      '.sh-shopping-store-picker__new-input',
    ) as HTMLInputElement
    fireEvent.input(input, { target: { value: 'Migros' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith(
        '/api/shopping/i1',
        { store: 'Migros' },
      )
    })
  })

  it('"← Back" returns from new-store input to the store list', async () => {
    wireApi({
      items: [{
        id: 'i1', text: 'Eggs', store: null, completed: false,
        created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1',
      }],
      stores: [{ name: 'Aldi', sort_order: 0 }],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-store-pill')).not.toBeNull()
    })
    fireEvent.click(container.querySelector('.sh-shopping-store-pill') as HTMLElement)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-store-picker__menu')).not.toBeNull()
    })
    const newBtn = Array.from(
      container.querySelectorAll('.sh-shopping-store-picker__opt'),
    ).find(b => b.textContent?.includes('New store')) as HTMLElement
    fireEvent.click(newBtn)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-store-picker__new-input'))
        .not.toBeNull()
    })
    const back = container.querySelector('.sh-shopping-store-picker__back') as HTMLElement
    expect(back).toBeTruthy()
    fireEvent.click(back)
    await waitFor(() => {
      // Back to list mode: the menu's ``<ul>`` returns.
      expect(container.querySelector('.sh-shopping-store-picker__new-input'))
        .toBeNull()
      const opts = container.querySelectorAll('.sh-shopping-store-picker__opt')
      expect(opts.length).toBeGreaterThan(0)
    })
  })

  it('store pill opens a picker; clicking a store calls PATCH with the new store', async () => {
    // Seed the item already assigned to a store so it lands in
    // ``Aldi``'s section (where the pill renders). Items in the
    // No-store section now intentionally hide the pill — the
    // section header already says "No store" and a redundant
    // "📍 SET STORE" pill on every row was the loudest, most-
    // repeated thing on the page.
    wireApi({
      items: [{
        id: 'i1', text: 'Milk', store: 'Aldi', completed: false,
        created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1',
      }],
      stores: [
        { name: 'Aldi', sort_order: 0 },
        { name: 'Migros', sort_order: 1 },
      ],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-store-pill')).not.toBeNull()
    })

    // Open the picker.
    const pill = container.querySelector('.sh-shopping-store-pill') as HTMLElement
    fireEvent.click(pill)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-store-picker__menu'))
        .not.toBeNull()
    })

    // Pick "Migros".
    const migros = Array.from(
      container.querySelectorAll('.sh-shopping-store-picker__opt'),
    ).find(b => b.textContent?.trim().startsWith('Migros')) as HTMLElement
    expect(migros).toBeTruthy()
    fireEvent.click(migros)

    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith(
        '/api/shopping/i1',
        { store: 'Migros' },
      )
    })
  })

  it('reassigns the store when an item is dropped on another store section', async () => {
    // Build state so the grouped view renders: two stores + items
    // already exist, so ``stores.length >= 2`` triggers the
    // auto-group default.
    wireApi({
      items: [
        {
          id: 'i1', text: 'Milk', store: 'Aldi', completed: false,
          created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1',
        },
        {
          id: 'i2', text: 'Bread', store: 'Migros', completed: false,
          created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1',
        },
      ],
      stores: [
        { name: 'Aldi', sort_order: 0 },
        { name: 'Migros', sort_order: 1 },
      ],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelectorAll('.sh-shopping-group').length).toBeGreaterThanOrEqual(2)
    })

    // Drag the Milk row (Aldi) to the Migros section.
    const milkRow = Array.from(
      container.querySelectorAll('.sh-shopping-item'),
    ).find(li => li.textContent?.includes('Milk')) as HTMLElement
    expect(milkRow).toBeTruthy()
    expect(milkRow.getAttribute('draggable')).toBe('true')

    const dataTransfer = makeDataTransfer()

    fireEvent.dragStart(milkRow, { dataTransfer })

    // Drop on the Migros section (its <section> root).
    const migrosSection = Array.from(
      container.querySelectorAll('.sh-shopping-group'),
    ).find(s => s.querySelector('.sh-shopping-group__name')?.textContent === 'Migros') as HTMLElement
    expect(migrosSection).toBeTruthy()
    fireEvent.dragOver(migrosSection, { dataTransfer })
    fireEvent.drop(migrosSection, { dataTransfer })

    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith(
        '/api/shopping/i1',
        { store: 'Migros' },
      )
    })
  })

  it('drop on the "No store" section clears the store', async () => {
    wireApi({
      items: [{
        id: 'i1', text: 'Milk', store: 'Aldi', completed: false,
        created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1',
      }],
      stores: [
        { name: 'Aldi', sort_order: 0 },
        { name: 'Migros', sort_order: 1 },
      ],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelectorAll('.sh-shopping-group').length).toBeGreaterThanOrEqual(1)
    })

    const milkRow = container.querySelector('.sh-shopping-item') as HTMLElement
    const dataTransfer = makeDataTransfer()

    fireEvent.dragStart(milkRow, { dataTransfer })

    const noStoreSection = Array.from(
      container.querySelectorAll('.sh-shopping-group'),
    ).find(s => s.querySelector('.sh-shopping-group__name')?.textContent === 'No store') as HTMLElement
    expect(noStoreSection).toBeTruthy()
    fireEvent.dragOver(noStoreSection, { dataTransfer })
    fireEvent.drop(noStoreSection, { dataTransfer })

    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith(
        '/api/shopping/i1',
        { store: null },
      )
    })
  })

  it('renders compact icon-only store pills in the grouped view (no redundant store name)', async () => {
    // In the grouped view the section header already names the store,
    // so repeating it on every row is redundant noise. The pill stays
    // (a tap target to assign / reassign / manage — drag is desktop-
    // only) but renders icon-only: no store-name label.
    wireApi({
      items: [
        // One assigned to Aldi (so grouping kicks in) + one
        // unassigned (lands in the No store section).
        { id: 'i1', text: 'Eggs',  store: 'Aldi', completed: false, created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1' },
        { id: 'i2', text: 'Tools', store: null,   completed: false, created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1' },
      ],
      stores: [
        { name: 'Aldi',   sort_order: 0 },
        { name: 'Migros', sort_order: 1 },
      ],
    })
    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelectorAll('.sh-shopping-group').length).toBeGreaterThanOrEqual(1)
    })

    // Eggs (under Aldi): pill present, compact, and does NOT repeat the
    // store name "Aldi" (the section header already shows it).
    const eggsRow = Array.from(container.querySelectorAll('.sh-shopping-item'))
      .find(li => li.textContent?.includes('Eggs')) as HTMLElement
    const eggsPill = eggsRow.querySelector('.sh-shopping-store-pill')
    expect(eggsPill).not.toBeNull()
    expect(eggsPill?.classList.contains('sh-shopping-store-pill--compact')).toBe(true)
    expect(eggsPill?.textContent).not.toContain('Aldi')

    // Tools (under "No store"): also a compact pill — a quiet assign
    // affordance, not the old "📍 SET STORE" label.
    const toolsRow = Array.from(container.querySelectorAll('.sh-shopping-item'))
      .find(li => li.textContent?.includes('Tools')) as HTMLElement
    const toolsPill = toolsRow.querySelector('.sh-shopping-store-pill')
    expect(toolsPill).not.toBeNull()
    expect(toolsPill?.classList.contains('sh-shopping-store-pill--compact')).toBe(true)
    expect(toolsPill?.textContent).not.toContain('Set store')
  })

  it('grouped view collects all done items into a single trailer (not per-store)', async () => {
    wireApi({
      items: [
        { id: 'a1', text: 'Eggs',          store: 'Aldi',   completed: false, created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1' },
        { id: 'm1', text: 'Bread',         store: 'Migros', completed: false, created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1' },
        { id: 'a2', text: 'Old apples',    store: 'Aldi',   completed: true,  created_at: '2026-05-15T10:00:00+00:00', created_by: 'u1' },
        { id: 'm2', text: 'Old mozzarella', store: 'Migros', completed: true, created_at: '2026-05-15T10:00:00+00:00', created_by: 'u1' },
      ],
      stores: [
        { name: 'Aldi',   sort_order: 0 },
        { name: 'Migros', sort_order: 1 },
      ],
    })
    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-divider')).not.toBeNull()
    })

    // Per-store done piles are gone — neither Aldi nor Migros
    // sections contain ``--done`` rows; both done items live in
    // a single trailer at the bottom.
    const groupDoneLists = container.querySelectorAll(
      '.sh-shopping-group .sh-shopping-list--done',
    )
    expect(groupDoneLists.length).toBe(0)

    const trailerDone = container.querySelector(
      'ul.sh-shopping-list--done',
    )
    expect(trailerDone).not.toBeNull()
    expect(trailerDone!.textContent).toContain('Old apples')
    expect(trailerDone!.textContent).toContain('Old mozzarella')
  })

  it('the header carries a Stores button that opens the store manager', async () => {
    wireApi({
      items: [{
        id: 'i1', text: 'Eggs', store: 'Aldi', completed: false,
        created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1',
      }],
      stores: [
        { name: 'Aldi',   sort_order: 0 },
        { name: 'Migros', sort_order: 1 },
      ],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container, getByRole } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-stores-btn')).not.toBeNull()
    })
    // Closed at rest.
    expect(container.querySelector('.sh-store-manager')).toBeNull()

    fireEvent.click(getByRole('button', { name: /stores/i }))
    await waitFor(() => {
      expect(document.querySelector('.sh-store-manager')).not.toBeNull()
    })
    // The whole catalogue is manageable from here — both stores are
    // listed, regardless of which one any item sits at.
    const rows = Array.from(
      document.querySelectorAll('.sh-store-manager__row'),
    ).map(r => r.textContent)
    expect(rows.join(' ')).toContain('Aldi')
    expect(rows.join(' ')).toContain('Migros')
  })

  it('offers the Stores button even when the list has no items at all', async () => {
    // Regression: store management used to live ONLY inside an item
    // row's 📍 popover, so an empty list (or a list with no active
    // items) left the household with no way to rename, reorder or
    // delete a store at all.
    wireApi({
      items: [],
      stores: [{ name: 'Aldi', sort_order: 0 }],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container, getByRole } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-empty-state')).not.toBeNull()
    })
    const btn = getByRole('button', { name: /stores/i })
    expect(btn).toBeTruthy()
    fireEvent.click(btn)
    await waitFor(() => {
      expect(document.querySelector('.sh-store-manager__row')).not.toBeNull()
    })
  })

  it('the item store picker no longer carries a manage affordance', async () => {
    // Rename / delete now live in exactly one place (the Stores
    // dialog); the row popover is assign-only.
    wireApi({
      items: [{
        id: 'i1', text: 'Eggs', store: 'Aldi', completed: false,
        created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1',
      }],
      stores: [
        { name: 'Aldi',   sort_order: 0 },
        { name: 'Migros', sort_order: 1 },
      ],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-store-pill')).not.toBeNull()
    })
    fireEvent.click(container.querySelector('.sh-shopping-store-pill') as HTMLElement)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-store-picker__menu')).not.toBeNull()
    })
    expect(container.querySelector('.sh-shopping-store-picker__manage')).toBeNull()
    // Assign-only behaviour survives: existing stores, "No store"
    // and "+ New store…".
    const menu = container.querySelector('.sh-shopping-store-picker__menu')!
    expect(menu.textContent).toContain('Aldi')
    expect(menu.textContent).toContain('No store')
    expect(menu.textContent).toContain('New store')
  })

  it('typing "Milk @" in the quick-add input pops the store autocomplete', async () => {
    wireApi({
      items: [],
      stores: [
        { name: 'Aldi', sort_order: 0 },
        { name: 'Migros', sort_order: 1 },
      ],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-add input')).not.toBeNull()
    })
    const input = container.querySelector(
      '.sh-shopping-add input',
    ) as HTMLInputElement
    // Type "Milk @" and put the caret at the end so the autocomplete
    // sees the @ context.
    input.value = 'Milk @'
    input.setSelectionRange(input.value.length, input.value.length)
    fireEvent.input(input, { target: input })

    await waitFor(() => {
      const popover = container.querySelector(
        '.sh-shopping-suggest[aria-label="Pick a store"]',
      )
      expect(popover).not.toBeNull()
    })
    const chips = Array.from(
      container.querySelectorAll(
        '.sh-shopping-suggest[aria-label="Pick a store"] .sh-chip',
      ),
    ).map(el => el.textContent)
    expect(chips).toEqual(expect.arrayContaining(['Aldi', 'Migros']))
  })

  it('picking a store from the autocomplete splices "@ Store " into the draft', async () => {
    wireApi({
      items: [],
      stores: [
        { name: 'Aldi', sort_order: 0 },
        { name: 'Migros', sort_order: 1 },
      ],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-add input')).not.toBeNull()
    })
    const input = container.querySelector(
      '.sh-shopping-add input',
    ) as HTMLInputElement
    input.value = 'Milk @ Al'
    input.setSelectionRange(input.value.length, input.value.length)
    fireEvent.input(input, { target: input })
    await waitFor(() => {
      const popover = container.querySelector(
        '.sh-shopping-suggest[aria-label="Pick a store"]',
      )
      expect(popover).not.toBeNull()
    })
    // The partial "Al" should narrow Aldi but exclude Migros.
    const chips = Array.from(
      container.querySelectorAll(
        '.sh-shopping-suggest[aria-label="Pick a store"] .sh-chip',
      ),
    )
    expect(chips.map(el => el.textContent)).toEqual(['Aldi'])

    fireEvent.click(chips[0] as HTMLElement)
    await waitFor(() => {
      expect(input.value).toBe('Milk @ Aldi ')
    })
  })

  it('tapping a store chip on touch (no click) still picks it', async () => {
    // On iOS/WKWebView the synthetic ``click`` is suppressed because the
    // chip preventDefaults its mousedown — so the pick must work from
    // ``touchend`` alone. Fire only the touch event, never a click.
    wireApi({
      items: [],
      stores: [{ name: 'Aldi', sort_order: 0 }],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-add input')).not.toBeNull()
    })
    const input = container.querySelector(
      '.sh-shopping-add input',
    ) as HTMLInputElement
    input.value = 'Milk @ Al'
    input.setSelectionRange(input.value.length, input.value.length)
    fireEvent.input(input, { target: input })
    await waitFor(() => {
      expect(
        container.querySelector(
          '.sh-shopping-suggest[aria-label="Pick a store"] .sh-chip',
        ),
      ).not.toBeNull()
    })
    const chip = container.querySelector(
      '.sh-shopping-suggest[aria-label="Pick a store"] .sh-chip',
    ) as HTMLElement
    fireEvent.touchEnd(chip)
    await waitFor(() => {
      expect(input.value).toBe('Milk @ Aldi ')
    })
  })

  it('autocomplete is scoped to the current comma-segment', async () => {
    // Pasting "Milk @ Aldi, Bread" with the caret after "Bread" must
    // NOT keep the autocomplete open against Aldi's "@" — the comma
    // ends the previous segment.
    wireApi({
      items: [],
      stores: [{ name: 'Aldi', sort_order: 0 }],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-add input')).not.toBeNull()
    })
    const input = container.querySelector(
      '.sh-shopping-add input',
    ) as HTMLInputElement
    input.value = 'Milk @ Aldi, Bread'
    input.setSelectionRange(input.value.length, input.value.length)
    fireEvent.input(input, { target: input })

    // Give Preact a tick to re-render then assert NO store popover.
    await new Promise(r => setTimeout(r, 10))
    const popover = container.querySelector(
      '.sh-shopping-suggest[aria-label="Pick a store"]',
    )
    expect(popover).toBeNull()
  })
})
