/**
 * Host↔iframe postMessage bridge for sandboxed Social Home apps.
 *
 * Security model:
 * - Identity is verified by ``event.source === iframe.contentWindow``,
 *   NOT by ``event.origin``. Sandboxed iframes produce an opaque origin
 *   (``"null"``), so origin-checking is unreliable. Any event whose
 *   source is not our iframe is silently dropped.
 * - The bearer token MUST NEVER be included in any reply. The app
 *   receives only its own data and context (appId, selfUserId).
 * - Replies use target origin ``'*'`` because the iframe's opaque origin
 *   means a specific origin target would always miss.
 */

import { api, ApiError } from '@/api'
import { ws, WsEvent } from '@/ws'

export interface BridgeContext {
  appId: string
  selfUserId: string
}

/** RPC envelope sent by the app iframe. */
interface RpcRequest {
  id: string | number
  method: string
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  params?: any // dynamic RPC payload — typed per method below
}

/** Success reply sent back to the iframe. */
interface RpcOkReply {
  id: string | number
  ok: true
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  result: any
}

/** Failure reply sent back to the iframe. */
interface RpcErrReply {
  id: string | number
  ok: false
  error: { code: string; message: string }
}

type RpcReply = RpcOkReply | RpcErrReply

/** One waiting invite, as ``GET /api/apps/{id}/pending-sessions`` lists it. */
interface PendingSession {
  session_id: string
  from_instance: string
  from_user: string | null
  payload: unknown
}

/**
 * Mount the bridge between ``iframe`` and the host.
 *
 * Returns a cleanup function that tears down both the ``message``
 * listener and the WS subscription — call it when the app frame
 * unmounts.
 */
