/**
 * StartGameSheet — "Start a game" in a space (group play §3.1): which
 * game, how many can play (within the app's range), how long it stays
 * open, an optional note, and "Let everyone know".
 *
 * Offers the games on the space's list that this home has added, turned
 * on, and that can be played as a game in a space. A game on the list
 * this home doesn't have shows under "Not in your home yet" with the
 * apps request ("Ask an admin", D10) — a household admin gets a link to
 * the app's page instead.
 *
 * "Let everyone know" (D5): everyone in the space gets one message in
 * Social Home (never a push), and a person may do it for one game a day
 * here — the line under it says so before anyone hits the limit.
 *
 * On success the game's own screen opens (players, Start, Leave).
 */
import { useEffect, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { AppIcon, AskButton } from '@/features/apps/AppDialogs'
import { isOne, locale, t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { catalog, installedApps } from '@/store/apps'
import {
  ANNOUNCES_PER_DAY, LOBBY_DAYS, LOBBY_DEFAULT_DAYS, NOTE_MAX, appLook, gamePath, installedHere, playersLabel,
  seatRange, startableApps, type SpaceTable,
} from './spaceGames'

interface Props {
  spaceId: string
  allowed: readonly string[]
  open: boolean
  onClose: () => void
  /** A game was opened (re-read the list). */
  onStarted: () => void
}

export function StartGameSheet({ spaceId, allowed, open, onClose, onStarted }: Props) {
  const { route } = useLocation()
  const lang = locale.value || 'en'
  const apps = startableApps(allowed, installedApps.value)
  const missing = allowed.filter((id) => !apps.some((a) => a.app_id === id))
  const [appId, setAppId] = useState<string | null>(null)
  const [seats, setSeats] = useState<number | null>(null)
  const [days, setDays] = useState(LOBBY_DEFAULT_DAYS)
  const [note, setNote] = useState('')
  const [announce, setAnnounce] = useState(false)
  const [saving, setSaving] = useState(false)

  // Each opening starts fresh; with one game to choose, it is chosen.
  useEffect(() => {
    if (!open) return
    setAppId(apps.length === 1 ? apps[0].app_id : null)
    setSeats(null)
    setDays(LOBBY_DEFAULT_DAYS)
    setNote('')
    setAnnounce(false)
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps
  // This home's apps arrived after the sheet opened (a deep link).
  useEffect(() => {
    if (open && appId === null && apps.length === 1) setAppId(apps[0].app_id)
  }, [open, apps.length]) // eslint-disable-line react-hooks/exhaustive-deps

  const chosen = apps.find((a) => a.app_id === appId) ?? null
  const range = chosen ? seatRange(chosen) : null
  // Default: as many as the game takes.
  const seatCount = range ? Math.min(range.max, Math.max(range.min, seats ?? range.max)) : null
  const householdAdmin = currentUser.value?.is_admin === true

  const submit = async (e?: Event) => {
    e?.preventDefault()
    if (!chosen || seatCount == null) return
    setSaving(true)
    try {
      const body: Record<string, unknown> = {
        app_id: chosen.app_id, max_players: seatCount, lobby_days: days, announce,
      }
      const trimmed = note.trim()
      if (trimmed) body.note = trimmed
      const res = await api.post<{ table: SpaceTable }>(`/api/spaces/${spaceId}/games`, body)
      showToast(t('space.games.started_toast'), 'success')
      onStarted()
      onClose()
      if (res?.table) route(gamePath(res.table))
    } catch (err: unknown) {
      showToast((err as Error)?.message || t('space.games.start_failed'), 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title={t('space.games.start')} class="sh-start-game">
      <form class="sh-form" onSubmit={(e) => { void submit(e) }}>
        {apps.length > 0 ? (
          <fieldset class="sh-start-game__apps">
            <legend class="sh-form-label">{t('space.games.which')}</legend>
            {apps.map((a) => {
              const look = appLook(a.app_id, installedApps.value, catalog.value, lang)
              const r = seatRange(a)
              return (
                <label key={a.app_id} class={appId === a.app_id
                  ? 'sh-start-game__app sh-start-game__app--on' : 'sh-start-game__app'}>
                  <input type="radio" name="sh-start-game-app" value={a.app_id}
                         checked={appId === a.app_id}
                         onChange={() => { setAppId(a.app_id); setSeats(null) }} />
                  <AppIcon src={look.icon} size="sm" />
                  <span class="sh-start-game__app-text">
                    <span class="sh-start-game__app-name">{look.name}</span>
                    <span class="sh-start-game__app-meta">{playersLabel(r.min, r.max)}</span>
                  </span>
                </label>
              )
            })}
          </fieldset>
        ) : (
          <p class="sh-muted" role="status">{t('space.games.none_here')}</p>
        )}

        {missing.length > 0 && (
          <div class="sh-start-game__missing">
            <p class="sh-form-label">{t('space.games.cant_start_here')}</p>
            <ul class="sh-start-game__missing-list">
              {missing.map((id) => {
                const look = appLook(id, installedApps.value, catalog.value, lang)
                const entry = catalog.value.find((x) => x.app_id === id)
                const here = installedHere(installedApps.value.find((a) => a.app_id === id))
                return (
                  <li key={id} class="sh-start-game__missing-row">
                    <AppIcon src={look.icon} size="sm" />
                    <span class="sh-start-game__app-text">
                      <span class="sh-start-game__app-name">{look.name}</span>
                      <span class="sh-start-game__app-meta">
                        {here === 'off' ? t('space.games.settings.here_off')
                          : here === 'yes' ? t('space.games.not_playable_here')
                            : t('space.games.settings.here_no')}
                      </span>
                    </span>
                    {householdAdmin ? (
                      <a class="sh-btn sh-btn--secondary" href={addBase(`/apps/${encodeURIComponent(id)}/about`)}>
                        {t('space.games.see_app', { name: look.name })}
                      </a>
                    ) : entry ? (
                      <AskButton entry={entry} name={look.name} />
                    ) : null}
                  </li>
                )
              })}
            </ul>
          </div>
        )}

        {chosen && range && (
          <>
            {range.min < range.max ? (
              <label>
                {t('space.games.how_many')}
                <select value={String(seatCount)}
                        onChange={(e) => setSeats(Number((e.target as HTMLSelectElement).value))}>
                  {Array.from({ length: range.max - range.min + 1 }, (_, i) => range.min + i).map((n) => (
                    <option key={n} value={String(n)}>{playersLabel(n, n)}</option>
                  ))}
                </select>
              </label>
            ) : (
              <p class="sh-form-help">
                {playersLabel(range.min, range.max)}
                {range.max === 2 && <> · {t('space.games.starts_when_taken')}</>}
              </p>
            )}
            <label>
              {t('space.games.open_for')}
              <select value={String(days)} onChange={(e) => setDays(Number((e.target as HTMLSelectElement).value))}>
                {LOBBY_DAYS.map((d) => (
                  <option key={d} value={String(d)}>
                    {t(isOne(d) ? 'space.games.days_one' : 'space.games.days', { n: String(d) })}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t('space.games.note')}
              <input type="text" value={note} maxLength={NOTE_MAX}
                     placeholder={t('space.games.note_placeholder')}
                     onInput={(e) => setNote((e.target as HTMLInputElement).value.replace(/[\r\n]+/g, ' '))} />
            </label>
            <label class="sh-form-row-cap sh-start-game__announce">
              <input type="checkbox" checked={announce}
                     aria-describedby="sh-start-game-announce-hint"
                     onChange={() => setAnnounce(!announce)} />{' '}
              {t('space.games.announce')}
            </label>
            <small id="sh-start-game-announce-hint" class="sh-form-help">
              {t(isOne(ANNOUNCES_PER_DAY) ? 'space.games.announce_hint_one' : 'space.games.announce_hint',
                { n: String(ANNOUNCES_PER_DAY) })}
            </small>
          </>
        )}
        <div class="sh-modal-actions">
          <Button type="button" variant="secondary" onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" loading={saving} disabled={!chosen}>{t('space.games.start_button')}</Button>
        </div>
      </form>
    </Modal>
  )
}
