/**
 * appGroups — the lobby's helpers over one group game
 * (``/api/apps/{id}/groups/{sid}`` and its ``join`` / ``decline`` /
 * ``leave`` / ``start`` / ``close`` / ``remove`` actions; see
 * docs/protocol/app-groups.md).
 *
 * The server's view maps to the client shape in ONE place: the bridge's
 * ``toGroupSession`` (what an app sees) plus the few fields only Social
 * Home's own lobby reads — when it closes, when it started, and (for the
 * person who started it) how many invites are still unanswered.
 *
 * A refusal is re-thrown as the ``ApiError`` it is, so the lobby can say
 * ``APP_GROUP_FULL`` / ``…_CLOSED`` / ``…_CANT_JOIN`` / ``…_TOO_FEW`` in
 * the person's own words.
 */
import { api } from '@/api'
import {
  toGroupSession,
  type GroupPlayer,
  type GroupSession,
  type GroupView,
} from '@/features/apps/groupBridge'

/** One group game as the lobby shows it. */
export interface LobbyGame extends GroupSession {
  /** When the game stops taking people (ISO 8601, UTC). */
  expiresAt: string | null
  /** When it went from waiting to playing. */
  startedAt: string | null
  createdAt: string | null
  /** Invites nobody answered yet — only for the household that started
   *  the game, else ``null``. */
  invited: number | null
}

/** The server's view of a game → the lobby's. */
export function toLobbyGame(v: GroupView): LobbyGame {
  return {
    ...toGroupSession(v),
    expiresAt: v.expires_at ?? null,
    startedAt: v.started_at ?? null,
    createdAt: v.created_at ?? null,
    invited: typeof v.invited === 'number' ? v.invited : null,
  }
}

/** The people in the game (not those who left or were removed), in seat
 *  order. */
export function seatedPlayers(g: Pick<GroupSession, 'players'>): GroupPlayer[] {
  return g.players.filter(p => p.state === 'seated').sort((a, b) => a.seat - b.seat)
}

/** How long a new group game waits for people (``lobby_days``). */
export const GROUP_OPEN_DAYS = [1, 3, 7, 14] as const
export type GroupOpenDays = typeof GROUP_OPEN_DAYS[number]
/** Most characters of the note on a new group game (server ``GROUP_NOTE_MAX``). */
export const GROUP_NOTE_MAX = 80

/** What the group sheet's Send asks the server for. */
export interface NewGroup {
  targets: { instance_id: string, user_ref: string, is_local: boolean }[]
  lobby_days: GroupOpenDays
  /** One line, at most :data:`GROUP_NOTE_MAX`; left out when empty. */
  note?: string
}

/** Open a group game with the people picked in the group sheet and invite
 *  them (``POST /api/apps/{id}/groups``). The app's own player range caps
 *  it on the server. */
export async function createGroup(appId: string, body: NewGroup): Promise<GroupSession> {
  const r = await api.post<{ group: GroupView }>(`/api/apps/${encodeURIComponent(appId)}/groups`, body)
  return toGroupSession(r.group)
}

function base(appId: string, sessionId: string): string {
  return `/api/apps/${encodeURIComponent(appId)}/groups/${encodeURIComponent(sessionId)}`
}

export async function getGroup(appId: string, sessionId: string): Promise<LobbyGame> {
  const r = await api.get<{ group: GroupView }>(base(appId, sessionId))
  return toLobbyGame(r.group)
}

/** Join: the answer is ``joining`` until the starter's household seats
 *  the person (an ``app.group`` frame brings the seat). */
export async function joinGroup(appId: string, sessionId: string): Promise<LobbyGame> {
  const r = await api.post<{ group: GroupView }>(`${base(appId, sessionId)}/join`)
  return toLobbyGame(r.group)
}

export async function declineGroup(appId: string, sessionId: string): Promise<void> {
  await api.post(`${base(appId, sessionId)}/decline`)
}

export async function leaveGroup(appId: string, sessionId: string): Promise<void> {
  await api.post(`${base(appId, sessionId)}/leave`)
}

/** Start (the person who started the game only). */
export async function startGroup(appId: string, sessionId: string): Promise<LobbyGame> {
  const r = await api.post<{ group: GroupView }>(`${base(appId, sessionId)}/start`)
  return toLobbyGame(r.group)
}

/** Close the game (the person who started it). */
export async function closeGroup(appId: string, sessionId: string): Promise<void> {
  await api.post(`${base(appId, sessionId)}/close`)
}

/** Take *seat* out of the game (the person who started it). The server
 *  can't undo it. */
export async function removeFromGroup(appId: string, sessionId: string, seat: number): Promise<LobbyGame> {
  const r = await api.post<{ group: GroupView }>(`${base(appId, sessionId)}/remove`, { seat })
  return toLobbyGame(r.group)
}
