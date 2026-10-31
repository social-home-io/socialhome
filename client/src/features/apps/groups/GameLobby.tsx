/**
 * GameLobby — Social Home's own "Who's in" screen for one group game
 * (design §3.1). The same screen for every app: who is in, how many more
 * are needed, Start for the person who started the game, Join / Decline
 * for an invite, Leave, Remove, Close this game, and "Open {app}" for the
 * app's own setup or play.
 *
 * Shared by the game's page (``GameLobbyPage``, ``/apps/:appId/tables/
 * :sessionId``) and the space Games section (PR 7). Self-loading and
 * live: it reads ``GET /api/apps/{appId}/groups/{sessionId}`` and follows
 * the ``app.group`` frames for that game (a frame with ``group`` replaces
 * the view; a frame with ``change`` asks again).
 *
 * Props:
 * - ``appId``     the app (a Social Home app id).
 * - ``sessionId`` the game.
 * - ``appName``   optional fallback name when the catalog doesn't know the
 *                 app (the catalog's name in the viewer's language wins,
 *                 then the installed name, then this, then ``appId``).
 * - ``onClosed``  optional; called after the person closed the game here.
 *
 * Words: never "host", "table", "lobby" — "Started this game", "game",
 * "Who's in", "free seat" (the glossary's „freier Platz“). Names are the
 * roster's display names; the view carries no usernames.
 *
 * Tap-jacking: Start ignores activation for :data:`START_ARM_MS` after it
 * turns on (the InviteSheet ``SEND_ARM_MS`` pattern) — a live roster can
 * turn it on under the finger. Remove is held behind an Undo toast and
 * sent when the toast goes (or this screen does): the server can't undo
 * a removal.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { ApiError } from '@/api'
import { appHref } from '@/baseUrl'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { confirmDialog } from '@/components/confirm'
import { dismissToast, showToast } from '@/components/Toast'
import { isOne, locale, t } from '@/i18n/i18n'
import { catalog, catalogLoaded, installedApps, loadCatalog, localized } from '@/store/apps'
import {
  closeGroup,
  declineGroup,
  getGroup,
  joinGroup,
  leaveGroup,
  removeFromGroup,
  seatedPlayers,
  startGroup,
  toLobbyGame,
  type LobbyGame,
} from '@/store/appGroups'
import { relativeFutureTime } from '@/utils/relativeTime'
import { ws, type WsEvent } from '@/ws'
import { AppIcon } from '@/features/apps/AppDialogs'
import type { GroupView } from '@/features/apps/groupBridge'
import { SeatList } from './SeatList'
import { refusalText } from './refusal'

/** Start ignores taps for this long after it turns on. */
export const START_ARM_MS = 600

/** Each has an ``apps.lobby.ended.<reason>`` line (else ``…ended.other``).
 *  Looked up dynamically, like ``apps.lobby.left`` / ``.removed`` /
 *  ``.declined`` for a place that's over. */
const END_REASONS = new Set([
  'finished', 'cancelled', 'expired', 'host_gone', 'app_removed', 'closed_by_moderator',
])

type View =
  | { kind: 'loading' }
  | { kind: 'ready'; game: LobbyGame }
  | { kind: 'gone' }
  | { kind: 'failed' }
  | { kind: 'out'; why: 'left' | 'removed' | 'declined' }

export interface GameLobbyProps {
  appId: string
  sessionId: string
  appName?: string
  onClosed?: () => void
}

