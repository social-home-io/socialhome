/**
 * SpaceZonesAdmin tests.
 *
 * Leaflet writes to ``HTMLElement.getBoundingClientRect`` and friends
 * which jsdom doesn't fully support — we mock the module so the test
 * focuses on the form logic, list rendering and API wiring rather
 * than DOM-mounted map plumbing. The map itself has its own coverage
 * via ``LocationMap.test.tsx``.
 */
import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/preact'

// jsdom doesn't ship ResizeObserver; the admin UI uses it to keep
// Leaflet's preview map sized correctly inside the modal.
beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
})

/** Every popup / tooltip content handed to a zone circle, in order. */
const overlays = vi.hoisted(() => [] as unknown[])

vi.mock('leaflet', () => {
  const noop = () => undefined
  const fluentSelf = (target: any) => new Proxy(target, {
    get(t, prop) {
      if (prop in t) return (t as any)[prop]
      return () => target
    },
  })
  const layer = fluentSelf({
    addTo: () => layer,
    bindTooltip: (c: unknown) => { overlays.push(c); return layer },
    bindPopup: (c: unknown) => { overlays.push(c); return layer },
    remove: noop,
    clearLayers: noop,
  })
  const map = fluentSelf({
    on: noop,
    addTo: () => map,
    setView: () => map,
    fitBounds: () => map,
    invalidateSize: noop,
    remove: noop,
  })
  return {
    default: {
      map: () => map,
      tileLayer: () => layer,
      layerGroup: () => layer,
      circle: () => layer,
      latLngBounds: () => ({ pad: () => ({}) }),
    },
  }
})
vi.mock('leaflet/dist/leaflet.css', () => ({}))

const mockApi = {
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}
vi.mock('@/api', () => ({
  get api() { return mockApi },
}))

// Map tiles come from the backend proxy via the shared helper — stub
// it so the admin UI test needs no ``/api/map/config`` round trip.
const addTileLayer = vi.fn<
  (
    map: unknown,
    isCancelled?: () => boolean,
    onError?: () => void,
  ) => Promise<void>
>()
vi.mock('@/utils/mapTiles', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/utils/mapTiles')>()),
  get addTileLayer() { return addTileLayer },
}))

vi.mock('./Toast', () => ({ showToast: vi.fn() }))
import { t } from '@/i18n/i18n'
import { SpaceZonesAdmin } from './SpaceZonesAdmin'

const _zone = (over: Partial<any> = {}) => ({
  id: 'z_office',
  space_id: 'sp_test',
  name: 'Office',
  latitude: 47.3769,
  longitude: 8.5417,
  radius_m: 150,
  color: '#3b82f6',
  icon: null,
  created_by: 'u_admin',
  created_at: '2026-04-27T12:00:00Z',
  updated_at: '2026-04-27T12:00:00Z',
  ...over,
})


/** Mount overlay content the way Leaflet's ``DivOverlay._updateContent``
 *  does: a string goes through ``innerHTML``, a node is appended. */
function leafletMount(content: unknown): HTMLElement {
  const el = document.createElement('div')
  if (typeof content === 'string') el.innerHTML = content
  else el.appendChild(content as Node)
  return el
}

