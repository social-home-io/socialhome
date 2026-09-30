/**
 * Timetable icons — one emoji per slot so children who can't read yet
 * still find "swimming" on their plan.
 *
 * ``ICON_PRESETS`` feeds the EntryDialog picker; ``suggestIcon`` guesses
 * a preset from a subject title in en / de / es / fr / nl so a parent
 * who types "Mathe" gets 🔢 without opening the picker.
 */
import { normalizeSubject } from './colors'

export interface IconPreset {
  emoji: string
  key: string
  /** i18n key of the localised name (``timetable.icon.<key>``). */
  label: string
}

const preset = (emoji: string, key: string): IconPreset =>
  ({ emoji, key, label: `timetable.icon.${key}` })

export const LESSON_ICONS: readonly IconPreset[] = [
  preset('🔢', 'maths'),
  preset('📖', 'reading'),
  preset('✏️', 'writing'),
  preset('🇬🇧', 'english'),
  preset('🇫🇷', 'french'),
  preset('🇪🇸', 'spanish'),
  preset('🇩🇪', 'german'),
  preset('🎵', 'music'),
  preset('🎨', 'art'),
  preset('⚽', 'sport'),
  preset('🏊', 'swimming'),
  preset('🔬', 'science'),
  preset('🌍', 'geography'),
  preset('🕰️', 'history'),
  preset('💻', 'computing'),
  preset('🌱', 'nature'),
  preset('🎭', 'drama'),
  preset('🧑‍🍳', 'cooking'),
  preset('🙏', 'religion'),
  preset('🧮', 'numeracy'),
]

export const BREAK_ICONS: readonly IconPreset[] = [
  preset('🍎', 'snack'),
  preset('🍽️', 'lunch'),
  preset('🧸', 'break'),
  preset('🚌', 'bus'),
  preset('🏠', 'home'),
]

export const ICON_PRESETS: readonly IconPreset[] = [...LESSON_ICONS, ...BREAK_ICONS]

export function presetFor(emoji: string | null | undefined): IconPreset | undefined {
  return emoji ? ICON_PRESETS.find(p => p.emoji === emoji) : undefined
}

/**
 * Keywords per preset, already normalised (lowercase, no diacritics).
 * Order matters: the first preset with a match wins, so the more
 * specific subjects (science before nature — "Naturwissenschaft"
 * starts with "natur") come first. Keywords of ≤ 3 letters ("pe",
 * "lo", "bk", "art") and those marked with a trailing ``$`` ("home$" —
 * not "Homework", "hause$" — not "Hausaufgaben") only match a whole
 * word; the rest match the start of a word ("math" → "Mathe",
 * "Mathematik", "Maths").
 */
const KEYWORDS: readonly (readonly [string, readonly string[]])[] = [
  ['maths', ['math', 'rechnen', 'matematica', 'wiskunde']],
  ['numeracy', ['numeracy', 'arithmetic', 'rekenen', 'calcul']],
  ['science', ['science', 'nawi', 'naturwissenschaft', 'biolog', 'bio', 'chemi', 'physi',
    'ciencias', 'natuurkunde', 'scheikunde']],
  ['swimming', ['schwimm', 'swim', 'natacion', 'natation', 'zwem']],
  ['sport', ['sport', 'turnen', 'pe', 'eps', 'educacion fisica', 'gym', 'lo',
    'gymnastik', 'bewegung']],
  ['music', ['music', 'musik', 'musica', 'musique', 'muziek']],
  ['english', ['englisch', 'english', 'ingles', 'anglais', 'engels']],
  ['french', ['franzosisch', 'french', 'francais', 'frances', 'frans']],
  ['spanish', ['spanisch', 'spanish', 'espanol', 'espagnol', 'spaans']],
  ['german', ['german', 'allemand', 'aleman', 'duits']],
  ['reading', ['deutsch', 'reading', 'lesen', 'lecture', 'lezen', 'lengua', 'lectura']],
  ['writing', ['writing', 'schreib', 'ecriture', 'escritura', 'schrijven']],
  ['art', ['art', 'arte', 'arts', 'kunst', 'bk', 'bildnerisch', 'zeichnen', 'dibujo',
    'dessin', 'tekenen']],
  ['geography', ['geograph', 'geografi', 'erdkunde', 'aardrijkskunde']],
  ['history', ['geschichte', 'history', 'histoire', 'historia', 'geschiedenis']],
  ['computing', ['informati', 'computing', 'computer', 'ict']],
  ['nature', ['nature', 'natur', 'sachkunde', 'sachunterricht', 'hsu', 'natuur',
    'naturaleza']],
  ['drama', ['drama', 'theater', 'theatre', 'teatro', 'toneel']],
  ['cooking', ['kochen', 'cooking', 'cuisine', 'cocina', 'koken', 'hauswirtschaft']],
  ['religion', ['religion', 'reli', 'ethik', 'ethics', 'ethique', 'etica', 'religie']],
  ['snack', ['snack', 'breakfast', 'jause', 'znuni', 'fruhstuck', 'gouter', 'merienda', 'pausenbrot']],
  ['lunch', ['mittag', 'lunch', 'cantine', 'comedor', 'mensa', 'dejeuner', 'almuerzo']],
  ['break', ['pause', 'break', 'recreo', 'recre', 'recreation', 'speelkwartier', 'pauze']],
  ['bus', ['bus$', 'schulbus']],
  ['home', ['home$', 'heim$', 'heimweg', 'hause$', 'zuhause', 'maison$', 'casa$', 'huis$']],
]

/** Guess an icon from a subject title — ``null`` when nothing matches. */
export function suggestIcon(title: string | null | undefined): string | null {
  const words = normalizeSubject(title ?? '').split(/[^\p{L}]+/u).filter(Boolean)
  if (words.length === 0) return null
  const hay = ` ${words.join(' ')} `
  for (const [key, keywords] of KEYWORDS) {
    for (const k of keywords) {
      const whole = k.endsWith('$')
      const needle = (whole ? k.slice(0, -1) : k).replace(/\s+/g, ' ')
      const hit = whole || needle.length <= 3
        ? hay.includes(` ${needle} `)
        : hay.includes(` ${needle}`)
      if (hit) return ICON_PRESETS.find(p => p.key === key)!.emoji
    }
  }
  return null
}
