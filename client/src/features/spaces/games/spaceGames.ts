/**
 * A space's games (group play, v_77) — the wire shapes of
 * ``GET /api/spaces/{id}/games`` and the pure rules the "Open games"
 * strip, the Games section, the Start-a-game sheet and Settings › Games
 * share. Pure, so every rule is testable without rendering.
 *
 * Words: the UI never says "table", "lobby" or "host" — a table is a
 * *game*, its host person "started this game" (glossary "Playing
 * together").
 */
import { isOne, t } from '@/i18n/i18n'
import { localized, type CatalogEntry, type InstalledApp } from '@/store/apps'

/** A card's state: ``open`` takes people, ``full`` has no seat left,
 *  ``playing`` started, ``closed`` ended. */
export type TableState = 'open' | 'full' | 'playing' | 'closed'

/** The caller's own view of a game they have a place at. */
export interface SpaceTableGroup {
  is_host: boolean
  /** ``invited`` | ``joining`` | ``seated`` | ``left`` | ``removed`` */
  my_state: string
  state?: string
}

export interface SpaceTable {
  session_id: string
  space_id?: string | null
  app_id: string
  host_name: string | null
  host_is_local: boolean
  note: string | null
  min: number
  max: number
  seated: number
  state: TableState
  end_reason?: string | null
  late_join: boolean
  created_at: string
  started_at?: string | null
  expires_at: string
  /** The app on this household (absent on a live frame's card). */
  app?: { installed: boolean, enabled: boolean }
  /** The caller's place, ``null`` when they have none. */
  group?: SpaceTableGroup | null
}

export interface SpaceGames {
  space_id: string
  allowed_apps: string[]
  /** ``false`` for a global or archived space: no new games. */
  games_possible: boolean
  /** Owner, admins and moderators choose the games and close any game. */
  can_edit_games: boolean
  /** The space's host household keeps a games list (v_77). */
  host_supports: boolean
  tables: SpaceTable[]
}

/** How many games the strip on top of the feed shows. */
export const STRIP_MAX = 3
/** "Let everyone know" — at most this many games a day per person here
 *  (``SPACE_TABLE_ANNOUNCES_PER_DAY``). */
export const ANNOUNCES_PER_DAY = 1
/** How long a game may wait for people (``GROUP_LOBBY_DAYS``). */
export const LOBBY_DAYS: readonly number[] = [1, 3, 7, 14]
export const LOBBY_DEFAULT_DAYS = 3
/** The starter's note (``GROUP_NOTE_MAX``, one line). */
export const NOTE_MAX = 80

/** Does the caller have a place at *table* (joining, seated, invited)? */
export function isMine(table: SpaceTable): boolean {
  const s = table.group?.my_state
  return !!s && s !== 'left' && s !== 'removed'
}

/** Did the caller start *table*? */
export function isStarter(table: SpaceTable): boolean {
  return table.group?.is_host === true
}

/** Still waiting for people (open or full, not started). */
export function inLobby(table: SpaceTable): boolean {
  return table.state === 'open' || table.state === 'full'
}

/** The games the strip shows: waiting ones, the caller's first, at most
 *  :data:`STRIP_MAX`. ``more`` — how many waiting games there are. */
export function stripTables(tables: readonly SpaceTable[]): { shown: SpaceTable[], total: number } {
  const waiting = tables.filter(inLobby)
  const ordered = [...waiting.filter(isMine), ...waiting.filter((x) => !isMine(x))]
  return { shown: ordered.slice(0, STRIP_MAX), total: waiting.length }
}

/** §3.5: a game of an app this home neither has nor finds in its catalog
 *  shows no card. Until the catalog has answered (or when it's off) the
 *  card stays — nothing to tell it by. */
export function knownTables(
  tables: readonly SpaceTable[], catalog: readonly CatalogEntry[], catalogReady: boolean,
): SpaceTable[] {
  if (!catalogReady) return [...tables]
  return tables.filter((x) => x.app?.installed || catalog.some((e) => e.app_id === x.app_id))
}

