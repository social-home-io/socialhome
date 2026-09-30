import { describe, it, expect } from 'vitest'
import { TIMETABLE_COLORS, hashColor, entryColor, colorClass, subjectColors } from './colors'
import { entry, timetable } from './testUtils'
import type { TimetableColor } from '@/types'

const SUBJECTS = ['Mathe', 'Deutsch', 'Englisch', 'Sport', 'Musik', 'Kunst',
  'Sachkunde', 'Religion', 'Schwimmen', 'Werken', 'Französisch', 'Informatik']

describe('timetable colours', () => {
  it('lists the twelve backend tokens', () => {
    expect([...TIMETABLE_COLORS].sort()).toEqual([
      'amber', 'coral', 'indigo', 'moss', 'olive', 'rose',
      'sand', 'sky', 'slate', 'teal', 'terracotta', 'violet',
    ])
  })

  it('gives 12 distinct subjects 12 distinct colours', () => {
    const tt = timetable({ entries: SUBJECTS.map((s, i) =>
      entry(i % 5, `${String(8 + Math.floor(i / 5)).padStart(2, '0')}:00`, `${String(8 + Math.floor(i / 5)).padStart(2, '0')}:45`, { title: s })) })
    const colors = new Set(tt.entries.map(e => entryColor(e, tt)))
    expect(colors.size).toBe(12)
  })

  it('is case-, space- and diacritic-insensitive per subject', () => {
    const tt = timetable({ entries: [
      entry(0, '08:00', '08:45', { title: 'Música' }),
      entry(1, '08:00', '08:45', { title: ' musica ' }),
      entry(2, '08:00', '08:45', { title: 'Mathe' }),
    ] })
    expect(entryColor(tt.entries[0], tt)).toBe(entryColor(tt.entries[1], tt))
    expect(entryColor(tt.entries[0], tt)).not.toBe(entryColor(tt.entries[2], tt))
  })

  it('is stable: adding a lesson with an existing title recolours nothing', () => {
    const base = SUBJECTS.slice(0, 6).map((s, i) => entry(i % 5, '08:00', '08:45', { title: s }))
    const before = timetable({ entries: base })
    const after = timetable({ entries: [...base, entry(3, '10:00', '10:45', { title: 'Kunst' })] })
    for (const e of base) expect(entryColor(e, after)).toBe(entryColor(e, before))
  })

  it('skips tokens other subjects use explicitly', () => {
    const tt = timetable({ entries: [
      entry(0, '08:00', '08:45', { title: 'Sport', color: 'terracotta' }),
      ...SUBJECTS.slice(0, 5).map((s, i) => entry(1, `0${8 + i}:00`, `0${8 + i}:45`, { title: s })),
    ] })
    const auto = tt.entries.slice(1).map(e => entryColor(e, tt))
    expect(auto).not.toContain('terracotta')
    expect(new Set(auto).size).toBe(auto.length)
  })

  it('falls back to a hash beyond the palette, and keeps explicit colours', () => {
    const many = Array.from({ length: 14 }, (_, i) => entry(i % 5, '08:00', '08:45', { title: `Fach ${String.fromCharCode(65 + i)}` }))
    const tt = timetable({ entries: many })
    expect(entryColor(many[13], tt)).toBe(hashColor('Fach N'))
    expect(entryColor({ ...many[0], color: 'teal' as TimetableColor }, tt)).toBe('teal')
  })

  it('keeps untitled slots neutral', () => {
    const tt = timetable({ entries: [entry(0, '08:00', '08:45')] })
    expect(entryColor(tt.entries[0], tt)).toBeNull()
    expect(subjectColors(tt).size).toBe(0)
  })

  it('maps a token to its CSS class, null to the neutral class', () => {
    expect(colorClass('teal')).toBe('sh-timetable-c--teal')
    expect(colorClass(null)).toBe('sh-timetable-c--neutral')
  })
})
