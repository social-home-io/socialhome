import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/preact'
import { pageTitle, useTitle } from './pageTitle'

describe('useTitle', () => {
  it('sets the in-app pageTitle signal and document.title', () => {
    renderHook(() => useTitle('My household'))
    expect(pageTitle.value).toBe('My household')
    expect(document.title).toBe('My household · Social Home')
  })

  it('falls back to the brand alone when title is empty', () => {
    renderHook(() => useTitle(''))
    expect(document.title).toBe('Social Home')
  })

  it('resets pageTitle + document.title on unmount', () => {
    const { unmount } = renderHook(() => useTitle('Calendar'))
    expect(document.title).toBe('Calendar · Social Home')
    act(() => { unmount() })
    expect(pageTitle.value).toBe('')
    expect(document.title).toBe('Social Home')
  })
})