/** Waiting games the caller could join now (the app here and on) —
 *  the section's dot. */
export function joinableCount(tables: readonly SpaceTable[]): number {
  return tables.filter((x) => x.state === 'open' && !isMine(x) && appHere(x) === 'yes').length
}

/** The Games section's three lists. */
export function groupTables(tables: readonly SpaceTable[]): {
  waiting: SpaceTable[], playing: SpaceTable[], finished: SpaceTable[]
} {
  return {
    waiting: [
      ...tables.filter((x) => inLobby(x) && isMine(x)),
      ...tables.filter((x) => inLobby(x) && !isMine(x)),
    ],
    playing: tables.filter((x) => x.state === 'playing'),
    finished: tables.filter((x) => x.state === 'closed'),
  }
}

/** Is the app here and on for this household? */
export type AppHere = 'yes' | 'off' | 'missing'

/** The same for an installed app (``undefined``: not added here). */
export function installedHere(app: Pick<InstalledApp, 'enabled'> | undefined): AppHere {
  if (!app) return 'missing'
  return app.enabled ? 'yes' : 'off'
}

export function appHere(table: Pick<SpaceTable, 'app'>): AppHere {
  if (!table.app?.installed) return 'missing'
  return table.app.enabled ? 'yes' : 'off'
}

/**
 * What a card's main button does:
 *   - ``open``  — the caller's own waiting game (go to it);
 *   - ``resume`` — the caller's game being played (go to it);
 *   - ``join``  — take a seat (a group game);
 *   - ``take``  — a two-player game: take the seat, it starts at once (D11);
 *   - ``full``  — no seat left (shown, disabled);
 *   - ``ask``   — the app isn't here or is off: ask an admin (a grown-up);
 *   - ``see``   — the same for a household admin: the app's page;
 *   - ``none``  — nothing (finished, or played without late joins).
 */
export type CardAction = 'open' | 'resume' | 'join' | 'take' | 'full' | 'ask' | 'see' | 'none'

export function cardAction(
  table: SpaceTable, viewer: { householdAdmin: boolean },
): CardAction {
  if (table.state === 'closed') return 'none'
  if (isMine(table)) return table.state === 'playing' ? 'resume' : 'open'
  const takesPeople = inLobby(table) || (table.state === 'playing' && table.late_join)
  if (!takesPeople) return 'none'
  if (table.state === 'full' || table.seated >= table.max) return 'full'
  if (appHere(table) !== 'yes') return viewer.householdAdmin ? 'see' : 'ask'
  return table.max === 2 && table.state !== 'playing' ? 'take' : 'join'
}

/** May the caller close *table*? The person who started it, or the
 *  owner / an admin / a moderator for any game (D9). */
export function canClose(table: SpaceTable, canEditGames: boolean): boolean {
  return table.state !== 'closed' && (isStarter(table) || canEditGames)
}

/** The game's screen (players, Start, Leave) — the lobby lives with the
 *  app (group play PR 5); a stripped in-app path. */
export function gamePath(table: Pick<SpaceTable, 'app_id' | 'session_id'>): string {
  return `/apps/${encodeURIComponent(table.app_id)}/tables/${encodeURIComponent(table.session_id)}`
}

/** An app's name and icon as this household knows it: its catalog
 *  entry in the reader's language, else the installed app, else its id. */
export interface AppLook { name: string, icon: string | null }

export function appLook(
  appId: string, installed: readonly InstalledApp[], catalog: readonly CatalogEntry[], lang: string,
): AppLook {
  const entry = catalog.find((e) => e.app_id === appId)
  const app = installed.find((a) => a.app_id === appId)
  return {
    name: entry ? localized(entry, lang).name : (app?.name || appId),
    icon: entry?.icon_url ?? app?.icon ?? null,
  }
}

/** "2 players", "3–30 players". */
export function playersLabel(min: number, max: number): string {
  return min === max
    ? t('space.games.players_exact', { n: String(max) })
    : t('space.games.players_range', { min: String(min), max: String(max) })
}

