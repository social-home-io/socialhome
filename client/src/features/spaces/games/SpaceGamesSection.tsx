/**
 * SpaceGamesSection — a space's Games section (``/spaces/:id/games``,
 * group play §3.1): "Start a game", the games waiting for people (yours
 * first), the games being played, and the ones that just finished.
 *
 * Who sees it: members with a writing seat — never followers, never in a
 * global space. The section is in the bar while the space allows at least
 * one game; the owner, admins and moderators (who choose the games, D9)
 * also see it while the list is empty, with "Choose games" — everyone
 * else sees nothing until there is something to play.
 *
 * States: loading; a failed read (Retry, never "no games"); the space's
 * host household too old for games (a note, no Start); an archived space
 * (no Start); nothing waiting ("No open games right now"). Live through
 * the ``space.app_table`` frame (``useSpaceGames``).
 */
import { useEffect, useState } from 'preact/hooks'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { LoadErrorState } from '@/components/LoadErrorState'
import { Spinner } from '@/components/Spinner'
import { locale, t } from '@/i18n/i18n'
import { catalog, catalogLoaded, ensureInstalled, installedApps, loadCatalog } from '@/store/apps'
import { settingsSectionPath } from '../spaceSettingsSections'
import { SpaceGameCard } from './SpaceGameCard'
import { appLook, groupTables, type SpaceTable } from './spaceGames'
import type { SpaceGamesState } from './useSpaceGames'
import { StartGameSheet } from './StartGameSheet'

interface Props {
  spaceId: string
  state: SpaceGamesState
}

/** Load this home's apps and the catalog once — the games' names and
 *  icons come from them. */
export function useGameApps(enabled = true): void {
  useEffect(() => {
    if (!enabled) return
    void ensureInstalled()
    if (!catalogLoaded.value) void loadCatalog()
  }, [enabled])
}

export function SpaceGamesSection({ spaceId, state }: Props) {
  const [starting, setStarting] = useState(false)
  useGameApps()

  const { games, loading, failed, reload } = state
  if (loading && !games) return <Spinner />
  if (failed || !games) {
    return <LoadErrorState headingLevel={2} message={t('space.games.load_failed')} onRetry={() => { void reload() }} />
  }

  const lang = locale.value || 'en'
  const { waiting, playing, finished } = groupTables(games.tables)
  const listEmpty = games.allowed_apps.length === 0
  const canStart = games.games_possible && games.host_supports && !listEmpty
  const chooseHref = addBase(settingsSectionPath(spaceId, 'games'))

  const cards = (list: SpaceTable[]) => (
    <div class="sh-games-list">
      {list.map((table) => (
        <SpaceGameCard
          key={table.session_id}
          spaceId={spaceId}
          table={table}
          look={appLook(table.app_id, installedApps.value, catalog.value, lang)}
          entry={catalog.value.find((e) => e.app_id === table.app_id) ?? null}
          canEditGames={games.can_edit_games}
          onChanged={() => { void reload() }}
          headingLevel={4}
        />
      ))}
    </div>
  )

  return (
    <div class="sh-games-section">
      <div class="sh-games-section__bar">
        <h2 class="sh-games-section__title">{t('space.games.section_title')}</h2>
        {canStart && (
          <Button onClick={() => setStarting(true)}>{t('space.games.start')}</Button>
        )}
      </div>

      {!games.host_supports && (
        <p class="sh-games-note" role="status">{t('space.games.host_too_old')}</p>
      )}

      {listEmpty ? (
        <div class="sh-empty-state">
          <div aria-hidden="true">🎲</div>
          <h3>{t('space.games.none_allowed_title')}</h3>
          <p class="sh-muted">{t('space.games.none_allowed_body')}</p>
          {games.can_edit_games && games.games_possible && (
            <a class="sh-btn sh-btn--primary" href={chooseHref}>{t('space.games.choose')}</a>
          )}
        </div>
      ) : (
        <>
          <section class="sh-games-group" aria-labelledby="sh-games-waiting-h">
            <h3 id="sh-games-waiting-h" class="sh-games-group__title">{t('space.games.strip_title')}</h3>
            {waiting.length > 0 ? cards(waiting) : (
              <p class="sh-muted sh-games-group__empty">
                {canStart ? t('space.games.none_open_start') : t('space.games.none_open')}
              </p>
            )}
          </section>
          {playing.length > 0 && (
            <section class="sh-games-group" aria-labelledby="sh-games-playing-h">
              <h3 id="sh-games-playing-h" class="sh-games-group__title">{t('space.games.being_played')}</h3>
              {cards(playing)}
            </section>
          )}
          {finished.length > 0 && (
            <section class="sh-games-group" aria-labelledby="sh-games-finished-h">
              <h3 id="sh-games-finished-h" class="sh-games-group__title">{t('space.games.finished')}</h3>
              {cards(finished)}
            </section>
          )}
          <p class="sh-games-section__foot sh-muted">
            {t('space.games.names_note')}
            {games.can_edit_games && (
              <>{' '}<a href={chooseHref}>{t('space.games.choose_link')}</a></>
            )}
          </p>
        </>
      )}

      <StartGameSheet
        spaceId={spaceId}
        allowed={games.allowed_apps}
        open={starting}
        onClose={() => setStarting(false)}
        onStarted={() => { void reload() }}
      />
    </div>
  )
}
