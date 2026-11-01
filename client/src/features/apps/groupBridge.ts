/**
 * Group tables in the app bridge (group play, v_76) — the host side of the
 * SDK's ``sh.group.*`` (docs/protocol/app-groups.md, "HTTP, WebSocket and
 * the app bridge").
 *
 * - ``group.list`` / ``get`` / ``start`` / ``send`` / ``leave`` /
 *   ``remove`` / ``close`` call the ``/api/apps/{id}/groups…`` routes;
 *   ``group.held`` hands over the held group messages (for an app that
 *   pulls). ``group.open`` is answered by bridge.ts, not here: it shows
 *   Social Home's group sheet under the same guards as ``people.pick``
 *   (D3: an app never opens a table itself), and the game it opens is
 *   remembered here (:meth:`GroupBridge.keep`).
 * - Live events are posted to the frame as ``{type: 'group:event', event}``:
 *   ``message`` from each ``app.group.message`` frame, and ``joined`` /
 *   ``left`` / ``removed`` / ``started`` / ``host-changed`` / ``ended``
 *   derived from each ``app.group`` frame against the last view of that
 *   table the bridge saw.
 *
 * People are seats: a table never carries a username into the frame.
 */

import { api, ApiError } from '@/api'
import { ws, WsEvent } from '@/ws'

/** A table as the app sees it (the SDK's ``GroupSession``). */
export interface GroupSession {
  id: string
  appId: string
  state: 'lobby' | 'playing' | 'ended'
  endReason?: string
  spaceId?: string
  me: number | null
  myState: 'invited' | 'joining' | 'seated' | 'left' | 'removed'
  host: number | null
  isHost: boolean
  min: number
  max: number
  lateJoin: boolean
  seq: number
  note?: string
  players: GroupPlayer[]
}

export interface GroupPlayer {
  seat: number
  name: string
  household: string | null
  isLocal: boolean
  state: 'seated' | 'left' | 'removed'
}

export type GroupEvent =
  | { type: 'message'; id: string; seq: number; from: number; data: unknown; direct: boolean; held?: true }
  | { type: 'joined' | 'left' | 'removed'; id: string; seat: number; session: GroupSession }
  | { type: 'started' | 'host-changed' | 'ended'; id: string; session: GroupSession }

/** A table as the server's routes and ``app.group`` frames carry it. */
export interface GroupView {
  session_id: string
  app_id: string
  state: GroupSession['state']
  end_reason?: string | null
  space_id?: string | null
  me: number | null
  my_state: GroupSession['myState']
  host: number | null
  is_host: boolean
  min: number
  max: number
  late_join: boolean
  seq?: number
  note?: string | null
  /** ISO 8601 (UTC). The lobby reads these; the app's session leaves them out. */
  created_at?: string | null
  started_at?: string | null
  expires_at?: string | null
  /** Unanswered invites — on the starter's own household only, else ``null``. */
  invited?: number | null
  /** Seats of players whose household this one can't reach now (the
   *  lobby's "Waiting for Ben's home to come back"); the app's session
   *  leaves it out. */
  away?: number[]
  players: { seat: number; name: string; household: string | null; is_local: boolean; state: GroupPlayer['state'] }[]
}

/** One held group message, as ``POST /api/apps/{id}/held`` lists it. */
export interface HeldGroupRow {
  session_id: string
  seq: number
  from_seat: number
  direct: boolean
  data: unknown
}

/** A ``group.*`` refusal with a stable code the SDK maps. */
export class GroupRefused extends Error {
  constructor(public readonly code: string, message: string, public readonly retryAfterMs?: number) {
    super(message)
  }
}

/** Most players any group game may have (server ``GROUP_MAX_PLAYERS``;
 *  an app's own ``players.max`` above it is cut to it). */
export const GROUP_MAX_PLAYERS = 30

/** A table id (what the server mints and accepts). */
const SESSION_ID = /^[A-Za-z0-9_-]{1,128}$/

/** Whether *method* is one of the ``group.*`` calls. */
export function isGroupMethod(method: string): boolean {
  return method.startsWith('group.')
}