export function GameLobby({ appId, sessionId, appName, onClosed }: GameLobbyProps) {
  const [view, setView] = useState<View>({ kind: 'loading' })
  const [attempt, setAttempt] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** Seats removed here whose Undo toast is still up. */
  const [removing, setRemoving] = useState<number[]>([])
  const pending = useRef(new Map<number, { toast: number, commit: () => void }>())
  /** When Start last turned on: taps before START_ARM_MS are ignored. */
  const armedAt = useRef<number | null>(null)

  const entry = catalog.value.find(e => e.app_id === appId)
  const installed = installedApps.value.find(a => a.app_id === appId)
  const name = entry
    ? localized(entry, locale.value || 'en').name
    : installed?.name ?? appName ?? appId

  useEffect(() => {
    if (!catalogLoaded.value) void loadCatalog()
  }, [])

  /** Ask the server; *change* is the ``app.group`` change that prompted it. */
  const refresh = async (change?: unknown) => {
    try {
      const game = await getGroup(appId, sessionId)
      setView({ kind: 'ready', game })
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setView(change === 'left' || change === 'removed' ? { kind: 'out', why: change } : { kind: 'gone' })
      } else {
        // Keep a game already on screen; only a first load fails outright.
        setView(v => (v.kind === 'ready' ? v : { kind: 'failed' }))
      }
    }
  }

  useEffect(() => {
    setView({ kind: 'loading' })
    void refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId, sessionId, attempt])

  useEffect(() => ws.on('app.group', (evt: WsEvent) => {
    const d = evt.data
    if (d?.app_id !== appId || d?.session_id !== sessionId) return
    if (d.group) {
      setView({ kind: 'ready', game: toLobbyGame(d.group as GroupView) })
    } else if (d.change) {
      void refresh(d.change)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [appId, sessionId])

  // A Remove still waiting for its toast is sent when this screen goes.
  useEffect(() => () => {
    for (const p of pending.current.values()) {
      dismissToast(p.toast)
      p.commit()
    }
    pending.current.clear()
  }, [])

  const game = view.kind === 'ready' ? view.game : null
  const seated = game ? seatedPlayers(game).filter(p => !removing.includes(p.seat)) : []
  const n = seated.length
  const need = game ? Math.max(0, game.min - n) : 0
  const lobby = game?.state === 'lobby'
  const isSeated = game?.myState === 'seated'
  const starter = !!game && game.isHost && isSeated
  const canStart = starter && lobby && need === 0
  // Arm Start the render it turns on (a ref: no extra render needed).
  if (!canStart) armedAt.current = null
  else if (armedAt.current === null) armedAt.current = Date.now()

  const starterName = game?.players.find(p => p.seat === game.host)?.name ?? ''
  const openHref = appHref(`/apps/${encodeURIComponent(appId)}?session=${encodeURIComponent(sessionId)}`)
  const backHref = appHref('/apps')

  async function act(fn: () => Promise<void>) {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await fn()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  const join = () => act(async () => {
    setView({ kind: 'ready', game: await joinGroup(appId, sessionId) })
  })
  const decline = () => act(async () => {
    await declineGroup(appId, sessionId)
    setView({ kind: 'out', why: 'declined' })
  })
  const leave = async () => {
    const ok = await confirmDialog(t('apps.lobby.leave_body'), {
      title: t('apps.lobby.leave_title'),
      confirmLabel: t('apps.lobby.leave'),
      destructive: true,
    })
    if (!ok) return
    await act(async () => {
      await leaveGroup(appId, sessionId)
      setView({ kind: 'out', why: 'left' })
    })
  }
  const close = async () => {
    const ok = await confirmDialog(t('apps.lobby.close_body'), {
      title: t('apps.lobby.close_title'),
      confirmLabel: t('apps.lobby.close'),
      destructive: true,
    })
    if (!ok) return
    await act(async () => {
      await closeGroup(appId, sessionId)
      onClosed?.()
      await refresh()
    })
  }
  const start = async () => {
    // Tap-jacking guard: a tap before the person could see Start turn on
    // is not a decision.
    if (!game || !canStart || armedAt.current === null) return
    if (Date.now() - armedAt.current < START_ARM_MS) return
    const free = game.max - n
    if (free > 0) {
      const ok = await confirmDialog(
        `${t(isOne(free) ? 'apps.lobby.free_left_one' : 'apps.lobby.free_left', { n: String(free) })} ${
          t(game.lateJoin ? 'apps.lobby.late_join' : 'apps.lobby.no_late_join')}`,
        {
          title: t(isOne(n) ? 'apps.lobby.start_confirm_one' : 'apps.lobby.start_confirm', { n: String(n) }),
          confirmLabel: t('apps.lobby.start'),
        },
      )
      if (!ok) return
    }
    await act(async () => {
      setView({ kind: 'ready', game: await startGroup(appId, sessionId) })
    })
  }
  const remove = (seat: number) => {
    const who = game?.players.find(p => p.seat === seat)?.name ?? ''
    if (pending.current.has(seat)) return
    setRemoving(cur => [...cur, seat])
    const settle = () => {
      pending.current.delete(seat)
      setRemoving(cur => cur.filter(s => s !== seat))
    }
    let done = false
    const commit = () => {
      if (done) return
      done = true
      pending.current.delete(seat)
      removeFromGroup(appId, sessionId, seat)
        .then(g => { setView({ kind: 'ready', game: g }) })
        .catch(() => { showToast(t('apps.lobby.remove_failed', { name: who }), 'error') })
        .finally(() => { setRemoving(cur => cur.filter(s => s !== seat)) })
    }
    const toast = showToast(t('apps.lobby.removed_toast', { name: who }), 'info', {
      action: {
        label: t('common.undo'),
        onClick: () => {
          if (done) return
          done = true
          settle()
        },
      },
      onExpire: commit,
    })
    pending.current.set(seat, { toast, commit })
  }

  // ── Render ──────────────────────────────────────────────────────────

  const head = (
    <header class="sh-game-lobby__head">
      <AppIcon src={entry?.icon_url ?? installed?.icon ?? null} />
      <div class="sh-game-lobby__titles">
        <h2 class="sh-game-lobby__title">{name}</h2>
        {game?.note && <q class="sh-game-lobby__note">{game.note}</q>}
      </div>
    </header>
  )
  const back = <a class="sh-btn sh-btn--secondary" href={backHref}>{t('apps.host.back_to_apps')}</a>

  if (view.kind === 'loading') {
    return (
      <section class="sh-game-lobby">
        <div class="sh-game-lobby__status" aria-live="polite"><Spinner size={32} label={t('apps.lobby.loading')} /></div>
      </section>
    )
  }
  if (view.kind === 'failed') {
    return (
      <section class="sh-game-lobby">
        {head}
        <div class="sh-game-lobby__state" role="alert">
          <p>{t('apps.lobby.load_failed')}</p>
          <div class="sh-game-lobby__actions">
            <Button onClick={() => setAttempt(a => a + 1)}>{t('common.try_again')}</Button>
          </div>
        </div>
      </section>
    )
  }
  if (view.kind === 'gone') {
    return (
      <section class="sh-game-lobby">
        {head}
        <div class="sh-game-lobby__state" role="status">
          <h3 class="sh-game-lobby__state-title">{t('apps.lobby.not_found')}</h3>
          <p>{t('apps.lobby.not_found_body')}</p>
          <div class="sh-game-lobby__actions">{back}</div>
        </div>
      </section>
    )
  }
  const out = view.kind === 'out'
    ? view.why
    : game?.myState === 'left' || game?.myState === 'removed' ? game.myState : null
  if (out) {
    return (
      <section class="sh-game-lobby">
        {head}
        <div class="sh-game-lobby__state" role="status">
          <p>{t(`apps.lobby.${out}`)}</p>
          <div class="sh-game-lobby__actions">{back}</div>
        </div>
      </section>
    )
  }
  if (!game) return null

  if (game.state === 'ended') {
    const reason = game.endReason && END_REASONS.has(game.endReason) ? game.endReason : 'other'
    return (
      <section class="sh-game-lobby">
        {head}
        <div class="sh-game-lobby__state" role="status">
          <p>{t(`apps.lobby.ended.${reason}`, { name })}</p>
          <div class="sh-game-lobby__actions">{back}</div>
        </div>
      </section>
    )
  }

  const invited = game.myState === 'invited'
  const joining = game.myState === 'joining'
  const closes = lobby && game.expiresAt ? relativeFutureTime(game.expiresAt) : null
  const meta = `${t('apps.lobby.count', { n: String(n), max: String(game.max) })}${
    closes ? ` · ${t('apps.lobby.closes', { when: closes })}` : ''}`
  const needText = t(isOne(need) ? 'apps.lobby.need_more_one' : 'apps.lobby.need_more', { n: String(need) })
  const pct = game.max > 0 ? Math.min(100, Math.round((n / game.max) * 100)) : 0

  let status: string[] = []
  if (game.state === 'playing') status = [t('apps.lobby.playing')]
  else if (invited) status = starterName ? [t('apps.lobby.invited', { name: starterName })] : []
  else if (joining) status = [t('apps.lobby.joining', { name: starterName })]
  else if (starter) {
    status = need > 0
      ? [needText]
      : [`${t('apps.lobby.can_start')} ${t(game.lateJoin ? 'apps.lobby.late_join' : 'apps.lobby.no_late_join')}`]
  } else if (isSeated) {
    status = [t('apps.lobby.wait_start', { name: starterName })]
    if (need > 0) status.push(needText)
  }

  return (
    <section class="sh-game-lobby">
      {head}

      <div class="sh-card sh-game-lobby__card">
        <div class="sh-game-lobby__card-head">
          <h3 class="sh-game-lobby__who">{t('apps.lobby.who_in')}</h3>
          <span class="sh-game-lobby__meta">{meta}</span>
        </div>
        <div
          class="sh-game-lobby__bar"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={game.max}
          aria-valuenow={n}
          aria-label={t('apps.lobby.progress', { n: String(n), max: String(game.max) })}
        >
          <span class="sh-game-lobby__fill" style={{ width: `${pct}%` }} />
        </div>
        <SeatList
          players={game.players.filter(p => !removing.includes(p.seat))}
          // Once a game started nobody else can join (unless the app
          // takes late joins): no free seats to show then.
          max={game.state === 'playing' && !game.lateJoin ? n : game.max}
          host={game.host}
          me={game.me}
          label={t('apps.lobby.who_in')}
          onRemove={starter && lobby ? remove : undefined}
        />
      </div>

      {status.length > 0 && (
        <div class={`sh-game-lobby__status-line${canStart ? ' is-ready' : ''}`} role="status">
          {status.map(s => <p key={s}>{s}</p>)}
          {starter && lobby && game.invited !== null && game.invited > 0 && (
            <p class="sh-game-lobby__sub">
              {t(isOne(game.invited) ? 'apps.lobby.invites_open_one' : 'apps.lobby.invites_open',
                { n: String(game.invited) })}
            </p>
          )}
        </div>
      )}

      {error && <p class="sh-game-lobby__error" role="alert">{error}</p>}

      <div class="sh-game-lobby__actions">
        {invited && (
          <>
            <Button loading={busy} onClick={() => { void join() }}>{t('apps.lobby.join')}</Button>
            <Button variant="secondary" disabled={busy} onClick={() => { void decline() }}>
              {t('apps.lobby.decline')}
            </Button>
          </>
        )}
        {starter && lobby && (
          <Button
            data-testid="lobby-start"
            loading={busy}
            disabled={!canStart}
            onClick={() => { void start() }}
          >
            {t(isOne(n) ? 'apps.lobby.start_with_one' : 'apps.lobby.start_with', { n: String(n) })}
          </Button>
        )}
        {isSeated && (
          <a
            class={`sh-btn ${game.state === 'playing' ? 'sh-btn--primary' : 'sh-btn--secondary'}`}
            href={openHref}
          >
            {t('apps.open_named', { name })}
          </a>
        )}
        {/* Anyone leaves a game being played (it passes on from the person
            who started it); before Start, that person closes it instead. */}
        {isSeated && (game.state === 'playing' || !starter) && (
          <Button variant="ghost" disabled={busy} onClick={() => { void leave() }}>{t('apps.lobby.leave')}</Button>
        )}
        {starter && lobby && (
          <Button variant="ghost" disabled={busy} onClick={() => { void close() }}>{t('apps.lobby.close')}</Button>
        )}
      </div>
    </section>
  )
}
