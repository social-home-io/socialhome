import { describe, it, expect } from 'vitest'
import type { TaskItem } from '@/types'
import { EMPTY_FILTERS, filtersActive, matchesFilters, getFilters, setFilters, resetFilters } from './filters'

const base: TaskItem = {
  id: 'a', list_id: 'l1', title: 'Water the roses', description: 'Back yard', status: 'todo',
  position: 0, due_date: null, assignees: ['u1'], created_by: 'u2', priority: 'high', labels: ['Garden'],
}

describe('board filters', () => {
  it('no filter matches everything and is not active', () => {
    expect(filtersActive(EMPTY_FILTERS)).toBe(false)
    expect(matchesFilters(base, EMPTY_FILTERS, 'u1')).toBe(true)
  })
  it('text searches title, description and labels, case-insensitively', () => {
    for (const text of ['ROSES', 'yard', 'garden']) {
      expect(matchesFilters(base, { ...EMPTY_FILTERS, text }, null)).toBe(true)
    }
    expect(matchesFilters(base, { ...EMPTY_FILTERS, text: 'dishes' }, null)).toBe(false)
  })
  it('assigned to me / assignee / label / priority narrow and combine', () => {
    expect(matchesFilters(base, { ...EMPTY_FILTERS, mine: true }, 'u1')).toBe(true)
    expect(matchesFilters(base, { ...EMPTY_FILTERS, mine: true }, 'u2')).toBe(false)
    expect(matchesFilters(base, { ...EMPTY_FILTERS, assignee: 'u3' }, null)).toBe(false)
    expect(matchesFilters(base, { ...EMPTY_FILTERS, label: 'garden' }, null)).toBe(true)
    expect(matchesFilters(base, { ...EMPTY_FILTERS, label: 'School' }, null)).toBe(false)
    expect(matchesFilters(base, { ...EMPTY_FILTERS, priority: 'high' }, null)).toBe(true)
    expect(matchesFilters(base, { ...EMPTY_FILTERS, priority: 'none' }, null)).toBe(false)
    expect(matchesFilters({ ...base, priority: null }, { ...EMPTY_FILTERS, priority: 'none' }, null)).toBe(true)
    expect(matchesFilters(base, { ...EMPTY_FILTERS, priority: 'high', label: 'School' }, null)).toBe(false)
  })
  it('remembers filters per key for the session', () => {
    resetFilters()
    setFilters('household:l1', { ...EMPTY_FILTERS, mine: true })
    expect(getFilters('household:l1').mine).toBe(true)
    expect(getFilters('household:l2')).toBe(EMPTY_FILTERS)
    resetFilters()
    expect(getFilters('household:l1')).toBe(EMPTY_FILTERS)
  })
})