describe('SpaceZonesAdmin', () => {
  beforeEach(() => {
    overlays.length = 0
    mockApi.get.mockReset()
    mockApi.post.mockReset()
    mockApi.patch.mockReset()
    mockApi.delete.mockReset()
    addTileLayer.mockReset()
    addTileLayer.mockResolvedValue(undefined)
  })

  it('renders the existing zones from /zones', async () => {
    mockApi.get.mockResolvedValue({ zones: [_zone({ name: 'Office' })] })
    const { findByText, getByText } = render(
      <SpaceZonesAdmin spaceId="sp_test" />,
    )
    await findByText('Office')
    expect(mockApi.get).toHaveBeenCalledWith('/api/spaces/sp_test/zones')
    expect(getByText(/1 of 50 zones used/i)).toBeTruthy()
  })

  it('shows the empty state when there are no zones', async () => {
    mockApi.get.mockResolvedValue({ zones: [] })
    const { findByText } = render(
      <SpaceZonesAdmin spaceId="sp_test" />,
    )
    await findByText(/No zones yet/i)
  })

  it('opens the create modal and POSTs a zone', async () => {
    mockApi.get.mockResolvedValueOnce({ zones: [] })
    mockApi.post.mockResolvedValueOnce(_zone({ name: 'Workshop' }))
    mockApi.get.mockResolvedValue({ zones: [_zone({ name: 'Workshop' })] })

    const { findByText, getByText, getByLabelText } = render(
      <SpaceZonesAdmin spaceId="sp_test" />,
    )
    await findByText(/No zones yet/i)
    fireEvent.click(getByText('+ Add zone'))

    const nameInput = getByLabelText('Name') as HTMLInputElement
    fireEvent.input(nameInput, { target: { value: 'Workshop' } })
    const latInput = getByLabelText('Latitude') as HTMLInputElement
    fireEvent.input(latInput, { target: { value: '47.3769' } })
    const lonInput = getByLabelText('Longitude') as HTMLInputElement
    fireEvent.input(lonInput, { target: { value: '8.5417' } })

    fireEvent.click(getByText('Create zone'))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledTimes(1))
    expect(mockApi.post).toHaveBeenCalledWith(
      '/api/spaces/sp_test/zones',
      expect.objectContaining({
        name: 'Workshop',
        latitude: 47.3769,
        longitude: 8.5417,
      }),
    )
  })

  it('PATCHes when editing an existing zone', async () => {
    mockApi.get.mockResolvedValueOnce({ zones: [_zone()] })
    mockApi.patch.mockResolvedValueOnce(_zone({ name: 'Renamed' }))
    mockApi.get.mockResolvedValue({ zones: [_zone({ name: 'Renamed' })] })

    const { findByText, getAllByText, getByText, getByLabelText } = render(
      <SpaceZonesAdmin spaceId="sp_test" />,
    )
    await findByText('Office')
    fireEvent.click(getAllByText('Edit')[0])

    const nameInput = getByLabelText('Name') as HTMLInputElement
    fireEvent.input(nameInput, { target: { value: 'Renamed' } })
    fireEvent.click(getByText('Save changes'))

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalledTimes(1))
    expect(mockApi.patch).toHaveBeenCalledWith(
      '/api/spaces/sp_test/zones/z_office',
      expect.objectContaining({ name: 'Renamed' }),
    )
  })

  it('shows a zone centre for people, not raw coordinates', async () => {
    mockApi.get.mockResolvedValueOnce({ zones: [_zone()] })
    const { findByText, container } = render(<SpaceZonesAdmin spaceId="sp_test" />)
    await findByText('Office')
    const meta = container.querySelector('[data-testid="zone-list"]')!.textContent!
    expect(meta).toContain('47.377° N, 8.542° E')
    expect(meta).not.toContain('47.3769')
  })

  it('deletes through ConfirmDialog', async () => {
    mockApi.get.mockResolvedValueOnce({ zones: [_zone()] })
    mockApi.delete.mockResolvedValueOnce(undefined)
    mockApi.get.mockResolvedValue({ zones: [] })

    const { findByText, getAllByText } = render(
      <SpaceZonesAdmin spaceId="sp_test" />,
    )
    await findByText('Office')
    // The row's Delete is quiet (no solid red per row); the confirm asks.
    const rowDelete = getAllByText('Delete')[0].closest('button')!
    expect(rowDelete.className).not.toContain('sh-btn--danger')
    fireEvent.click(getAllByText('Delete')[0])  // row button
    // Two "Delete" labels are now in the DOM — the row button (still
    // there) and the ConfirmDialog's confirm button. Click the
    // dialog's, which is the last one rendered.
    const buttons = getAllByText('Delete')
    fireEvent.click(buttons[buttons.length - 1])
    await waitFor(() => expect(mockApi.delete).toHaveBeenCalledTimes(1))
    expect(mockApi.delete).toHaveBeenCalledWith(
      '/api/spaces/sp_test/zones/z_office',
    )
  })

  it('disables "+ Add zone" at the 50-zone cap', async () => {
    const fifty = Array.from({ length: 50 }, (_, i) =>
      _zone({ id: `z_${i}`, name: `Zone ${i}` }))
    mockApi.get.mockResolvedValue({ zones: fifty })
    const { findByText, getByText } = render(
      <SpaceZonesAdmin spaceId="sp_test" />,
    )
    await findByText('Zone 0')
    const button = getByText('+ Add zone') as HTMLButtonElement
    expect(button.disabled).toBe(true)
  })

  it('shows a tiles-unavailable message on the preview map when the tile config fails', async () => {
    addTileLayer.mockRejectedValue(new Error('API 502: /api/map/config'))
    mockApi.get.mockResolvedValue({ zones: [] })
    const { findByText } = render(<SpaceZonesAdmin spaceId="sp_test" />)
    // The overlay renders the translated map.tile_error copy.
    await findByText(t('map.tile_error'))
  })
  it('surfaces a tile-load failure on the preview map too', async () => {
    let report: (() => void) | undefined
    addTileLayer.mockImplementation((_map, _cancelled, onError) => {
      report = onError
      return Promise.resolve()
    })
    mockApi.get.mockResolvedValue({ zones: [] })
    const { findByText } = render(<SpaceZonesAdmin spaceId="sp_test" />)

    await waitFor(() => { expect(report).toBeTypeOf('function') })
    act(() => { report!() })

    // The overlay renders the translated map.tile_error copy.
    await findByText(t('map.tile_error'))
  })

  it('shows a hostile zone name literally in the map popup and tooltip', async () => {
    const name = '<img src=x onerror="window.__xss=1"> & <b>Bold</b>'
    mockApi.get.mockResolvedValue({ zones: [_zone({ name })] })
    const { findByText } = render(<SpaceZonesAdmin spaceId="sp_test" />)
    await findByText(name)
    await waitFor(() => expect(overlays.length).toBe(2))
    for (const content of overlays) {
      const el = leafletMount(content)
      expect(el.querySelector('img')).toBeNull()
      expect(el.querySelector('b')).toBeNull()
      expect(el.textContent).toBe(name)
    }
  })

  it('never lets a hostile zone colour inject extra CSS into the list swatch', async () => {
    mockApi.get.mockResolvedValue({ zones: [_zone({ color: 'red;position:fixed;inset:0;background-image:url(https://evil.example/b)' })] })
    const { container, findByText } = render(<SpaceZonesAdmin spaceId="sp_test" />)
    await findByText('Office')
    const sw = container.querySelector('.sh-zones-admin__swatch') as HTMLElement
    expect(sw.style.position).toBe('')
    expect(sw.style.backgroundImage).not.toContain('url')
    expect(sw.getAttribute('style') ?? '').not.toContain('evil')
  })
  it('lists zones alphabetically, ignoring case', async () => {
    mockApi.get.mockResolvedValue({ zones: [
      _zone({ id: 'a', name: 'work' }),
      _zone({ id: 'b', name: 'Home' }),
      _zone({ id: 'c', name: 'gym' }),
    ] })
    const { container, findByText } = render(<SpaceZonesAdmin spaceId="sp_test" />)
    await findByText('Home')
    const names = [...container.querySelectorAll('.sh-zones-admin__meta strong')]
      .map(el => el.textContent)
    expect(names).toEqual(['gym', 'Home', 'work'])
  })

  it('shows the zone icon inside the list swatch', async () => {
    mockApi.get.mockResolvedValue({ zones: [_zone({ icon: '🏢' })] })
    const { container, findByText } = render(<SpaceZonesAdmin spaceId="sp_test" />)
    await findByText('Office')
    const sw = container.querySelector('.sh-zones-admin__swatch') as HTMLElement
    expect(sw.textContent).toBe('🏢')
    expect(sw.classList.contains('sh-zone-icon--has-icon')).toBe(true)
  })

  it('POSTs the icon picked in the create form', async () => {
    mockApi.get.mockResolvedValueOnce({ zones: [] })
    mockApi.post.mockResolvedValueOnce(_zone({ name: 'School', icon: '🏫' }))
    mockApi.get.mockResolvedValue({ zones: [] })
    const { findByText, getByText, getByLabelText, getByRole } = render(
      <SpaceZonesAdmin spaceId="sp_test" />,
    )
    await findByText(/No zones yet/i)
    fireEvent.click(getByText('+ Add zone'))
    // A new zone starts without an icon.
    expect(getByRole('button', { name: 'No icon' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.input(getByLabelText('Name'), { target: { value: 'School' } })
    fireEvent.input(getByLabelText('Latitude'), { target: { value: '47.3769' } })
    fireEvent.input(getByLabelText('Longitude'), { target: { value: '8.5417' } })
    fireEvent.click(getByRole('button', { name: 'School' }))
    expect(getByRole('button', { name: 'School' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(getByText('Create zone'))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledTimes(1))
    expect(mockApi.post).toHaveBeenCalledWith(
      '/api/spaces/sp_test/zones',
      expect.objectContaining({ name: 'School', icon: '🏫' }),
    )
  })

  it('PATCHes icon: null when "No icon" clears an existing icon', async () => {
    mockApi.get.mockResolvedValueOnce({ zones: [_zone({ icon: '🏢' })] })
    mockApi.patch.mockResolvedValueOnce(_zone())
    mockApi.get.mockResolvedValue({ zones: [_zone()] })
    const { findByText, getAllByText, getByText, getByRole } = render(
      <SpaceZonesAdmin spaceId="sp_test" />,
    )
    await findByText('Office')
    fireEvent.click(getAllByText('Edit')[0])
    // The stored icon is pre-selected.
    expect(getByRole('button', { name: 'Work' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(getByRole('button', { name: 'No icon' }))
    fireEvent.click(getByText('Save changes'))
    await waitFor(() => expect(mockApi.patch).toHaveBeenCalledTimes(1))
    expect(mockApi.patch.mock.calls[0][1]).toMatchObject({ icon: null })
  })

  it('keeps the stored icon on a PATCH that only renames', async () => {
    mockApi.get.mockResolvedValueOnce({ zones: [_zone({ icon: '🦄' })] })
    mockApi.patch.mockResolvedValueOnce(_zone())
    mockApi.get.mockResolvedValue({ zones: [_zone()] })
    const { findByText, getAllByText, getByText, getByLabelText } = render(
      <SpaceZonesAdmin spaceId="sp_test" />,
    )
    await findByText('Office')
    fireEvent.click(getAllByText('Edit')[0])
    fireEvent.input(getByLabelText('Name'), { target: { value: 'HQ' } })
    fireEvent.click(getByText('Save changes'))
    await waitFor(() => expect(mockApi.patch).toHaveBeenCalledTimes(1))
    expect(mockApi.patch.mock.calls[0][1]).toMatchObject({ name: 'HQ', icon: '🦄' })
  })
})
