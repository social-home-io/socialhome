/**
 * "What is <name> for?" — the choice a new space's owner makes right
 * after create (owner decision 3: a new space starts with feed, chat and
 * calendar; the rest is opt-in).
 *
 * A preset picks a set of sections; the section chips fine-tune it; Done
 * PATCHes the features. Which spaces still ask is the creator's own
 * ``space_setup_pending`` preference (``users.preferences_json``, kept on
 * the server so the card follows them to another device): create adds
 * the id, a choice or Skip removes it — the card never comes back.
 */
import { t } from '@/i18n/i18n'
import { getPreferences, setPreference } from '@/utils/preferences'

/** The sections the card offers, in the space's section order. */
export type SetupSection =
  | 'chat' | 'calendar' | 'tasks' | 'pages' | 'gallery' | 'stickies' | 'bazaar' | 'map' | 'timetable'

export const SETUP_SECTIONS: readonly SetupSection[] = [
  'chat', 'calendar', 'tasks', 'pages', 'gallery', 'stickies', 'bazaar', 'map', 'timetable',
]

/** The ``SpaceFeatures`` key behind each section. */
const FEATURE_KEY: Record<SetupSection, string> = {
  chat: 'chat',
  calendar: 'calendar',
  tasks: 'todo',
  pages: 'pages',
  gallery: 'gallery',
  stickies: 'stickies',
  bazaar: 'bazaar',
  map: 'location',
  timetable: 'timetable',
}

export type SetupPreset = 'family' | 'club' | 'neighbours' | 'feed'

export const SETUP_PRESETS: readonly SetupPreset[] = ['family', 'club', 'neighbours', 'feed']

/** What each preset turns on. "Just a feed" is honest: nothing else. The
 *  Map is never part of a preset — sharing where people are is opted
 *  into on its own (§23.8.6). */
export const PRESET_SECTIONS: Record<SetupPreset, readonly SetupSection[]> = {
  family: ['chat', 'calendar', 'tasks', 'gallery'],
  club: ['chat', 'calendar', 'pages'],
  neighbours: ['chat', 'calendar', 'stickies', 'bazaar'],
  feed: [],
}

export const PRESET_EMOJI: Record<SetupPreset, string> = {
  family: '👪',
  club: '📚',
  neighbours: '🏘️',
  feed: '🧩',
}

/** Literal keys so ``i18n:check`` sees them. */
export function presetLabel(p: SetupPreset): string {
  switch (p) {
    case 'family':     return t('space.setup.preset.family')
    case 'club':       return t('space.setup.preset.club')
    case 'neighbours': return t('space.setup.preset.neighbours')
    case 'feed':       return t('space.setup.preset.feed')
  }
}

export function presetHint(p: SetupPreset): string {
  switch (p) {
    case 'family':     return t('space.setup.preset.family_hint')
    case 'club':       return t('space.setup.preset.club_hint')
    case 'neighbours': return t('space.setup.preset.neighbours_hint')
    case 'feed':       return t('space.setup.preset.feed_hint')
  }
}

export function setupSectionLabel(s: SetupSection): string {
  switch (s) {
    case 'chat':      return t('space.chat.view_chat')
    case 'calendar':  return t('nav.calendar')
    case 'tasks':     return t('nav.tasks')
    case 'pages':     return t('nav.pages')
    case 'gallery':   return t('nav.gallery')
    case 'stickies':  return t('nav.stickies')
    case 'bazaar':    return t('nav.bazaar')
    case 'map':       return t('space.tab.map')
    case 'timetable': return t('nav.timetable')
  }
}

// Words that give a space's purpose away, in the UI languages.
const FAMILY = /famil|eltern|parents|kinder|kids|enfants|ni[ñn]os|gezin|oma|opa|grand/i
const NEIGHBOURS = /neighbo|nachbar|quartier|street|stra(ss|ß)e|voisin|vecin|barrio|buurt|siedlung|block|village|dorf/i
const CLUB = /club|verein|group|gruppe|team|chor|choir|band|book|buch|lese|klasse|class|school|schule|kurs|course|sport|football|fu(ss|ß)ball|soccer|equipe|équipe|grupo|ploeg|vereniging/i

/** A best guess from the name (then the discovery category), or ``null``
 *  — the card then starts with nothing picked. */
export function guessPreset(name: string, category?: string | null): SetupPreset | null {
  if (FAMILY.test(name)) return 'family'
  if (NEIGHBOURS.test(name)) return 'neighbours'
  if (CLUB.test(name)) return 'club'
  switch (category) {
    case 'family_parenting': return 'family'
    case 'local': return 'neighbours'
    case 'hobby_crafts': case 'sports_outdoors': case 'gaming': case 'music_arts':
    case 'food_drink': case 'tech': case 'learning':
      return 'club'
    default: return null
  }
}

/** The sections on in ``features`` (absent keys read as the server's
 *  class defaults: on, except the map and the timetable). */
export function sectionsOn(features: Record<string, unknown> | undefined): SetupSection[] {
  return SETUP_SECTIONS.filter((s) => {
    const v = features?.[FEATURE_KEY[s]]
    return typeof v === 'boolean' ? v : !(s === 'map' || s === 'timetable')
  })
}

/** The ``features`` PATCH body for exactly ``on`` — every offered key is
 *  sent, so what the owner sees is what the space gets. */
export function featuresFor(on: readonly SetupSection[]): Record<string, boolean> {
  return Object.fromEntries(SETUP_SECTIONS.map((s) => [FEATURE_KEY[s], on.includes(s)]))
}

const PREF = 'space_setup_pending'
/** Enough for anyone; the oldest fall off. */
const MAX_PENDING = 20

function pending(): string[] {
  const v = getPreferences()[PREF]
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

/** Does ``spaceId`` still wait for its "What is it for?" choice? */
export function isSetupPending(spaceId: string): boolean {
  return pending().includes(spaceId)
}

/** Ask "What is it for?" on this space's first visit (after create). */
export async function markSetupPending(spaceId: string): Promise<void> {
  const next = [...pending().filter((id) => id !== spaceId), spaceId].slice(-MAX_PENDING)
  await setPreference(PREF, next)
}

/** A choice or Skip: never ask again for this space. */
export async function clearSetupPending(spaceId: string): Promise<void> {
  const left = pending().filter((id) => id !== spaceId)
  await setPreference(PREF, left.length > 0 ? left : null)
}
