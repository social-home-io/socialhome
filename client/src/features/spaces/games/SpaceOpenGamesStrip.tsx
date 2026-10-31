/**
 * SpaceOpenGamesStrip — "Open games" on top of a space's feed (group
 * play D5): the games still waiting for people, your own first, at most
 * three, then "All games (5)" to the Games section.
 *
 * Shows nothing at all when no game is waiting (no empty box), while the
 * list loads, or when it can't be read — the feed is what the page is
 * for. Followers never get here (the page doesn't mount it for them).
 */
import { addBase } from '@/baseUrl'
import { locale, t } from '@/i18n/i18n'
import { catalog, installedApps } from '@/store/apps'
import { spaceSectionPath } from '../spaceTabs'
import { SpaceGameCard } from './SpaceGameCard'
import { appLook, stripTables, type SpaceGames } from './spaceGames'

interface Props {
  spaceId: string
  games: SpaceGames | null
  onChanged: () => void
}

export function SpaceOpenGamesStrip({ spaceId, games, onChanged }: Props) {
  if (!games) return null
  const { shown, total } = stripTables(games.tables)
  if (shown.length === 0) return null
  const lang = locale.value || 'en'
  return (
    <section class="sh-games-strip" aria-labelledby="sh-games-strip-h">
      <div class="sh-games-strip__head">
        <h2 id="sh-games-strip-h" class="sh-games-strip__title">{t('space.games.strip_title')}</h2>
        <a class="sh-games-strip__all" href={addBase(spaceSectionPath(spaceId, 'games'))}>
          {t('space.games.all', { n: String(total) })}
        </a>
      </div>
      <div class="sh-games-strip__row">
        {shown.map((table) => (
          <SpaceGameCard
            key={table.session_id}
            spaceId={spaceId}
            table={table}
            look={appLook(table.app_id, installedApps.value, catalog.value, lang)}
            entry={catalog.value.find((e) => e.app_id === table.app_id) ?? null}
            canEditGames={games.can_edit_games}
            onChanged={onChanged}
          />
        ))}
      </div>
    </section>
  )
}
