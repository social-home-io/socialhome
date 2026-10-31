/**
 * Settings › Games — "Games allowed here" (group play §4): which apps
 * people in this space may start as a game. Nothing is allowed until
 * someone here turns a game on; the Games section appears with the first.
 *
 * Who: the owner, admins AND moderators (owner decision D9 — the one
 * setting a moderator changes). The copy says so; never "only admins".
 *
 * The list: this home's apps that can be played as a game in a space,
 * the catalog's, and anything already on the list (so it can always come
 * off) — each with its players range, age rating and whether this home
 * has it ("In your home" / "Turned off here" / "Not in your home yet":
 * the list never adds an app to anyone's home).
 *
 * Each switch saves as it flips (the settings page's rule) with the whole
 * list (``PUT /api/spaces/{id}/games/allowed``), one save at a time; the
 * row says "Saving…" meanwhile and a failure puts the switch back with a
 * toast. On a space hosted by another household the edit goes to that
 * household (and may wait for the owner's approval): the toast says so and
 * the list changes once the host applies it. A host household too old for
 * games: a note, switches off.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { AppIcon } from '@/features/apps/AppDialogs'
import { ratingLabel } from '@/features/apps/appFacts'
import { locale, t } from '@/i18n/i18n'
import { catalog, catalogError, catalogLoaded, catalogLoading, installedApps } from '@/store/apps'
import { useGameApps } from '../games/SpaceGamesSection'
import type { Space } from '@/types'
import { gameChoices, playersLabel, type GameChoice } from '../games/spaceGames'
import { useServerValue } from './spacePatch'
import { Switch } from '@/components/Switch'

/** At most this many games on a space's list (``SPACE_ALLOWED_APPS_MAX``). */
export const ALLOWED_APPS_MAX = 20

/** The list with *appId* turned *on* (or off), in a stable order. */
export function nextAllowedApps(current: readonly string[], appId: string, on: boolean): string[] {
  const rest = current.filter((id) => id !== appId)
  return on ? [...rest, appId] : rest
}

function hereLabel(c: GameChoice): string {
  switch (c.here) {
    case 'yes': return t('space.games.settings.here_yes')
    case 'off': return t('space.games.settings.here_off')
    default: return t('space.games.settings.here_no')
  }
}

export function GamesSection({ space, onUpdate }: { space: Space; onUpdate: () => void }) {
  const [allowed, setAllowed] = useServerValue<string[]>(space.features?.allowed_apps ?? [])
  const [saving, setSaving] = useState<string | null>(null)
  const [hostSupports, setHostSupports] = useState(true)
  const lang = locale.value || 'en'

  useGameApps()
  // Whether the space's host household keeps a games list (v_77).
  useEffect(() => {
    let stop = false
    api.get<{ host_supports?: boolean }>(`/api/spaces/${space.id}/games`)
      .then((b) => { if (!stop) setHostSupports(b?.host_supports !== false) })
      .catch(() => { /* unknown: let the save answer */ })
    return () => { stop = true }
  }, [space.id])

  const choices = gameChoices(allowed, installedApps.value, catalog.value, lang)
  const full = allowed.length >= ALLOWED_APPS_MAX

  const flip = async (appId: string, on: boolean) => {
    if (saving) return
    const before = allowed
    const next = nextAllowedApps(allowed, appId, on)
    const name = choices.find((c) => c.app_id === appId)?.name ?? appId
    setAllowed(next)
    setSaving(appId)
    try {
      const res = await api.put<{ allowed_apps: string[], forwarded?: boolean }>(
        `/api/spaces/${space.id}/games/allowed`, { allowed_apps: next },
      )
      if (res?.forwarded) {
        // The host decides; until its config comes back, show ours.
        setAllowed(before)
        showToast(t('space.settings.forwarded'), 'info')
      } else {
        setAllowed(res?.allowed_apps ?? next)
        showToast(t(on ? 'space.games.settings.on_toast' : 'space.games.settings.off_toast', { name }), 'success')
      }
      onUpdate()
    } catch (err: unknown) {
      setAllowed(before)
      showToast((err as Error)?.message || t('space.settings.save_failed'), 'error')
    } finally {
      setSaving(null)
    }
  }

  return (
    <div class="sh-ss-card sh-games-settings">
      {!hostSupports && (
        <p class="sh-games-note" role="status">{t('space.games.host_too_old')}</p>
      )}
      {choices.length === 0 ? (
        <p class="sh-ss-hint" role="status">
          {catalogLoading.value && !catalogLoaded.value
            ? t('common.loading')
            : t('space.games.settings.no_apps')}
        </p>
      ) : (
        <ul class="sh-games-settings__list">
          {choices.map((c) => {
            const on = allowed.includes(c.app_id)
            const id = `sh-game-allowed-${c.app_id}`
            const metaId = `${id}-meta`
            const meta = [
              c.players ? playersLabel(c.players.min, c.players.max) : null,
              ratingLabel(c.min_age),
            ].filter(Boolean).join(' · ')
            return (
              <li key={c.app_id} class="sh-games-settings__row">
                <AppIcon src={c.icon} size="sm" />
                <div class="sh-games-settings__text">
                  <label for={id} class="sh-games-settings__name">{c.name}</label>
                  <span id={metaId} class="sh-games-settings__meta">
                    {meta}
                    {' · '}
                    <span class={`sh-games-settings__here sh-games-settings__here--${c.here}`}>{hereLabel(c)}</span>
                    {saving === c.app_id && <> · <span role="status">{t('space.games.settings.saving')}</span></>}
                  </span>
                </div>
                {/* While a save runs the others wait (``flip`` ignores them);
                    none is disabled, so the one just flipped keeps the focus. */}
                <Switch id={id} checked={on} aria-describedby={metaId}
                        aria-disabled={saving !== null ? 'true' : undefined}
                        aria-busy={saving === c.app_id ? 'true' : undefined}
                        disabled={!hostSupports || (!on && full)}
                        onChange={(next) => { void flip(c.app_id, next) }} />
              </li>
            )
          })}
        </ul>
      )}
      {catalogError.value && (
        <p class="sh-ss-hint">{t('space.games.settings.catalog_failed')}</p>
      )}
      {full && <p class="sh-ss-hint">{t('space.games.settings.full', { n: String(ALLOWED_APPS_MAX) })}</p>}
      <p class="sh-games-note">{t('space.games.settings.off_note')}</p>
      <p class="sh-ss-hint">{t('space.games.settings.who')}</p>
    </div>
  )
}
