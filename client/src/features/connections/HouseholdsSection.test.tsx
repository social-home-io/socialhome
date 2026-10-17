/**
 * HouseholdsSection — the map view loads FederationMap on demand, and
 * loading this section must not change how the rest of the app handles
 * events. ``preact/compat`` patches ``options.vnode`` globally the moment
 * it is imported (``onChange`` on text inputs becomes ``oninput``,
 * ``onFocus``/``onBlur`` become the bubbling ``focusin``/``focusout``,
 * numeric styles get ``px``), so a lazy route that pulled it in changed
 * every form in the app after the first visit to Connections.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/preact'
import { Component, type ComponentChildren } from 'preact'

vi.mock('@/i18n/i18n', () => ({ t: (key: string) => key }))

const { mapLoad } = vi.hoisted(() => ({
  mapLoad: { fail: false as boolean },
}))
vi.mock('./FederationMap', () => {
  // A throwing factory makes the dynamic import reject, like a chunk
  // that failed to download.
  if (mapLoad.fail) throw new Error('chunk failed')
  return { default: (p: { rows?: unknown[] }) => <div data-testid="map-stub">{p.rows?.length}</div> }
})

import { HouseholdsSection } from './HouseholdsSection'
import type { Household } from './model'

const household = {
  conn: { instance_id: 'peer-1', display_name: 'Birch', home_lat: 1, home_lon: 2 },
  status: 'online',
} as unknown as Household

const props = {
  view: 'map' as const,
  onView: () => {},
  loading: false,
  error: false,
  isAdmin: false,
  households: [household],
  pending: [],
  spaceOnly: [],
  compat: new Map(),
  onCancelPending: () => {},
  onRestartPending: () => {},
  onRetry: () => {},
}

class Boundary extends Component<{ children: ComponentChildren }, { error: string | null }> {
  state = { error: null as string | null }
  static getDerivedStateFromError(e: Error) { return { error: e.message } }
  render() {
    return this.state.error ? <p data-testid="boundary">{this.state.error}</p> : this.props.children
  }
}

describe('HouseholdsSection — map chunk fails to load', () => {
  // Runs first: a loaded map is cached for the rest of the file. The
  // loader itself keeps no failure (Chrome's module map still does, so in
  // the browser a failed chunk stays failed until reload, as with lazy).
  it('shows the loading line, hands a load error to the error boundary, and retries on the next mount', async () => {
    mapLoad.fail = true
    const first = render(<Boundary><HouseholdsSection {...props} /></Boundary>)
    expect(screen.getByText('connections.loading_map')).toBeTruthy()
    expect(await screen.findByTestId('boundary')).toBeTruthy()
    first.unmount()
    mapLoad.fail = false
    render(<HouseholdsSection {...props} />)
    expect(screen.getByText('connections.loading_map')).toBeTruthy()
    expect(await screen.findByTestId('map-stub')).toBeTruthy()
    expect(screen.queryByText('connections.loading_map')).toBeNull()
    expect(screen.getByTestId('map-stub').textContent).toBe('1')
  })
})

describe('HouseholdsSection', () => {
  beforeEach(() => { mapLoad.fail = false })

  it('leaves Preact core event semantics alone (no preact/compat side effects)', () => {
    const onChange = vi.fn()
    const onFocus = vi.fn()
    render(
      <div onFocus={onFocus}>
        <input type="time" data-testid="t" value="22:00" onChange={onChange} />
      </div>,
    )
    const input = screen.getByTestId('t')
    // A time input commits on ``change``; ``input`` alone must not save.
    fireEvent.input(input)
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.change(input)
    expect(onChange).toHaveBeenCalledTimes(1)
    // ``onFocus`` on a container is the non-bubbling ``focus`` event.
    input.focus()
    expect(onFocus).not.toHaveBeenCalled()
  })

  it('leaves numeric inline styles unitless, as Preact 11 core renders them', () => {
    // compat would turn this into ``width: 10px``.
    render(<div data-testid="s" style={{ width: 10, zIndex: 3 }} />)
    const s = screen.getByTestId('s')
    expect(s.style.width).toBe('')
    expect(s.style.zIndex).toBe('3')
  })

  it('renders the map straight away once it has loaded before', () => {
    render(<HouseholdsSection {...props} />)
    expect(screen.getByTestId('map-stub')).toBeTruthy()
    expect(screen.queryByText('connections.loading_map')).toBeNull()
  })
})
