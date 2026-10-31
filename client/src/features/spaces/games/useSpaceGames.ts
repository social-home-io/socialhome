/**
 * useSpaceGames — a space's games for its page: ``GET
 * /api/spaces/{id}/games``, kept live by the ``space.app_table`` frame.
 *
 * The frame carries one game's card as this person may see it (the server
 * filters it per person) but not the app's state here nor the caller's
 * own place, so a frame for this space re-reads the list — debounced, as
 * several cards can change at once (an app taken off the list closes all
 * its waiting games). A failed live re-read keeps what is on screen.
 *
 * Off (``enabled`` false: a follower, a global space, no games allowed)
 * it reads nothing and listens to nothing — a follower's request would
 * only be refused.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { APP_FRAMES } from '@/store/apps'
import type { SpaceGames } from './spaceGames'

/** The live frame (``SpaceAppTableService.WS_FRAME_TYPE``). */
export const SPACE_APP_TABLE_FRAME = 'space.app_table'
/** Frames within this many ms re-read once. */
const REFRESH_DEBOUNCE_MS = 300

/** The answer with every list a list (a missing field reads as none). */
export function normalizeGames(spaceId: string, body: Partial<SpaceGames> | null | undefined): SpaceGames {
  return {
    space_id: body?.space_id ?? spaceId,
    allowed_apps: Array.isArray(body?.allowed_apps) ? body.allowed_apps : [],
    games_possible: body?.games_possible !== false,
    can_edit_games: body?.can_edit_games === true,
    host_supports: body?.host_supports !== false,
    tables: Array.isArray(body?.tables) ? body.tables : [],
  }
}

export interface SpaceGamesState {
  games: SpaceGames | null
  loading: boolean
  /** The first read failed (nothing to show). */
  failed: boolean
  /** Re-read quietly (live updates, after a join or close). */
  reload: () => Promise<void>
  /** Re-read showing the spinner and the error again (Retry). */
  retry: () => Promise<void>
}

/** ``revision`` — re-read when it changes (the space's games list, which
 *  reaches the page with the space's config). */
export function useSpaceGames(
  spaceId: string, enabled: boolean, revision = '',
): SpaceGamesState {
  const [games, setGames] = useState<SpaceGames | null>(null)
  const [loading, setLoading] = useState(enabled)
  const [failed, setFailed] = useState(false)
  // The newest request wins: a slow answer for an older space (or an
  // older re-read) never overwrites a newer one.
  const seq = useRef(0)
  const shownFor = useRef<string | null>(null)

  const load = async (quiet: boolean) => {
    const mine = ++seq.current
    if (!quiet) { setLoading(true); setFailed(false) }
    try {
      const body = await api.get<Partial<SpaceGames> | null>(`/api/spaces/${spaceId}/games`)
      if (mine !== seq.current) return
      setGames(normalizeGames(spaceId, body))
      setFailed(false)
    } catch {
      if (mine !== seq.current) return
      if (!quiet) { setGames(null); setFailed(true) }
    } finally {
      if (mine === seq.current && !quiet) setLoading(false)
    }
  }

  useEffect(() => {
    if (!enabled) {
      seq.current++
      shownFor.current = null
      setGames(null)
      setLoading(false)
      setFailed(false)
      return
    }
    // Another space: never show the last one's games meanwhile.
    if (shownFor.current !== spaceId) {
      setGames(null)
      shownFor.current = spaceId
    }
    void load(false)
    let timer: ReturnType<typeof setTimeout> | null = null
    const off = ws.on(SPACE_APP_TABLE_FRAME, (e) => {
      if (e.data.space_id !== spaceId) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => { timer = null; void load(true) }, REFRESH_DEBOUNCE_MS)
    })
    // An app added, updated, turned on / off or removed here changes what
    // each card offers (Join vs "Ask an admin").
    const offApps = APP_FRAMES.map((type) => ws.on(type, () => { void load(true) }))
    return () => { off(); offApps.forEach((o) => o()); if (timer) clearTimeout(timer) }
  }, [spaceId, enabled, revision]) // eslint-disable-line react-hooks/exhaustive-deps

  return { games, loading, failed, reload: () => load(true), retry: () => load(false) }
}
