/**
 * MapOverview — the People / Zones panel beside (or, on phones, over)
 * the person map: tabs that hide when empty, rows, the shared selection
 * with the map, focus on open / Back, and the sheet's grabber.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render } from '@testing-library/preact'
import { useState } from 'preact/hooks'
import { MapOverview, overlayInsets, type OverviewPerson, type OverviewZone } from './MapOverview'
import type { MapInsets } from './LocationMap'

const NOW = Date.parse('2026-10-13T12:00:00Z')

const anna: OverviewPerson = {
  id: 'u-anna', user_id: 'u-anna', name: 'Anna', avatar_url: 'api/users/u-anna/picture?v=1',
  status: 'Home', zone_id: 'z-home', updated_at: '2026-10-13T11:55:00Z',
  online: 'online',
}
const ben: OverviewPerson = {
  id: 'u-ben', name: 'Ben', status: 'School', zone_id: 'z-school',
  updated_at: '2026-10-13 10:00:00', online: 'offline', last_seen_at: '2026-10-13T11:30:00Z',
}
const cara: OverviewPerson = {
  id: 'u-cara', name: 'Cara', status: 'Away', zone_id: null, updated_at: null,
}
const zones: OverviewZone[] = [
  { id: 'z-school', name: 'School', radius_m: 250, color: null, icon: '🏫' },
  { id: 'z-work', name: 'work', radius_m: 1200, color: '#3366cc', icon: null },
  { id: 'z-home', name: 'Home', radius_m: 100, color: null, icon: '🏠', is_home: true },
]

/** The overview with its own selection state, as a page holds it. */
function Harness({
  people = [anna, ben, cara], zoneList = zones, initial = null, onMapInsets,
}: {
  people?: OverviewPerson[], zoneList?: OverviewZone[], initial?: string | null,
  onMapInsets?: (i: MapInsets) => void,
}) {
  const [sel, setSel] = useState<string | null>(initial)
  return (
    <>
      {/* Stands in for a marker click on the map. */}
      <button type="button" data-testid="map-pick-ben" onClick={() => setSel('u-ben')}>map</button>
      <button type="button" data-testid="map-pick-school" onClick={() => setSel('zone:z-school')}>map</button>
      <MapOverview
        people={people}
        zones={zoneList}
        selectedId={sel}
        onSelect={setSel}
        renderMap={(insets) => {
          onMapInsets?.(insets)
          return <div data-testid="map" data-selected={sel ?? ''} />
        }}
      />
    </>
  )
}

const tabButtons = (c: Element) =>
  Array.from(c.querySelectorAll<HTMLButtonElement>('.sh-map-overview__tabs button'))
const rows = (c: Element) =>
  Array.from(c.querySelectorAll<HTMLButtonElement>('.sh-map-overview__body .sh-map-overview__row'))

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
})
afterEach(() => { vi.useRealTimers() })

describe('MapOverview tabs', () => {
  it('shows People and Zones with counts, People first', () => {
    const { container } = render(<Harness />)
    expect(tabButtons(container).map((b) => b.textContent)).toEqual(['People · 3', 'Zones · 3'])
    expect(tabButtons(container)[0].getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('[role="group"]')!.getAttribute('aria-label')).toBeTruthy()
  })

  it('hides an empty tab and lands on the one with items', () => {
    const { container, getByText } = render(<Harness people={[]} />)
    expect(tabButtons(container)).toHaveLength(0)
    expect(getByText('Zones', { selector: 'h3' })).toBeTruthy()
    expect(rows(container).map((r) => r.dataset.overviewId)).toEqual([
      'zone:z-home', 'zone:z-school', 'zone:z-work',
    ])
  })

  it('falls back to the first tab with items when the chosen tab empties', () => {
    const { container, rerender } = render(<Harness />)
    fireEvent.click(tabButtons(container)[1])
    expect(rows(container)[0].dataset.overviewId).toBe('zone:z-home')
    rerender(<Harness zoneList={[]} />)
    expect(tabButtons(container)).toHaveLength(0)
    expect(rows(container).map((r) => r.dataset.overviewId)).toEqual(['u-anna', 'u-ben', 'u-cara'])
  })

  it('draws only the map when there is nothing to list', () => {
    const { container, getByTestId } = render(<Harness people={[]} zoneList={[]} />)
    expect(getByTestId('map')).toBeTruthy()
    expect(container.querySelector('.sh-map-overview__panel')).toBeNull()
  })
})