/** "3 joined" / "1 of 2" for a two-player game. */
export function seatedLabel(table: Pick<SpaceTable, 'seated' | 'max'>): string {
  if (table.max === 2) return t('space.games.seated_of', { n: String(table.seated), max: String(table.max) })
  return t(isOne(table.seated) ? 'space.games.joined_one' : 'space.games.joined', { n: String(table.seated) })
}

/** Whole days until *iso* (rounded), at least 1 while time is left;
 *  0 when it has passed. */
export function daysLeft(iso: string, now: number = Date.now()): number {
  const ms = Date.parse(iso) - now
  if (Number.isNaN(ms) || ms <= 0) return 0
  return Math.max(1, Math.round(ms / 86_400_000))
}

/** "Closes in less than a day" / "Closes in 1 day" / "Closes in 3 days". */
export function closesLabel(iso: string, now: number = Date.now()): string {
  const ms = Date.parse(iso) - now
  if (Number.isNaN(ms) || ms <= 86_400_000) return t('space.games.closes_soon')
  const d = daysLeft(iso, now)
  return t(isOne(d) ? 'space.games.closes_in_one' : 'space.games.closes_in', { n: String(d) })
}

/** Who started *table*, in a sentence. */
export function starterLabel(table: SpaceTable): string {
  if (isStarter(table)) return t('space.games.started_by_you')
  return table.host_name
    ? t('space.games.started_by', { name: table.host_name })
    : t('space.games.started_by_someone')
}

/** Why a finished game ended, or ``null``. */
export function endedLabel(table: SpaceTable): string | null {
  if (table.state !== 'closed') return null
  switch (table.end_reason) {
    case 'closed_by_moderator': return t('space.games.ended.moderator')
    case 'app_removed': return t('space.games.ended.not_allowed')
    case 'expired': return t('space.games.ended.expired')
    case 'finished': return t('space.games.ended.finished')
    case 'cancelled': return t('space.games.ended.cancelled')
    default: return t('space.games.ended.over')
  }
}

/** One row of Settings › Games: an app this household knows of that can
 *  be opened as a game in a space, or one already on the list. */
export interface GameChoice {
  app_id: string
  name: string
  icon: string | null
  players: { min: number, max: number } | null
  min_age: number | null
  /** In this household: on, turned off, or not added yet. */
  here: AppHere
}

/** The apps Settings › Games offers: the household's added apps that can
 *  be opened as a game in a space, the catalog's, and every app already
 *  on the list (so one can always be taken off) — by name. */
export function gameChoices(
  allowed: readonly string[],
  installed: readonly InstalledApp[],
  catalog: readonly CatalogEntry[],
  lang: string,
): GameChoice[] {
  const ids = new Set<string>(allowed)
  for (const a of installed) if (a.tables) ids.add(a.app_id)
  for (const e of catalog) if (e.tables) ids.add(e.app_id)
  const out: GameChoice[] = []
  for (const id of ids) {
    const app = installed.find((a) => a.app_id === id)
    const entry = catalog.find((e) => e.app_id === id)
    const look = appLook(id, installed, catalog, lang)
    out.push({
      app_id: id,
      name: look.name,
      icon: look.icon,
      players: app?.players && app.tables ? app.players : (entry?.players ?? null),
      min_age: app?.min_age ?? entry?.min_age ?? null,
      here: installedHere(app),
    })
  }
  return out.sort((a, b) => a.name.localeCompare(b.name))
}

/** The games the caller can start here: on the list, added and on in
 *  this household, and able to be opened as a game in a space. */
export function startableApps(
  allowed: readonly string[], installed: readonly InstalledApp[],
): InstalledApp[] {
  return installed.filter((a) => allowed.includes(a.app_id) && a.enabled && a.tables)
}

/** The players a new game may seat: the app's range, at most 30. */
export function seatRange(app: Pick<InstalledApp, 'players'>): { min: number, max: number } {
  const min = Math.max(2, app.players?.min ?? 2)
  const max = Math.min(30, Math.max(min, app.players?.max ?? 2))
  return { min, max }
}
