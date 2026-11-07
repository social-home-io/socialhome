import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/preact'
import {
  pageTitle, pageTitleAvatar, pageTitleGlyph, useTitle, useTitleAvatar, useTitleGlyph,
} from './pageTitle'

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

  it('a replaced title-setter unmounting late leaves the newer title alone', () => {
    // Preact 11 runs a removed component's effect cleanup after paint,
    // so the old setter's cleanup can land after its replacement's
    // effect (ConversationView's loading frame → thread swap).
    const older = renderHook(() => useTitle('Chats'))
    renderHook(() => useTitle('Bob Builder'))
    act(() => { older.unmount() })
    expect(pageTitle.value).toBe('Bob Builder')
    expect(document.title).toBe('Bob Builder · Social Home')
  })

  it('the current owner unmounting still resets the title', () => {
    const older = renderHook(() => useTitle('Chats'))
    const newer = renderHook(() => useTitle('Bob Builder'))
    act(() => { older.unmount() })
    act(() => { newer.unmount() })
    expect(pageTitle.value).toBe('')
    expect(document.title).toBe('Social Home')
  })
})

describe('useTitle with a separate tab title', () => {
  it('the TopBar shows the heading, the browser tab the tab title', () => {
    const { unmount } = renderHook(() => useTitle('Garden', '🌻 Garden'))
    expect(pageTitle.value).toBe('Garden')
    expect(document.title).toBe('🌻 Garden · Social Home')
    act(() => { unmount() })
    expect(document.title).toBe('Social Home')
  })
})

describe('useTitleGlyph', () => {
  it('sets the glyph and clears it when the page leaves', () => {
    const { unmount } = renderHook(() =>
      useTitleGlyph({ iconUrl: '/api/media/x.webp', emoji: '🌻', name: 'Garden' }))
    expect(pageTitleGlyph.value).toEqual({ iconUrl: '/api/media/x.webp', emoji: '🌻', name: 'Garden' })
    act(() => { unmount() })
    expect(pageTitleGlyph.value).toBeNull()
  })

  it('null sets no glyph; a replaced setter unmounting late leaves the newer one', () => {
    const older = renderHook(() => useTitleGlyph({ iconUrl: null, emoji: '🌻', name: 'Garden' }))
    const newer = renderHook(() => useTitleGlyph({ iconUrl: null, emoji: '📚', name: 'Books' }))
    act(() => { older.unmount() })
    expect(pageTitleGlyph.value?.name).toBe('Books')
    act(() => { newer.unmount() })
    const none = renderHook(() => useTitleGlyph(null))
    expect(pageTitleGlyph.value).toBeNull()
    none.unmount()
  })
})

describe('useTitleAvatar', () => {
  it('a replaced avatar unmounting late leaves the newer avatar alone', () => {
    const older = renderHook(() => useTitleAvatar({ src: null, name: 'Chats' }))
    const newer = renderHook(() => useTitleAvatar({ src: null, name: 'Bob Builder' }))
    act(() => { older.unmount() })
    expect(pageTitleAvatar.value?.name).toBe('Bob Builder')
    act(() => { newer.unmount() })
    expect(pageTitleAvatar.value).toBeNull()
  })
})
