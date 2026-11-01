/**
 * SeatList — who's in a group game: one row per person, then the free
 * places. Purely presentational (no fetching, no state).
 *
 * Shared by the game's own page (``GameLobby``) and the space Games
 * section (PR 7) — keep it free of page concerns.
 *
 * Props:
 * - ``players``  every player of the game, any order. Only the ``seated``
 *                ones show (who ``left`` or was ``removed`` is gone), in
 *                seat order.
 * - ``max``      places in all: ``max - seated`` free places show as up to
 *                :data:`FREE_ROWS` "Free seat" rows, then "+ N more free
 *                seats".
 * - ``host``     the seat of the person who started the game — chip
 *                "Started this game" (never "host" in the UI).
 * - ``me``       the viewer's seat — chip "You". ``null``: not seated.
 * - ``onRemove`` optional; when given (the starter's view) every other
 *                person's row gets a Remove button calling it with that
 *                seat. Never on the viewer's own row.
 * - ``label``    optional accessible name of the list.
 *
 * A player of the viewer's own household (``isLocal``) reads "Your
 * household"; anyone else their household's name (none when it isn't
 * known here). Names come from the game's roster: display names, never
 * usernames.
 */
import { Avatar } from '@/components/Avatar'
import { isOne, t } from '@/i18n/i18n'
import type { GroupPlayer } from '@/features/apps/groupBridge'

/** Free places drawn as rows before they collapse into "+ N more". */
export const FREE_ROWS = 2

export interface SeatListProps {
  players: GroupPlayer[]
  max: number
  host: number | null
  me: number | null
  onRemove?: (seat: number) => void
  label?: string
}

export function SeatList({ players, max, host, me, onRemove, label }: SeatListProps) {
  const seated = players.filter(p => p.state === 'seated').sort((a, b) => a.seat - b.seat)
  const free = Math.max(0, max - seated.length)
  const freeRows = Math.min(free, FREE_ROWS)
  const more = free - freeRows

  return (
    <ul class="sh-seat-list" aria-label={label}>
      {seated.map(p => {
        const mine = me !== null && p.seat === me
        const started = host !== null && p.seat === host
        return (
          <li key={p.seat} class="sh-seat-list__row">
            <span class="sh-seat-list__avatar" aria-hidden="true"><Avatar name={p.name} size={40} /></span>
            <span class="sh-seat-list__who">
              <span class="sh-seat-list__line">
                <span class="sh-seat-list__name">{p.name}</span>
                {started && <span class="sh-seat-list__tag">{t('apps.lobby.started_this')}</span>}
                {mine && <span class="sh-seat-list__tag sh-seat-list__tag--you">{t('apps.lobby.you')}</span>}
              </span>
              {(p.isLocal || p.household) && (
                <span class="sh-seat-list__home">{p.isLocal ? t('apps.invite.your_household') : p.household}</span>
              )}
            </span>
            {onRemove && !mine && (
              <button
                type="button"
                class="sh-seat-list__remove"
                aria-label={t('apps.lobby.remove_named', { name: p.name })}
                onClick={() => onRemove(p.seat)}
              >
                {t('apps.lobby.remove')}
              </button>
            )}
          </li>
        )
      })}
      {Array.from({ length: freeRows }, (_, i) => (
        <li key={`free-${i}`} class="sh-seat-list__row sh-seat-list__row--free">
          <span class="sh-seat-list__free-dot" aria-hidden="true">+</span>
          <span class="sh-seat-list__free">{t('apps.lobby.free_seat')}</span>
        </li>
      ))}
      {more > 0 && (
        <li class="sh-seat-list__more">
          {t(isOne(more) ? 'apps.lobby.more_free_one' : 'apps.lobby.more_free', { n: String(more) })}
        </li>
      )}
    </ul>
  )
}
