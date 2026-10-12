/**
 * FederationMap component tests.
 *
 * Leaflet is mocked so jsdom doesn't have to implement a real canvas +
 * tile fetch pipeline.  Assertions focus on the JSX output (container
 * element, "Not on map" footer, peer listings) rather than Leaflet API
 * calls.
 */
import { describe, test, expect, beforeEach, vi, beforeAll } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/preact'

// ResizeObserver is used by FederationMap to handle hidden-tab reflow.
beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
})

// Stub the CSS import so Vite/jsdom doesn't choke on it.
vi.mock('leaflet/dist/leaflet.css', () => ({}))

// Vanilla-Leaflet mock — returns chainable stubs so the useEffect
// initialisation doesn't throw.
vi.mock('leaflet', () => {
  const layerGroupStub = {
    addTo: vi.fn().mockReturnThis(),
    clearLayers: vi.fn().mockReturnThis(),
    removeLayer: vi.fn().mockReturnThis(),
    getBounds: vi.fn(() => ({ pad: vi.fn(() => ({})) })),
  }
  const markerStub = {
    addTo: vi.fn().mockReturnThis(),
    bindPopup: vi.fn().mockReturnThis(),
    getLatLng: vi.fn(() => ({ lat: 0, lng: 0 })),
    setLatLng: vi.fn().mockReturnThis(),
    setIcon: vi.fn().mockReturnThis(),
    setPopupContent: vi.fn().mockReturnThis(),
    getElement: vi.fn(() => undefined),
  }
  const mapStub = {
    remove: vi.fn(),
    invalidateSize: vi.fn(),
    setView: vi.fn().mockReturnThis(),
    fitBounds: vi.fn().mockReturnThis(),
    addTo: vi.fn().mockReturnThis(),
  }
  return {
    default: {
      map: vi.fn(() => mapStub),
      tileLayer: vi.fn(() => ({ addTo: vi.fn() })),
      layerGroup: vi.fn(() => layerGroupStub),
      marker: vi.fn(() => markerStub),
      divIcon: vi.fn(() => ({})),
      featureGroup: vi.fn(() => layerGroupStub),
      latLngBounds: vi.fn(() => ({ pad: vi.fn(() => ({})) })),
    },
  }
})

// Tiles come from the backend proxy (``/api/map/config``) through the
// shared helper; stub it so the test needs no fetch.
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

import L from 'leaflet'
import { connections, selfLat, selfLon } from '@/store/connections'
import FederationMap from './FederationMap'

