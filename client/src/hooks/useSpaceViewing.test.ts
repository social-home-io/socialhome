import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render } from '@testing-library/preact'
import { h } from 'preact'

const send = vi.fn()
vi.mock('@/ws', () => ({ ws: { send: (...a: unknown[]) => send(...a) } }))

import { SPACE_VIEWING_INTERVAL_MS, useSpaceViewing } from './useSpaceViewing'

function View({ id }: { id: string | null }) {
  useSpaceViewing(id)
  return null
}

const views = () => send.mock.calls.filter(c => c[0] === 'space.viewing').map(c => c[1])

let visibility: ReturnType<typeof vi.spyOn> | null = null
function setVisibility(state: DocumentVisibilityState) {
  visibility ??= vi.spyOn(document, 'visibilityState', 'get')
  visibility.mockReturnValue(state)
  act(() => { document.dispatchEvent(new Event('visibilitychange')) })
}

beforeEach(() => { send.mockClear(); vi.useFakeTimers() })
afterEach(() => {
  act(() => { cleanup() })
  visibility?.mockRestore()
  visibility = null
  vi.useRealTimers()
})

describe('useSpaceViewing', () => {
  it('says the space is open at once and then once a minute', () => {
    render(h(View, { id: 'sp-1' }))
    expect(views()).toEqual([{ space_id: 'sp-1' }])
    act(() => { vi.advanceTimersByTime(SPACE_VIEWING_INTERVAL_MS) })
    expect(views()).toHaveLength(2)
  })

  it('says nothing while the tab is hidden, and again when it comes back', () => {
    render(h(View, { id: 'sp-1' }))
    setVisibility('hidden')
    act(() => { vi.advanceTimersByTime(SPACE_VIEWING_INTERVAL_MS) })
    expect(views()).toHaveLength(1)
    setVisibility('visible')
    expect(views()).toHaveLength(2)
  })

  it('stops when the space closes and claims nothing without an id', () => {
    const { unmount } = render(h(View, { id: 'sp-1' }))
    act(() => { unmount() })
    act(() => { vi.advanceTimersByTime(3 * SPACE_VIEWING_INTERVAL_MS) })
    expect(views()).toHaveLength(1)
    render(h(View, { id: null }))
    expect(views()).toHaveLength(1)
  })
})
