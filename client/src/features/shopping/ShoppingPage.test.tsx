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

vi.mock('@/ws', async () => {
  const { signal } = await import('@preact/signals')
  return {
    ws: { on: vi.fn(() => () => {}) },
    connectionState: signal('open'),
  }
})

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

  // ─── Organize overhaul (PR 2) ─────────────────────────────────────

  const MILK = {
    id: 'i1', text: 'Milk', store: null, completed: false,
    created_at: '2026-05-17T10:00:00+00:00', created_by: 'u1',
  }
  const BREAD = { ...MILK, id: 'i2', text: 'Bread' }
  const OLD_EGGS = { ...MILK, id: 'd1', text: 'Old eggs', completed: true }

  async function mount(f: MockFixtures) {
    wireApi(f)
    const tl = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const toast = await import('@/components/Toast')
    const undo = await import('@/utils/undoableDelete')
    const store = await import('@/store/shopping')
    toast.toasts.value = []
    const r = tl.render(<mod.default />)
    return { ...tl, ...r, toast, undo, store }
  }

  it('shows an error with Retry when the load fails — never an endless spinner', async () => {
    let fail = true
    apiGet.mockImplementation(async (url: string) => {
      if (fail) throw new Error('offline')
      if (url.startsWith('/api/shopping/stores')) return []
      return [MILK]
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { getByRole, container } = render(<mod.default />)
    await waitFor(() => {
      expect(getByRole('alert').textContent).toContain("Couldn't load the shopping list.")
    })
    expect(container.querySelector('[aria-busy="true"]')).toBeNull()
    fail = false
    fireEvent.click(getByRole('button', { name: 'Retry' }))
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-item')).not.toBeNull()
    })
  })

  it('shows a single busy skeleton while loading', async () => {
    apiGet.mockImplementation(() => new Promise(() => {}))
    const { render } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { container } = render(<mod.default />)
    expect(container.querySelectorAll('[aria-busy="true"]').length).toBe(1)
    expect(container.querySelectorAll('[role="status"]').length).toBe(1)
  })

  it('shares one fetch with the Organize hub when both load together', async () => {
    wireApi({ items: [MILK], stores: [] })
    const store = await import('@/store/shopping')
    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    void store.ensureShopping()
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-shopping-item')).not.toBeNull()
    })
    const itemGets = apiGet.mock.calls.filter(([u]) => String(u).startsWith('/api/shopping?'))
    expect(itemGets.length).toBe(1)
  })

  it('delete hides the row at once and only DELETEs when the Undo toast expires', async () => {
    const t = await mount({ items: [MILK, BREAD], stores: [] })
    await t.waitFor(() => expect(t.container.querySelectorAll('.sh-shopping-item').length).toBe(2))
    t.fireEvent.click(t.getByRole('button', { name: 'Delete Milk' }))
    await t.waitFor(() => expect(t.container.querySelectorAll('.sh-shopping-item').length).toBe(1))
    expect(apiDelete).not.toHaveBeenCalled()
    const row = t.toast.toasts.value.find(x => x.message === 'Deleted Milk')!
    expect(row.action?.label).toBe('Undo')
    row.onExpire!()
    await t.waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/shopping/i1'))
  })

  it('Undo brings the deleted row back and never sends the DELETE', async () => {
    const t = await mount({ items: [MILK, BREAD], stores: [] })
    await t.waitFor(() => expect(t.container.querySelectorAll('.sh-shopping-item').length).toBe(2))
    t.fireEvent.click(t.getByRole('button', { name: 'Delete Milk' }))
    await t.waitFor(() => expect(t.container.querySelectorAll('.sh-shopping-item').length).toBe(1))
    // A WS re-add of the hidden row meanwhile must not resurrect it.
    t.store.items.value = [...t.store.items.value.map(i => ({ ...i }))]
    await new Promise(r => setTimeout(r, 0))
    expect(t.container.textContent).not.toContain('Milk')
    t.toast.toasts.value.find(x => x.message === 'Deleted Milk')!.action!.onClick()
    await t.waitFor(() => expect(t.container.textContent).toContain('Milk'))
    expect(apiDelete).not.toHaveBeenCalled()
  })

  it('"Clear all" hides bought items with Undo — no confirm — and deletes exactly those ids', async () => {
    const t = await mount({ items: [MILK, OLD_EGGS], stores: [] })
    const confirm = await import('@/components/confirm')
    await t.waitFor(() => expect(t.container.querySelector('.sh-shopping-list--done')).not.toBeNull())
    t.fireEvent.click(t.getByRole('button', { name: 'Clear all bought items' }))
    await t.waitFor(() => expect(t.container.querySelector('.sh-shopping-list--done')).toBeNull())
    expect(confirm.confirmDialog).not.toHaveBeenCalled()
    expect(apiDelete).not.toHaveBeenCalled()
    const row = t.toast.toasts.value.find(x => x.message === 'Cleared 1 bought item')!
    expect(row).toBeTruthy()
    row.onExpire!()
    await t.waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/shopping/d1'))
    expect(apiPost).not.toHaveBeenCalledWith('/api/shopping/clear-completed', {})
  })

  it('an item ticked off during the Undo window survives the clear-all commit', async () => {
    const t = await mount({ items: [MILK, OLD_EGGS], stores: [] })
    await t.waitFor(() => expect(t.container.querySelector('.sh-shopping-list--done')).not.toBeNull())
    t.fireEvent.click(t.getByRole('button', { name: 'Clear all bought items' }))
    // Milk is bought while the Undo toast is still up.
    t.fireEvent.click(await t.findByRole('checkbox', { name: 'Bought Milk' }))
    await t.waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/shopping/i1/complete'))
    t.toast.toasts.value.find(x => x.message === 'Cleared 1 bought item')!.onExpire!()
    await t.waitFor(() => expect(apiDelete).toHaveBeenCalledTimes(1))
    expect(apiDelete).toHaveBeenCalledWith('/api/shopping/d1')
    expect(apiDelete).not.toHaveBeenCalledWith('/api/shopping/i1')
    await t.waitFor(() => {
      expect(t.container.querySelector('.sh-shopping-list--done')?.textContent).toContain('Milk')
    })
  })

  it('shows no big title — the top bar has it — but keeps an sr-only h2 and the counts', async () => {
    const t = await mount({ items: [MILK, OLD_EGGS], stores: [] })
    await t.waitFor(() => expect(t.container.querySelector('.sh-shopping-item')).not.toBeNull())
    const h2 = t.getByRole('heading', { level: 2, name: 'Shopping list' })
    expect(h2.className).toContain('sr-only')
    expect(t.container.querySelector('.sh-organize-header__counts')!.textContent)
      .toBe('1 to buy·1 done')
  })

  it('rename is keyboard reachable: a button trigger, Enter saves, focus comes back', async () => {
    const t = await mount({ items: [MILK], stores: [] })
    const trigger = await t.findByRole('button', { name: 'Rename Milk' })
    expect(trigger.tagName).toBe('BUTTON')
    t.fireEvent.click(trigger)
    const input = await t.findByRole('textbox', { name: 'Item name' }) as HTMLInputElement
    t.fireEvent.input(input, { target: { value: 'Oat milk' } })
    t.fireEvent.keyDown(input, { key: 'Enter' })
    await t.waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith('/api/shopping/i1', { text: 'Oat milk' })
    })
    await t.waitFor(() => {
      expect(document.activeElement?.className).toContain('sh-shopping-item__text')
    })
  })

  it('Escape cancels a rename without saving', async () => {
    const t = await mount({ items: [MILK], stores: [] })
    t.fireEvent.click(await t.findByRole('button', { name: 'Rename Milk' }))
    const input = await t.findByRole('textbox', { name: 'Item name' })
    t.fireEvent.input(input, { target: { value: 'Something else' } })
    t.fireEvent.keyDown(input, { key: 'Escape' })
    await t.waitFor(() => expect(t.container.querySelector('.sh-shopping-item--edit')).toBeNull())
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('leaving the rename field commits the edit', async () => {
    const t = await mount({ items: [MILK], stores: [] })
    t.fireEvent.click(await t.findByRole('button', { name: 'Rename Milk' }))
    const input = await t.findByRole('textbox', { name: 'Item name' })
    t.fireEvent.input(input, { target: { value: 'Soy milk' } })
    // Real focus move out of the editor (jsdom fires focusout).
    ;(input as HTMLInputElement).focus()
    t.getByRole('textbox', { name: 'New shopping item' }).focus()
    await t.waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith('/api/shopping/i1', { text: 'Soy milk' })
    })
  })

  it('the check is a real checkbox that toggles the item', async () => {
    const t = await mount({ items: [MILK], stores: [] })
    const box = await t.findByRole('checkbox', { name: 'Bought Milk' })
    expect(box.getAttribute('aria-checked')).toBe('false')
    t.fireEvent.click(box)
    await t.waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/shopping/i1/complete'))
  })

  it('the group-by toggle is a radiogroup that switches views and remembers it', async () => {
    const t = await mount({
      items: [{ ...MILK, store: 'Aldi' }, { ...BREAD, store: 'Migros' }],
      stores: [{ name: 'Aldi', sort_order: 0 }, { name: 'Migros', sort_order: 1 }],
    })
    const group = await t.findByRole('radiogroup', { name: 'View' })
    const grouped = t.getByRole('radio', { name: 'Group by store' })
    expect(grouped.getAttribute('aria-checked')).toBe('true')
    expect(t.container.querySelector('.sh-shopping-group')).not.toBeNull()
    t.fireEvent.keyDown(grouped, { key: 'ArrowRight' })
    await t.waitFor(() => expect(t.container.querySelector('.sh-shopping-group')).toBeNull())
    expect(t.getByRole('radio', { name: 'Show as list' }).getAttribute('aria-checked')).toBe('true')
    expect(localStorage.getItem('sh_shopping_group_by_store')).toBe('off')
    expect(group).toBeTruthy()
    localStorage.removeItem('sh_shopping_group_by_store')
  })

  it('the store picker is keyboard operable and hands focus back to its pill', async () => {
    const t = await mount({
      items: [{ ...MILK, store: 'Aldi' }],
      stores: [{ name: 'Aldi', sort_order: 0 }, { name: 'Migros', sort_order: 1 }],
    })
    const pill = await t.findByRole('button', { name: 'Store: Aldi — change' })
    t.fireEvent.click(pill)
    const current = await t.findByRole('menuitemradio', { name: /Aldi/ })
    expect(current.getAttribute('aria-checked')).toBe('true')
    expect(document.activeElement).toBe(current)
    t.fireEvent.keyDown(current, { key: 'ArrowDown' })
    expect(document.activeElement?.textContent).toContain('Migros')
    t.fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    await t.waitFor(() => expect(t.queryByRole('menu')).toBeNull())
    expect(document.activeElement).toBe(pill)
  })

  it('the suggestion rows are button groups, not listboxes', async () => {
    const t = await mount({ items: [OLD_EGGS], stores: [] })
    const input = await t.findByRole('textbox', { name: 'New shopping item' })
    t.fireEvent.focus(input)
    const group = await t.findByRole('group', { name: 'Re-add a recent item' })
    expect(group.querySelector('button')?.textContent).toBe('Old eggs')
    expect(t.container.querySelector('[role="listbox"]')).toBeNull()
  })

  it('the empty state offers a button that focuses the add field', async () => {
    const t = await mount({ items: [], stores: [] })
    const cta = await t.findByRole('button', { name: 'Add an item' })
    ;(document.activeElement as HTMLElement | null)?.blur()
    t.fireEvent.click(cta)
    expect(document.activeElement).toBe(t.getByRole('textbox', { name: 'New shopping item' }))
  })

  it('says "1 duplicate skipped" / "2 duplicates skipped"', async () => {
    const t = await mount({ items: [MILK, BREAD], stores: [] })
    const input = await t.findByRole('textbox', { name: 'New shopping item' }) as HTMLInputElement
    t.fireEvent.input(input, { target: { value: 'Milk, Eggs' } })
    t.fireEvent.submit(input.closest('form')!)
    await t.waitFor(() => {
      expect(t.toast.toasts.value.map(x => x.message)).toContain('1 duplicate skipped')
    })
    t.fireEvent.input(input, { target: { value: 'Milk, Bread, Jam' } })
    t.fireEvent.submit(input.closest('form')!)
    await t.waitFor(() => {
      expect(t.toast.toasts.value.map(x => x.message)).toContain('2 duplicates skipped')
    })
  })

  // ─── PR 2 review fixes ────────────────────────────────────────────

  it('a 404 on the single-delete commit keeps the row gone', async () => {
    const t = await mount({ items: [MILK, BREAD], stores: [] })
    apiDelete.mockRejectedValue(Object.assign(new Error('gone'), { status: 404 }))
    await t.waitFor(() => expect(t.container.querySelectorAll('.sh-shopping-item').length).toBe(2))
    t.fireEvent.click(t.getByRole('button', { name: 'Delete Milk' }))
    t.toast.toasts.value.find(x => x.message === 'Deleted Milk')!.onExpire!()
    await t.waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/shopping/i1'))
    await new Promise(r => setTimeout(r, 20))
    expect(t.container.textContent).not.toContain('Milk')
    expect(t.store.items.value.map(i => i.id)).toEqual(['i2'])
    expect(t.toast.toasts.value.some(x => x.type === 'error')).toBe(false)
  })

  it('Undo puts focus on the restored row', async () => {
    const t = await mount({ items: [MILK, BREAD], stores: [] })
    await t.waitFor(() => expect(t.container.querySelectorAll('.sh-shopping-item').length).toBe(2))
    t.fireEvent.click(t.getByRole('button', { name: 'Delete Milk' }))
    t.toast.toasts.value.find(x => x.message === 'Deleted Milk')!.action!.onClick()
    await t.waitFor(() => {
      expect(document.activeElement?.getAttribute('aria-label')).toBe('Bought Milk')
    })
  })

  it('a successful Retry moves focus to the add field', async () => {
    let fail = true
    apiGet.mockImplementation(async (url: string) => {
      if (fail) throw new Error('offline')
      return url.startsWith('/api/shopping/stores') ? [] : [MILK]
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./ShoppingPage')
    const { getByRole } = render(<mod.default />)
    const retry = await waitFor(() => getByRole('button', { name: 'Retry' }))
    retry.focus()
    fail = false
    fireEvent.click(retry)
    await waitFor(() => {
      expect(document.activeElement).toBe(getByRole('textbox', { name: 'New shopping item' }))
    })
  })

  it('rows carry the reveal-host class so the ✕ shows on row hover / focus', async () => {
    const t = await mount({ items: [MILK], stores: [] })
    await t.waitFor(() => expect(t.container.querySelector('.sh-shopping-item')).not.toBeNull())
    expect(t.container.querySelector('.sh-shopping-item')!.classList.contains('sh-row-reveal-host')).toBe(true)
  })

  it('the picker marks the current store checked across ASCII case', async () => {
    const t = await mount({
      items: [{ ...MILK, store: 'aldi' }],
      stores: [{ name: 'Aldi', sort_order: 0 }],
    })
    t.fireEvent.click(await t.findByRole('button', { name: 'Store: aldi — change' }))
    const opt = await t.findByRole('menuitemradio', { name: /Aldi/ })
    expect(opt.getAttribute('aria-checked')).toBe('true')
    expect(t.getByRole('menuitemradio', { name: /No store/ }).getAttribute('aria-checked')).toBe('false')
  })
})
