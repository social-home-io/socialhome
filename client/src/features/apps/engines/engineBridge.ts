/**
 * App engines in the app bridge (v_78) — the host side of the SDK's
 * ``sh.engine.<id>.*`` (docs/protocol/app-engines.md).
 *
 * An app engine is a Social Home feature an app at a table calls; the
 * first is the gift draw (``draw``). An app may call only the engines its
 * manifest declares (``engines: ["draw"]``) and this Social Home has —
 * ``app.context`` lists them as ``engines`` — else the call is refused
 * with ``engine_not_declared`` / ``engine_not_supported``.
 *
 * The gift draw:
 * - ``engine.draw.configure`` (a patch: only the fields given change) /
 *   ``setWishes`` / ``start`` / ``status`` call
 *   ``/api/apps/{id}/groups/{table}/engines/draw…``;
 * - ``engine.draw.reveal`` opens Social Home's OWN result sheet, outside
 *   the app's frame, and resolves with nothing. No call returns a result,
 *   and the frame itself can't reach the API (sandboxed, opaque origin,
 *   ``connect-src 'none'``).
 * - Live state is posted to the frame as ``{type: 'engine:event', engine:
 *   'draw', event: {type: 'status', id, status}}`` from each
 *   ``app.engine`` frame.
 */

import { api, ApiError } from '@/api'
import { ws, type WsEvent } from '@/ws'
import { GroupRefused } from '@/features/apps/groupBridge'

/** The engines this Social Home's bridge speaks. */
export const BRIDGE_ENGINES: readonly string[] = ['draw']

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
  /** Organiser only: homes that broke a round (a complaint without proof,
   *  a key nobody can seal to). */
  blamed?: { seats: number[] }[]
  settings: DrawSettings | null
  /** The person's own wish list. */
  wish: string
  /** Whether the person has a name to see (``engine.draw.reveal``). */
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

/** The draw as the server's routes and ``app.engine`` frames carry it. */
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
  blamed: { seats: number[] }[] | null
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

/** Whether *method* is one of the ``engine.*`` calls. */
export function isEngineMethod(method: string): boolean {
  return method.startsWith('engine.')
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
  if (v.blamed) out.blamed = v.blamed
  return out
}

function engineBase(appId: string, id: string, engine: string): string {
  return `/api/apps/${encodeURIComponent(appId)}/groups/${encodeURIComponent(id)}/engines/${engine}`
}

/** The result for Social Home's own sheet. */
export function getDrawResult(appId: string, id: string): Promise<DrawResult> {
  return api.get<{ result: DrawResult }>(`${engineBase(appId, id, 'draw')}/result`).then(r => r.result)
}

/** A server refusal as the code an app sees: ``APP_ENGINE_NOT_DECLARED`` →
 *  ``engine_not_declared``, ``APP_ENGINE_UNSUPPORTED`` →
 *  ``engine_not_supported``, ``APP_DRAW_TOO_FEW`` → ``too_few`` …
 *  ``null``: not an engine refusal. */
export function engineRefusal(err: unknown): GroupRefused | null {
  if (!(err instanceof ApiError) || !err.code) return null
  if (err.code === 'APP_ENGINE_NOT_DECLARED') return new GroupRefused('engine_not_declared', err.message)
  if (err.code === 'APP_ENGINE_UNSUPPORTED') return new GroupRefused('engine_not_supported', err.message)
  if (err.code.startsWith('APP_DRAW_')) {
    return new GroupRefused(err.code.slice('APP_DRAW_'.length).toLowerCase(), err.message)
  }
  return null
}

function tableId(params: { id?: unknown } | undefined): string {
  const id = params?.id
  if (typeof id !== 'string' || !SESSION_ID.test(id)) {
    throw new GroupRefused('invalid', 'An engine call needs the table id.')
  }
  return id
}

/**
 * The ``engine.*`` calls of one app frame and the live state it hears.
 * *engines*: what ``app.context`` says the app may call (declared and
 * here); *declared*: what its manifest declares. *reveal* opens Social
 * Home's result sheet for a table.
 */
export class EngineBridge {
  constructor(
    private readonly appId: string,
    private readonly post: (msg: unknown) => void,
    private readonly engines: readonly string[],
    private readonly declared: readonly string[],
    private readonly reveal?: (sessionId: string) => void,
  ) {}

  /** What ``app.context`` says: the engines the app may call. */
  available(): string[] {
    return this.engines.filter(e => BRIDGE_ENGINES.includes(e))
  }

  async call(method: string, params: Record<string, unknown> | undefined): Promise<unknown> {
    const [, engine, op] = method.split('.')
    if (!engine || !op) throw new GroupRefused('unknown_method', `Unknown method: ${method}`)
    if (!this.declared.includes(engine)) {
      throw new GroupRefused('engine_not_declared', `This app didn't declare the ${engine} engine.`)
    }
    if (!this.available().includes(engine)) {
      throw new GroupRefused('engine_not_supported', `This Social Home doesn't have the ${engine} engine.`)
    }
    const id = tableId(params)
    try {
      return await this.draw(op, id, params)
    } catch (err) {
      throw engineRefusal(err) ?? err
    }
  }

  private async draw(op: string, id: string, params: Record<string, unknown> | undefined): Promise<unknown> {
    const base = engineBase(this.appId, id, 'draw')
    switch (op) {
      case 'status': {
        const r = await api.get<{ state: DrawView }>(base)
        return toDrawStatus(id, r.state)
      }
      case 'configure': {
        // A patch: only the fields the app gives change.
        const s = (params?.settings ?? {}) as Record<string, unknown>
        const body: Record<string, unknown> = {}
        for (const k of ['name', 'budget', 'day', 'pairs'] as const) {
          if (k in s) body[k] = s[k]
        }
        const r = await api.post<{ state: DrawView }>(`${base}/configure`, body)
        return toDrawStatus(id, r.state)
      }
      case 'setWishes': {
        const r = await api.post<{ state: DrawView }>(`${base}/wish`, { text: String(params?.text ?? '') })
        return toDrawStatus(id, r.state)
      }
      case 'start': {
        const r = await api.post<{ state: DrawView }>(`${base}/start`, { again: params?.again === true })
        return toDrawStatus(id, r.state)
      }
      case 'reveal': {
        // Social Home's own sheet, outside the frame: the app gets nothing.
        if (!this.reveal) throw new GroupRefused('unavailable', 'This Social Home can\'t show the result here.')
        this.reveal(id)
        return null
      }
      default:
        throw new GroupRefused('unknown_method', `Unknown method: engine.draw.${op}`)
    }
  }

  /** Post each ``app.engine`` frame of this app's available engines to the
   *  frame while *allowed* says the app may hear about people. */
  listen(allowed: () => boolean): () => void {
    return ws.on('app.engine', (evt: WsEvent) => {
      const d = evt.data as { app_id?: unknown; engine?: unknown; session_id?: unknown; state?: DrawView } | undefined
      if (!d || d.app_id !== this.appId || typeof d.session_id !== 'string' || !d.state) return
      if (d.engine !== 'draw' || !this.available().includes('draw') || !allowed()) return
      this.post({
        type: 'engine:event',
        engine: 'draw',
        event: { type: 'status', id: d.session_id, status: toDrawStatus(d.session_id, d.state) },
      })
    })
  }
}
