/**
 * The household dashboard's Map tab script (socialhome/dashboard/static/map.js).
 *
 * It is a plain classic script served by the dashboard's own port, not
 * part of this SPA — tested here because this is where the JS test
 * runner lives. Each test runs it in a fresh jsdom window (it keeps its
 * state in a closure and listens on the document), with a map box of a
 * fixed size (jsdom has no layout) and the markup the server renders.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { resolve } from 'path'
import { JSDOM } from 'jsdom'

// Outside the client root (vite's fs guard refuses `?raw` there).
const mapScript = readFileSync(
  resolve(__dirname, '../../socialhome/dashboard/static/map.js'),
  'utf-8',
)

const W = 746
const H = 694

interface Who {
  key: string
  lat: number
  lon: number
  radius?: number
  members: string
}

const HOME: Who = { key: 'zone:hz_home', lat: 47.3769, lon: 8.5417, radius: 120, members: 'u-mia u-pascal' }
const SCHOOL: Who = { key: 'zone:hz_school', lat: 47.3812, lon: 8.548, radius: 90, members: 'u-leo' }
const ANNA: Who = { key: 'person:u-anna', lat: 47.3745, lon: 8.537, members: 'u-anna' }

function markup(groups: Who[]): string {
  const bubbles = groups
    .map(
      (g) =>
        `<span class="dash-mp-bubble" data-bubble="${g.key}" data-lat="${g.lat}" data-lon="${g.lon}"` +
        (g.radius ? ` data-radius="${g.radius}" data-in-zone="${g.key.slice(5)}"` : '') +
        (g.key === HOME.key ? ' data-home' : '') +
        ` data-members="${g.members}" data-caption="x" data-label="x">` +
        g.members
          .split(' ')
          .map((id) => `<span class="dash-mp-av" data-face="${id}"></span>`)
          .join('') +
        '</span>',
    )
    .join('')
  return (
    '<div class="dash-view"><div class="dash-mp-map" data-map data-more-one="+1" data-more-other="+{n}">' +
    `<div data-map-tiles></div>${bubbles}</div>` +
    '<button data-map-reset hidden></button>' +
    groups
      .flatMap((g) => g.members.split(' '))
      .map((id) => `<button data-select="${id}" aria-pressed="false"></button>`)
      .join('') +
    '</div>'
  )
}

function boot(groups: Who[]) {
  const dom = new JSDOM(
    `<!doctype html><body data-dash><main data-main>${markup(groups)}</main></body>`,
    { runScripts: 'outside-only', url: 'http://127.0.0.1:8100/map' },
  )
  const win = dom.window
  const size = (value: number) => ({
    configurable: true,
    get(this: Element) {
      return this.hasAttribute('data-map') ? value : 0
    },
  })
  Object.defineProperty(win.HTMLElement.prototype, 'clientWidth', size(W))
  Object.defineProperty(win.HTMLElement.prototype, 'clientHeight', size(H))
  let now = 1_800_000_000_000
  win.Date.now = () => now
  win.eval(mapScript)
  const doc = win.document
  return {
    doc,
    tick(ms: number) {
      now += ms
    },
    /** dashboard.js's 30 s refresh: fresh server markup, then "dash:swap". */
    swap(next: Who[]) {
      doc.querySelector('[data-main]')!.innerHTML = markup(next)
      doc.dispatchEvent(new win.CustomEvent('dash:swap'))
    },
    tiles(): HTMLImageElement[] {
      return Array.from(doc.querySelectorAll<HTMLImageElement>('.dash-mp-tile'))
    },
  }
}

function srcs(imgs: HTMLImageElement[]): string[] {
  return imgs.map((img) => new URL(img.src).pathname).sort()
}

describe('dashboard map.js', () => {
  it('asks only this dashboard for tiles, all on the grid, never closer than zoom 16', () => {
    const map = boot([HOME, SCHOOL, ANNA])
    const tiles = map.tiles()
    expect(tiles.length).toBeGreaterThan(0)
    for (const img of tiles) {
      const url = new URL(img.src)
      expect(url.origin).toBe('http://127.0.0.1:8100')
      const m = url.pathname.match(/^\/tiles\/(\d+)\/(\d+)\/(\d+)$/)
      expect(m).not.toBeNull()
      const [z, x, y] = m!.slice(1).map(Number)
      expect(z).toBeGreaterThanOrEqual(9)
      expect(z).toBeLessThanOrEqual(16)
      expect(x).toBeLessThan(2 ** z)
      expect(y).toBeLessThan(2 ** z)
    }
  })

  it('keeps the frame and the very same tiles on a refresh with the same people', () => {
    const map = boot([HOME, SCHOOL, ANNA])
    const before = map.tiles()
    const left = map.doc.querySelector<HTMLElement>('[data-bubble="person:u-anna"]')!.style.left
    map.swap([HOME, SCHOOL, ANNA])
    const after = map.tiles()
    expect(after).toEqual(before) // the same <img> elements: nothing reloads
    expect(map.doc.querySelector<HTMLElement>('[data-bubble="person:u-anna"]')!.style.left).toBe(left)
  })

  it('keeps the frame while someone moves a little, re-frames when they leave it', () => {
    const map = boot([HOME, SCHOOL, ANNA])
    const before = srcs(map.tiles())
    map.swap([HOME, SCHOOL, { ...ANNA, lat: 47.3747 }])
    expect(srcs(map.tiles())).toEqual(before)
    map.swap([HOME, SCHOOL, { ...ANNA, lat: 47.43, lon: 8.642 }]) // across town
    expect(srcs(map.tiles())).not.toEqual(before)
  })

  it('asks for a failed tile again after a wait that grows while it keeps failing', () => {
    const map = boot([HOME, SCHOOL, ANNA])
    const first = map.tiles()[0]
    const src = first.src
    first.dispatchEvent(new first.ownerDocument.defaultView!.Event('error'))
    expect(first.hidden).toBe(true)

    map.swap([HOME, SCHOOL, ANNA]) // too soon: the gap stays
    expect(map.tiles()).toContain(first)

    map.tick(30_000)
    map.swap([HOME, SCHOOL, ANNA])
    const again = map.tiles().find((img) => img.src === src)!
    expect(again).not.toBe(first)
    expect(again.hidden).toBe(false)

    // It fails again: now the wait is doubled.
    again.dispatchEvent(new again.ownerDocument.defaultView!.Event('error'))
    map.tick(30_000)
    map.swap([HOME, SCHOOL, ANNA])
    expect(map.tiles()).toContain(again)
    map.tick(30_000)
    map.swap([HOME, SCHOOL, ANNA])
    expect(map.tiles()).not.toContain(again)
  })

  it('selects a person from the list, zooms to 16 and goes back on a second tap', () => {
    const map = boot([HOME, SCHOOL, ANNA])
    const overview = srcs(map.tiles())
    const row = map.doc.querySelector<HTMLButtonElement>('[data-select="u-anna"]')!
    row.click()
    expect(row.getAttribute('aria-pressed')).toBe('true')
    expect(map.tiles().every((img) => img.src.includes('/tiles/16/'))).toBe(true)
    expect(map.doc.querySelector<HTMLButtonElement>('[data-map-reset]')!.hidden).toBe(false)
    row.click()
    expect(row.getAttribute('aria-pressed')).toBe('false')
    expect(srcs(map.tiles())).toEqual(overview)
  })
})
