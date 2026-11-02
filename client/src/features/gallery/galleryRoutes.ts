/**
 * Gallery addresses — every album and every photo has its own link.
 *
 *   /gallery                              the albums
 *   /gallery/albums/:albumId              one album
 *   /gallery/albums/:albumId/:itemId      one photo or video in the viewer
 *   /gallery/trash                        the household trash (no space
 *                                         mirror: a space has no trash)
 *   /gallery/favourites[/:itemId]         your own favourites across the
 *                                         albums you see (no space mirror:
 *                                         one list spans them all)
 *   /gallery/search[/:itemId]?q=          a search across every album you
 *                                         see (a space's: only its albums)
 *
 * A space gallery mirrors them under its section:
 * ``/spaces/:id/gallery[/albums/:albumId[/:itemId]]``.
 *
 * The router hands the page everything after ``/gallery`` as ONE rest
 * segment (``/gallery/:sub*``, ``/spaces/:id/:section?/:sub*``), so moving
 * between the list, an album and a photo re-renders the page instead of
 * remounting it — the grid stays put behind the viewer and keeps its
 * scroll. These paths are in-app paths: links wrap them in ``addBase`` and
 * ``route()`` adds the ingress prefix itself.
 */

export interface GalleryRoute {
  albumId: string | null
  itemId: string | null
  /** ``false`` for a rest the gallery doesn't know (``/gallery/xyz``):
   *  the page replaces it with the albums. */
  valid: boolean
  /** ``/gallery/trash`` — the household trash. */
  trash?: boolean
  /** ``/gallery/favourites[/:itemId]`` — the person's own favourites;
   *  ``itemId`` is the photo open in the viewer. */
  favourites?: boolean
  /** ``/gallery/search[/:itemId]?q=`` (and its space mirror) — a search;
   *  ``itemId`` is the result open in the viewer. */
  search?: boolean
}

/** Read the ``:sub*`` rest after ``/gallery``. */
export function parseGallerySub(sub: string | undefined | null): GalleryRoute {
  const parts = (sub ?? '').split('/').filter(Boolean)
  if (parts.length === 0) return { albumId: null, itemId: null, valid: true }
  if (parts.length === 1 && parts[0] === 'trash') {
    return { albumId: null, itemId: null, valid: true, trash: true }
  }
  if (parts[0] === 'favourites' && parts.length <= 2) {
    return { albumId: null, itemId: parts[1] ?? null, valid: true, favourites: true }
  }
  if (parts[0] === 'search' && parts.length <= 2) {
    return { albumId: null, itemId: parts[1] ?? null, valid: true, search: true }
  }
  if (parts[0] !== 'albums' || parts.length < 2 || parts.length > 3) {
    return { albumId: null, itemId: null, valid: false }
  }
  return { albumId: parts[1], itemId: parts[2] ?? null, valid: true }
}

const BACK_KEY = 'shGalleryBack'

/** Note on the current history entry that the entry before it is
 *  ``path`` (the album list behind an album, the album behind a photo).
 *  ``history.state`` survives a reload, so a refreshed photo still closes
 *  with a step back instead of piling a second album entry on top. */
export function markBackTo(path: string): void {
  try {
    const state = (history.state ?? {}) as Record<string, unknown>
    if (state[BACK_KEY] === path) return
    history.replaceState({ ...state, [BACK_KEY]: path }, '')
  } catch { /* no history (tests, sandboxed frames): nothing to note */ }
}

/** Does a step back lead to ``path`` (see ``markBackTo``)? */
export function backLeadsTo(path: string): boolean {
  try {
    return ((history.state ?? {}) as Record<string, unknown>)[BACK_KEY] === path
  } catch {
    return false
  }
}

/** The household trash's in-app path. */
export const TRASH_PATH = '/gallery/trash'

/** The in-app path of your favourites, or of one of them in the viewer. */
export function favouritesPath(itemId?: string | null): string {
  return itemId ? `/gallery/favourites/${encodeURIComponent(itemId)}` : '/gallery/favourites'
}

/** The in-app path of a gallery search (the household's, or one space's),
 *  or of one result in the viewer. The words travel in ``?q=`` so Back,
 *  a refresh and a shared link keep the search. */
export function searchPath(
  spaceId: string | null | undefined,
  q: string,
  itemId?: string | null,
): string {
  let p = `${galleryPath(spaceId)}/search`
  if (itemId) p += `/${encodeURIComponent(itemId)}`
  return q ? `${p}?q=${encodeURIComponent(q)}` : p
}

/** The in-app path of the gallery, an album or an item in it. */
export function galleryPath(
  spaceId?: string | null,
  albumId?: string | null,
  itemId?: string | null,
): string {
  let p = spaceId ? `/spaces/${encodeURIComponent(spaceId)}/gallery` : '/gallery'
  if (albumId) {
    p += `/albums/${encodeURIComponent(albumId)}`
    if (itemId) p += `/${encodeURIComponent(itemId)}`
  }
  return p
}

/** The album list of a gallery — a space's, or the household's — for
 *  ``GET`` (the albums) and ``POST`` (a new album there). */
export function albumsUrl(spaceId?: string | null): string {
  return spaceId ? `/api/spaces/${spaceId}/gallery/albums` : '/api/gallery/albums'
}
