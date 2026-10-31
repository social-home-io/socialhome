/**
 * GameLobbyPage — the page of one group game, ``/apps/:appId/tables/
 * :sessionId`` (a group invite's and "joined your game" row's link). It
 * wraps :func:`GameLobby`; the topbar names the section (Apps), the
 * lobby's heading names the app.
 *
 * Both ids are checked against the server's own id rule before anything
 * is fetched; a malformed address shows "This game isn't here any more".
 */
import { useRoute } from 'preact-iso'
import { appHref } from '@/baseUrl'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { GameLobby } from './GameLobby'

/** An app or game id as the server mints and accepts it. */
const ID = /^[A-Za-z0-9_-]{1,128}$/

/** *appId* / *sessionId* if both are well-formed ids, else ``null``. */
export function lobbyParams(params: Record<string, string | undefined>): { appId: string, sessionId: string } | null {
  const { appId, sessionId } = params
  return appId && sessionId && ID.test(appId) && ID.test(sessionId) ? { appId, sessionId } : null
}

export default function GameLobbyPage() {
  const { params } = useRoute()
  useTitle(t('nav.apps'))
  const ids = lobbyParams(params)
  if (!ids) {
    return (
      <section class="sh-game-lobby">
        <div class="sh-game-lobby__state" role="status">
          <h2 class="sh-game-lobby__state-title">{t('apps.lobby.not_found')}</h2>
          <div class="sh-game-lobby__actions">
            <a class="sh-btn sh-btn--secondary" href={appHref('/apps')}>{t('apps.host.back_to_apps')}</a>
          </div>
        </div>
      </section>
    )
  }
  return <GameLobby key={`${ids.appId}/${ids.sessionId}`} appId={ids.appId} sessionId={ids.sessionId} />
}
