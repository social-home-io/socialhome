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
 *
 * Permissions (owner decision D2): the bridge is the sandbox's only way
 * out, so it only offers what the household agreed to when the app was
 * installed — ``storage`` (saves its own data), ``people`` (lets you pick
 * people) and ``federation`` (sends to other households; implies
 * ``people``), as ``/runtime`` lists them for the installed version. A
 * call outside them is answered ``{ok: false, error: {code:
 * 'permission_denied', permission}}`` without reaching the server, and
 * ``app.context`` lists ``permissions`` so an app can leave out what it
 * may not do. The server checks every route again and is authoritative:
 * its ``403 APP_PERMISSION_DENIED`` comes back as the same typed error.
 *
 * Invites (owner decision D3, "invites go through Social Home"): the bridge
 * offers no way to open a session. ``people.pick({max, suggest})`` asks the
 * host to draw its own invite sheet *outside* the frame (``pickPeople``);
 * the person picks and taps Send, Social Home opens the games, and the app
 * gets back only who was picked and their session ids. One sheet at a time
 * (``busy``); after one closes the same app waits
 * :data:`PICK_COOLDOWN_MS` (``too_soon``, ``retryAfterMs``); the sheet's
 * Send ignores taps for its first 600 ms (InviteSheet.tsx) so an app can't
 * open it under a double tap. ``app.openSession`` answers ``use_people_pick``.
 * ``contacts.list`` is only the people picked and played with in this app,
 * and ``app.send`` only reaches games the server knows for that person
 * (``unknown_session`` otherwise).
 *
 * "Your turn" (owner decision 2026-10-30): ``app.send`` params may carry
 * ``turn: true`` — this move hands the other player the turn. Social Home
 * passes it on (only an exact ``true``) and, while that player's app is
 * closed, shows them one "Your turn with … in …" row per game. Without it
 * a message is informational (a chat line, an emote) and raises nothing.
 *
 * Look (owner decision D14, "apps follow your language and light/dark"):
 * ``app.context`` also carries ``locale``, ``dir``, ``theme`` (``light`` /
 * ``dark``, auto resolved), ``reducedMotion`` and eight colour ``tokens``
 * (plain ``#rrggbb[aa]`` values — appContext.ts). It needs no permission.
 * When any of it changes while the app is open the bridge posts
 * ``{type: 'app.context-changed', context}`` with the same fields — only
 * after the app has spoken, so it never lands in the frame's blank
 * pre-load document. Older SDKs ignore the unknown ``type``.
 *
 * Group tables (group play, v_76): ``group.*`` calls and ``group:event``
 * posts — groupBridge.ts and docs/protocol/app-groups.md. ``group.open
 * ({suggest})`` is the group twin of ``people.pick``: the host draws Social
 * Home's group sheet outside the frame (``openGroup``), the person picks
 * and taps Send, Social Home opens the game, and the app gets back the
 * :type:`GroupSession` (``null`` on Cancel). It shares people.pick's
 * guards — one sheet at a time across frames (``busy``), the per-app
 * cooldown (``too_soon``) and the 600 ms arm on Send. ``app.context``
 * says ``groups: true``; opened from a table (``?session=<table id>``)
 * its ``launch`` is ``{sessionId, group}`` and no invite is accepted (a
 * table is joined through Social Home's own screens).
 */

import { api, ApiError } from '@/api'
import { ws, WsEvent } from '@/ws'
import type { AppPermission } from '@/store/apps'
import type { AppHostContext } from '@/features/apps/appContext'
import {
  GROUP_MAX_PLAYERS,
  GroupBridge,
  GroupRefused,
  groupRefusal,
  heldToEvent,
  isGroupMethod,
  type GroupEvent,
  type GroupSession,
  type HeldGroupRow,
} from '@/features/apps/groupBridge'
import { DrawBridge, isDrawMethod } from '@/features/apps/draw/drawBridge'


/** The typed refusal an app gets for a call outside its permissions. */
export const PERMISSION_DENIED = 'permission_denied'
/** ``app.openSession`` is gone (D3): invites go through ``people.pick``. */
export const USE_PEOPLE_PICK = 'use_people_pick'
/** ``people.pick`` while a sheet is already open (one at a time). */
export const PICK_BUSY = 'busy'
/** ``people.pick`` again within :data:`PICK_COOLDOWN_MS` of the last
 *  sheet closing (``retryAfterMs`` says how long). */
export const PICK_TOO_SOON = 'too_soon'
/** A message for a game the household doesn't know (server
 *  ``APP_SESSION_UNKNOWN``): an app can't message an old opponent on a
 *  new session id. */
export const UNKNOWN_SESSION = 'unknown_session'

/** ``people.pick`` asked for something it can't be (``continue`` without
 *  the game's ``sessionId`` or the person). */
export const PICK_INVALID = 'invalid'

/** After a sheet closes, the same app can't open another for this long:
 *  an app can't keep throwing it up under the person's finger. */
export const PICK_COOLDOWN_MS = 5_000
/** Most people one ``people.pick`` may ask for (the open-invite cap). */
export const PICK_MAX = 10

/** A person the app names: who to suggest in the sheet (a rematch). */
export interface PersonRef {
  instance_id: string
  user_ref: string
  is_local: boolean
}

/** What an app asks the invite sheet for. ``purpose`` is a fixed word,
 *  never the app's own text: Social Home writes every line of the sheet,
 *  so an app can't put words in its mouth. */
export interface PickRequest {
  /** ``play`` — invite someone new; ``continue`` — pick up a game Social
   *  Home no longer knows (``sessionId``) with the person ``suggest``
   *  names ("Continue game with Ben"). */
  purpose: 'play' | 'continue'
  /** How many people may be picked (1…:data:`PICK_MAX`; 1 to continue). */
  max: number
  /** ``continue`` only: the app's own id for the game. */
  sessionId?: string
  /** One person to show selected — the person still taps Send, which
   *  names them. Ignored when they aren't someone the sheet lists. */
  suggest?: PersonRef
}

/** Most people one ``group.open`` may suggest: every player of the
 *  largest game but the person opening it. */
export const GROUP_SUGGEST_MAX = GROUP_MAX_PLAYERS - 1

/** What an app asks the group sheet for: only whom to start ticked
 *  (well-formed person references; nothing it writes is shown). */
export interface GroupOpenRequest {
  suggest: PersonRef[]
}

/** A person picked in the sheet, with the game Social Home opened. */
export interface PickedPerson extends PersonRef {
  display_name: string
  session_id: string
}

export interface BridgeContext {
  appId: string
  selfUserId: string
  /** What the household agreed to (``/runtime``'s ``permissions``): only
   *  these bridge calls are offered. Unknown names grant nothing. */
  permissions: readonly string[]
  /** Show Social Home's invite sheet (outside the frame) and resolve with
   *  who was picked and the games opened for them — ``[]`` on Cancel.
   *  Without it ``people.pick`` answers ``unavailable``. */
  pickPeople?: (req: PickRequest) => Promise<PickedPerson[]>
  /** Show Social Home's group sheet (outside the frame) and resolve with
   *  the group game opened — ``null`` on Cancel. It may reject with a
   *  ``GroupRefused`` (``groups_not_supported``). Without it
   *  ``group.open`` answers ``unavailable``. */
  openGroup?: (req: GroupOpenRequest) => Promise<GroupSession | null>
  /** ``?session=`` the app was opened with ("Play" in Needs you): the
   *  one invite to take in and hand to the app. */
  launchSession?: string
  /** Called once, on the app's first bridge request: the app's code runs
   *  and talks to the host — it started. An SDK that posts ``app.ready``
   *  says so explicitly; older ones send ``app.context`` / ``store.*``
   *  first, which counts the same. A frame that never asks (a bundle
   *  error page, a crashed script) never calls it. */
  onReady?: () => void
  /** The look to hand the app (D14) — ``currentAppContext`` in the SPA. */
  hostContext?: () => AppHostContext
  /** Watch the look; the bridge pushes ``app.context-changed`` on each call.
   *  Returns the function that stops watching. */
  watchHostContext?: (cb: (c: AppHostContext) => void) => () => void
  /** Show Social Home's own gift-draw result sheet (outside the frame)
   *  for the table *sessionId* — ``draw.reveal``. The app gets nothing
   *  back. Without it ``draw.reveal`` answers ``unavailable``. */
  revealDraw?: (sessionId: string) => void
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
  /** ``permission`` is set on a ``permission_denied`` refusal. */
  error: { code: string; message: string; permission?: AppPermission; retryAfterMs?: number }
}

/** A call outside the app's permissions (here or on the server). */
class PermissionDenied extends Error {
  constructor(public readonly permission: AppPermission) {
    super(`This app doesn't have the ${permission} permission.`)
  }
}

/** A refusal with a stable code the SDK maps (``ShError.code``). */
class Refused extends Error {
  constructor(public readonly code: string, message: string, public readonly retryAfterMs?: number) {
    super(message)
  }
}

/** One sheet at a time (invite or group), across every app frame on the page. */
let sheetOpen = false
/** When each app's last sheet closed (``Date.now()``), for the cooldown. */
const sheetClosedAt = new Map<string, number>()

/** Tests only. */
export function _resetPickForTests(): void {
  sheetOpen = false
  sheetClosedAt.clear()
}

/** A person reference the app passed, or ``undefined`` when it isn't one. */
function personRef(v: unknown): PersonRef | undefined {
  const o = v as Partial<PersonRef> | null | undefined
  if (!o || typeof o !== 'object') return undefined
  if (typeof o.instance_id !== 'string' || !o.instance_id) return undefined
  if (typeof o.user_ref !== 'string' || !o.user_ref) return undefined
  if (typeof o.is_local !== 'boolean') return undefined
  return { instance_id: o.instance_id, user_ref: o.user_ref, is_local: o.is_local }
}

/** ``people.pick`` params as a :type:`PickRequest` — nothing the app wrote
 *  is shown as text. */
export function pickRequest(params: unknown): PickRequest {
  const p = (params ?? {}) as { max?: unknown; suggest?: unknown; purpose?: unknown; sessionId?: unknown }
  const suggest = personRef(p.suggest)
  if (p.purpose === 'continue') {
    // One game, one person, both named by the app — the person confirms.
    const sid = typeof p.sessionId === 'string' && SESSION_ID.test(p.sessionId) ? p.sessionId : null
    if (!sid || !suggest) {
      throw new Refused(PICK_INVALID, 'Continuing a game needs its sessionId and the person (suggest).')
    }
    return { purpose: 'continue', max: 1, sessionId: sid, suggest }
  }
  const max = typeof p.max === 'number' && Number.isFinite(p.max) ? Math.floor(p.max) : 1
  const req: PickRequest = { purpose: 'play', max: Math.min(PICK_MAX, Math.max(1, max)) }
  if (suggest) req.suggest = suggest
  return req
}

/** ``group.open`` params as a :type:`GroupOpenRequest`: each suggestion a
 *  well-formed person (bad ones dropped, repeats once), at most
 *  :data:`GROUP_SUGGEST_MAX`. */
export function groupOpenRequest(params: unknown): GroupOpenRequest {
  const raw = (params as { suggest?: unknown } | null | undefined)?.suggest
  const suggest: PersonRef[] = []
  const seen = new Set<string>()
  for (const v of Array.isArray(raw) ? raw : []) {
    if (suggest.length >= GROUP_SUGGEST_MAX) break
    const ref = personRef(v)
    if (!ref) continue
    const k = `${ref.instance_id}\u0000${ref.user_ref}`
    if (seen.has(k)) continue
    seen.add(k)
    suggest.push(ref)
  }
  return { suggest }
}

/** Show one of Social Home's sheets for *appId* under the guards every
 *  sheet shares: one at a time across frames (``busy``), and
 *  :data:`PICK_COOLDOWN_MS` after the app's last one closed
 *  (``too_soon``). */
async function withSheet<T>(appId: string, show: () => Promise<T>): Promise<T> {
  if (sheetOpen) {
    throw new Refused(PICK_BUSY, 'An invite sheet is already open.')
  }
  const wait = (sheetClosedAt.get(appId) ?? -Infinity) + PICK_COOLDOWN_MS - Date.now()
  if (wait > 0) {
    throw new Refused(PICK_TOO_SOON, 'The invite sheet was just closed.', wait)
  }
  sheetOpen = true
  try {
    return await show()
  } finally {
    sheetOpen = false
    sheetClosedAt.set(appId, Date.now())
  }
}

/** An app's own session id (what ``?session=`` and the server accept). */
const SESSION_ID = /^[A-Za-z0-9_-]{1,128}$/

/** The permission a game with *target* takes: ``people`` for someone in
 *  this household, ``federation`` for anyone in another one (the same rule
 *  the server applies). Anything not clearly local counts as remote. */
function targetPermission(target: unknown): AppPermission {
  return (target as { is_local?: unknown } | null)?.is_local === true ? 'people' : 'federation'
}

/** The permission each bridge call needs (``null``: none). A method not
 *  listed is unknown and refused anyway. */
function permissionFor(method: string, params: { target?: unknown } | undefined): AppPermission | null {
  switch (method) {
    case 'store.get':
    case 'store.set':
    case 'store.delete':
    case 'store.list':
      return 'storage'
    case 'contacts.list':
    case 'people.pick':
    case 'app.held':
    case 'app.pendingSessions':
      return 'people'
    case 'peers.list':
      return 'federation'
    case 'app.send':
    case 'app.cancelSession':
      return targetPermission(params?.target)
    default:
      // Every group and draw call is about people at a table.
      return isGroupMethod(method) || isDrawMethod(method) ? 'people' : null
  }
}

/** The server's ``403 APP_PERMISSION_DENIED``, as the permission it names. */
function serverDenied(err: unknown): AppPermission | null {
  if (!(err instanceof ApiError) || err.code !== 'APP_PERMISSION_DENIED') return null
  const p = (err.extra?.params as { permission?: unknown } | undefined)?.permission
  return p === 'storage' || p === 'people' || p === 'federation' ? p : null
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
  /** Opened from a table the person has a place at (Join / Play). */
  group?: GroupSession
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
  const granted = new Set(ctx.permissions)
  // ``federation`` implies ``people`` (as on the server).
  if (granted.has('federation')) granted.add('people')
  const has = (p: AppPermission): boolean => granted.has(p)
  /** The known permissions the app has, for ``app.context``. */
  // In the server's order (``APP_PERMISSIONS``, domain/apps.py).
  const permissions = (['storage', 'people', 'federation'] as const).filter(has)

  /** The launch context: the one invite "Play" opened the app for. An app
   *  without ``people`` has no games, so nothing is accepted for it. */
  async function launch(): Promise<LaunchContext> {
    if (!launchSession || !has('people')) return {}
    // A table first: its seat is never "accepted" like a 1:1 invite.
    const group = await groups.launch(launchSession)
    if (group) return { sessionId: launchSession, group }
    const invite = await acceptOnce(appId, launchSession)
    return invite ? { sessionId: launchSession, invite } : { sessionId: launchSession }
  }

  function reply(r: RpcReply): void {
    iframe.contentWindow?.postMessage(r, '*')
  }

  /** The mailbox, taken once per half for this frame (the server clears
   *  what it hands over): 1:1 messages (``one``) and each table player's
   *  latest (``group``). An app that pushes takes both in one request; an
   *  app that pulls takes each half with its own call (``?only=``), so
   *  pulling one never clears the other. A failed request hands over
   *  nothing: whatever wasn't cleared waits for the next open. */
  type Held = { one: HeldEvent[]; group: GroupEvent[] }
  let heldOne: Promise<HeldEvent[]> | null = null
  let heldGroup: Promise<GroupEvent[]> | null = null
  function fetchHeld(only?: 'messages' | 'group'): Promise<Held> {
    const q = only ? `?only=${only}` : ''
    return Promise.resolve().then(() => api.post<{ messages?: HeldRow[]; group_messages?: HeldGroupRow[] }>(
      `/api/apps/${encodeURIComponent(appId)}/held${q}`,
    )).then(r => ({
      one: (r?.messages ?? []).map((m): HeldEvent => ({
        kind: m.kind === 'session' ? 'session' : 'message',
        sessionId: m.session_id,
        fromInstance: m.from_instance,
        fromUser: m.from_user ?? undefined,
        payload: m.payload,
      })),
      group: (r?.group_messages ?? []).map(heldToEvent),
    }), () => ({ one: [], group: [] }))
  }
  function takeHeld(): Promise<Held> {
    if (!heldOne && !heldGroup) {
      const both = fetchHeld()
      heldOne = both.then(h => h.one)
      heldGroup = both.then(h => h.group)
    }
    return Promise.all([takeOne(), takeGroup()]).then(([one, group]) => ({ one, group }))
  }
  function takeOne(): Promise<HeldEvent[]> {
    if (!heldOne) heldOne = fetchHeld('messages').then(h => h.one)
    return heldOne
  }
  function takeGroup(): Promise<GroupEvent[]> {
    if (!heldGroup) heldGroup = fetchHeld('group').then(h => h.group)
    return heldGroup
  }

  const groups = new GroupBridge(
    appId,
    msg => iframe.contentWindow?.postMessage(msg, '*'),
    async () => {
      heldHandedOver = true
      return takeGroup()
    },
  )

  /** The gift draw (v_78): ``draw.*`` calls and ``draw:event`` posts. */
  const draws = new DrawBridge(
    appId,
    msg => iframe.contentWindow?.postMessage(msg, '*'),
    ctx.revealDraw,
  )

  /** Whether the held messages were handed over (pushed or pulled). */
  let heldHandedOver = false
  /** An app that doesn't pull: post its held messages as live events. */
  async function pushHeld(): Promise<void> {
    if (heldHandedOver) return
    heldHandedOver = true
    if (!has('people')) return
    const { one, group } = await takeHeld()
    for (const e of one) {
      iframe.contentWindow?.postMessage({ type: 'app:event', ...e }, '*')
    }
    for (const event of group) {
      iframe.contentWindow?.postMessage({ type: 'group:event', event }, '*')
    }
  }

  async function handleRpc(req: RpcRequest): Promise<void> {
    const { id, method, params } = req

    try {
      const needs = permissionFor(method, params)
      if (needs && !has(needs)) throw new PermissionDenied(needs)
      if (method === 'group.open') {
        // D3: the app asks; Social Home's group sheet (outside the frame)
        // asks the person and opens the game. Same guards as people.pick.
        const open = ctx.openGroup
        if (!open) {
          throw new Refused('unavailable', 'This Social Home can\'t show the group sheet here.')
        }
        const req = groupOpenRequest(params)
        const session = await withSheet(appId, () => open(req))
        // Later ``app.group`` frames of this game diff against it.
        reply({ id, ok: true, result: session ? groups.keep(session) : null })
        return
      }
      if (isGroupMethod(method)) {
        reply({ id, ok: true, result: await groups.call(method, params) })
        return
      }
      if (isDrawMethod(method)) {
        reply({ id, ok: true, result: await draws.call(method, params) })
        return
      }
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
            result: {
              // The look first: identity fields always win.
              ...ctx.hostContext?.(),
              appId: ctx.appId,
              selfUserId: ctx.selfUserId,
              permissions,
              // This host speaks ``group.*`` (group play, v_76).
              groups: true,
              // …and ``draw.*`` (the gift draw, v_78).
              draw: true,
              launch: await launch(),
            },
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
          reply({ id, ok: true, result: await takeOne() })
          break
        }

        case 'app.send': {
          const { session_id, target, payload, turn } = params ?? {}
          await api.post(`/api/apps/${encodeURIComponent(appId)}/messages`, {
            session_id,
            target,
            payload,
            // "Your turn": only an exact ``true`` says so.
            ...(turn === true ? { turn: true } : {}),
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
          // Only who the person picked and who they play with in this app
          // (D3) — the server never hands an app the roster.
          const resp = await api.get<{ contacts: unknown[] }>(
            `/api/apps/${encodeURIComponent(appId)}/contacts`,
          )
          reply({ id, ok: true, result: resp.contacts })
          break
        }

        case 'people.pick': {
          // D3: the app asks; Social Home draws the sheet outside the frame,
          // the person picks and taps Send, and Social Home opens the games.
          // The app gets back only who was picked and the session ids.
          if (!ctx.pickPeople) {
            throw new Refused('unavailable', 'This Social Home can\'t show the invite sheet here.')
          }
          const pickReq = pickRequest(params)
          const pick = ctx.pickPeople
          const picked = await withSheet(appId, () => pick(pickReq))
          reply({
            id,
            ok: true,
            result: picked.map(p => ({
              instance_id: p.instance_id,
              user_ref: p.user_ref,
              is_local: p.is_local,
              display_name: p.display_name,
              session_id: p.session_id,
            })),
          })
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
          // Gone (D3): an app never opens a session itself — it asks for
          // the invite sheet (``people.pick``), where the person picks and
          // Social Home sends. Refused with a code an SDK ≥ 1.4 maps; an
          // older app sees the call fail (its games and Play still work).
          throw new Refused(
            USE_PEOPLE_PICK,
            'Invites go through Social Home: use people.pick (app SDK 1.4 or newer).',
          )
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
      const denied = err instanceof PermissionDenied ? err.permission : serverDenied(err)
      if (denied) {
        reply({
          id,
          ok: false,
          error: { code: PERMISSION_DENIED, message: new PermissionDenied(denied).message, permission: denied },
        })
        return
      }
      const refused = err instanceof GroupRefused ? err : groupRefusal(err)
      if (refused) {
        reply({
          id,
          ok: false,
          error: {
            code: refused.code,
            message: refused.message,
            ...(refused.retryAfterMs !== undefined ? { retryAfterMs: Math.ceil(refused.retryAfterMs) } : {}),
          },
        })
        return
      }
      if (err instanceof Refused) {
        reply({
          id,
          ok: false,
          error: {
            code: err.code,
            message: err.message,
            ...(err.retryAfterMs !== undefined ? { retryAfterMs: Math.ceil(err.retryAfterMs) } : {}),
          },
        })
        return
      }
      if (err instanceof ApiError && err.code === 'APP_SESSION_UNKNOWN') {
        reply({
          id,
          ok: false,
          error: { code: UNKNOWN_SESSION, message: 'This app can only message games you started or accepted.' },
        })
        return
      }
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
    // Every app message is from a person in a game: ``people`` only. A live
    // invite (``verb: 'open'``) never reaches the app (D3): it waits in
    // Needs you, and the app learns of the inviter only on Play.
    const payload = evt.data?.payload as { verb?: unknown } | null | undefined
    if (evt.data?.kind === 'session' && payload?.verb === 'open') return
    if (evt.data?.app_id === appId && has('people')) {
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

  // Group tables: messages and roster changes as ``group:event`` posts.
  const offGroups = groups.listen(() => has('people'))
  // The gift draw: its state (never a result) as ``draw:event`` posts.
  const offDraws = draws.listen(() => has('people'))

  window.addEventListener('message', onMessage)

  // The person switched language or theme while the app is open.
  const stopWatching = ctx.watchHostContext?.((context) => {
    if (!ready) return // the app asks for its context first anyway
    iframe.contentWindow?.postMessage({ type: 'app.context-changed', context }, '*')
  })

  return () => {
    window.removeEventListener('message', onMessage)
    offWs()
    offGroups()
    offDraws()
    stopWatching?.()
  }
}
