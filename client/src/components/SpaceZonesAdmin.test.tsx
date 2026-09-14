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
    bindTooltip: () => layer,
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
vi.mock('@/i18n/i18n', () => ({
  t: (k: string) => k,
  locale: { value: 'en' },
}))

import { SpaceZonesAdmin } from './SpaceZonesAdmin'

const _zone = (over: Partial<any> = {}) => ({
  id: 'z_office',
  space_id: 'sp_test',
  name: 'Office',
  latitude: 47.3769,
  longitude: 8.5417,
  radius_m: 150,
  color: '#3b82f6',
  created_by: 'u_admin',
  created_at: '2026-04-27T12:00:00Z',
  updated_at: '2026-04-27T12:00:00Z',
  ...over,
})


describe('SpaceZonesAdmin', () => {
  beforeEach(() => {
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

  it('deletes through ConfirmDialog', async () => {
    mockApi.get.mockResolvedValueOnce({ zones: [_zone()] })
    mockApi.delete.mockResolvedValueOnce(undefined)
    mockApi.get.mockResolvedValue({ zones: [] })

    const { findByText, getAllByText } = render(
      <SpaceZonesAdmin spaceId="sp_test" />,
    )
    await findByText('Office')
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
    await findByText(/Map unavailable/)
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

    await findByText(/Map unavailable/)
  })
})
