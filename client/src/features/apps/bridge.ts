/**
 * Host↔iframe postMessage bridge for sandboxed Social Home apps.
 *
 * Security model:
 * - Identity is verified by ``event.source === iframe.contentWindow``,
 *   NOT by ``event.origin``. Sandboxed iframes produce an opaque origin
 *   (``"null"``), so origin-checking is unreliable. Any event whose
 *   source is not our iframe is silently dropped.
 * - The bearer token MUST NEVER be included in any reply. The app
 *   receives only its own data and context (appId, selfUserId, launch).
 *
 * Launch context (D12, "Play opens the game you were invited to"): the
 * host opens an app as ``/apps/<id>?session=<sid>`` from "Play" in Needs
 * you. The bridge then takes in exactly that invite (``POST …/sessions/
 * <sid>/accept``) the first time the app asks for its context, and hands
 * it over as ``app.context``'s ``launch`` — ``{sessionId, invite?}``;
 * ``invite`` is missing when the invite no longer waits (declined on
 * another device, withdrawn, expired). Opened without ``?session=``,
 * ``launch`` is ``{}``: nothing is accepted, invites stay in Needs you.
 * A ``launch`` key at all tells the app this host works this way (older
 * hosts leave it out). Apps from before launch contexts call
 * ``app.pendingSessions`` instead: it answers with the launch invite
 * alone (``[]`` without one) — never every waiting invite.
 *
 * Held messages (D11, "moves and declines wait for you"): Social Home
 * keeps the latest message of each game that arrived while the app was
 * closed or turned off. The bridge takes them (``POST …/held``, once per
 * frame) when the app has started and hands them over as ordinary
 * ``app:event`` posts, in order — right after the ``app.context`` reply,
 * so an app from before this change gets them through the handlers it
 * already has. An app that applies them itself, after loading its own
 * state, says so with ``app.context`` params ``{held: 'pull'}`` (SDK
 * ≥ 1.3) and asks with ``app.held`` instead; nothing is pushed then. A
 * held message may repeat one the app already saw live: apps dedupe
 * (chess by move count).
 * - Replies use target origin ``'*'`` because the iframe's opaque origin
 *   means a specific origin target would always miss.
 */

import { api, ApiError } from '@/api'
import { ws, WsEvent } from '@/ws'

