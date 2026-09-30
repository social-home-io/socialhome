import { describe, it, expect } from 'vitest'
import { ICON_PRESETS, LESSON_ICONS, BREAK_ICONS, suggestIcon, presetFor } from './icons'

const E = (key: string) => ICON_PRESETS.find(p => p.key === key)!.emoji

describe('icon presets', () => {
  it('has the lesson and break/extra presets with i18n label keys', () => {
    expect(LESSON_ICONS.map(p => p.key)).toEqual([
      'maths', 'reading', 'writing', 'english', 'french', 'spanish', 'german',
      'music', 'art', 'sport', 'swimming', 'science', 'geography', 'history',
      'computing', 'nature', 'drama', 'cooking', 'religion', 'numeracy',
    ])
    expect(BREAK_ICONS.map(p => p.key)).toEqual(['snack', 'lunch', 'break', 'bus', 'home'])
    for (const p of ICON_PRESETS) expect(p.label).toBe(`timetable.icon.${p.key}`)
    expect(E('maths')).toBe('🔢')
    expect(E('lunch')).toBe('🍽️')
  })

  it('finds the preset for an emoji', () => {
    expect(presetFor('🎵')?.key).toBe('music')
    expect(presetFor('🦄')).toBeUndefined()
    expect(presetFor(null)).toBeUndefined()
  })
})

describe('suggestIcon', () => {
  const cases: [string, string][] = [
    // English
    ['Maths', 'maths'], ['Math', 'maths'], ['PE', 'sport'], ['Gym', 'sport'],
    ['Music', 'music'], ['Reading', 'reading'], ['Art', 'art'],
    ['Science', 'science'], ['English', 'english'], ['Swimming', 'swimming'],
    ['Lunch', 'lunch'], ['Break', 'break'],
    // German
    ['Mathe', 'maths'], ['Mathematik', 'maths'], ['Rechnen', 'maths'],
    ['Turnen', 'sport'], ['Musik', 'music'], ['Deutsch', 'reading'],
    ['Lesen', 'reading'], ['Kunst', 'art'], ['Bildnerisches Gestalten', 'art'],
    ['Zeichnen', 'art'], ['NaWi', 'science'], ['Naturwissenschaften', 'science'],
    ['Biologie', 'science'], ['Bio', 'science'], ['Chemie', 'science'],
    ['Physik', 'science'], ['Englisch', 'english'], ['Schwimmen', 'swimming'],
    ['Mittagessen', 'lunch'], ['Pause', 'break'],
    // Spanish
    ['Matemáticas', 'maths'], ['Educación física', 'sport'], ['Música', 'music'],
    ['Lengua', 'reading'], ['Arte', 'art'], ['Inglés', 'english'],
    ['Natación', 'swimming'], ['Comedor', 'lunch'], ['Recreo', 'break'],
    // French
    ['Mathématiques', 'maths'], ['EPS', 'sport'], ['Musique', 'music'],
    ['Lecture', 'reading'], ['Sciences', 'science'], ['Anglais', 'english'],
    ['Natation', 'swimming'], ['Cantine', 'lunch'], ['Récré', 'break'],
    ['Récréation', 'break'],
    // Dutch
    ['Wiskunde', 'maths'], ['LO', 'sport'], ['Muziek', 'music'], ['Lezen', 'reading'],
    ['BK', 'art'], ['Engels', 'english'], ['Zwemmen', 'swimming'],
    ['Speelkwartier', 'break'],
  ]
  for (const [title, key] of cases) {
    it(`"${title}" → ${key}`, () => {
      expect(suggestIcon(title)).toBe(E(key))
    })
  }

  it('is case- and diacritic-insensitive', () => {
    expect(suggestIcon('MATHÉMATIQUES')).toBe(E('maths'))
    expect(suggestIcon('musica')).toBe(E('music'))
  })

  it('matches on word starts inside a longer title', () => {
    expect(suggestIcon('Mathe (Frau Huber)')).toBe(E('maths'))
    expect(suggestIcon('Sport – Halle 2')).toBe(E('sport'))
  })

  it('never matches inside a word or short keywords as prefixes', () => {
    // "art" is not the start of "Karton"; "pe" is not "Peter"; "lo" not "Lotte".
    expect(suggestIcon('Karton')).toBeNull()
    expect(suggestIcon('Peter')).toBeNull()
    expect(suggestIcon('Lotte')).toBeNull()
    expect(suggestIcon('Förderunterricht')).toBeNull()
  })

  it('"home" words only match whole words (Homework / Heimatkunde are not "home")', () => {
    expect(suggestIcon('Homework')).toBeNull()
    expect(suggestIcon('Hausaufgaben')).toBeNull()
    expect(suggestIcon('Busy bees')).toBeNull()
    expect(suggestIcon('Home')).toBe(E('home'))
    expect(suggestIcon('Nach Hause')).toBe(E('home'))
    expect(suggestIcon('Bus')).toBe(E('bus'))
    expect(suggestIcon('Breakfast club')).toBe(E('snack'))
  })

  it('returns null for empty input', () => {
    expect(suggestIcon('')).toBeNull()
    expect(suggestIcon(null)).toBeNull()
  })
})
