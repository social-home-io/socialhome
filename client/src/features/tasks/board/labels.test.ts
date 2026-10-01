import { describe, it, expect } from 'vitest'
import type { TaskItem } from '@/types'
import { addLabel, collectLabels, labelColorClass, MAX_LABELS } from './labels'

describe('labels', () => {
  it('adds a cleaned label', () => {
    expect(addLabel(['School'], '  Garden   work ')).toEqual({ labels: ['School', 'Garden work'] })
  })
  it('refuses empty, too long, duplicate (any case) and an 11th label', () => {
    expect(addLabel([], '   ').error).toBe('empty')
    expect(addLabel([], 'x'.repeat(33)).error).toBe('too_long')
    expect(addLabel([], 'x'.repeat(32)).error).toBeUndefined()
    expect(addLabel(['Garden'], 'garden').error).toBe('duplicate')
    const ten = Array.from({ length: MAX_LABELS }, (_, i) => `L${i}`)
    expect(addLabel(ten, 'more')).toEqual({ labels: ten, error: 'too_many' })
  })
  it('identity is the cleaned label lower-cased, like the server (accents count)', async () => {
    const { labelKey } = await import('./labels')
    expect(labelKey('  Garden  Work ')).toBe(labelKey('garden work'))
    expect(labelKey('Café')).not.toBe(labelKey('Cafe'))
    expect(addLabel(['Café'], 'Cafe').error).toBeUndefined()
  })

  it('colours follow the name, not its case', () => {
    expect(labelColorClass('Garden')).toBe(labelColorClass('garden'))
    expect(labelColorClass('Garden')).toMatch(/^sh-timetable-c--/)
  })
  it('collects one spelling per label, sorted', () => {
    const rows = [
      { labels: ['school', 'Urgent'] }, { labels: ['School', 'garden'] }, {},
    ] as unknown as TaskItem[]
    expect(collectLabels(rows, 'en')).toEqual(['garden', 'school', 'Urgent'])
  })
})