export function mountBridge(
  iframe: HTMLIFrameElement,
  ctx: BridgeContext,
): () => void {
  const { appId } = ctx

  function reply(r: RpcReply): void {
    iframe.contentWindow?.postMessage(r, '*')
  }

  async function handleRpc(req: RpcRequest): Promise<void> {
    const { id, method, params } = req

    try {
      switch (method) {
        case 'store.get': {
          const key = String(params?.key ?? '')
          try {
            const resp = await api.get<{ key: string; value: unknown }>(
              `/api/apps/${encodeURIComponent(appId)}/store/${encodeURIComponent(key)}`,
            )
            reply({ id, ok: true, result: resp.value })
          } catch (err) {
            if (err instanceof ApiError && err.status === 404) {
              reply({ id, ok: true, result: null })
            } else {
              throw err
            }
          }
          break
        }

        case 'store.set': {
          const key = String(params?.key ?? '')
          const value = params?.value
          await api.put(`/api/apps/${encodeURIComponent(appId)}/store/${encodeURIComponent(key)}`, { value })
          reply({ id, ok: true, result: { ok: true } })
          break
        }

        case 'store.delete': {
          const key = String(params?.key ?? '')
          await api.delete(`/api/apps/${encodeURIComponent(appId)}/store/${encodeURIComponent(key)}`)
          reply({ id, ok: true, result: { ok: true } })
          break
        }

        case 'store.list': {
          const resp = await api.get<{ items: unknown[] }>(`/api/apps/${encodeURIComponent(appId)}/store`)
          reply({ id, ok: true, result: resp.items })
          break
        }

        case 'app.context': {
          // NEVER include a token — the app runs in a sandboxed iframe
          // and must not gain host credentials.
          reply({ id, ok: true, result: { appId: ctx.appId, selfUserId: ctx.selfUserId } })
          break
        }

        case 'app.send': {
          const { session_id, target, payload } = params ?? {}
          await api.post(`/api/apps/${encodeURIComponent(appId)}/messages`, {
            session_id,
            target,
            payload,
          })
          reply({ id, ok: true, result: { ok: true } })
          break
        }

        case 'peers.list': {
          const resp = await api.get<{ peers: unknown[] }>(
            `/api/apps/${encodeURIComponent(appId)}/peers`,
          )
          reply({ id, ok: true, result: resp.peers })
          break
        }

        case 'contacts.list': {
          const resp = await api.get<{ contacts: unknown[] }>(
            `/api/apps/${encodeURIComponent(appId)}/contacts`,
          )
          reply({ id, ok: true, result: resp.contacts })
          break
        }

        case 'app.pendingSessions': {
          const resp = await api.get<{ sessions: unknown[] }>(
            `/api/apps/${encodeURIComponent(appId)}/pending-sessions`,
          )
          reply({ id, ok: true, result: resp.sessions })
          break
        }

        case 'app.openSession': {
          const { target } = params ?? {}
          const resp = await api.post<{ session_id: string }>(
            `/api/apps/${encodeURIComponent(appId)}/sessions`,
            { target },
          )
          reply({ id, ok: true, result: resp.session_id })
          break
        }

        case 'app.cancelSession': {
          // The challenger withdraws an invite: the invitee's "Needs you"
          // item goes and their app hears {verb: 'close', reason: 'withdrawn'}.
          const { sessionId, target } = params ?? {}
          await api.post(
            `/api/apps/${encodeURIComponent(appId)}/sessions/${encodeURIComponent(String(sessionId ?? ''))}/cancel`,
            { target },
          )
          reply({ id, ok: true, result: { ok: true } })
          break
        }

        default:
          reply({
            id,
            ok: false,
            error: { code: 'unknown_method', message: `Unknown method: ${method}` },
          })
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      reply({ id, ok: false, error: { code: 'error', message } })
    }
  }

  function onMessage(event: MessageEvent): void {
    // Security: reject any message not originating from our iframe.
    // Do NOT check event.origin — sandboxed iframes emit "null".
    if (event.source !== iframe.contentWindow) return

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const req = event.data as any
    if (!req || typeof req.method !== 'string') return

    // eslint-disable-next-line @typescript-eslint/no-floating-promises
    handleRpc(req as RpcRequest)
  }

  /** Invites this mount already handed the app live (no second copy). */
  const relayed = new Set<string>()

  /**
   * A game invite reached the app while it is open: the app has it, so
   * the host takes the waiting invites in (the drain the app does on start)
   * — its "Needs you" item reads as accepted — and hands the app any it
   * hasn't seen yet. Best effort: the app drains again on its next start.
   */
  async function takeInvitesIn(): Promise<void> {
    try {
      const resp = await api.get<{ sessions?: PendingSession[] }>(
        `/api/apps/${encodeURIComponent(appId)}/pending-sessions`,
      )
      for (const s of resp?.sessions ?? []) {
        if (relayed.has(s.session_id)) continue
        relayed.add(s.session_id)
        iframe.contentWindow?.postMessage(
          {
            type: 'app:event',
            kind: 'session',
            sessionId: s.session_id,
            fromInstance: s.from_instance,
            fromUser: s.from_user ?? undefined,
            payload: s.payload,
          },
          '*',
        )
      }
    } catch { /* the app's own drain on its next start picks them up */ }
  }

  // WS relay: push app-specific real-time frames into the iframe.
  // Forwards the full identity so multi-game / multi-friend apps can route
  // a message to the right session and distinguish invites from in-game moves.
  const offWs = ws.on('app.message', (evt: WsEvent) => {
    if (evt.data?.app_id === appId) {
      iframe.contentWindow?.postMessage(
        {
          type: 'app:event',
          kind: evt.data.kind,            // 'session' | 'message'
          sessionId: evt.data.session_id,
          fromInstance: evt.data.from_instance,
          fromUser: evt.data.from_user,   // present on session-open frames; undefined otherwise
          payload: evt.data.payload,
        },
        '*',
      )
      const payload = evt.data.payload as { verb?: unknown } | undefined
      if (evt.data.kind === 'session' && payload?.verb === 'open') {
        relayed.add(String(evt.data.session_id))
        void takeInvitesIn()
      }
    }
  })

  window.addEventListener('message', onMessage)

  return () => {
    window.removeEventListener('message', onMessage)
    offWs()
  }
}
