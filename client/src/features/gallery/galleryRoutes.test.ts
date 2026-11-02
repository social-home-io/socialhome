import { describe, it, expect } from 'vitest'
import { TRASH_PATH, albumsUrl, backLeadsTo, favouritesPath, galleryPath, markBackTo, parseGallerySub, searchPath } from './galleryRoutes'

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

  it('knows your favourites (/gallery/favourites[/:itemId]) — under the ingress prefix too, an in-app path', () => {
    expect(parseGallerySub('favourites')).toEqual({ albumId: null, itemId: null, valid: true, favourites: true })
    expect(parseGallerySub('favourites/i1')).toEqual({ albumId: null, itemId: 'i1', valid: true, favourites: true })
    expect(parseGallerySub('favourites/i1/x').valid).toBe(false)
    expect(favouritesPath()).toBe('/gallery/favourites')
    expect(favouritesPath('a b')).toBe('/gallery/favourites/a%20b')
  })

  it('knows a search (/gallery/search[/:itemId]?q=) — under the ingress prefix too, an in-app path', () => {
    expect(parseGallerySub('search')).toEqual({ albumId: null, itemId: null, valid: true, search: true })
    expect(parseGallerySub('search/i1')).toEqual({ albumId: null, itemId: 'i1', valid: true, search: true })
    expect(parseGallerySub('search/i1/x').valid).toBe(false)
    expect(searchPath(null, 'Juli 2025')).toBe('/gallery/search?q=Juli%202025')
    expect(searchPath('s1', 'von Lena', 'i 1')).toBe('/spaces/s1/gallery/search/i%201?q=von%20Lena')
    expect(searchPath(null, '')).toBe('/gallery/search')
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

  it("names a space's album list, or the household's", () => {
    expect(albumsUrl('sp-1')).toBe('/api/spaces/sp-1/gallery/albums')
    expect(albumsUrl(null)).toBe('/api/gallery/albums')
    expect(albumsUrl()).toBe('/api/gallery/albums')
  })
})