export function toGroupSession(v: GroupView): GroupSession {
  const out: GroupSession = {
    id: v.session_id,
    appId: v.app_id,
    state: v.state,
    me: v.me ?? null,
    myState: v.my_state,
    host: v.host ?? null,
    isHost: v.is_host === true,
    min: v.min,
    max: v.max,
    lateJoin: v.late_join === true,
    seq: typeof v.seq === 'number' ? v.seq : 0,
    players: (v.players ?? []).map(p => ({
      seat: p.seat,
      name: p.name,
      household: p.household ?? null,
      isLocal: p.is_local === true,
      state: p.state,
    })),
  }
  if (v.end_reason) out.endReason = v.end_reason
  if (v.space_id) out.spaceId = v.space_id
  if (v.note) out.note = v.note
  return out
}

export function heldToEvent(h: HeldGroupRow): GroupEvent {
  return {
    type: 'message',
    id: h.session_id,
    seq: h.seq,
    from: h.from_seat,
    data: h.data,
    direct: h.direct === true,
    held: true,
  }
}

/** What changed from *prev* to *next* of one table, as app events. */
export function diffEvents(prev: GroupSession, next: GroupSession): GroupEvent[] {
  const out: GroupEvent[] = []
  const before = new Map(prev.players.map(p => [p.seat, p.state]))
  for (const p of next.players) {
    const was = before.get(p.seat)
    if (p.state === was) continue
    if (p.state === 'seated') out.push({ type: 'joined', id: next.id, seat: p.seat, session: next })
    else if (was === 'seated') out.push({ type: p.state, id: next.id, seat: p.seat, session: next })
  }
  if (prev.state === 'lobby' && next.state === 'playing') {
    out.push({ type: 'started', id: next.id, session: next })
  }
  if (prev.host !== null && next.host !== null && prev.host !== next.host) {
    out.push({ type: 'host-changed', id: next.id, session: next })
  }
  if (prev.state !== 'ended' && next.state === 'ended') {
    out.push({ type: 'ended', id: next.id, session: next })
  }
  return out
}

/** A server refusal as the code an app sees: ``APP_GROUP_BUSY`` →
 *  ``group_busy`` (``retryAfterMs`` from ``params.retry_after``),
 *  ``PAYLOAD_TOO_LARGE`` → ``too_large``. ``null``: not one of these. */
export function groupRefusal(err: unknown): GroupRefused | null {
  if (!(err instanceof ApiError) || !err.code) return null
  if (err.code === 'PAYLOAD_TOO_LARGE') return new GroupRefused('too_large', err.message)
  // ``APP_GROUP_*`` and ``APP_GROUPS_NOT_SUPPORTED``.
  if (!err.code.startsWith('APP_GROUP')) return null
  const code = err.code.slice('APP_'.length).toLowerCase()
  const params = err.extra?.params as { retry_after?: unknown } | undefined
  const retry = typeof params?.retry_after === 'number' ? params.retry_after * 1000 : undefined
  return new GroupRefused(code === 'group_cant_join' ? 'cant_join' : code, err.message, retry)
}

function tableId(params: { id?: unknown } | undefined): string {
  const id = params?.id
  if (typeof id !== 'string' || !SESSION_ID.test(id)) {
    throw new GroupRefused('invalid', 'A group call needs the table id.')
  }
  return id
}

function seat(v: unknown): v is number {
  return typeof v === 'number' && Number.isInteger(v) && v > 0
}

/**
 * The ``group.*`` calls of one app frame, and the live events it gets.
 * *post* hands a message to the frame; *heldGroup* is the frame's one take
 * of the mailbox (shared with ``app.held``).
 */
export class GroupBridge {
  /** The last view of each table the frame was told about. */
  private readonly seen = new Map<string, GroupSession>()

  constructor(
    private readonly appId: string,
    private readonly post: (msg: unknown) => void,
    private readonly heldGroup: () => Promise<GroupEvent[]>,
  ) {}

  private base(id?: string): string {
    const root = `/api/apps/${encodeURIComponent(this.appId)}/groups`
    return id === undefined ? root : `${root}/${encodeURIComponent(id)}`
  }

  /** Remember *view* as the frame's view of its table and return it. */
  remember(view: GroupView): GroupSession {
    const s = toGroupSession(view)
    this.seen.set(s.id, s)
    return s
  }

  /** Remember *session* (a game Social Home's group sheet just opened) as
   *  the frame's view of it, so later ``app.group`` frames diff against
   *  it; returns it. */
  keep(session: GroupSession): GroupSession {
    this.seen.set(session.id, session)
    return session
  }

