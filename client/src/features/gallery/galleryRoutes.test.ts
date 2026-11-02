import { describe, it, expect } from 'vitest'
import { TRASH_PATH, backLeadsTo, galleryPath, markBackTo, parseGallerySub } from './galleryRoutes'

describe('galleryRoutes', () => {
  it('builds the household and the space addresses', () => {
    expect(galleryPath()).toBe('/gallery')
    expect(galleryPath(null, 'a1')).toBe('/gallery/albums/a1')
    expect(galleryPath(undefined, 'a1', 'i1')).toBe('/gallery/albums/a1/i1')
    expect(galleryPath('s1')).toBe('/spaces/s1/gallery')
    expect(galleryPath('s1', 'a1', 'i1')).toBe('/spaces/s1/gallery/albums/a1/i1')
    // An item without its album has no address of its own.
    expect(galleryPath(null, null, 'i1')).toBe('/gallery')
  })

  it('reads the rest after /gallery', () => {
    expect(parseGallerySub(undefined)).toEqual({ albumId: null, itemId: null, valid: true })
    expect(parseGallerySub('albums/a1')).toEqual({ albumId: 'a1', itemId: null, valid: true })
    expect(parseGallerySub('albums/a1/i1')).toEqual({ albumId: 'a1', itemId: 'i1', valid: true })
  })

  it('knows the household trash (/gallery/trash) — under the ingress prefix too, it is an in-app path', () => {
    expect(parseGallerySub('trash')).toEqual({ albumId: null, itemId: null, valid: true, trash: true })
    expect(parseGallerySub('trash/x').valid).toBe(false)
    expect(TRASH_PATH).toBe('/gallery/trash')
  })

  it('an address it does not know is not valid', () => {
    for (const bad of ['albums', 'photos/x', 'albums/a1/i1/extra', 'xyz']) {
      expect(parseGallerySub(bad).valid).toBe(false)
    }
  })

  it('notes on the history entry what a step back leads to, keeping its other state', () => {
    history.replaceState({ other: 1 }, '', '/gallery/albums/a1')
    expect(backLeadsTo('/gallery')).toBe(false)
    markBackTo('/gallery')
    expect(backLeadsTo('/gallery')).toBe(true)
    expect(backLeadsTo('/gallery/albums/a1')).toBe(false)
    expect((history.state as Record<string, unknown>).other).toBe(1)
    expect(location.pathname).toBe('/gallery/albums/a1')
  })

  it('round-trips', () => {
    const p = galleryPath('s1', 'a1', 'i1')
    const rest = p.replace('/spaces/s1/gallery/', '')
    expect(parseGallerySub(rest)).toMatchObject({ albumId: 'a1', itemId: 'i1' })
  })
})
