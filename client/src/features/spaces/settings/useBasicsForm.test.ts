import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/preact'
import { basicsChanges, basicsOf, useBasicsForm } from './useBasicsForm'

const stored = basicsOf({ name: 'Family', description: null, emoji: '🏡', about_markdown: 'Hi' })

describe('Basics draft', () => {
  it('reads absent values as empty', () => {
    expect(stored).toEqual({ name: 'Family', description: '', emoji: '🏡', about: 'Hi' })
  })

  it('sends only what changed, with "" to clear', () => {
    expect(basicsChanges(stored, stored)).toEqual({})
    expect(basicsChanges({ ...stored, name: 'Fam', emoji: '' }, stored))
      .toEqual({ name: 'Fam', emoji: '' })
    expect(basicsChanges({ ...stored, about: 'Hello' }, stored)).toEqual({ about_markdown: 'Hello' })
  })

  it('counts unsaved fields and discards them', () => {
    const { result } = renderHook(() => useBasicsForm(stored))
    expect(result.current.dirtyCount).toBe(0)
    act(() => { result.current.fields.name.value = 'Fam' })
    act(() => { result.current.fields.description.value = 'Us' })
    expect(result.current.dirtyCount).toBe(2)
    act(() => { result.current.reset() })
    expect(result.current.dirtyCount).toBe(0)
    expect(result.current.fields.name.value).toBe('Family')
  })

  it('an edit reverted by hand is no change', () => {
    const { result } = renderHook(() => useBasicsForm(stored))
    act(() => { result.current.fields.name.value = 'X' })
    act(() => { result.current.fields.name.value = 'Family' })
    expect(result.current.dirtyCount).toBe(0)
  })

  it('a reload moves untouched fields and keeps edited ones', () => {
    const { result, rerender } = renderHook(
      (props: { s: typeof stored }) => useBasicsForm(props.s), { initialProps: { s: stored } },
    )
    act(() => { result.current.fields.description.value = 'Mine' })
    act(() => { rerender({ s: { ...stored, name: 'Renamed elsewhere', description: 'Theirs' } }) })
    expect(result.current.fields.name.value).toBe('Renamed elsewhere')
    expect(result.current.fields.description.value).toBe('Mine')
    expect(result.current.changes).toEqual({ description: 'Mine' })
  })

  it('fills every field once the space arrives (empty draft before it)', () => {
    const empty = basicsOf({ name: '', description: '', emoji: '', about_markdown: '' })
    const { result, rerender } = renderHook(
      (props: { s: typeof stored }) => useBasicsForm(props.s), { initialProps: { s: empty } },
    )
    act(() => { rerender({ s: stored }) })
    expect(result.current.fields.name.value).toBe('Family')
    expect(result.current.dirtyCount).toBe(0)
  })
})