  /** The table *sessionId* if the person has a place at it — the launch
   *  context of a Play / Join on a table — else ``null``. */
  async launch(sessionId: string): Promise<GroupSession | null> {
    try {
      const r = await api.get<{ group?: GroupView }>(this.base(sessionId))
      return r?.group ? this.remember(r.group) : null
    } catch {
      return null
    }
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async call(method: string, params: any): Promise<unknown> {
    switch (method) {
      case 'group.list': {
        const r = await api.get<{ groups: GroupView[] }>(this.base())
        return (r?.groups ?? []).map(g => this.remember(g))
      }
      case 'group.get': {
        const r = await api.get<{ group: GroupView }>(this.base(tableId(params)))
        return this.remember(r.group)
      }
      case 'group.start': {
        const r = await api.post<{ group: GroupView }>(`${this.base(tableId(params))}/start`)
        return this.remember(r.group)
      }
      case 'group.remove': {
        if (!seat(params?.seat)) throw new GroupRefused('invalid', 'group.remove needs a seat.')
        const r = await api.post<{ group: GroupView }>(
          `${this.base(tableId(params))}/remove`, { seat: params.seat },
        )
        return this.remember(r.group)
      }
      case 'group.leave':
      case 'group.close': {
        await api.post(`${this.base(tableId(params))}/${method === 'group.leave' ? 'leave' : 'close'}`)
        return { ok: true }
      }
      case 'group.send': {
        const id = tableId(params)
        const body: { data: unknown; to?: number[]; turn?: number; final?: boolean } = { data: params?.data ?? null }
        if (params?.to !== undefined) {
          if (!Array.isArray(params.to) || !params.to.length || !params.to.every(seat)) {
            throw new GroupRefused('invalid', 'to must be a list of seats.')
          }
          body.to = params.to
        }
        if (params?.turn !== undefined) {
          if (!seat(params.turn)) throw new GroupRefused('invalid', 'turn must be a seat.')
          body.turn = params.turn
        }
        if (params?.final !== undefined) {
          if (typeof params.final !== 'boolean') throw new GroupRefused('invalid', 'final must be true or false.')
          if (params.final) body.final = true
        }
        const r = await api.post<{ seq: number | null }>(`${this.base(id)}/messages`, body)
        return { seq: typeof r?.seq === 'number' ? r.seq : null }
      }
      case 'group.held':
        return this.heldGroup()
      case 'group.open':
        // Handled by mountBridge (the group sheet and its guards); a
        // GroupBridge on its own has no sheet to show.
        throw new GroupRefused('unavailable', 'This Social Home can\'t show the group sheet here.')
      default:
        throw new GroupRefused('unknown_method', `Unknown method: ${method}`)
    }
  }

  /** Relay this app's table frames into the frame as events. Returns the
   *  function that stops listening. */
  listen(enabled: () => boolean): () => void {
    const offMsg = ws.on('app.group.message', (evt: WsEvent) => {
      const d = evt.data
      if (d?.app_id !== this.appId || !enabled()) return
      this.post({
        type: 'group:event',
        event: {
          type: 'message',
          id: String(d.session_id),
          seq: Number(d.seq),
          from: Number(d.from_seat),
          data: d.data,
          direct: d.direct === true,
        } satisfies GroupEvent,
      })
    })
    const offTable = ws.on('app.group', (evt: WsEvent) => {
      const d = evt.data
      if (d?.app_id !== this.appId || !enabled()) return
      for (const event of this.change(d)) this.post({ type: 'group:event', event })
    })
    return () => {
      offMsg()
      offTable()
    }
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private change(d: any): GroupEvent[] {
    const sid = String(d.session_id ?? '')
    const prev = this.seen.get(sid)
    if (d.group) {
      const next = this.remember(d.group as GroupView)
      return prev ? diffEvents(prev, next) : []
    }
    // The person's own place is over (their row went).
    if (!prev || prev.me === null || (d.change !== 'left' && d.change !== 'removed')) return []
    const state = d.change as 'left' | 'removed'
    const next: GroupSession = {
      ...prev,
      myState: state,
      players: prev.players.map(p => (p.seat === prev.me ? { ...p, state } : p)),
    }
    this.seen.set(sid, next)
    return [{ type: state, id: sid, seat: prev.me, session: next }]
  }
}