describe('MapOverview rows', () => {
  it('lists people by name with their place and last update', () => {
    const { container } = render(<Harness people={[cara, ben, anna]} />)
    const r = rows(container)
    expect(r.map((x) => x.querySelector('.sh-map-overview__row-name')!.textContent))
      .toEqual(['Anna', 'Ben', 'Cara'])
    expect(r[0].querySelector('.sh-map-overview__row-sub')!.textContent).toBe('Home · 5 min ago')
    // A naive SQLite stamp is UTC.
    expect(r[1].querySelector('.sh-map-overview__row-sub')!.textContent).toBe('School · 2h ago')
    expect(r[2].querySelector('.sh-map-overview__row-sub')!.textContent).toBe('Away')
  })

  it('frames each avatar in the person colour, photo over initials', () => {
    const { container } = render(<Harness />)
    const avatar = rows(container)[0].querySelector('.sh-map-avatar')!
    expect(avatar.className).toMatch(/sh-timetable-c--/)
    expect(avatar.querySelector('.sh-map-avatar__initials')!.textContent).toBe('A')
    const img = avatar.querySelector('img')!
    expect(img.getAttribute('src')).toBe('api/users/u-anna/picture?v=1')
    fireEvent.error(img)
    expect(avatar.querySelector('img')).toBeNull()
  })

  it('lists zones Home first with icon, radius and who is there', () => {
    const { container } = render(<Harness />)
    fireEvent.click(tabButtons(container)[1])
    const r = rows(container)
    expect(r.map((x) => x.querySelector('.sh-map-overview__row-name')!.textContent))
      .toEqual(['Home', 'School', 'work'])
    expect(r.map((x) => x.querySelector('.sh-map-overview__zone-badge')!.textContent))
      .toEqual(['🏠', '🏫', 'W'])
    expect(r.map((x) => x.querySelector('.sh-map-overview__row-sub')!.textContent))
      .toEqual(['100 m', '250 m', '1.2 km'])
    expect(r.map((x) => x.querySelector('.sh-map-overview__count')!.textContent))
      .toEqual(['1 person', '1 person', 'Nobody'])
  })

  it('counts several people in one zone', () => {
    const { container } = render(<Harness people={[anna, { ...cara, zone_id: 'z-home' }]} />)
    fireEvent.click(tabButtons(container)[1])
    expect(rows(container)[0].querySelector('.sh-map-overview__count')!.textContent).toBe('2 people')
  })
})

describe('MapOverview selection', () => {
  it('selects a person from a row and shows their detail, focus on the heading', () => {
    const { container, getByTestId } = render(<Harness />)
    fireEvent.click(rows(container)[1])
    expect(getByTestId('map').dataset.selected).toBe('u-ben')
    const heading = container.querySelector('h3.sh-map-overview__title')!
    expect(heading.textContent).toBe('Ben')
    expect(document.activeElement).toBe(heading)
    const facts = container.querySelector('.sh-map-overview__facts')!.textContent!
    expect(facts).toContain('School')
    expect(facts).toContain('2h ago')
    expect(facts).toContain('Last seen 30 min ago')
  })

  it('shows online people as online', () => {
    const { container } = render(<Harness initial="u-anna" />)
    expect(container.querySelector('.sh-map-overview__facts')!.textContent).toContain('Online')
  })

  it('selects a zone from a row and lists who is there now', () => {
    const { container, getByTestId } = render(<Harness people={[anna, { ...cara, zone_id: 'z-home' }]} />)
    fireEvent.click(tabButtons(container)[1])
    fireEvent.click(rows(container)[0])
    expect(getByTestId('map').dataset.selected).toBe('zone:z-home')
    expect(container.querySelector('h3.sh-map-overview__title')!.textContent).toBe('Home')
    expect(container.querySelector('.sh-map-overview__zone-meta')!.textContent)
      .toBe('Radius 100 m · 2 people')
    expect(rows(container).map((r) => r.dataset.overviewId)).toEqual(['u-anna', 'u-cara'])
  })

  it('says so when nobody is in the selected zone', () => {
    const { container, getByText } = render(<Harness initial="zone:z-work" />)
    expect(container.querySelector('h3.sh-map-overview__title')!.textContent).toBe('work')
    expect(getByText('Nobody is here right now.')).toBeTruthy()
  })

  it('opens the detail for a marker picked on the map, without stealing focus', () => {
    const { container, getByTestId } = render(<Harness />)
    const marker = getByTestId('map-pick-school')
    marker.focus()
    fireEvent.click(marker)
    expect(container.querySelector('h3.sh-map-overview__title')!.textContent).toBe('School')
    expect(document.activeElement).toBe(marker)
    fireEvent.click(getByTestId('map-pick-ben'))
    expect(container.querySelector('h3.sh-map-overview__title')!.textContent).toBe('Ben')
  })

  it('goes from a zone to a person in it', () => {
    const { container, getByTestId } = render(<Harness initial="zone:z-school" />)
    fireEvent.click(rows(container)[0])
    expect(getByTestId('map').dataset.selected).toBe('u-ben')
    expect(container.querySelector('h3.sh-map-overview__title')!.textContent).toBe('Ben')
  })

  it('Back clears the selection and puts focus back on the row', () => {
    const { container, getByTestId, getByRole } = render(<Harness />)
    fireEvent.click(tabButtons(container)[1])
    fireEvent.click(rows(container)[1])
    fireEvent.click(getByRole('button', { name: 'Back to zones' }))
    expect(getByTestId('map').dataset.selected).toBe('')
    const focused = document.activeElement as HTMLElement
    expect(focused.dataset.overviewId).toBe('zone:z-school')
    // Still on the Zones tab.
    expect(tabButtons(container)[1].getAttribute('aria-pressed')).toBe('true')
  })

  it('Back from a person picked on the map lands on their row in People', () => {
    const { container, getByTestId, getByRole } = render(<Harness />)
    fireEvent.click(tabButtons(container)[1])
    fireEvent.click(getByTestId('map-pick-ben'))
    fireEvent.click(getByRole('button', { name: 'Back to people' }))
    expect((document.activeElement as HTMLElement).dataset.overviewId).toBe('u-ben')
    expect(tabButtons(container)[0].getAttribute('aria-pressed')).toBe('true')
  })

  it('closes the detail when the selected person leaves the list', () => {
    const { container, rerender } = render(<Harness initial="u-cara" />)
    expect(container.querySelector('h3.sh-map-overview__title')).not.toBeNull()
    rerender(<Harness people={[anna, ben]} initial="u-cara" />)
    expect(container.querySelector('h3.sh-map-overview__title')).toBeNull()
  })
})

