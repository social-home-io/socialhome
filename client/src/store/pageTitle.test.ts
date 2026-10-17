import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/preact'
import { pageTitle, pageTitleAvatar, useTitle, useTitleAvatar } from './pageTitle'

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
