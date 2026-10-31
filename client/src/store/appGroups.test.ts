/**
 * appGroups — the lobby's typed helpers over ``/api/apps/{id}/groups/{sid}``.
 *
 * Pinned here: every helper hits its own route (ids URL-encoded), the
 * server view maps to the client shape in one place (the bridge's
 * ``toGroupSession`` plus the lobby's own times and invite count), and a
 * refusal's code reaches the caller untouched.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const h = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('@/api', async (orig) => ({
  ...(await orig<typeof import('@/api')>()),
  api: { get: h.get, post: h.post },
}))

import { ApiError } from '@/api'
import {
  closeGroup,
  createGroup,
  declineGroup,
  getGroup,
  joinGroup,
  leaveGroup,
  removeFromGroup,
  seatedPlayers,
  startGroup,
  toLobbyGame,
} from './appGroups'
import type { GroupView } from '@/features/apps/groupBridge'

const VIEW: GroupView = {
  session_id: 's1',
  app_id: 'gift-draw',
  state: 'lobby',
  end_reason: null,
  space_id: null,
  me: 1,
  my_state: 'seated',
  host: 1,
  is_host: true,
  min: 3,
  max: 12,
  late_join: false,
  seq: 4,
  note: 'Wichteln 2026',
  created_at: '2026-10-30T12:00:00+00:00',
  started_at: null,
  expires_at: '2026-11-02T12:00:00+00:00',
  invited: 2,
  players: [
    { seat: 2, name: 'Ben', household: 'Okafor', is_local: false, state: 'seated' },
    { seat: 1, name: 'Anna', household: null, is_local: true, state: 'seated' },
    { seat: 3, name: 'Cleo', household: 'Krämer', is_local: false, state: 'left' },
  ],
}

describe('appGroups store', () => {
  beforeEach(() => {
    h.get.mockReset()
    h.post.mockReset()
  })

  it('maps the server view once: the bridge session plus times and the invite count', () => {
    const g = toLobbyGame(VIEW)
    expect(g.id).toBe('s1')
    expect(g.appId).toBe('gift-draw')
    expect(g.isHost).toBe(true)
    expect(g.note).toBe('Wichteln 2026')
    expect(g.expiresAt).toBe('2026-11-02T12:00:00+00:00')
    expect(g.createdAt).toBe('2026-10-30T12:00:00+00:00')
    expect(g.startedAt).toBeNull()
    expect(g.invited).toBe(2)
    expect(g.players[1]).toEqual({ seat: 1, name: 'Anna', household: null, isLocal: true, state: 'seated' })
  })

  it('a view without the lobby fields maps to nulls', () => {
    const { expires_at: _e, created_at: _c, started_at: _s, invited: _i, ...bare } = VIEW
    const g = toLobbyGame(bare)
    expect(g.expiresAt).toBeNull()
    expect(g.createdAt).toBeNull()
    expect(g.startedAt).toBeNull()
    expect(g.invited).toBeNull()
  })

  it('seatedPlayers keeps the seated people in seat order', () => {
    expect(seatedPlayers(toLobbyGame(VIEW)).map(p => p.seat)).toEqual([1, 2])
  })

  it('getGroup reads one game', async () => {
    h.get.mockResolvedValue({ group: VIEW })
    const g = await getGroup('gift-draw', 's1')
    expect(h.get).toHaveBeenCalledWith('/api/apps/gift-draw/groups/s1')
    expect(g.id).toBe('s1')
  })

  it('ids are URL-encoded', async () => {
    h.get.mockResolvedValue({ group: VIEW })
    await getGroup('a b', 's/1')
    expect(h.get).toHaveBeenCalledWith('/api/apps/a%20b/groups/s%2F1')
  })

  it('join / start / remove answer with the new view', async () => {
    h.post.mockResolvedValue({ group: { ...VIEW, my_state: 'joining' } })
    expect((await joinGroup('gift-draw', 's1')).myState).toBe('joining')
    expect(h.post).toHaveBeenLastCalledWith('/api/apps/gift-draw/groups/s1/join')
    h.post.mockResolvedValue({ group: { ...VIEW, state: 'playing' } })
    expect((await startGroup('gift-draw', 's1')).state).toBe('playing')
    expect(h.post).toHaveBeenLastCalledWith('/api/apps/gift-draw/groups/s1/start')
    h.post.mockResolvedValue({ group: VIEW })
    await removeFromGroup('gift-draw', 's1', 2)
    expect(h.post).toHaveBeenLastCalledWith('/api/apps/gift-draw/groups/s1/remove', { seat: 2 })
  })

  it('decline / leave / close post to their own routes', async () => {
    h.post.mockResolvedValue({ ok: true })
    await declineGroup('gift-draw', 's1')
    expect(h.post).toHaveBeenLastCalledWith('/api/apps/gift-draw/groups/s1/decline')
    await leaveGroup('gift-draw', 's1')
    expect(h.post).toHaveBeenLastCalledWith('/api/apps/gift-draw/groups/s1/leave')
    await closeGroup('gift-draw', 's1')
    expect(h.post).toHaveBeenLastCalledWith('/api/apps/gift-draw/groups/s1/close')
  })

  it('a refusal reaches the caller with its code', async () => {
    const err = new ApiError(409, '/x', { code: 'APP_GROUP_FULL', detail: 'full' })
    h.post.mockRejectedValue(err)
    await expect(joinGroup('gift-draw', 's1')).rejects.toBe(err)
  })

  it('createGroup posts the picked people, how long it stays open and the note; returns the session', async () => {
    h.post.mockResolvedValue({ group: VIEW })
    const body = {
      targets: [{ instance_id: 'okafor', user_ref: 'ben', is_local: false }],
      lobby_days: 3 as const,
      note: 'Wichteln 2026',
    }
    const s = await createGroup('gift draw', body)
    expect(h.post).toHaveBeenLastCalledWith('/api/apps/gift%20draw/groups', body)
    expect(s).toMatchObject({ id: 's1', isHost: true, min: 3, max: 12 })
    // The app's session: no lobby-only fields.
    expect(s).not.toHaveProperty('expiresAt')
  })
})