describe('FederationMap', () => {
  beforeEach(() => {
    selfLat.value = null
    selfLon.value = null
    connections.value = []
    addTileLayer.mockReset()
    addTileLayer.mockResolvedValue(undefined)
  })

  test('renders the map container element', () => {
    render(<FederationMap />)
    expect(screen.getByTestId('sh-federation-map')).toBeDefined()
  })

  test('renders without crashing when no coords are present', () => {
    render(<FederationMap />)
    // No footer when no peers
    expect(screen.queryByText('Not on map')).toBeNull()
  })

  test('shows "Not on map" footer for peers without home coords', () => {
    connections.value = [
      {
        instance_id: 'p1',
        display_name: 'Bob',
        reachable: true,
        home_lat: null,
        home_lon: null,
      },
    ]
    render(<FederationMap />)
    expect(screen.getByText('Not on map')).toBeDefined()
    expect(screen.getByText('Bob')).toBeDefined()
    expect(screen.getByText('Paired, but no home location yet.')).toBeDefined()
  })

  test('omits the footer when the host page names off-map peers itself', () => {
    connections.value = [
      { instance_id: 'p1', display_name: 'Bob', reachable: true, home_lat: null, home_lon: null },
    ]
    render(<FederationMap showOffMapFooter={false} />)
    expect(screen.queryByText('Not on map')).toBeNull()
  })

  test('omits the footer when every peer has coords', () => {
    connections.value = [
      {
        instance_id: 'p1',
        display_name: 'Bob',
        reachable: true,
        home_lat: 52.52,
        home_lon: 13.40,
      },
    ]
    render(<FederationMap />)
    expect(screen.queryByText('Not on map')).toBeNull()
  })

  test('lists multiple peers without coords in the footer', () => {
    connections.value = [
      {
        instance_id: 'p1',
        display_name: 'Alice',
        reachable: true,
        home_lat: null,
        home_lon: null,
      },
      {
        instance_id: 'p2',
        display_name: 'Carol',
        reachable: true,
        home_lat: null,
        home_lon: null,
      },
      {
        instance_id: 'p3',
        display_name: 'Dave',
        reachable: true,
        home_lat: 48.85,
        home_lon: 2.35,
      },
    ]
    render(<FederationMap />)
    expect(screen.getByText('Alice')).toBeDefined()
    expect(screen.getByText('Carol')).toBeDefined()
    // Dave has coords — should NOT appear in the footer
    // (he will be on the map, not in the footer list)
    const footer = screen.getByTestId('sh-federation-map').querySelector(
      '.sh-federation-map__footer',
    )
    expect(footer).not.toBeNull()
    // Footer rows: Alice + Carol only
    const rows = footer!.querySelectorAll('.sh-federation-map__footer-row')
    expect(rows.length).toBe(2)
  })

  test('uses instance_id as fallback when display_name is absent', () => {
    connections.value = [
      {
        instance_id: 'some-uuid',
        display_name: undefined,
        reachable: true,
        home_lat: null,
        home_lon: null,
      } as never,
    ]
    render(<FederationMap />)
    expect(screen.getByText('some-uuid')).toBeDefined()
  })

  test('shows a tiles-unavailable message when the tile config fails', async () => {
    addTileLayer.mockRejectedValue(new Error('API 502: /api/map/config'))
    render(<FederationMap />)
    await waitFor(() => {
      expect(screen.getByText(/Map unavailable/)).toBeDefined()
    })
  })
  test('surfaces a tile-load failure the same way as a config failure', async () => {
    let report: (() => void) | undefined
    addTileLayer.mockImplementation((_map, _cancelled, onError) => {
      report = onError
      return Promise.resolve()
    })
    render(<FederationMap />)

    await waitFor(() => { expect(report).toBeTypeOf('function') })
    act(() => { report!() })

    await waitFor(() => {
      expect(screen.getByText(/Map unavailable/)).toBeDefined()
    })
  })

  test('peer-supplied name and instance id stay inert in the pin and popup HTML', () => {
    const marker = L.marker([0, 0])
    vi.mocked(marker.bindPopup).mockClear()
    vi.mocked(L.divIcon).mockClear()
    const hostileId = '"><img src=x onerror="window.__xss=1">'
    connections.value = [
      {
        instance_id: hostileId,
        display_name: '<img src=x>',
        reachable: true,
        home_lat: 52.52,
        home_lon: 13.40,
      },
    ]
    render(<FederationMap onManage={() => {}} />)
    const pinHtml = vi.mocked(L.divIcon).mock.calls[0][0]!.html as string
    // Mount the pin the way Leaflet does: ``innerHTML`` on the overlay node.
    const pin = document.createElement('div')
    pin.innerHTML = pinHtml
    expect(pin.querySelector('img')).toBeNull()
    expect(pin.querySelector('.sh-conn-av')!.textContent).toBe('<')
    expect(pin.querySelector('.sh-conn-pin__label')!.textContent).toBe('<img src=x>')
    // The popup is a DOM node built from text, never an HTML string.
    const popup = vi.mocked(marker.bindPopup).mock.calls[0][0] as HTMLElement
    expect(popup).toBeInstanceOf(HTMLElement)
    expect(popup.querySelector('img')).toBeNull()
    expect(popup.querySelector('strong')!.textContent).toBe('<img src=x>')
  })

  function pinFor(peer: Record<string, unknown>, props: Record<string, unknown> = {}) {
    const marker = L.marker([0, 0])
    vi.mocked(marker.bindPopup).mockClear()
    vi.mocked(L.divIcon).mockClear()
    vi.mocked(L.marker).mockClear()
    connections.value = [{ home_lat: 52.52, home_lon: 13.40, reachable: true, ...peer } as never]
    render(<FederationMap {...props} />)
    const opts = vi.mocked(L.divIcon).mock.calls[0][0]!
    const pin = document.createElement('div')
    pin.innerHTML = opts.html as string
    const popup = vi.mocked(marker.bindPopup).mock.calls[0][0] as HTMLElement
    const markerOpts = vi.mocked(L.marker).mock.calls.at(-1)![1] as { title?: string }
    return { opts, pin, popup, markerOpts }
  }

  test('a pin is the household\'s ConnAvatar, anchored at the avatar centre, with a name chip', () => {
    const { opts, pin, markerOpts } = pinFor({ instance_id: 'peer-1', display_name: 'Beta House', status: 'confirmed' })
    expect(opts.iconSize).toEqual([44, 44])
    expect(opts.iconAnchor).toEqual([22, 22])
    const av = pin.querySelector('.sh-conn-av')!
    expect(av.className).toMatch(/sh-conn-av--(moss|hearth|honey|ink)/)
    expect(av.getAttribute('data-s')).toBe('ok')
    expect(av.textContent).toBe('B')
    expect(pin.querySelector('.sh-conn-badge')).toBeNull()
    expect(pin.querySelector('.sh-conn-pin__label')!.textContent).toBe('Beta House')
    expect(markerOpts.title).toBe('Beta House · Connected')
  })

  test.each([
    [{ reachable: false, status: 'confirmed' }, 'down', 'sh-conn-badge--down', 'Unreachable'],
    [{ status: 'pending_sent' }, 'pending', 'sh-conn-badge--pending', 'Waiting for the other household'],
  ])('%o → ring %s with a shape badge, and the status in the popup', (over, ring, badge, label) => {
    const { pin, popup } = pinFor({ instance_id: 'p', display_name: 'P', ...over })
    expect(pin.querySelector('.sh-conn-av')!.getAttribute('data-s')).toBe(ring)
    expect(pin.querySelector(`.${badge}`)).not.toBeNull()
    expect(popup.querySelector('.sh-fed-popup__status')!.textContent).toBe(label)
  })

  test('the page decides "needs an update" (admin-only data) through statusOf', () => {
    const { pin, popup } = pinFor(
      { instance_id: 'p', display_name: 'P', status: 'confirmed' },
      { statusOf: () => 'warn' },
    )
    expect(pin.querySelector('.sh-conn-av')!.getAttribute('data-s')).toBe('warn')
    expect(pin.querySelector('.sh-conn-badge--warn')).not.toBeNull()
    expect(popup.querySelector('.sh-fed-popup__status')!.textContent).toBe('Needs an update')
  })

  test('a space-only household gets no status ring', () => {
    const { pin } = pinFor({ instance_id: 'p', display_name: 'P', status: 'confirmed', source: 'space_session' })
    expect(pin.querySelector('.sh-conn-av')!.hasAttribute('data-s')).toBe(false)
  })

  test.each([
    ['rtc', 'Direct'],
    ['https', 'Over the internet'],
    ['gfs_relay', 'Through the GFS'],
  ] as const)('a %s peer names its transport in the popup, in words', (transport, words) => {
    const { popup } = pinFor({ instance_id: 'p', display_name: 'P', status: 'confirmed', transport })
    expect(popup.textContent).toContain(words)
    expect(popup.textContent).not.toMatch(/[⚡🌐☁]/u)
  })

  test('a transport/status update edits pins in place and never refits the view', async () => {
    const map = L.map(document.createElement('div'))
    const marker = L.marker([0, 0])
    const peer = { instance_id: 'p1', display_name: 'P', reachable: true, status: 'confirmed', home_lat: 1, home_lon: 2 }
    connections.value = [peer, { ...peer, instance_id: 'p2', home_lat: 3, home_lon: 4 }]
    render(<FederationMap />)
    const created = vi.mocked(L.marker).mock.calls.length
    const fits = vi.mocked(map.fitBounds).mock.calls.length + vi.mocked(map.setView).mock.calls.length
    vi.mocked(marker.setIcon).mockClear()
    act(() => {
      connections.value = [{ ...peer, transport: 'https', reachable: false } as never, connections.value[1]]
    })
    await waitFor(() => expect(marker.setIcon).toHaveBeenCalled())
    expect(vi.mocked(L.marker).mock.calls.length).toBe(created)
    expect(vi.mocked(map.fitBounds).mock.calls.length + vi.mocked(map.setView).mock.calls.length).toBe(fits)
  })

  test('a household joining the map refits; one leaving is removed', async () => {
    const map = L.map(document.createElement('div'))
    const layer = L.layerGroup()
    const peer = { instance_id: 'p1', display_name: 'P', reachable: true, status: 'confirmed', home_lat: 1, home_lon: 2 }
    connections.value = [peer]
    render(<FederationMap />)
    const fits = vi.mocked(map.fitBounds).mock.calls.length + vi.mocked(map.setView).mock.calls.length
    act(() => { connections.value = [peer, { ...peer, instance_id: 'p2' }] })
    await waitFor(() => {
      expect(vi.mocked(map.fitBounds).mock.calls.length + vi.mocked(map.setView).mock.calls.length).toBe(fits + 1)
    })
    vi.mocked(layer.removeLayer).mockClear()
    act(() => { connections.value = [peer] })
    await waitFor(() => expect(layer.removeLayer).toHaveBeenCalledOnce())
  })

  test('pins only the rows it is given', () => {
    vi.mocked(L.divIcon).mockClear()
    const peer = { instance_id: 'p1', display_name: 'P', reachable: true, status: 'confirmed', home_lat: 1, home_lon: 2 }
    connections.value = [peer, { ...peer, instance_id: 'z', display_name: 'Zeta', status: 'pending_sent' }]
    render(<FederationMap rows={[peer]} />)
    expect(vi.mocked(L.divIcon).mock.calls).toHaveLength(1)
  })

  test('this household is a hearth "You" disc', () => {
    selfLat.value = 48.1
    selfLon.value = 11.6
    vi.mocked(L.divIcon).mockClear()
    connections.value = []
    render(<FederationMap />)
    const opts = vi.mocked(L.divIcon).mock.calls[0][0]!
    const pin = document.createElement('div')
    pin.innerHTML = opts.html as string
    expect(pin.querySelector('.sh-conn-pin__you')!.textContent).toBe('You')
    expect(opts.iconAnchor).toEqual([24, 24])
  })

  test('Manage in a peer popup is a real button that opens that peer', () => {
    const marker = L.marker([0, 0])
    vi.mocked(marker.bindPopup).mockClear()
    const peer = {
      instance_id: 'peer-1', display_name: 'Bob', reachable: true,
      status: 'confirmed', home_lat: 52.52, home_lon: 13.40,
    }
    connections.value = [peer]
    const onManage = vi.fn()
    render(<FederationMap onManage={onManage} />)
    const popup = vi.mocked(marker.bindPopup).mock.calls[0][0] as HTMLElement
    // No ``href="#"`` — under ingress it resolves against <base href>
    // and navigates the iframe away.
    expect(popup.querySelector('a')).toBeNull()
    const btn = popup.querySelector('button') as HTMLButtonElement
    expect(btn.type).toBe('button')
    expect(btn.textContent).toBe('Manage')
    btn.click()
    expect(onManage).toHaveBeenCalledOnce()
    expect(onManage.mock.calls[0][0].instance_id).toBe('peer-1')
  })

  test('no Manage control without a handler or when the peer is not manageable', () => {
    const marker = L.marker([0, 0])
    connections.value = [{
      instance_id: 'peer-1', display_name: 'Bob', reachable: true,
      home_lat: 52.52, home_lon: 13.40,
    }]
    vi.mocked(marker.bindPopup).mockClear()
    render(<FederationMap />)
    let popup = vi.mocked(marker.bindPopup).mock.calls[0][0] as HTMLElement
    expect(popup.querySelector('button, a')).toBeNull()

    vi.mocked(marker.bindPopup).mockClear()
    render(<FederationMap onManage={vi.fn()} canManage={() => false} />)
    popup = vi.mocked(marker.bindPopup).mock.calls[0][0] as HTMLElement
    expect(popup.querySelector('button, a')).toBeNull()
  })
})
