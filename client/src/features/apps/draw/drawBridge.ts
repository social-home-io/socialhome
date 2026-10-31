/**
 * The gift draw in the app bridge (v_78) — the host side of the SDK's
 * ``sh.draw.*`` (docs/protocol/app-groups.md, "The gift draw").
 *
 * - ``draw.configure`` / ``setWishes`` / ``start`` / ``status`` call the
 *   ``/api/apps/{id}/groups/{table}/draw…`` routes;
 * - ``draw.reveal`` opens Social Home's OWN result sheet, outside the
 *   app's frame, and resolves with nothing: the app never learns whom
 *   anybody gives to (decision D13). There is no call that returns a
 *   result, and the frame itself can't reach the API (sandboxed, opaque
 *   origin, ``connect-src 'none'``).
 * - Live state is posted to the frame as ``{type: 'draw:event', event:
 *   {type: 'status', id, status}}`` from each ``app.draw`` frame.
 */

import { api, ApiError } from '@/api'
import { ws, type WsEvent } from '@/ws'
import { GroupRefused } from '@/features/apps/groupBridge'

/** The draw's state as the app may know it (the SDK's ``DrawStatus``). */
export interface DrawStatus {
  id: string
  state: 'gathering' | 'drawing' | 'ready' | 'failed'
  /** Why a draw failed: ``rules`` (the pairs never fit) / ``too_few``. */
  reason?: string
  round: number
  /** How often the names were drawn again after a reveal. */
  again: number
  /** People in the draw, and (organiser only) how many are through the
   *  current step. */
  of: number
  done?: number
  /** Organiser only, after an hour without a home's step: the seats of
   *  each home the draw waits for (``sh.group.remove`` takes them out). */
  waitingFor?: { seats: number[] }[]
  /** Organiser only: homes that stopped a round without proof. */
  falseComplaints?: { seats: number[] }[]
  settings: DrawSettings | null
  /** The person's own wish list. */
  wish: string
  /** Whether the person has a name to see (``draw.reveal``). */
  hasResult: boolean
  isHost: boolean
}

export interface DrawSettings {
  name: string
  budget: string
  day: string | null
  /** Seats who must not draw each other. */
  pairs: [number, number][]
}

/** The draw as the server's routes and ``app.draw`` frames carry it. */
export interface DrawView {
  state: DrawStatus['state']
  reason?: string | null
  gen: number
  again: number
  round: number
  of: number
  done: number | null
  settings: DrawSettings | null
  wish: string
  has_result: boolean
  is_host: boolean
  waiting_for: { seats: number[] }[] | null
  false_complaints: { seats: number[] }[] | null
}

/** Whom the person gives to — for the result sheet only, never the app. */
export interface DrawResult {
  app_id: string
  session_id: string
  name: string
  household: string
  wish: string
  wish_updated: boolean
  draw_name: string
  budget: string
  day: string | null
  again: number
  handed_over_by: string | null
}

const SESSION_ID = /^[A-Za-z0-9_-]{1,128}$/

/** Whether *method* is one of the ``draw.*`` calls. */
export function isDrawMethod(method: string): boolean {
  return method.startsWith('draw.')
}

export function toDrawStatus(id: string, v: DrawView): DrawStatus {
  const out: DrawStatus = {
    id,
    state: v.state,
    round: v.round,
    again: v.again,
    of: v.of,
    settings: v.settings ?? null,
    wish: v.wish ?? '',
    hasResult: v.has_result === true,
    isHost: v.is_host === true,
  }
  if (v.reason) out.reason = v.reason
  if (typeof v.done === 'number') out.done = v.done
  if (v.waiting_for) out.waitingFor = v.waiting_for
  if (v.false_complaints) out.falseComplaints = v.false_complaints
  return out
}

function drawBase(appId: string, id: string): string {
  return `/api/apps/${encodeURIComponent(appId)}/groups/${encodeURIComponent(id)}/draw`
}

/** The result for Social Home's own sheet. */
export function getDrawResult(appId: string, id: string): Promise<DrawResult> {
  return api.get<{ result: DrawResult }>(`${drawBase(appId, id)}/result`).then(r => r.result)
}

/** A server refusal as the code an app sees: ``APP_DRAW_TOO_FEW`` →
 *  ``too_few`` and so on. ``null``: not a draw refusal. */
export function drawRefusal(err: unknown): GroupRefused | null {
  if (!(err instanceof ApiError) || !err.code?.startsWith('APP_DRAW_')) return null
  return new GroupRefused(err.code.slice('APP_DRAW_'.length).toLowerCase(), err.message)
}

function tableId(params: { id?: unknown } | undefined): string {
  const id = params?.id
  if (typeof id !== 'string' || !SESSION_ID.test(id)) {
    throw new GroupRefused('invalid', 'A draw call needs the table id.')
  }
  return id
}

/**
 * The ``draw.*`` calls of one app frame and the live state it hears.
 * *reveal* opens Social Home's result sheet for a table.
 */
export class DrawBridge {
  constructor(
    private readonly appId: string,
    private readonly post: (msg: unknown) => void,
    private readonly reveal?: (sessionId: string) => void,
  ) {}

  async call(method: string, params: Record<string, unknown> | undefined): Promise<unknown> {
    const id = tableId(params)
    const base = drawBase(this.appId, id)
    try {
      switch (method) {
        case 'draw.status': {
          const r = await api.get<{ draw: DrawView }>(base)
          return toDrawStatus(id, r.draw)
        }
        case 'draw.configure': {
          const s = (params?.settings ?? {}) as Record<string, unknown>
          const body: Record<string, unknown> = {}
          for (const k of ['name', 'budget', 'day', 'pairs'] as const) {
            if (k in s) body[k] = s[k]
          }
          const r = await api.patch<{ draw: DrawView }>(base, body)
          return toDrawStatus(id, r.draw)
        }
        case 'draw.setWishes': {
          const r = await api.put<{ draw: DrawView }>(`${base}/wish`, { text: String(params?.text ?? '') })
          return toDrawStatus(id, r.draw)
        }
        case 'draw.start': {
          const r = await api.post<{ draw: DrawView }>(`${base}/start`, { again: params?.again === true })
          return toDrawStatus(id, r.draw)
        }
        case 'draw.reveal': {
          // Social Home's own sheet, outside the frame: the app gets nothing.
          if (!this.reveal) throw new GroupRefused('unavailable', 'This Social Home can\'t show the result here.')
          this.reveal(id)
          return null
        }
        default:
          throw new GroupRefused('unknown_method', `Unknown method: ${method}`)
      }
    } catch (err) {
      throw drawRefusal(err) ?? err
    }
  }

  /** Post each ``app.draw`` frame of this app to the frame while
   *  *allowed* says the app may hear about people. */
  listen(allowed: () => boolean): () => void {
    return ws.on('app.draw', (evt: WsEvent) => {
      const d = evt.data as { app_id?: unknown; session_id?: unknown; draw?: DrawView } | undefined
      if (!d || d.app_id !== this.appId || typeof d.session_id !== 'string' || !d.draw) return
      if (!allowed()) return
      this.post({ type: 'draw:event', event: { type: 'status', id: d.session_id, status: toDrawStatus(d.session_id, d.draw) } })
    })
  }
}
