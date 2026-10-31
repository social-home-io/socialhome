/**
 * SpaceGameCard — one game in a space (group play §3.1): the app's icon
 * and name, who started it, how many play and have joined, how long it
 * stays open, the starter's note, and one main action.
 *
 * The main action (``cardAction``):
 *   - **Join** — take a seat; the game's own screen opens (players,
 *     Start, Leave live there).
 *   - **Play** — a two-player game: the seat starts it at once (D11).
 *   - **Open** / **Play** on your own game — back to it.
 *   - **Full** — shown, disabled.
 *   - **Ask an admin** / **Ask a grown-up** — the app isn't in this home,
 *     or is off (the apps request, D10); a household admin gets **See …**
 *     (the app's page) instead. A join the server refuses because the app
 *     went away meanwhile opens the same request.
 *
 * **Close this game** — the person who started it, and the owner, an
 * admin or a moderator for any game (D9) — asks first. Everyone who
 * joined is told; a moderator's close says a moderator closed it.
 *
 * Joining asks nothing first: being in the space and pressing Join is
 * the consent (D7). Every refusal is a toast in the reader's language.
 */
import { useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { api, ApiError } from '@/api'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { confirmDialog } from '@/components/confirm'
import { showToast } from '@/components/Toast'
import { AppIcon, AskAdminDialog } from '@/features/apps/AppDialogs'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import type { CatalogEntry } from '@/store/apps'
import {
  cardAction, canClose, closesLabel, endedLabel, gamePath, inLobby, isStarter, playersLabel,
  seatedLabel, starterLabel, type AppLook, type SpaceTable,
} from './spaceGames'

/** Join refusals that mean "this app isn't usable here" — ask for it. */
const ASK_CODES = new Set(['APP_NOT_FOUND', 'APP_NOT_ENABLED'])

interface Props {
  spaceId: string
  table: SpaceTable
  look: AppLook
  /** The app's catalog entry (for "Ask an admin"), when the catalog has it. */
  entry: CatalogEntry | null
  /** Owner / admin / moderator of the space: may close any game. */
  canEditGames: boolean
  /** Something changed (joined, closed): re-read the list. */
  onChanged: () => void
  /** The heading level of the app's name (inside a section's h2). */
  headingLevel?: 3 | 4
}

/** A stand-in catalog entry for the request when the catalog has none. */
function entryFor(table: SpaceTable, look: AppLook, entry: CatalogEntry | null): CatalogEntry {
  return entry ?? {
    app_id: table.app_id, name: look.name, latest_version: '', description: null,
    icon_url: look.icon, capabilities: [],
  }
}

export function SpaceGameCard({
  spaceId, table, look, entry, canEditGames, onChanged, headingLevel = 3,
}: Props) {
  const { route } = useLocation()
  const [busy, setBusy] = useState(false)
  const [closing, setClosing] = useState(false)
  const [asking, setAsking] = useState(false)
  const householdAdmin = currentUser.value?.is_admin === true
  const grownUp = currentUser.value?.protected === true
  const action = cardAction(table, { householdAdmin })
  const name = look.name
  const H = headingLevel === 4 ? 'h4' : 'h3'
  const ended = endedLabel(table)
  const titleId = `game-${table.session_id}`

  const join = async () => {
    setBusy(true)
    try {
      await api.post(`/api/spaces/${spaceId}/games/${encodeURIComponent(table.session_id)}/join`)
      onChanged()
      route(gamePath(table))
    } catch (err: unknown) {
      if (err instanceof ApiError && ASK_CODES.has(err.code ?? '')) {
        setAsking(true)
      } else {
        showToast((err as Error)?.message || t('space.games.join_failed'), 'error')
      }
      onChanged()
    } finally {
      setBusy(false)
    }
  }

  const close = async () => {
    const mine = isStarter(table)
    const ok = await confirmDialog(
      mine ? t('space.games.close_confirm_body_mine') : t('space.games.close_confirm_body', { name }),
      {
        title: t('space.games.close_confirm_title'),
        confirmLabel: t('space.games.close'),
        cancelLabel: t('space.games.keep_open'),
        destructive: true,
      },
    )
    if (!ok) return
    setClosing(true)
    try {
      await api.post(`/api/spaces/${spaceId}/games/${encodeURIComponent(table.session_id)}/close`)
      showToast(t('space.games.closed_toast'), 'success')
      onChanged()
    } catch (err: unknown) {
      showToast((err as Error)?.message || t('space.games.close_failed'), 'error')
    } finally {
      setClosing(false)
    }
  }

  const main = (() => {
    switch (action) {
      case 'join':
        return (
          <Button onClick={() => { void join() }} loading={busy}
                  aria-label={t('space.games.join_named', { name })}>
            {t('space.games.join')}
          </Button>
        )
      case 'take':
        return (
          <Button onClick={() => { void join() }} loading={busy}
                  aria-label={t('space.games.take_named', { name })}>
            {t('space.games.take')}
          </Button>
        )
      case 'open':
        return (
          <a class="sh-btn sh-btn--secondary" href={addBase(gamePath(table))}
             aria-label={t('space.games.open_named', { name })}>
            {t('space.games.open')}
          </a>
        )
      case 'resume':
        return (
          <a class="sh-btn sh-btn--primary" href={addBase(gamePath(table))}
             aria-label={t('space.games.resume_named', { name })}>
            {t('space.games.resume')}
          </a>
        )
      case 'full':
        return <Button variant="secondary" disabled>{t('space.games.full')}</Button>
      case 'see':
        return (
          <a class="sh-btn sh-btn--secondary" href={addBase(`/apps/${encodeURIComponent(table.app_id)}/about`)}>
            {t('space.games.see_app', { name })}
          </a>
        )
      case 'ask':
        return entry?.requested ? (
          <span class="sh-chip sh-chip--muted">{t('apps.ask.asked')}</span>
        ) : (
          <Button variant="secondary" onClick={() => setAsking(true)}
                  aria-label={t(grownUp ? 'apps.ask.button_grown_up_named' : 'apps.ask.button_named', { name })}>
            {t(grownUp ? 'apps.ask.button_grown_up' : 'apps.ask.button')}
          </Button>
        )
      default:
        return null
    }
  })()

  // Why the app can't be played here, under the card's lines.
  const appNote = (action === 'ask' || action === 'see')
    ? (table.app?.installed ? t('space.games.app_off') : t('space.games.app_missing'))
    : null

  return (
    <article class={`sh-game-card sh-game-card--${table.state}`} aria-labelledby={titleId}>
      <div class="sh-game-card__head">
        <AppIcon src={look.icon} size="sm" />
        <div class="sh-game-card__text">
          <H id={titleId} class="sh-game-card__title">{name}</H>
          <p class="sh-game-card__line">{starterLabel(table)}</p>
          <p class="sh-game-card__line sh-game-card__meta">
            {table.max === 2 && action === 'take'
              ? `${playersLabel(2, 2)} · ${t('space.games.anyone_can_take')}`
              : playersLabel(table.min, table.max)}
            {inLobby(table) && <> · {closesLabel(table.expires_at)}</>}
            {table.state === 'playing' && <> · {t('space.games.playing')}</>}
          </p>
          {ended && <p class="sh-game-card__line sh-game-card__meta">{ended}</p>}
          {appNote && <p class="sh-game-card__line sh-game-card__meta">{appNote}</p>}
        </div>
      </div>
      {table.note && <p class="sh-game-card__note">{table.note}</p>}
      <div class="sh-game-card__foot">
        <span class="sh-game-card__seated">{seatedLabel(table)}</span>
        <div class="sh-game-card__actions">
          {canClose(table, canEditGames) && (
            <button type="button" class="sh-link sh-game-card__close" disabled={closing}
                    onClick={() => { void close() }}
                    aria-label={t('space.games.close_named', { name })}>
              {t('space.games.close')}
            </button>
          )}
          {main}
        </div>
      </div>
      {asking && (
        <AskAdminDialog entry={entryFor(table, look, entry)} name={name} open
                        onClose={() => setAsking(false)} />
      )}
    </article>
  )
}