export interface BridgeContext {
  appId: string
  selfUserId: string
  /** ``?session=`` the app was opened with ("Play" in Needs you): the
   *  one invite to take in and hand to the app. */
  launchSession?: string
  /** Called once, on the app's first bridge request: the app's code runs
   *  and talks to the host — it started. An SDK that posts ``app.ready``
   *  says so explicitly; older ones send ``app.context`` / ``store.*``
   *  first, which counts the same. A frame that never asks (a bundle
   *  error page, a crashed script) never calls it. */
  onReady?: () => void
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

/** One game invite, as ``POST /api/apps/{id}/sessions/{sid}/accept``
 *  returns it. */
interface PendingSession {
  session_id: string
  from_instance: string
  from_user: string | null
  payload: unknown
}

/** An invite as the app receives it (the SDK's ``AppMessage``). */
interface AppInvite {
  sessionId: string
  fromInstance: string
  fromUser?: string
  payload: unknown
}

/** One message the mailbox kept, as ``POST /api/apps/{id}/held`` lists it. */
interface HeldRow {
  session_id: string
  from_instance: string
  from_user: string | null
  kind: 'message' | 'session'
  payload: unknown
}

/** A held message as the app receives it — a live ``app:event`` minus
 *  its ``type``. */
export interface HeldEvent {
  kind: 'message' | 'session'
  sessionId: string
  fromInstance: string
  fromUser?: string
  payload: unknown
}

/** What ``app.context`` says about why the app was opened. */
export interface LaunchContext {
  sessionId?: string
  invite?: AppInvite
}

/** Accepts already made, by ``<appId>/<sessionId>``: a retry or a new
 *  frame for the same Play hands over the same invite instead of asking
 *  again (the server would answer 409 — it was taken in). */
const accepted = new Map<string, Promise<AppInvite | null>>()

/** Take in the one invite *sessionId* (once per page). ``null`` when it no
 *  longer waits — the app then opens that game if it knows it. */
function acceptOnce(appId: string, sessionId: string): Promise<AppInvite | null> {
  const key = `${appId}/${sessionId}`
  let p = accepted.get(key)
  if (!p) {
    p = api.post<{ session: PendingSession }>(
      `/api/apps/${encodeURIComponent(appId)}/sessions/${encodeURIComponent(sessionId)}/accept`,
    ).then(({ session: s }) => ({
      sessionId: s.session_id,
      fromInstance: s.from_instance,
      fromUser: s.from_user ?? undefined,
      payload: s.payload,
    }), () => {
      // Not cached: a network failure may succeed on Try again.
      accepted.delete(key)
      return null
    })
    accepted.set(key, p)
  }
  return p
}

/** Tests only. */
export function _resetAcceptedForTests(): void {
  accepted.clear()
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
  const { appId, launchSession } = ctx

  /** The launch context: the one invite "Play" opened the app for. */
  async function launch(): Promise<LaunchContext> {
    if (!launchSession) return {}
    const invite = await acceptOnce(appId, launchSession)
    return invite ? { sessionId: launchSession, invite } : { sessionId: launchSession }
  }

  function reply(r: RpcReply): void {
    iframe.contentWindow?.postMessage(r, '*')
  }

  /** The mailbox, taken once for this frame (the server clears what it
   *  hands over). A failed request hands over nothing: whatever wasn't
   *  cleared waits for the next open. */
  let held: Promise<HeldEvent[]> | null = null
  function takeHeld(): Promise<HeldEvent[]> {
    if (!held) {
      held = Promise.resolve().then(() => api.post<{ messages?: HeldRow[] }>(
        `/api/apps/${encodeURIComponent(appId)}/held`,
      )).then(r => (r?.messages ?? []).map((m): HeldEvent => ({
        kind: m.kind === 'session' ? 'session' : 'message',
        sessionId: m.session_id,
        fromInstance: m.from_instance,
        fromUser: m.from_user ?? undefined,
        payload: m.payload,
      })), () => [])
    }
    return held
  }

  /** Whether the held messages were handed over (pushed or pulled). */
  let heldHandedOver = false
  /** An app that doesn't pull: post its held messages as live events. */
  async function pushHeld(): Promise<void> {
    if (heldHandedOver) return
    heldHandedOver = true
    for (const e of await takeHeld()) {
      iframe.contentWindow?.postMessage({ type: 'app:event', ...e }, '*')
    }
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

        case 'app.ready': {
          // The app says it started (handled by ``onReady`` below for
          // every request); nothing else to do.
          reply({ id, ok: true, result: { ok: true } })
          break
        }

        case 'app.context': {
          // NEVER include a token — the app runs in a sandboxed iframe
          // and must not gain host credentials.
          reply({
            id,
            ok: true,
            result: { appId: ctx.appId, selfUserId: ctx.selfUserId, launch: await launch() },
          })
          // The app listens now: what waited for it follows, unless it
          // pulls held messages itself (``app.held``).
          if (params?.held === 'pull') heldHandedOver = true
          else await pushHeld()
          break
        }

        case 'app.held': {
          // An app that applies held messages itself, once its state is in.
          heldHandedOver = true
          reply({ id, ok: true, result: await takeHeld() })
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
          // An app from before launch contexts: only the invite "Play"
          // opened it for — opening the app accepts nothing else.
          const { invite } = await launch()
          reply({
            id,
            ok: true,
            result: invite
              ? [{
                  session_id: invite.sessionId,
                  from_instance: invite.fromInstance,
                  from_user: invite.fromUser ?? null,
                  payload: invite.payload,
                }]
              : [],
          })
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

  /** Whether ``ctx.onReady`` already fired for this mount. */
  let ready = false

  function onMessage(event: MessageEvent): void {
    // Security: reject any message not originating from our iframe.
    // Do NOT check event.origin — sandboxed iframes emit "null".
    if (event.source !== iframe.contentWindow) return

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const req = event.data as any
    if (!req || typeof req.method !== 'string') return

    if (!ready) {
      ready = true
      ctx.onReady?.()
    }
    // eslint-disable-next-line @typescript-eslint/no-floating-promises
    handleRpc(req as RpcRequest)
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
      // A live invite is only news: it waits in Needs you until "Play".
    }
  })

  window.addEventListener('message', onMessage)

  return () => {
    window.removeEventListener('message', onMessage)
    offWs()
  }
}