describe('MapOverview sheet', () => {
  it('starts collapsed and the grabber toggles it with aria-expanded', () => {
    const { container, getByRole } = render(<Harness />)
    const grabber = getByRole('button', { name: 'Show list' })
    expect(grabber.getAttribute('aria-expanded')).toBe('false')
    const body = container.querySelector('.sh-map-overview__body')!
    expect(grabber.getAttribute('aria-controls')).toBe(body.id)
    expect(container.querySelector('.sh-map-overview')!.classList.contains('is-expanded')).toBe(false)
    fireEvent.click(grabber)
    expect(grabber.getAttribute('aria-expanded')).toBe('true')
    expect(grabber.getAttribute('aria-label')).toBe('Hide list')
    expect(container.querySelector('.sh-map-overview')!.classList.contains('is-expanded')).toBe(true)
  })

  it('opens when a tab is picked or something is selected', () => {
    const first = render(<Harness />)
    fireEvent.click(tabButtons(first.container)[1])
    expect(first.container.querySelector('.sh-map-overview')!.classList.contains('is-expanded')).toBe(true)
    first.unmount()
    const second = render(<Harness />)
    fireEvent.click(second.getByTestId('map-pick-ben'))
    expect(second.container.querySelector('.sh-map-overview')!.classList.contains('is-expanded')).toBe(true)
  })

  it('hands the map no insets while nothing covers it', () => {
    const seen: MapInsets[] = []
    render(<Harness onMapInsets={(i) => seen.push(i)} />)
    expect(seen.at(-1)).toEqual({})
  })
})

describe('overlayInsets', () => {
  const map = { top: 0, left: 0, right: 400, bottom: 380 }

  it('reports a bottom sheet as the strip it covers', () => {
    expect(overlayInsets(map, { top: 300, left: 0, right: 400, bottom: 380 })).toEqual({ bottom: 80 })
  })

  it('reports nothing for a panel beside the map', () => {
    expect(overlayInsets(map, { top: 0, left: 412, right: 700, bottom: 380 })).toEqual({})
  })

  it('reports a panel floating over the right edge as a right inset', () => {
    expect(overlayInsets(map, { top: 10, left: 250, right: 390, bottom: 370 })).toEqual({ right: 150 })
  })

  it('reports nothing for an unmeasured layout', () => {
    const zero = { top: 0, left: 0, right: 0, bottom: 0 }
    expect(overlayInsets(zero, zero)).toEqual({})
  })
})
